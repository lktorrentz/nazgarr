"""Scheduler in-process per le run schedulate (docs/SPEC.md §8, Fase 5).

Un solo job APScheduler ('scheduled_run'), il cui trigger cron viene letto
da app_settings alla chiave 'schedule_cron' all'avvio e può essere
riconfigurato a runtime senza restart — coerente con la regola di
configurazione dinamica di docs/SPEC.md §4 (tutto ciò che non è
disk_scan_root/data_dir vive nel DB, editabile da UI). Nessun cron
configurato = nessun job aggiunto, mai un default implicito.

BackgroundScheduler (thread proprio), non AsyncIOScheduler: il lavoro
vero (pipeline.run_bulk_import) è sincrono/bloccante (I/O filesystem,
richieste HTTP sincrone via httpx.Client) — lo stesso motivo per cui il
trigger manuale (nazgarr/api/runs.py) usa BackgroundTasks di FastAPI invece
di una coroutine.
"""

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy.orm import sessionmaker

from nazgarr.core import settings_repo
from nazgarr.reseed import pipeline, review

logger = logging.getLogger(__name__)

JOB_ID = "scheduled_run"
SETTING_KEY = "schedule_cron"


def _run_scheduled(session_factory: sessionmaker, data_dir: str) -> None:
    session = session_factory()
    try:
        try:
            run = pipeline.try_start_run(session, run_type="scheduled")
        except pipeline.RunInProgressError:
            logger.info("Run schedulata saltata: ce n'è già una in corso")
            return
        pipeline.run_bulk_import(session, run, data_dir)
    except Exception:
        # Non deve mai far morire il thread dello scheduler: un ciclo
        # fallito non deve impedire il prossimo (docs/ROADMAP.md Fase 5,
        # "diverse esecuzioni consecutive senza intervento manuale").
        logger.exception("Run schedulata fallita")
    finally:
        session.close()


def _add_job(scheduler: BackgroundScheduler, cron_expr: str, session_factory: sessionmaker, data_dir: str) -> None:
    scheduler.add_job(
        _run_scheduled,
        CronTrigger.from_crontab(cron_expr),
        args=[session_factory, data_dir],
        id=JOB_ID,
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )


RECONCILE_JOB_ID = "reconcile_seed_jobs"
# Consegne di webhook e notifiche (nazgarr/integrations/webhooks.py): spesso, costa una query.
EVENTS_JOB_ID = "deliver_events"
EVENTS_INTERVAL_SECONDS = 15
RECONCILE_INTERVAL_SECONDS = 120


def _reconcile_between_runs(session_factory: sessionmaker) -> None:
    """Esito dei recheck fra una run e l'altra: senza, un seed approvato a
    mano restava "pending" fino alla run successiva, anche col torrent già
    in seed. Solo se c'è qualcosa in attesa e nessuna run in corso (la run
    fa già il suo reconcile alla fine, e non si scrive in due sul DB)."""
    session = session_factory()
    try:
        if pipeline.run_in_progress(session):
            return
        if review.has_pending_seed_jobs(session):
            review.reconcile_pending_seed_jobs(session)
    except Exception:
        logger.exception("Reconcile periodico dei seed_job fallito")
    finally:
        session.close()


def _deliver_events(session_factory: sessionmaker) -> None:
    from nazgarr.integrations import webhooks

    session = session_factory()
    try:
        webhooks.deliver_due(session)
    except Exception:
        logger.exception("Consegna degli eventi fallita")
    finally:
        session.close()


ARR_HOOKS_JOB_ID = "arr_webhook_events"
ARR_HOOKS_INTERVAL_SECONDS = 15


def _apply_arr_webhook_events(session_factory: sessionmaker, data_dir: str) -> None:
    """Gli eventi dei webhook di Radarr/Sonarr in coda (nazgarr/integrations/arr_webhooks.py)."""
    from nazgarr.integrations import arr_webhooks

    session = session_factory()
    try:
        arr_webhooks.process_pending(session, data_dir=data_dir)
    except Exception:
        logger.exception("Eventi dei webhook di Radarr/Sonarr non applicati")
    finally:
        session.close()


UPDATE_JOB_ID = "update_check"
UPDATE_INTERVAL_SECONDS = 3600


