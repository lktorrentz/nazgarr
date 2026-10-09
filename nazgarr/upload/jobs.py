"""Flusso di upload v2 (docs/SPEC.md §9 "Upload flow v2"): creazione dei job,
transizioni di stato e registro degli eventi.

Un UploadJob è la sorgente (file o cartella) di un upload verso N tracker,
uno per UploadTarget. Gli stati awaiting_match e awaiting_decision sono i
due soli punti in cui serve l'utente; tutti gli altri li fa avanzare il
worker (nazgarr/upload/worker.py), che chiama gli handler di ogni stato.

Le transizioni sono condizionali (UPDATE ... WHERE status = <atteso>): il
worker gira in un altro thread con la sua sessione, e un annullamento
chiesto dall'utente nel frattempo non deve essere sovrascritto dal passo
che il worker stava finendo.
"""

import json
import logging
import os
import shutil
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import update
from sqlalchemy.orm import Session

from nazgarr.core.errors import CodedError
from nazgarr.core.fs_scope import resolve_scoped
from nazgarr.core.models import (
    UPLOAD_TARGET_STATUSES,
    Disk,
    TorrentClient,
    Tracker,
    TrackerUploadProfile,
    UploadEvent,
    UploadJob,
    UploadTarget,
)
from nazgarr.torrents import client_labels

logger = logging.getLogger(__name__)

# Stati in cui il worker ha qualcosa da fare; gli altri aspettano l'utente o
# sono finali.
WORKER_STATES = ("identifying", "analyzing", "queued", "running")
GATE_STATES = ("awaiting_match", "awaiting_decision")
FINAL_STATES = ("done", "partial", "failed", "cancelled")

FORCED_ID_KEYS = ("tmdb", "imdb", "tvdb", "mal")


class UploadJobError(CodedError):
    pass


def log_event(
    session: Session, job: UploadJob, code: str, *, level: str = "info", target: UploadTarget | None = None,
    **params,
) -> UploadEvent:
    event = UploadEvent(
        job_id=job.id, target_id=target.id if target is not None else None, level=level, code=code,
        params_json=json.dumps(params) if params else None,
    )
    session.add(event)
    return event


class TargetStatus(StrEnum):
    """Gli stati di un target (un tracker di un job): gli stessi del CHECK
    del DB (models.UPLOAD_TARGET_STATUSES)."""

    PENDING = "pending"
    CHECKING = "checking"
    AWAITING_DECISION = "awaiting_decision"
    APPROVED = "approved"
    VERIFYING = "verifying"
    PREPARING = "preparing"
    UPLOADING = "uploading"
    SEEDING = "seeding"
    DONE = "done"
    SKIPPED = "skipped"
    FAILED = "failed"


assert {s.value for s in TargetStatus} == set(UPLOAD_TARGET_STATUSES)

_S = TargetStatus
# I passaggi previsti, per leggere il ciclo di vita di un target in un posto
# solo. Uno non previsto non si blocca (ripresa, annullamento e riapertura
# del match hanno percorsi rari) ma si scrive nel log.
EXPECTED_TARGET_TRANSITIONS: dict[TargetStatus, frozenset[TargetStatus]] = {
    _S.PENDING: frozenset({_S.CHECKING, _S.AWAITING_DECISION, _S.FAILED}),
    _S.CHECKING: frozenset({_S.AWAITING_DECISION, _S.FAILED, _S.PENDING}),
    _S.AWAITING_DECISION: frozenset({_S.VERIFYING, _S.APPROVED, _S.SKIPPED, _S.PENDING, _S.CHECKING}),
    _S.VERIFYING: frozenset({_S.AWAITING_DECISION, _S.APPROVED}),
    _S.APPROVED: frozenset({_S.PREPARING, _S.UPLOADING, _S.DONE, _S.FAILED, _S.PENDING}),
    _S.PREPARING: frozenset({_S.UPLOADING, _S.SEEDING, _S.APPROVED, _S.DONE, _S.FAILED}),
    _S.UPLOADING: frozenset({_S.SEEDING, _S.DONE, _S.FAILED}),
    _S.SEEDING: frozenset({_S.DONE, _S.FAILED}),
    _S.DONE: frozenset({_S.PENDING}),
    _S.SKIPPED: frozenset({_S.PENDING, _S.APPROVED}),
    _S.FAILED: frozenset({_S.APPROVED, _S.PENDING}),
}


