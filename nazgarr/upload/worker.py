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
non gestita porta il job in 'failed' con il messaggio nel registro eventi
(e nel log), anche se il passo aveva già cambiato lo stato del job; solo un
job annullato (o già finito) nel frattempo non si segna.

Mai due passi dello stesso job insieme (audit 2026-10-18): un kick per un
job già in attesa nell'esecutore "light" non ne aggiunge un altro (quello
che c'è legge lo stato del momento in cui parte), uno che arriva mentre un
passo del job gira si ricorda e, finito il passo, il job si guarda di nuovo.
Un job approvato lo esegue solo la coda "heavy": un kick "light" vecchio
che lo trova in coda o in esecuzione non fa niente.
"""

import logging
import threading
from collections import Counter
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


# Dopo un errore che il worker non è riuscito nemmeno a registrare (il DB
# non scrivibile), la coda riprova da sola dopo un po' invece di restare
# ferma fino al prossimo kick o a un riavvio.
RETRY_QUEUE_SECONDS = 30.0


def _log_failure(future) -> None:
    """Un'eccezione uscita da un compito degli esecutori: senza, finiva in
    un Future che nessuno legge, e non restava traccia da nessuna parte."""
    if future.cancelled():
        return
    exc = future.exception()
    if exc is not None:
        logger.error("Compito del worker di upload fallito", exc_info=exc)


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
        # Un kick arrivato mentre la coda finiva il suo giro (audit
        # 2026-10-18): la coda guarda di nuovo prima di fermarsi, invece di
        # perderlo e lasciare il job in coda finché qualcos'altro la sveglia.
        self._rescan = False
        # I job su cui un passo sta girando adesso, con quanti (is_active): un
        # job annullato può avere ancora un tracker in invio. Più di uno solo
        # per un attimo: la coda che parte mentre il passo "light" che ha
        # portato il job alla decisione finisce di chiudersi.
        self._active: Counter[int] = Counter()
        # I job con un passo "light" già nell'esecutore, non ancora partito, e
        # quelli con un kick arrivato mentre un loro passo girava (audit
        # 2026-10-18): due analisi dello stesso job insieme si sovrascrivevano
        # (annullato e ripreso con il primo kick ancora in attesa).
        self._pending: set[int] = set()
        self._again: set[int] = set()
        self._closed = False

    def kick(self, job_id: int, status: str) -> None:
        """Da chiamare dopo ogni transizione verso uno stato del worker."""
        if status in LIGHT_STATES:
            with self._lock:
                if job_id in self._pending:
                    return  # quello in attesa leggerà lo stato di quando parte
                if self._active[job_id]:
                    self._again.add(job_id)  # finito il passo che gira, si guarda di nuovo
                    return
                self._pending.add(job_id)
            self._submit(self._light, self._run_job, job_id)
        elif status in HEAVY_STATES:
            self._schedule_queue()

    def is_active(self, job_id: int) -> bool:
        """Un passo del worker sta lavorando su questo job in questo momento."""
        with self._lock:
            return self._active[job_id] > 0

    def resume(self) -> None:
        session = self.session_factory()
        try:
            job_ids = upload_jobs.reset_interrupted(session, self.data_dir)
            statuses = {j.id: j.status for j in session.query(UploadJob).filter(UploadJob.id.in_(job_ids))}
        finally:
            session.close()
        for job_id in job_ids:
            self.kick(job_id, statuses[job_id])
        self._resume_auto_checks()

    def _resume_auto_checks(self) -> None:
        """I full hash check partiti da soli (nazgarr/upload/auto.py) e
        fermati da un riavvio ripartono, o l'avviso "upload pronto" che
        aspettavano arriva adesso: senza, non arrivava mai."""
        from nazgarr.upload import auto as upload_auto

        session = self.session_factory()
        try:
            jobs = (session.query(UploadJob).filter(UploadJob.status == "awaiting_decision",
                                                    UploadJob.analysis_json.like('%"ready_pending"%')).all())
            for job in jobs:
                try:
                    upload_auto.settle(session, job, self, after_analysis=False)
                except Exception:
                    session.rollback()
                    logger.warning("Upload %s: verifiche automatiche non riprese", job.id, exc_info=True)
        finally:
            session.close()

    def submit_verify(self, target_id: int, torrent_id_remote: str) -> None:
        self._submit(self._verify, self._run_verify, target_id, torrent_id_remote)

    def shutdown(self) -> None:
        self._closed = True
        for executor in (self._light, self._heavy, self._verify):
            executor.shutdown(wait=False, cancel_futures=True)

    # --- esecuzione ---------------------------------------------------------

    @staticmethod
    def _submit(executor: Executor, fn, *args) -> None:
        future = executor.submit(fn, *args)
        if future is not None:  # InlineExecutor nei test esegue subito
            future.add_done_callback(_log_failure)

    def _schedule_queue(self) -> None:
        if self._closed:
            return  # es. il timer di _retry_later dopo lo spegnimento: l'esecutore è chiuso
        with self._lock:
            if self._heavy_scheduled:
                self._rescan = True
                return
            self._heavy_scheduled = True
            self._rescan = False
        self._submit(self._heavy, self._drain_queue)

    def _next_due(self) -> int | None:
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
            return job.id if job is not None else None
        finally:
            session.close()

    def _drain_queue(self) -> None:
        """Esegue i job in coda uno dopo l'altro finché ce ne sono. Uno
        programmato (scheduled_at) aspetta la sua ora: intanto passano gli
        altri, e lo sveglia il pianificatore (nazgarr/scheduler.py).

        Fermarsi (niente più da fare) e un kick arrivato nel frattempo si
        decidono sotto lo stesso lock: un job approvato mentre la coda
        finiva il giro non resta in coda senza che nessuno lo prenda."""
        idle = False
        try:
            while True:
                try:
                    job_id = self._next_due()
                except Exception:
                    # Il DB non si legge (un errore di I/O, una pagina
                    # rovinata): senza, la coda si fermava fino al prossimo kick.
                    logger.exception("La coda degli upload non riesce a leggere il prossimo job")
                    self._retry_later()
                    return
                if job_id is None:
                    with self._lock:
                        if self._rescan:
                            self._rescan = False
                            continue
                        self._heavy_scheduled = False
                        idle = True
                    return
                try:
                    if not self._run_job(job_id, from_queue=True):
                        self._retry_later()
                        return
                except Exception:
                    # _run_job registra già i passi falliti: qui arriva solo
                    # quello che non è riuscito a registrare (es. il DB non
                    # scrivibile). Rifare subito lo stesso job girerebbe a
                    # vuoto: la coda riprova fra poco.
                    logger.exception("Upload %s: la coda degli upload si ferma per un errore", job_id)
                    self._retry_later()
                    return
        finally:
            if not idle:
                with self._lock:
                    self._heavy_scheduled = False

    def _retry_later(self) -> None:
        if self._closed:
            return
        timer = threading.Timer(RETRY_QUEUE_SECONDS, self._schedule_queue)
        timer.daemon = True
        timer.start()

    def _run_job(self, job_id: int, from_queue: bool = False) -> bool:
        """from_queue: dalla coda "heavy" (_drain_queue), l'unica che esegue
        i job approvati, uno alla volta. False se il job non si è potuto
        guardare: dalla coda, un job 'running' su cui gira ancora un altro
        passo (la coda riprova fra poco)."""
        with self._lock:
            if not from_queue:
                self._pending.discard(job_id)
                if self._active[job_id]:
                    # Un altro passo dello stesso job sta girando: lo si
                    # guarda di nuovo quando finisce, mai due insieme.
                    self._again.add(job_id)
                    return True
            busy = self._active[job_id] > 0
            self._active[job_id] += 1
        again = False
        try:
            again, done = self._step(job_id, from_queue, busy)
        finally:
            with self._lock:
                self._active[job_id] -= 1
                if self._active[job_id] <= 0:
                    del self._active[job_id]
                again = job_id in self._again or again
                self._again.discard(job_id)
        if again:
            self._kick_current(job_id)
        return done

    def _kick_current(self, job_id: int) -> None:
        """Dopo un passo: il job, se è (ancora o di nuovo) in uno stato del
        worker, riparte dallo stato che ha adesso."""
        session = self.session_factory()
        try:
            status = session.query(UploadJob.status).filter(UploadJob.id == job_id).scalar()
        finally:
            session.close()
        if status is not None:
            self.kick(job_id, status)

    def _step(self, job_id: int, from_queue: bool, busy: bool) -> tuple[bool, bool]:
        """Il passo del job per il suo stato. (da guardare di nuovo, fatto)."""
        session = self.session_factory()
        try:
            job = session.get(UploadJob, job_id)
            if job is None:
                return False, True
            if not from_queue and job.status in HEAVY_STATES:
                # Un kick vecchio dell'esecutore "light" (un annullamento e una
                # ripresa con lui ancora in attesa, un id riusato) che trova il
                # job approvato: lo esegue solo la coda, uno alla volta (audit
                # 2026-10-18). Prima lo eseguiva lui, fuori dalla coda, e la
                # coda poi lo prendeva per un orfano e lo rifaceva insieme.
                return False, True
            handler = self.handlers.get(job.status)
            if handler is None:
                if job.status == "running":
                    # Dalla coda: nessun'altra esecuzione può girare, quindi è
                    # rimasto 'running' da un errore che non si è potuto
                    # registrare. Torna in coda con i target sistemati, e la
                    # coda lo finisce (audit 2026-10-18; prima "passo non
                    # disponibile", con i target bloccati in invio). Mai
                    # mentre un altro passo dello stesso job gira ancora.
                    if busy:
                        logger.info("Upload %s: in esecuzione con un altro passo ancora aperto, la coda riprova",
                                    job_id)
                        return False, False
                    if upload_jobs.requeue_orphan(session, job, self.data_dir):
                        logger.warning("Upload %s: rimasto in esecuzione senza un passo, torna in coda", job_id)
                elif job.status in upload_jobs.WORKER_STATES:
                    # Uno stato senza handler non deve restare in coda per sempre.
                    self._fail(session, job, "upload_step_not_available", status=job.status)
                return False, True
            status_before = job.status
            try:
                handler(session, job, self)
            except Exception as exc:
                self._step_failed(session, job_id, job, status_before, exc)
                return False, True
            try:
                session.refresh(job)
            except Exception:
                logger.info("Upload %s: eliminato mentre il passo %r finiva", job_id, status_before)
                return False, True
            return job.status != status_before and job.status in LIGHT_STATES, True
        finally:
            session.close()

    def _step_failed(self, session: Session, job_id: int, job: UploadJob, step: str, exc: Exception) -> None:
        """Un passo finito con un'eccezione. Prima bastava che lo stato fosse
        cambiato per prenderlo per un annullamento, ma il passo d'esecuzione
        porta lui il job da 'queued' a 'running': ogni suo errore spariva
        senza log e il job finiva con "passo non disponibile" (audit
        2026-10-18). Ora solo uno stato finale (annullato, o già concluso) è
        "niente da segnare"; il resto fallisce con l'errore vero."""
        try:
            session.rollback()
            session.refresh(job)
        except Exception:
            # Eliminato mentre il passo girava: niente da segnare.
            logger.warning("Upload %s: il passo %r si è fermato, il job non esiste più", job_id, step, exc_info=exc)
            return
        if job.status in upload_jobs.FINAL_STATES:
            if job.status != "cancelled":
                logger.warning("Upload %s: errore dopo la fine del passo %r", job_id, step, exc_info=exc)
            return
        logger.error("Upload %s: passo %r fallito", job_id, step, exc_info=exc)
        if isinstance(exc, CodedError):
            code, params = exc.code, dict(exc.params)
        else:
            code, params = "upload_step_failed", {"step": step, "error": upload_jobs.error_text(exc)}
        status = "failed"
        if step == "queued":
            # L'esecuzione: nessun target resta "in invio" (un upload già
            # pubblicato torna 'done' con seed_failed), quelli da fare
            # falliscono con l'errore (così "Riprova" li rifà), e con un
            # tracker già fatto l'esito viene dai target; tutto nello stesso
            # commit dello stato del job (audit 2026-10-18).
            upload_jobs.settle_failed_execution(session, job, self.data_dir, code)
            if any(t.status == "done" and t.action != "skip" for t in job.targets):
                status = upload_jobs.outcome(job)
        self._fail(session, job, code, final=status, **params)

    def _run_verify(self, target_id: int, torrent_id_remote: str) -> None:
        from nazgarr.upload import auto as upload_auto
        from nazgarr.upload import verify as upload_verify

        session = self.session_factory()
        try:
            upload_verify.execute(session, target_id, torrent_id_remote)
            # La verifica dopo, o il reseed approvato da solo (nazgarr/upload/auto.py).
            upload_auto.after_verify(session, target_id, self)
        except Exception:
            logger.exception("Full hash check del target %s non riuscito", target_id)
        finally:
            session.close()

    def _fail(self, session: Session, job: UploadJob, code: str, final: str = "failed", **params) -> None:
        """Il job finisce (di solito 'failed') con l'errore nel registro.
        params sono i parametri dell'evento: "final" è un nome a parte perché
        un parametro può chiamarsi anche "status"."""
        values = {"error_message": code} if final != "done" else {}
        if upload_jobs.transition(session, job, job.status, final, stage=None, progress_done=None,
                                  progress_total=None, **values):
            upload_jobs.log_event(session, job, code, level="error", **params)
            session.commit()
