"""Worker in-process del flusso di upload v2 (docs/SPEC.md §9).

Lo stato vive nel DB (upload_job.status), non in memoria come per i full
check: un riavvio riprende la coda da dove era (upload_jobs.reset_interrupted).
Due esecutori separati:
- "light" per identificazione e analisi (rete verso TMDB/tracker, mediainfo):
  secondi, possono girare mentre un altro upload è in corso;
- "heavy" per l'esecuzione dei job approvati (hash dei piece, screenshot,
  invio): uno alla volta, nell'ordine della coda (queue_position), come i
  full check, per non leggere due volte in parallelo dallo stesso disco.

Ogni stato del worker ha un handler (`handlers`) che riceve la sessione e
il job e ne porta avanti lo stato con upload_jobs.transition(). Un'eccezione
non gestita porta il job in 'failed' con il messaggio nel registro eventi.
"""

import logging
import threading
from collections.abc import Callable
from concurrent.futures import Executor, ThreadPoolExecutor
from datetime import UTC, datetime

from sqlalchemy.orm import Session, sessionmaker

from nazgarr.core.errors import CodedError
from nazgarr.core.models import UploadJob
from nazgarr.upload import jobs as upload_jobs

logger = logging.getLogger(__name__)

Handler = Callable[[Session, UploadJob, "UploadWorker"], None]

LIGHT_STATES = ("identifying", "analyzing")
HEAVY_STATES = ("queued", "running")


def default_handlers() -> dict[str, Handler]:
    # Import qui: i moduli dei passi importano a loro volta il worker per
    # i tipi, e così restano sostituibili nei test senza cicli.
    from nazgarr.upload import analysis as upload_analysis
    from nazgarr.upload import execute as upload_execute
    from nazgarr.upload import identify as upload_identify

    return {
        "identifying": upload_identify.handle,
        "analyzing": upload_analysis.handle,
        "queued": upload_execute.handle,
    }


class UploadWorker:
    def __init__(
        self,
        session_factory: sessionmaker,
        data_dir: str,
        *,
        handlers: dict[str, Handler] | None = None,
        light_executor: Executor | None = None,
        heavy_executor: Executor | None = None,
        verify_executor: Executor | None = None,
    ):
        self.session_factory = session_factory
        self.data_dir = data_dir
        self.handlers = handlers if handlers is not None else default_handlers()
        self._light = light_executor or ThreadPoolExecutor(max_workers=2, thread_name_prefix="upload-light")
        self._heavy = heavy_executor or ThreadPoolExecutor(max_workers=1, thread_name_prefix="upload-heavy")
        # I full hash check leggono tutta la sorgente: uno alla volta, a parte
        # dalla coda, così un controllo chiesto a un punto di approvazione non
        # aspetta la fine degli upload in corso (e viceversa).
        self._verify = verify_executor or ThreadPoolExecutor(max_workers=1, thread_name_prefix="upload-verify")
        self._lock = threading.Lock()
        self._heavy_scheduled = False

    def kick(self, job_id: int, status: str) -> None:
        """Da chiamare dopo ogni transizione verso uno stato del worker."""
        if status in LIGHT_STATES:
            self._light.submit(self._run_job, job_id)
        elif status in HEAVY_STATES:
            self._schedule_queue()

    def resume(self) -> None:
        session = self.session_factory()
        try:
            job_ids = upload_jobs.reset_interrupted(session)
            statuses = {j.id: j.status for j in session.query(UploadJob).filter(UploadJob.id.in_(job_ids))}
        finally:
            session.close()
        for job_id in job_ids:
            self.kick(job_id, statuses[job_id])

    def submit_verify(self, target_id: int, torrent_id_remote: str) -> None:
        self._verify.submit(self._run_verify, target_id, torrent_id_remote)

    def shutdown(self) -> None:
        for executor in (self._light, self._heavy, self._verify):
            executor.shutdown(wait=False, cancel_futures=True)

    # --- esecuzione ---------------------------------------------------------

    def _schedule_queue(self) -> None:
        with self._lock:
            if self._heavy_scheduled:
                return
            self._heavy_scheduled = True
        self._heavy.submit(self._drain_queue)

    def _drain_queue(self) -> None:
        """Esegue i job in coda uno dopo l'altro finché ce ne sono. Uno
        programmato (scheduled_at) aspetta la sua ora: intanto passano gli
        altri, e lo sveglia il pianificatore (nazgarr/scheduler.py)."""
        try:
            while True:
                session = self.session_factory()
                try:
                    now = datetime.now(UTC)
                    jobs = (
                        session.query(UploadJob)
                        .filter(UploadJob.status.in_(HEAVY_STATES))
                        .order_by(UploadJob.status.desc(), UploadJob.queue_position, UploadJob.id)
                        .all()
                    )
                    job = next((j for j in jobs if j.status == "running" or upload_jobs.is_due(j, now)), None)
                    job_id = job.id if job is not None else None
                finally:
                    session.close()
                if job_id is None:
                    return
                self._run_job(job_id)
        finally:
            with self._lock:
                self._heavy_scheduled = False

    def _run_job(self, job_id: int) -> None:
        session = self.session_factory()
        try:
            job = session.get(UploadJob, job_id)
            if job is None:
                return
            handler = self.handlers.get(job.status)
            if handler is None:
                if job.status in upload_jobs.WORKER_STATES:
                    # Uno stato senza handler non deve restare in coda per sempre.
                    self._fail(session, job, "upload_step_not_available", status=job.status)
                return
            status_before = job.status
            try:
                handler(session, job, self)
            except Exception as exc:
                session.rollback()
                session.refresh(job)
                if job.status != status_before:
                    return  # annullato nel frattempo: niente da segnare
                logger.exception("Upload %s: passo %r fallito", job_id, status_before)
                if isinstance(exc, CodedError):
                    self._fail(session, job, exc.code, **exc.params)
                else:
                    self._fail(session, job, "upload_step_failed", step=status_before, error=str(exc))
                return
            session.refresh(job)
            if job.status != status_before and job.status in LIGHT_STATES:
                self.kick(job.id, job.status)
        finally:
            session.close()

    def _run_verify(self, target_id: int, torrent_id_remote: str) -> None:
        from nazgarr.upload import verify as upload_verify

        session = self.session_factory()
        try:
            upload_verify.execute(session, target_id, torrent_id_remote)
        except Exception:
            logger.exception("Full hash check del target %s non riuscito", target_id)
        finally:
            session.close()

    def _fail(self, session: Session, job: UploadJob, code: str, **params) -> None:
        if upload_jobs.transition(session, job, job.status, "failed", error_message=code, stage=None):
            upload_jobs.log_event(session, job, code, level="error", **params)
            session.commit()