def set_target_status(target: UploadTarget, status: TargetStatus | str) -> None:
    """L'unico modo di cambiare lo stato di un target: un nome sbagliato
    fallisce subito (ValueError), un passaggio non previsto si vede nel log."""
    new = TargetStatus(status)
    old = target.status
    if old is not None and old != new and new not in EXPECTED_TARGET_TRANSITIONS.get(TargetStatus(old), ()):
        logger.warning("Target %s: passaggio di stato non previsto %s -> %s", target.id, old, new.value)
    target.status = new.value


def transition(
    session: Session, job: UploadJob, expected: str | tuple[str, ...], new: str, emit: bool = True, **values
) -> bool:
    """Porta il job da uno degli stati attesi a `new`, insieme agli altri
    valori dati. False (e niente scritto) se nel frattempo lo stato è
    cambiato, es. l'utente ha annullato. Fa commit. emit=False: niente
    upload.finished (un job diviso in episodi non è un upload finito)."""
    expected_states = (expected,) if isinstance(expected, str) else expected
    if new in FINAL_STATES and "finished_at" not in values:
        values["finished_at"] = datetime.now(UTC)
    result = session.execute(
        update(UploadJob)
        .where(UploadJob.id == job.id, UploadJob.status.in_(expected_states))
        .values(status=new, **values)
        .execution_options(synchronize_session=False)
    )
    session.commit()
    session.refresh(job)
    if result.rowcount == 1 and new in FINAL_STATES and emit:
        _emit_finished(session, job)
    return result.rowcount == 1


def _emit_finished(session: Session, job: UploadJob) -> None:
    """upload.finished (nazgarr/core/events.py): la transizione è un UPDATE in blocco,
    che gli hook sul DB non vedono."""
    from nazgarr.core import events

    events.emit(session, "upload.finished", {
        "job_id": job.id, "status": job.status, "title": job.title, "year": job.year,
        "content_type": job.content_type, "tmdb_id": job.tmdb_id,
        "targets": [
            {"tracker": t.tracker.label, "action": t.action, "status": t.status,
             "torrent_id_remote": t.torrent_id_remote, "error": t.error_message}
            for t in job.targets
        ],
    })
    session.commit()


def default_client_id(session: Session, tracker: Tracker) -> int | None:
    """Il client del tracker se esiste ed è abilitato, altrimenti il primo
    abilitato: la stessa regola del reseeding (nazgarr/reseed/review.py::_client_for)."""
    if tracker.torrent_client_id is not None:
        row = session.get(TorrentClient, tracker.torrent_client_id)
        if row is not None and row.enabled:
            return row.id
    row = session.query(TorrentClient).filter_by(enabled=True).order_by(TorrentClient.id).first()
    return row.id if row is not None else None


def upload_trackers(session: Session) -> list[Tracker]:
    """I tracker verso cui si può fare upload: abilitati e con un profilo."""
    return (
        session.query(Tracker)
        .join(TrackerUploadProfile, TrackerUploadProfile.tracker_id == Tracker.id)
        .filter(Tracker.enabled.is_(True))
        .order_by(Tracker.id)
        .all()
    )


def _clean_forced_ids(forced_ids: dict | None) -> dict:
    cleaned = {}
    for key in FORCED_ID_KEYS:
        value = (forced_ids or {}).get(key)
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        cleaned[key] = value.strip() if isinstance(value, str) else value
    return cleaned


def _has_symlinks(source_path: str) -> bool:
    if not os.path.isdir(source_path):
        return False
    for dirpath, dirnames, filenames in os.walk(source_path):
        if any(os.path.islink(os.path.join(dirpath, n)) for n in (*dirnames, *filenames)):
            return True
    return False


def create_job(
    session: Session,
    disk: Disk,
    relative_path: str,
    tracker_ids: list[int] | None = None,
    forced_ids: dict | None = None,
    overrides: dict | None = None,
    tracker_choices: dict[int, dict] | None = None,
    origin: str | None = None,
) -> UploadJob:
    """Crea il job in 'identifying' (il worker lo prende da lì) con un target
    per tracker. tracker_ids None = tutti i tracker con un profilo di upload.
    Il percorso passa sempre da resolve_scoped (ScopeViolation al chiamante)."""
    source_path = resolve_scoped(disk.root_path, relative_path)
    if os.path.islink(os.path.join(disk.root_path, relative_path)) or _has_symlinks(source_path):
        # Si pubblicherebbe (e si metterebbe in seed) il file a cui punta il link.
        raise UploadJobError("upload_source_has_symlinks", path=relative_path)
    if not os.path.exists(source_path):
        raise UploadJobError("upload_source_not_found", path=relative_path)
    if source_path == os.path.realpath(disk.root_path):
        raise UploadJobError("upload_source_is_disk_root")
    return insert_job(session, disk, relative_path, source_path, os.path.isdir(source_path), tracker_ids,
                      forced_ids, overrides, tracker_choices, origin)


