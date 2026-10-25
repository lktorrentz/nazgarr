"""Il link di download del .torrent di un candidato, senza la chiave del
tracker nel DB (audit 2026-10-18).

Un link UNIT3D è /torrent/download/<id>.<rsskey>: la chiave è personale,
cifrata in tracker.rss_key, e porta alla passkey (il .torrent scaricato con
la chiave contiene l'announce personale). candidate.download_link la teneva
in chiaro, così bastava una copia del DB per averla, senza APP_SECRET_KEY.

Ora il candidato salva il link con un segnaposto al posto della chiave, e la
chiave torna solo quando il .torrent si scarica (esecuzione, controllo
completo), dall'adapter del tracker: rewrite_download_link con la chiave
attuale, altrimenti il link del dettaglio del torrent. Così un candidato in
coda resta valido anche dopo un cambio di chiave. Un tracker il cui adapter
non sa rimettere la chiave (un plugin senza rewrite_download_link) salva il
link com'è: non si saprebbe come ricostruirlo.

Un adapter con una sua forma di link (Torznab: la API key dell'endpoint
nella query) lo dice con strip_download_link e restore_download_link, che
qui hanno la precedenza."""

import logging
import re
from urllib.parse import urlsplit

from nazgarr.adapters.tracker.base import NotSupportedError, TrackerRateLimitedError, UploadError

logger = logging.getLogger(__name__)

# Alfanumerico come una chiave vera: rewrite_download_link dell'adapter UNIT3D
# lo sostituisce con la chiave attuale come farebbe con una chiave vecchia.
PLACEHOLDER = "rsskey"
_KEYED = re.compile(r"(/torrent/download/\d+\.)([A-Za-z0-9]+)$")


def strip_key(link: str | None) -> str | None:
    """Il link con il segnaposto al posto della chiave; invariato se non ha
    la forma /torrent/download/<id>.<chiave>."""
    if not link:
        return link
    parts = urlsplit(link)
    if not _KEYED.search(parts.path):
        return link
    return parts._replace(path=_KEYED.sub(rf"\g<1>{PLACEHOLDER}", parts.path)).geturl()


def stored_link(adapter, link: str | None) -> str | None:
    """Il link da salvare sul candidato: senza chiave, se l'adapter del
    tracker la sa rimettere."""
    strip = getattr(adapter, "strip_download_link", None)
    if strip is not None:
        return strip(link)
    if getattr(adapter, "rewrite_download_link", None) is None:
        return link
    return strip_key(link)


def is_stripped(link: str | None) -> bool:
    match = _KEYED.search(urlsplit(link).path) if link else None
    return match is not None and match.group(2) == PLACEHOLDER


def _detail_link(adapter, candidate) -> str | None:
    get_detail = getattr(adapter, "get_torrent_detail", None)
    if get_detail is None:
        return None
    try:
        link = get_detail(candidate.torrent_id_remote).download_link
    except TrackerRateLimitedError:
        raise
    except (UploadError, NotSupportedError, KeyError, ValueError):
        logger.warning("Dettaglio del torrent %s non disponibile", candidate.torrent_id_remote, exc_info=True)
        return None
    return None if is_stripped(link) else link


def usable_link(adapter, candidate) -> str | None:
    """Il link con cui scaricare ora il .torrent del candidato."""
    link = candidate.download_link
    restore = getattr(adapter, "restore_download_link", None)
    if restore is not None:
        return restore(link)
    if not is_stripped(link):
        return link
    rewrite = getattr(adapter, "rewrite_download_link", None)
    rebuilt = rewrite(link) if rewrite is not None else link
    return rebuilt if not is_stripped(rebuilt) else _detail_link(adapter, candidate)


def download(adapter, candidate) -> bytes:
    """I byte del .torrent del candidato dal suo tracker. Con un link salvato
    senza chiave, se il tracker rifiuta quella attuale (cambiata da poco) si
    riprova una volta col link del dettaglio."""
    link = usable_link(adapter, candidate)
    if not link:
        raise UploadError("No download key is known for this tracker yet: run a scan, then try again")
    try:
        return adapter.download_torrent(link)
    except TrackerRateLimitedError:
        raise
    except UploadError:
        if not is_stripped(candidate.download_link):
            raise
        fresh = _detail_link(adapter, candidate)
        if not fresh or fresh == link:
            raise
        return adapter.download_torrent(fresh)
