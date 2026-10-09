"""Una stagione incompleta divisa in episodi (decisione dell'utente,
2026-10-09): un season pack che non ha tutti gli episodi della stagione (es.
i primi 2 di 8 usciti insieme) non si carica come pack, ma con un upload per
episodio, come quelli usciti uno alla volta. Anche con un solo episodio
mancante.

Dalla cartella osservata succede da solo, dopo il match automatico
(impostazione upload_watch_split_incomplete, accesa di default); al match a
mano c'è "Dividi in episodi". L'upload originale si chiude (annullato, con
l'elenco dei nuovi, e non si riprende); ogni episodio ha gli stessi tracker,
lo stesso match e lo stesso ordinamento degli episodi, riparte dall'analisi
e si ferma alla decisione come sempre. Quando l'ultimo è in seed, la
cartella osservata rimasta (nfo, sample...) si toglie
(nazgarr/upload/execute.py)."""

import json
import logging
import posixpath

from sqlalchemy.orm import Session

from nazgarr.core import settings_registry
from nazgarr.core.models import UploadJob
from nazgarr.library import episode_orders
from nazgarr.upload import jobs as upload_jobs
from nazgarr.upload.source import scan_source

logger = logging.getLogger(__name__)

SPLIT_SETTING = "upload_watch_split_incomplete"


def enabled(session: Session) -> bool:
    return settings_registry.get_bool(session, SPLIT_SETTING)


def season_count(job: UploadJob, details: dict | None) -> tuple[int, int] | None:
    """(episodi trovati, episodi della stagione) di un season pack
    confermato, nella numerazione scelta al match; None se non si sa."""
    seasons = json.loads(job.seasons_json or "[]")
    if job.kind != "season_pack" or len(seasons) != 1:
        return None
    season = seasons[0]
    found = episode_orders.found_in_layout(job.layout_json)
    snapshot = json.loads(job.episode_order_json or "null")
    if snapshot and snapshot.get("chosen", {}).get("seasons") is not None:
        chosen = episode_orders.EpisodeOrder.from_dict(snapshot["chosen"])
        files = episode_orders.EpisodeOrder.from_dict(snapshot.get("files") or snapshot["chosen"])
        have = len(episode_orders.translate_found(files, chosen, found).get(season, []))
        expected = len(chosen.seasons.get(season, []))
    else:
        have = len(found.get(season, []))
        expected = next((s.get("episode_count") or 0 for s in (details or {}).get("seasons") or []
                         if s.get("season_number") == season), 0)
    return (have, expected) if expected else None


def is_incomplete(job: UploadJob, details: dict | None) -> bool:
    count = season_count(job, details)
    return count is not None and 0 < count[0] < count[1]


def _tracker_choices(job: UploadJob) -> dict[int, dict]:
    choices = {}
    for target in job.targets:
        freeleech = json.loads(target.flags_json or "{}").get("freeleech")
        if freeleech:
            choices[target.tracker_id] = {"freeleech": freeleech}
    return choices


def split(session: Session, job: UploadJob, details: dict | None) -> list[UploadJob]:
    """Un upload per video del pack, già confermato (job in 'analyzing',
    dopo il match). Quelli il cui episodio non si legge dal nome restano al
    match, da confermare a mano. Il job si chiude. Restituisce i nuovi."""
    from nazgarr.upload import identify  # import qui: identify chiama split

    if job.status != "analyzing" or job.kind != "season_pack" or job.disk is None:
        raise upload_jobs.UploadJobError("upload_split_not_a_pack")
    layout = json.loads(job.layout_json or "{}")
    candidates = json.loads(job.candidates_json or "[]")
    overrides = json.loads(job.overrides_json or "{}")
    forced = json.loads(job.forced_ids_json or "{}")
    tracker_ids = [t.tracker_id for t in job.targets]
    choices = _tracker_choices(job)
    children = []
    for video in layout.get("videos") or []:
        relative = posixpath.join(job.relative_path, video["relative_path"])
        child = upload_jobs.create_job(session, job.disk, relative, tracker_ids, forced, overrides, choices,
                                       origin=job.origin)
        child.split_from_id = job.id
        upload_jobs.log_event(session, child, "split_from", upload=job.id)
        session.commit()
        source = scan_source(child.source_path)
        identify.record_identification(session, child, source, candidates)
        episode = source.videos[0].episodes[0] if source.videos and source.videos[0].episodes else None
        try:
            identify.confirm(session, child, content_type="tv", tmdb_id=job.tmdb_id, kind=source.kind,
                             seasons=source.seasons, episode=episode, details=details, order_key=job.episode_order,
                             translate=True)
        except (upload_jobs.UploadJobError, episode_orders.EpisodeOrderError) as exc:
            session.rollback()
            child = session.get(UploadJob, child.id)
            upload_jobs.log_event(session, child, "auto_match_failed", level="warning",
                                  reason=getattr(exc, "code", None) or "episode_order")
        session.commit()
        children.append(child)
    if not upload_jobs.transition(session, job, "analyzing", "cancelled", emit=False, stage=None,
                                  queue_position=None):
        raise upload_jobs.UploadJobError("upload_job_wrong_status", status=job.status)
    upload_jobs.log_event(session, job, "job_split", count=len(children), jobs=[c.id for c in children])
    session.commit()
    logger.info("Upload %s: stagione incompleta divisa in %s upload (%s)", job.id, len(children),
                ", ".join(str(c.id) for c in children))
    return children