def insert_job(
    session: Session,
    disk: Disk,
    relative_path: str,
    source_path: str,
    is_dir: bool,
    tracker_ids: list[int] | None,
    forced_ids: dict | None,
    overrides: dict | None,
    tracker_choices: dict[int, dict] | None,
    origin: str | None,
    pack_json: str | None = None,
) -> UploadJob:
    """Il job e i suoi target, con la sorgente già controllata dal chiamante
    (create_job, o nazgarr/upload/pack.py per i file scelti a mano)."""
    available = {t.id: t for t in upload_trackers(session)}
    if tracker_ids is None:
        trackers = list(available.values())
    else:
        missing = [tid for tid in tracker_ids if tid not in available]
        if missing:
            raise UploadJobError("upload_tracker_not_available", id=missing[0])
        trackers = [available[tid] for tid in dict.fromkeys(tracker_ids)]
    if not trackers:
        raise UploadJobError("upload_no_trackers")

    job = UploadJob(
        disk_id=disk.id, relative_path=relative_path, source_path=source_path,
        is_dir=is_dir, status="identifying", pack_json=pack_json,
        forced_ids_json=json.dumps(_clean_forced_ids(forced_ids)),
        overrides_json=json.dumps(overrides or {}),
        origin=origin,
    )
    session.add(job)
    session.flush()
    for tracker in trackers:
        choice = (tracker_choices or {}).get(tracker.id) or {}
        # Scelte fatte alla creazione (per ora il freeleech): diventano i flag
        # di partenza del target, i default del profilo completano il resto.
        flags = {"freeleech": int(choice["freeleech"])} if choice.get("freeleech") else None
        session.add(UploadTarget(
            job_id=job.id, tracker_id=tracker.id, torrent_client_id=default_client_id(session, tracker),
            flags_json=json.dumps(flags) if flags else None,
        ))
    log_event(session, job, "job_created", path=relative_path, trackers=[t.label for t in trackers])
    session.commit()
    return job


def cancel_job(session: Session, job: UploadJob) -> None:
    """Annulla un job non ancora finito. Un target già in upload verso il
    tracker non si ferma a metà: il worker lo porta a termine e poi smette
    (vedi nazgarr/upload/worker.py)."""
    if job.status in FINAL_STATES:
        raise UploadJobError("upload_job_wrong_status", status=job.status)
    previous = job.status
    if not transition(session, job, job.status, "cancelled", queue_position=None):
        raise UploadJobError("upload_job_wrong_status", status=job.status)
    # Da dove riprenderlo (resume_job).
    log_event(session, job, "job_cancelled", previous=previous)
    session.commit()


def _cancelled_from(job: UploadJob) -> str:
    """Lo stato in cui il job è stato annullato: scritto nell'evento, o (per
    i job annullati prima) ricavato da quello che il job ha già."""
    for event in sorted(job.events, key=lambda e: e.id, reverse=True):
        if event.code == "job_cancelled":
            previous = json.loads(event.params_json or "{}").get("previous")
            if previous:
                return previous
            break
    if any(t.status in ("approved", "done") for t in job.targets):
        return "queued"
    if job.analysis_json and job.targets and all(t.status == "awaiting_decision" for t in job.targets):
        return "awaiting_decision"
    return "analyzing" if job.tmdb_id else "identifying"


