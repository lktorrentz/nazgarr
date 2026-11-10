"""Il claim della resurrection di un torrent morto prima di rimetterlo in
seed (decisione dell'utente, 2026-11-10): UNIT3D lo accetta solo con 0
seeder, quindi va fatto prima che il client lo annunci. Una sola regola per
i due flussi che rimettono in seed un torrent del tracker: il reseed del
flusso di upload (nazgarr/upload/execute.py::_claim_resurrection) e
l'esecuzione della coda di revisione (nazgarr/reseed/executor.py::_seed),
entrambi prima di ogni hardlink e del client.

Solo con la sessione web salvata sul tracker (tracker.history_session_cookie,
la riga Cookie del login): senza, nessuna richiesta, e il claim resta a mano.
Il lavoro sul sito lo fa l'adapter (claim_resurrection, SDK 1.9; per UNIT3D
nazgarr/adapters/tracker/unit3d_web.py)."""

import logging

from nazgarr.adapters.tracker.base import NotSupportedError, TrackerWebResult
from nazgarr.core.logs import safe_error

logger = logging.getLogger(__name__)

# Gli esiti dopo cui si rimette in seed: fatto, non offerto dalla pagina
# (vivo, troppo recente, un claim già in attesa, anche fatto a mano),
# rifiutato (un tuo upload). Gli altri (sessione scaduta, un errore) fermano
# il reseed prima di toccare file e client, così l'occasione non si perde.
GO_ON = ("claimed", "not_offered", "refused")


def has_session(tracker) -> bool:
    return bool(tracker is not None and tracker.history_session_cookie)


def claim_before_seed(tracker, adapter, torrent_id_remote: str, seeders: int | None = None) -> TrackerWebResult | None:
    """L'esito del claim, o None se non c'era niente da fare: nessuna
    sessione, un adapter che non sa farlo, un torrent con seeder.
    seeders: quanti ne ha visti chi chiama (il dupe check); None = non si sa,
    e si chiedono all'API del tracker (get_torrent_detail), una richiesta
    prima del sito: per un torrent vivo il sito non si tocca. Se l'API non
    risponde decide la pagina del torrent, che offre il claim solo a un
    torrent morto."""
    if not has_session(tracker):
        return None
    claim = getattr(adapter, "claim_resurrection", None)  # un plugin che non deriva da TrackerAdapter
    if claim is None:
        return None
    torrent = str(torrent_id_remote)
    if seeders is None:
        detail = getattr(adapter, "get_torrent_detail", None)
        if detail is not None:
            try:
                seeders = detail(torrent).seeders
            except Exception:
                logger.warning("Seeder di %s su %s non letti: decide la pagina del torrent", torrent,
                               tracker.label, exc_info=True)
    if seeders is not None and seeders > 0:
        return None
    try:
        result = claim(torrent)
    except NotSupportedError:
        return None
    except Exception as exc:
        logger.warning("Claim della resurrection di %s su %s fallito", torrent, tracker.label, exc_info=True)
        result = TrackerWebResult("failed", safe_error(exc))
    return None if result.status == "no_session" else result
