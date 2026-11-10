"""Un torrent identico sul tracker durante un upload, senza aspettare
l'utente (decisione dell'utente, 2026-11-10; impostazione
upload_identical_auto, spenta di default):

- "verify": il full hash check di quel torrent parte da solo appena
  l'analisi lo trova (nazgarr/upload/verify.py), uno alla volta come
  sempre; se non passa, quello dopo dello stesso tracker;
- "reseed": in più, quando ogni tracker del job ha una risposta certa (un
  reseed verificato al 100%, o "salta" perché quei file sono già in seed lì
  con un suo torrent), la decisione si approva da sola e il job va in coda.
  Basta un tracker che chieda altro (un upload, una verifica fallita, un
  dupe check fallito) e decide l'utente, con il reseed già proposto.

L'approvazione automatica è la stessa di decision.approve: un reseed solo
dopo un full hash check passato, la conferma che per ogni altra decisione
resta dell'utente (docs/SPEC.md §9). Come l'esecuzione automatica dei
reseed del motore è un'opzione da accendere (CLAUDE.md, "nothing runs
without approval"), una delle protezioni che una API key non cambia.

L'avviso "upload pronto" (upload.ready, webhook e notifiche) aspetta le
verifiche partite da sole: arriva quando la decisione tocca davvero
all'utente, mai per un job che poi si approva da solo.
"""

import json
import logging

from sqlalchemy.orm import Session

from nazgarr.core import events, settings_registry
from nazgarr.core.models import UploadJob, UploadTarget
from nazgarr.upload import decision as upload_decision
from nazgarr.upload import jobs as upload_jobs
from nazgarr.upload import verify as upload_verify
from nazgarr.upload.jobs import TargetStatus, UploadJobError

logger = logging.getLogger(__name__)

SETTING = "upload_identical_auto"
# In analysis_json: l'avviso "upload pronto" aspetta le verifiche partite da sole.
_READY_PENDING = "ready_pending"


def mode(session: Session) -> str:
    return settings_registry.get_choice(session, SETTING)


def _dupes(target: UploadTarget) -> list[dict]:
    try:
        dupes = json.loads(target.dupes_json or "[]")
    except ValueError:
        return []
    return [d for d in dupes if isinstance(d, dict)] if isinstance(dupes, list) else []


def _identical(target: UploadTarget) -> list[dict]:
    return [d for d in _dupes(target) if d.get("verdict") == "identical" and d.get("torrent_id_remote")]


def passed_torrent(target: UploadTarget) -> str | None:
    """Il torrent identico di questo tracker con il full hash check passato."""
    for dupe in _identical(target):
        if (dupe.get("verification") or {}).get("status") == "passed":
            return dupe["torrent_id_remote"]
    return None


def _unverified_torrent(target: UploadTarget) -> str | None:
    for dupe in _identical(target):
        if not dupe.get("verification"):
            return dupe["torrent_id_remote"]
    return None


def _seeding_here(job: UploadJob) -> dict:
    try:
        analysis = json.loads(job.analysis_json or "{}")
    except ValueError:
        return {}
    return analysis.get("seeding_here") or {}


def _open(target: UploadTarget) -> bool:
    return target.status == TargetStatus.AWAITING_DECISION and target.action is None


def _auto_decisions(job: UploadJob) -> list[dict] | None:
    """La decisione di ogni tracker, se tutte sono certe: reseed del torrent
    verificato, o salta se quei file sono già in seed lì. None se anche uno
    solo chiede l'utente, o se non c'è nessun reseed."""
    seeding = _seeding_here(job)
    decisions = []
    for target in job.targets:
        if str(target.id) in seeding:
            decisions.append({"target_id": target.id, "action": "skip"})
            continue
        torrent = passed_torrent(target)
        if torrent is None:
            return None
        decisions.append({"target_id": target.id, "action": "reseed", "reseed_torrent_id": torrent})
    return decisions if any(d["action"] == "reseed" for d in decisions) else None