def resume_job(session: Session, job: UploadJob) -> str:
    """Riprende un job annullato da dove si era fermato: a un punto di
    approvazione ci torna con le scelte fatte, un passo del worker riparte,
    l'esecuzione torna in coda con i soli tracker non ancora fatti (uno già
    pubblicato non si ricarica mai). Restituisce il nuovo stato."""
    if job.status != "cancelled":
        raise UploadJobError("upload_job_wrong_status", status=job.status)
    if any(event.code == "job_split" for event in job.events):
        raise UploadJobError("upload_job_split")  # continua negli upload degli episodi
    if not os.path.exists(job.source_path):
        raise UploadJobError("upload_source_missing", path=job.relative_path)
    previous = _cancelled_from(job)
    if previous == "running":
        previous = "queued"
    if previous == "queued" and not any(t.status == "approved" for t in job.targets):
        # Nessun tracker rimasto da fare: si torna alla decisione per quelli non pubblicati.
        previous = "analyzing"
    if previous == "analyzing" and any(t.status == "done" for t in job.targets):
        raise UploadJobError("upload_resume_nothing_left")
    values: dict = {"finished_at": None, "stage": None}
    if previous == "queued":
        values["queue_position"] = next_queue_position(session)
    if not transition(session, job, "cancelled", previous, **values):
        raise UploadJobError("upload_job_wrong_status", status=job.status)
    log_event(session, job, "job_resumed_by_user", state=previous)
    session.commit()
    return previous


def job_folder(data_dir: str, job_id: int) -> str:
    """Dove l'esecuzione tiene i file di un upload: i .torrent (creato, copia
    del tracker, reseed) e gli screenshot."""
    return os.path.join(data_dir, "uploads", str(job_id))


def delete_job(session: Session, job: UploadJob, data_dir: str) -> None:
    """Solo job fermi (a un punto di approvazione o finiti): uno che il
    worker sta lavorando va prima annullato. Con lui va la sua cartella: il
    client ha già la sua copia del .torrent, e SQLite può ridare lo stesso id
    al prossimo upload, che non deve trovarci i file di questo."""
    if job.status in WORKER_STATES:
        raise UploadJobError("upload_job_wrong_status", status=job.status)
    job_id = job.id
    session.delete(job)
    session.commit()
    shutil.rmtree(job_folder(data_dir, job_id), ignore_errors=True)


def next_queue_position(session: Session) -> int:
    last = (
        session.query(UploadJob.queue_position)
        .filter(UploadJob.queue_position.isnot(None))
        .order_by(UploadJob.queue_position.desc())
        .first()
    )
    return (last[0] + 1) if last else 1


def reset_interrupted(session: Session) -> list[int]:
    """All'avvio: i job lasciati a metà da un riavvio. identifying/analyzing/
    queued ripartono da capo (passi senza effetti fuori da Nazgarr). Un job
    'running' torna in coda, ma un target che stava inviando al tracker
    finisce in 'failed': non si sa se il tracker l'ha ricevuto, e ripetere
    l'invio rischierebbe un doppio upload. Restituisce gli id da riprendere."""
    for job in session.query(UploadJob).filter(UploadJob.status == "running").all():
        for target in job.targets:
            if target.status == "uploading":
                set_target_status(target, TargetStatus.FAILED)
                target.error_message = "interrupted"
                log_event(session, job, "target_interrupted", level="error", target=target)
            elif target.status in ("preparing", "verifying"):
                set_target_status(target, TargetStatus.APPROVED)
        job.status = "queued"
        job.stage = None
        log_event(session, job, "job_resumed", level="warning")
    # Un full hash check interrotto (in memoria nel worker) non riparte da
    # solo: il target torna alla decisione, l'utente lo rilancia se vuole.
    for target in (
        session.query(UploadTarget)
        .join(UploadJob, UploadJob.id == UploadTarget.job_id)
        .filter(UploadJob.status == "awaiting_decision", UploadTarget.status == "verifying")
        .all()
    ):
        set_target_status(target, TargetStatus.AWAITING_DECISION)
        log_event(session, target.job, "verify_interrupted", level="warning", target=target)
    session.commit()
    return [
        row.id
        for row in session.query(UploadJob.id)
        .filter(UploadJob.status.in_(WORKER_STATES))
        .order_by(UploadJob.id)
        .all()
    ]


TV_KINDS = ("episode", "season_pack", "complete_pack")


