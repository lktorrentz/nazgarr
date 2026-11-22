"""Approva tutti: le review in coda approvate in un colpo, dal pulsante in
cima alla coda della pagina Reseeding (richiesta dell'utente, 2026-11-22).

Ogni review passa da review.request_approval, come dal suo pulsante: con la
verifica accesa (default) entra nella coda dei controlli completi, uno alla
volta sul disco, e si esegue solo a controllo superato; spenta, si esegue
subito. È un clic dell'utente dopo una conferma con i numeri, mai
un'esecuzione automatica: quella resta l'impostazione a parte, spenta di
default.

Di default solo le consigliate (sopra soglia, auto_approved); quelle sotto
soglia solo se l'utente le include. Restano fuori quelle già in verifica e
quelle il cui controllo completo è già fallito: ripeterlo darebbe lo stesso
esito dopo un'altra lettura completa dei file.

In background (BackgroundTasks, come "Scan now"): con la verifica spenta
ogni esecuzione crea gli hardlink, scarica il .torrent e lo aggiunge al
client, e con centinaia di review la richiesta resterebbe appesa per
minuti. Un giro alla volta; lo stato è in memoria, come quello dei
controlli completi (un riavvio lo perde, le review approvate restano).
"""

import logging
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from nazgarr.core.logs import safe_error
from nazgarr.core.models import MatchReview
from nazgarr.reseed import review

logger = logging.getLogger(__name__)

RECOMMENDED = "auto_approved"


@dataclass(frozen=True)
class Counts:
    recommended: int  # consigliate, sopra soglia
    below_threshold: int
    verify_failed: int  # controllo completo già fallito: restano in coda


def _ready(session: Session):
    """Le review in coda senza esecuzioni, con quello che serve a sceglierle."""
    return review.without_seed_job(
        session.query(MatchReview.id, MatchReview.status, MatchReview.verify_status)
        .filter(MatchReview.status.in_(review.READY_FOR_DECISION_STATUSES))
    )


def counts(session: Session) -> Counts:
    recommended = below = failed = 0
    for _id, status, verify_status in _ready(session):
        if verify_status == "verifying":
            continue
        if verify_status == "failed":
            failed += 1
        elif status == RECOMMENDED:
            recommended += 1
        else:
            below += 1
    return Counts(recommended=recommended, below_threshold=below, verify_failed=failed)


def selected_ids(session: Session, include_below_threshold: bool) -> list[int]:
    """Gli id da approvare, nell'ordine della coda (review.queue_groups):
    i controlli completi partono in quell'ordine, prima quello che la
    pagina mostra in alto."""
    wanted = {
        review_id for review_id, status, verify_status in _ready(session)
        if verify_status not in ("verifying", "failed") and (status == RECOMMENDED or include_below_threshold)
    }
    return [review_id for group in review.queue_groups(session) for review_id in group if review_id in wanted]


@dataclass
class Batch:
    id: str
    total: int
    verify: bool  # con il controllo completo prima dell'esecuzione
    done: int = 0
    approved: int = 0  # approvate: eseguite, o in coda per il controllo completo
    skipped: int = 0  # decise da un'altra parte nel frattempo (una run, un altro clic)
    failed: int = 0
    last_error: str | None = None
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None


class InProgress(Exception):
    """Un "Approva tutti" è già in corso."""


_lock = threading.Lock()
_current: Batch | None = None


def current() -> Batch | None:
    return _current


def start(ids: list[int], verify: bool) -> Batch:
    """Il giro nuovo, uno alla volta. Senza niente da approvare nasce già
    finito, e chi chiama non lancia run()."""
    global _current
    with _lock:
        if _current is not None and _current.finished_at is None:
            raise InProgress
        _current = Batch(id=uuid.uuid4().hex[:12], total=len(ids), verify=verify)
        if not ids:
            _current.finished_at = _current.started_at
        return _current


def run(session_factory: Callable[[], Session], batch: Batch, ids: list[int]) -> None:
    session = session_factory()
    try:
        for review_id in ids:
            row = session.get(MatchReview, review_id)
            try:
                if row is None:
                    batch.skipped += 1
                else:
                    review.request_approval(session, row, session_factory)
                    batch.approved += 1
            except (review.AlreadyVerifyingError, review.ReviewAlreadyDecidedError):
                session.rollback()
                batch.skipped += 1
            except Exception as exc:
                session.rollback()
                logger.exception("Approva tutti: review %s non approvata", review_id)
                batch.failed += 1
                batch.last_error = safe_error(exc)
            batch.done += 1
    finally:
        batch.finished_at = datetime.now(UTC)
        session.close()
        logger.info("Approva tutti: %d approvate, %d saltate, %d non riuscite su %d",
                    batch.approved, batch.skipped, batch.failed, batch.total)