def _set_ready_pending(job: UploadJob, value: bool) -> None:
    try:
        analysis = json.loads(job.analysis_json or "{}")
    except ValueError:
        analysis = {}
    if bool(analysis.get(_READY_PENDING)) == value:
        return
    if value:
        analysis[_READY_PENDING] = True
    else:
        analysis.pop(_READY_PENDING, None)
    job.analysis_json = json.dumps(analysis)


def _ready_pending(job: UploadJob) -> bool:
    try:
        return bool(json.loads(job.analysis_json or "{}").get(_READY_PENDING))
    except ValueError:
        return False


def emit_ready(session: Session, job: UploadJob) -> None:
    """Per webhook e notifiche: un upload aspetta la tua decisione."""
    events.emit(session, "upload.ready", {
        "upload_id": job.id, "title": job.title, "year": job.year, "path": job.relative_path,
        "origin": job.origin, "trackers": [t.tracker.label for t in job.targets],
    })


def _advance(session: Session, job: UploadJob, worker) -> str:
    """Un passo: la prossima verifica da far partire, o l'approvazione, o
    niente. "off" (spento, o il job non aspetta più una decisione),
    "verifying" (una verifica gira), "approved", "ready" (decide l'utente)."""
    current = mode(session)
    if current == "off" or job.status != "awaiting_decision":
        return "off"
    if any(t.status == TargetStatus.VERIFYING for t in job.targets):
        return "verifying"
    seeding = _seeding_here(job)
    for target in job.targets:
        if not _open(target) or str(target.id) in seeding or passed_torrent(target):
            continue
        torrent = _unverified_torrent(target)
        if torrent is None:
            continue
        # Prima della verifica: con l'esecutore dei test la verifica (e il
        # passo dopo) gira dentro start, prima che questa funzione ritorni.
        _set_ready_pending(job, True)
        upload_jobs.log_event(session, job, "auto_verify_started", target=target, torrent=torrent)
        try:
            upload_verify.start(session, job, target, torrent, worker)  # il commit è suo
        except UploadJobError as exc:
            session.rollback()
            logger.info("Upload %s: full hash check automatico di %s non partito (%s)", job.id, torrent, exc.code)
            continue
        return "verifying"
    if current == "reseed" and all(_open(t) for t in job.targets):
        decisions = _auto_decisions(job)
        if decisions is not None:
            try:
                upload_decision.approve(session, job, decisions)
            except UploadJobError as exc:
                # L'utente ha deciso nello stesso momento, o un pack misto: decide lui.
                session.rollback()
                logger.info("Upload %s: reseed automatico non approvato (%s)", job.id, exc.code)
                return "ready"
            upload_jobs.log_event(session, job, "auto_reseed_approved",
                                  trackers=", ".join(t.tracker.label for t in job.targets if t.action == "reseed"))
            session.commit()
            return "approved"
    return "ready"


def settle(session: Session, job: UploadJob, worker, after_analysis: bool) -> None:
    """Dopo l'analisi (after_analysis) o dopo un full hash check: fa il
    passo e, quando la decisione tocca all'utente, manda l'avviso "upload
    pronto" una volta sola."""
    outcome = _advance(session, job, worker)
    if outcome == "verifying":
        return
    session.refresh(job)
    pending = _ready_pending(job)
    if pending:
        _set_ready_pending(job, False)
    if outcome == "approved":
        session.commit()
        worker.kick(job.id, job.status)
        return
    if after_analysis or (pending and job.status == "awaiting_decision"):
        emit_ready(session, job)
    session.commit()


def after_verify(session: Session, target_id: int, worker) -> None:
    """Nel thread del worker, finito un full hash check (automatico o chiesto)."""
    target = session.get(UploadTarget, target_id)
    if target is None:
        return
    settle(session, target.job, worker, after_analysis=False)