def confirm_match(
    session: Session,
    job: UploadJob,
    *,
    content_type: str,
    tmdb_id: int,
    kind: str,
    seasons: list[int],
    episode: int | None,
    details: dict | None,
    forced: dict,
) -> None:
    """Primo punto di approvazione: l'utente conferma il contenuto (e per le
    serie stagione/episodio). details = full_details di TMDB, None senza
    chiave TMDB. Gli id forzati dall'utente vincono su quelli di TMDB.
    Porta il job in 'analyzing'; il chiamante sveglia il worker."""
    if job.status != "awaiting_match":
        raise UploadJobError("upload_job_wrong_status", status=job.status)
    if content_type == "movie" and kind != "movie":
        raise UploadJobError("upload_kind_mismatch", kind=kind, content_type=content_type)
    if content_type == "tv":
        if kind not in TV_KINDS:
            raise UploadJobError("upload_kind_mismatch", kind=kind, content_type=content_type)
        if not seasons:
            raise UploadJobError("upload_season_required")
        if kind in ("episode", "season_pack") and len(seasons) != 1:
            raise UploadJobError("upload_single_season_required", kind=kind)
        if kind == "episode" and episode is None:
            raise UploadJobError("upload_episode_required")
    if kind in ("season_pack", "complete_pack") and not job.is_dir:
        raise UploadJobError("upload_pack_requires_folder")

    details = details or {}
    values = dict(
        content_type=content_type, tmdb_id=tmdb_id, kind=kind,
        seasons_json=json.dumps(sorted(set(seasons)) if content_type == "tv" else []),
        episode=episode if kind == "episode" else None,
        imdb_id=forced.get("imdb") or details.get("imdb_id"),
        tvdb_id=forced.get("tvdb") or details.get("tvdb_id"),
        mal_id=forced.get("mal"),
        # Per la categoria anime del client (nazgarr/torrents/client_labels.py).
        anime=client_labels.is_anime(details) if details else None,
    )
    for key in ("title", "year", "poster_path"):
        if details.get(key) is not None:
            values[key] = details[key]
    if not transition(session, job, "awaiting_match", "analyzing", **values):
        raise UploadJobError("upload_job_wrong_status", status=job.status)
    log_event(
        session, job, "match_confirmed", title=job.title, year=job.year, tmdb_id=tmdb_id, kind=kind,
        seasons=json.loads(job.seasons_json or "[]"), episode=job.episode,
    )
    session.commit()


def reidentify(session: Session, job: UploadJob, forced_ids: dict | None) -> None:
    """Rifà l'identificazione con altri id forzati: dal punto di match, o da
    un job fallito mentre identificava."""
    if job.status not in ("awaiting_match", "failed") or (job.status == "failed" and job.tmdb_id is not None):
        raise UploadJobError("upload_job_wrong_status", status=job.status)
    if not transition(
        session, job, job.status, "identifying",
        forced_ids_json=json.dumps(_clean_forced_ids(forced_ids)), error_message=None, finished_at=None,
    ):
        raise UploadJobError("upload_job_wrong_status", status=job.status)
    log_event(session, job, "reidentify_requested")
    session.commit()


def back_to_match(session: Session, job: UploadJob) -> None:
    """Il rollback del match (decisione dell'utente, 2026-10-02): dalla
    decisione si torna a scegliere il contenuto, per un match automatico (o
    manuale) sbagliato. Quello che dipendeva dal match si rifà: analisi,
    nomi proposti, dupe-check. Gli override dell'utente restano. Mai durante
    un full hash check: il suo esito apparterrebbe al match vecchio."""
    if job.status != "awaiting_decision":
        raise UploadJobError("upload_job_wrong_status", status=job.status)
    if any(target.status == "verifying" for target in job.targets):
        raise UploadJobError("upload_verify_in_progress")
    if not transition(session, job, "awaiting_decision", "awaiting_match", analysis_json=None, stage=None):
        raise UploadJobError("upload_job_wrong_status", status=job.status)
    for target in job.targets:
        set_target_status(target, TargetStatus.PENDING)
        target.suggested_action = target.action = None
        target.dupes_json = target.reseed_torrent_id = None
        target.proposed_name = target.approved_name = None
        target.category_id = target.type_id = target.resolution_id = None
    log_event(session, job, "match_reopened", title=job.title, year=job.year)
    session.commit()


def reorder_queue(session: Session, job_ids: list[int]) -> None:
    """Nuovo ordine dei job in coda: prima quelli indicati, nell'ordine dato,
    poi gli altri in coda nell'ordine di prima. Un job non in coda viene
    ignorato (nel frattempo può essere partito)."""
    queued = session.query(UploadJob).filter(UploadJob.status == "queued").order_by(UploadJob.queue_position).all()
    by_id = {job.id: job for job in queued}
    ordered = [by_id[i] for i in dict.fromkeys(job_ids) if i in by_id]
    ordered += [job for job in queued if job not in ordered]
    for position, job in enumerate(ordered, start=1):
        job.queue_position = position
    session.commit()