def _check_updates(session_factory: sessionmaker) -> None:
    """Ogni ora, ma chiama GitHub solo se l'utente ha acceso il controllo
    automatico e sono passate 12 ore dall'ultimo (nazgarr/core/updates.py):
    accenderlo o spegnerlo non deve riprogrammare niente."""
    from nazgarr.core import updates

    session = session_factory()
    try:
        updates.check_if_due(session)
    except Exception:
        logger.exception("Controllo automatico degli aggiornamenti fallito")
    finally:
        session.close()


WATCH_JOB_ID = "upload_watch"


def _scan_watch_folders(session_factory: sessionmaker, worker) -> None:
    from nazgarr.upload import watch as upload_watch

    session = session_factory()
    try:
        upload_watch.scan(session, kick=worker.kick)
    except Exception:
        logger.exception("Giro della cartella osservata fallito")
    finally:
        session.close()


SCHEDULED_UPLOADS_JOB_ID = "upload_scheduled"
SCHEDULED_UPLOADS_INTERVAL_SECONDS = 30


def _start_due_uploads(session_factory: sessionmaker, worker) -> None:
    """Gli upload programmati la cui ora è arrivata: si sveglia la coda, che
    li prende nel suo ordine (nazgarr/upload/worker.py)."""
    from nazgarr.core.models import UploadJob
    from nazgarr.upload import jobs as upload_jobs

    session = session_factory()
    try:
        due = [
            job for job in session.query(UploadJob).filter(
                UploadJob.status == "queued", UploadJob.scheduled_at.isnot(None))
            if upload_jobs.is_due(job)
        ]
        if due:
            worker.kick(due[0].id, "queued")
    except Exception:
        logger.exception("Controllo degli upload programmati fallito")
    finally:
        session.close()


def add_watch_job(scheduler: BackgroundScheduler, session_factory: sessionmaker, worker) -> None:
    """La cartella osservata per le release (nazgarr/upload/watch.py): serve il
    worker degli upload per svegliarlo sui job creati, quindi si aggiunge
    dopo averlo creato."""
    from nazgarr.upload import watch as upload_watch

    scheduler.add_job(
        _scan_watch_folders, IntervalTrigger(seconds=upload_watch.INTERVAL_SECONDS),
        args=[session_factory, worker], id=WATCH_JOB_ID, replace_existing=True, max_instances=1, coalesce=True,
    )
    # Gli upload programmati: anche questo ha bisogno del worker.
    scheduler.add_job(
        _start_due_uploads, IntervalTrigger(seconds=SCHEDULED_UPLOADS_INTERVAL_SECONDS),
        args=[session_factory, worker], id=SCHEDULED_UPLOADS_JOB_ID, replace_existing=True, max_instances=1,
        coalesce=True,
    )


def build_scheduler(session_factory: sessionmaker, data_dir: str) -> BackgroundScheduler:
    scheduler = BackgroundScheduler()
    with session_factory() as session:
        cron_expr = settings_repo.get_setting(session, SETTING_KEY)
    if cron_expr:
        _add_job(scheduler, cron_expr, session_factory, data_dir)
    scheduler.add_job(
        _reconcile_between_runs, IntervalTrigger(seconds=RECONCILE_INTERVAL_SECONDS),
        args=[session_factory], id=RECONCILE_JOB_ID, replace_existing=True, max_instances=1, coalesce=True,
    )
    scheduler.add_job(
        _deliver_events, IntervalTrigger(seconds=EVENTS_INTERVAL_SECONDS),
        args=[session_factory], id=EVENTS_JOB_ID, replace_existing=True, max_instances=1, coalesce=True,
    )
    scheduler.add_job(
        _apply_arr_webhook_events, IntervalTrigger(seconds=ARR_HOOKS_INTERVAL_SECONDS),
        args=[session_factory, data_dir], id=ARR_HOOKS_JOB_ID, replace_existing=True, max_instances=1, coalesce=True,
    )
    scheduler.add_job(
        _check_updates, IntervalTrigger(seconds=UPDATE_INTERVAL_SECONDS),
        args=[session_factory], id=UPDATE_JOB_ID, replace_existing=True, max_instances=1, coalesce=True,
    )
    return scheduler


def reschedule(
    scheduler: BackgroundScheduler, cron_expr: str | None, session_factory: sessionmaker, data_dir: str
) -> None:
    """Riconfigura (o rimuove, se cron_expr è vuoto/None) il job schedulato
    a runtime, chiamata da nazgarr/api/schedule.py dopo un aggiornamento."""
    if scheduler.get_job(JOB_ID) is not None:
        scheduler.remove_job(JOB_ID)
    if cron_expr:
        _add_job(scheduler, cron_expr, session_factory, data_dir)
