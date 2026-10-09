"""Webhook di Radarr e Sonarr: aggiornare la libreria quando un file arriva,
cambia o sparisce, senza una scansione (decisione dell'utente, 2026-10-06).

Radarr e Sonarr chiamano POST /api/arr-hooks/{radarr|sonarr}/{id} (nazgarr/api/
arr_hooks.py) con la password del webhook dell'istanza. L'evento si salva in
arr_webhook_event e lo scheduler lo applica (process_pending) dopo qualche
secondo di quiete, mai durante una run: una scansione completa nel frattempo
lo renderebbe inutile o contraddittorio.

Cosa fa ogni evento, solo sui file nominati (nazgarr/library/scanner.py, un
stat e un hash parziale: si sveglia solo il loro disco):
- Download (import o upgrade): il file nuovo entra nella libreria, collegato
  al file del torrent da cui è stato hardlinkato (stesso inode), con
  l'identità che Radarr/Sonarr conoscono già; i file sostituiti escono.
- Rename: il file col nome vecchio esce, quello nuovo entra con la stessa
  identità.
- MovieFileDelete / EpisodeFileDelete: il file esce.
- Test: solo registrato, per dire all'utente che il collegamento funziona.
Gli altri eventi (Grab, Health, ...) si ignorano.

I percorsi sono quelli che vede Radarr/Sonarr: il file si cerca sotto le
cartelle media dei dischi provando le parti finali del percorso, dalla più
corta (come nazgarr/integrations/arr.py::path_key, ma con un stat invece
dell'indice), e la dimensione deve coincidere. Facoltativo
(arr_webhook_search, spento): dopo un'importazione si cerca il file sui
tracker, come "Cerca ora".
"""

import json
import logging
import os
import re
import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

from nazgarr.adapters.media_resolver.base import ResolvedMedia
from nazgarr.core import settings_registry
from nazgarr.core.fs_scope import ScopeViolation, resolve_scoped
from nazgarr.core.models import (
    ArrWebhookEvent,
    Disk,
    MediaFile,
    RadarrInstance,
    SonarrInstance,
    TorrentClient,
    Tracker,
)
from nazgarr.integrations import arr
from nazgarr.library import disk_folders, file_changes, scanner
from nazgarr.library.resolution import complete_media_items, get_or_create_media_item
from nazgarr.library.scan_state import latest_scan_by_disk

logger = logging.getLogger(__name__)

MODELS = {"radarr": RadarrInstance, "sonarr": SonarrInstance}
HANDLED = {"Test", "Download", "Rename", "MovieFileDelete", "EpisodeFileDelete"}
QUIET = timedelta(seconds=10)  # un'importazione manda più eventi di fila
KEEP_EVENTS = 300
SEARCH_SETTING = "arr_webhook_search"


def new_token() -> str:
    return secrets.token_urlsafe(32)


def instance_for_token(session: Session, source: str, instance_id: int, token: str | None):
    """L'istanza, se il token è il suo (confronto a tempo costante)."""
    model = MODELS.get(source)
    instance = session.get(model, instance_id) if model is not None else None
    if instance is None or not instance.webhook_token or not token:
        return None
    return instance if secrets.compare_digest(instance.webhook_token, token) else None


def receive(session: Session, source: str, instance, payload: dict) -> ArrWebhookEvent:
    event_type = str(payload.get("eventType") or "")
    event = ArrWebhookEvent(
        source=source, instance_id=instance.id, event_type=event_type or "unknown",
        payload_json=json.dumps(payload), received_at=datetime.now(UTC),
        status="pending" if event_type in HANDLED else "ignored",
        detail=None if event_type in HANDLED else "event not used by Nazgarr",
    )
    session.add(event)
    session.commit()
    return event


def last_event(session: Session, source: str, instance_id: int) -> ArrWebhookEvent | None:
    return (
        session.query(ArrWebhookEvent).filter_by(source=source, instance_id=instance_id)
        .order_by(ArrWebhookEvent.id.desc()).first()
    )


# --- dove sta il file sui dischi di Nazgarr ---------------------------------------------


def _parts(arr_path: str) -> list[str]:
    return [p for p in arr_path.replace("\\", "/").split("/") if p]


def locate(session: Session, arr_path: str, size: int | None) -> tuple[Disk, str] | None:
    """Il file su un disco: le parti finali del percorso di Radarr/Sonarr sotto
    ogni cartella media, dalla più corta. Con la dimensione, se c'è. Il
    percorso arriva da fuori: passa dallo scoping (mai fuori dalla cartella
    media, nemmeno con un "..") e un link simbolico non vale, come per lo
    scanner."""
    parts = _parts(arr_path)
    if not parts:
        return None
    for disk in session.query(Disk).all():
        for folder in disk_folders.absolute(disk, "media"):
            for k in range(1, len(parts) + 1):
                relative = "/".join(parts[-k:])
                try:
                    candidate = resolve_scoped(folder, relative)
                    if os.path.islink(os.path.join(folder, relative)) or not os.path.isfile(candidate):
                        continue
                    if not size or os.path.getsize(candidate) == size:
                        return disk, candidate
                except (ScopeViolation, OSError):
                    continue
    return None


def known_file(session: Session, arr_path: str, size: int | None) -> MediaFile | None:
    """La riga attuale di un file che non c'è più (cancellato o rinominato):
    stessa cartella e nome (le ultime due parti), e la stessa dimensione."""
    parts = _parts(arr_path)
    if not parts:
        return None
    tail = "/".join(parts[-2:]).lower()
    current = latest_scan_by_disk(session, MediaFile)
    rows = (
        session.query(MediaFile)
        .filter(func.lower(MediaFile.relative_path).like(f"%{tail}"))
        .all()
    )
    for row in rows:
        if current.get(row.disk_id) == row.last_scan_id and (not size or row.size_bytes == size):
            if row.relative_path.lower() == tail or row.relative_path.lower().endswith("/" + tail):
                return row
    return None


# --- identità ----------------------------------------------------------------------------


def _identity(source: str, instance_id: int, payload: dict, episode: dict | None = None) -> ResolvedMedia | None:
    if source == "radarr":
        movie = payload.get("movie") or {}
        if not movie.get("tmdbId"):
            return None
        return ResolvedMedia(tmdb_id=movie["tmdbId"], content_type="movie", source="radarr",
                             title=movie.get("title"), year=movie.get("year") or None,
                             imdb_id=movie.get("imdbId") or None, arr_instance_id=instance_id,
                             poster_path=arr.tmdb_poster_path(movie))
    series = payload.get("series") or {}
    if not series.get("tmdbId"):
        return None  # Sonarr senza tmdbId: la prossima scansione lo identifica come sempre
    episode = episode or {}
    return ResolvedMedia(tmdb_id=series["tmdbId"], content_type="tv", source="sonarr",
                         season_number=episode.get("seasonNumber"), episode_number=episode.get("episodeNumber"),
                         title=series.get("title"), year=series.get("year") or None,
                         imdb_id=series.get("imdbId") or None, arr_instance_id=instance_id,
                         arr_slug=series.get("titleSlug"))


def _identify(session: Session, media_file: MediaFile, resolved: ResolvedMedia | None) -> None:
    if resolved is None:
        return
    item = get_or_create_media_item(session, resolved)
    media_file.media_item_id = item.id
    media_file.resolver_source = resolved.source
    session.commit()


# --- gli eventi ----------------------------------------------------------------------------


def _file_of(source: str, payload: dict) -> dict:
    return payload.get("movieFile" if source == "radarr" else "episodeFile") or {}


def _first_episode(payload: dict) -> dict | None:
    episodes = payload.get("episodes") or []
    return min(episodes, key=lambda e: (e.get("seasonNumber") or 0, e.get("episodeNumber") or 0)) if episodes else None


def _forget(session: Session, arr_file: dict) -> MediaFile | None:
    row = known_file(session, arr_file.get("path") or "", arr_file.get("size"))
    return row if row is not None and scanner.forget_media_file(session, row) else None


_INFO_HASH = re.compile(r"[0-9a-f]{40}")


def _register_download(session: Session, payload: dict) -> str | None:
    """Il torrent da cui Radarr/Sonarr hanno importato (downloadId: per un
    client torrent è l'info hash): i suoi file e il torrent nel client si
    registrano subito (nazgarr/library/seed_refresh.py), senza aspettare la
    scansione. Prima, fino alla scansione successiva il file sembrava non in
    seed: in libreria senza torrent, e la ricerca dopo l'importazione (o
    "Cerca ora") lo proponeva in reseed (segnalato 2026-10-09). Un download
    da usenet non ha un info hash: niente da fare."""
    from nazgarr.integrations import adapter_factory
    from nazgarr.library import seed_refresh

    info_hash = str(payload.get("downloadId") or "").strip().lower()
    if not _INFO_HASH.fullmatch(info_hash) or seed_refresh.is_registered(session, info_hash):
        return None
    for client in session.query(TorrentClient).filter_by(enabled=True).order_by(TorrentClient.id).all():
        adapter = None
        try:
            adapter = adapter_factory.build_torrent_client_adapter(client)
            info = adapter.get_torrent_info(info_hash)
        except Exception:
            logger.warning("Torrent %s non letto da %r", info_hash, client.label, exc_info=True)
            continue
        finally:
            if adapter is not None:
                adapter_factory.close_adapter(adapter)
        if info is not None:
            linked = seed_refresh.register_client_torrent(session, client, info)
            return f"its torrent is in {client.label} ({linked} file(s) linked)"
    return None


def _download(session: Session, event: ArrWebhookEvent, payload: dict) -> tuple[str, list[int], list[int]]:
    arr_file = _file_of(event.source, payload)
    gone = [row.id for row in (_forget(session, f) for f in payload.get("deletedFiles") or []) if row is not None]
    found = locate(session, arr_file.get("path") or "", arr_file.get("size"))
    if found is None:
        return f"file not found on the disks: {arr_file.get('path')}", [], gone
    disk, full_path = found
    media_file = scanner.record_media_file(session, disk, full_path)
    if media_file is None:
        return f"{full_path}: the disk was never scanned, the first scan will add it", [], gone
    resolved = _identity(event.source, event.instance_id, payload, _first_episode(payload))
    _identify(session, media_file, resolved)
    note = f"added {media_file.relative_path}"
    torrent = _register_download(session, payload)
    if torrent:
        note += f", {torrent}"
    if resolved is None:
        note += " (not identified: the next scan will try TMDB)"
    if gone:
        note += f", {len(gone)} replaced file(s) removed"
    return note, [media_file.id], [media_file.id, *gone]


def _rename(session: Session, event: ArrWebhookEvent, payload: dict) -> tuple[str, list[int], list[int]]:
    renamed = payload.get("renamedMovieFiles" if event.source == "radarr" else "renamedEpisodeFiles") or []
    moved = 0
    touched: list[int] = []
    for f in renamed:
        old = known_file(session, f.get("previousPath") or "", f.get("size"))
        found = locate(session, f.get("path") or "", f.get("size"))
        if found is None:
            continue
        media_file = scanner.record_media_file(session, *found)
        if media_file is None:
            continue
        if old is not None and old.id != media_file.id:
            if old.media_item_id is not None and media_file.media_item_id is None:
                media_file.media_item_id, media_file.resolver_source = old.media_item_id, old.resolver_source
                session.commit()
            scanner.forget_media_file(session, old)
            touched.append(old.id)
        touched.append(media_file.id)
        moved += 1
    return f"{moved} of {len(renamed)} renamed file(s) updated", [], touched


def _delete(session: Session, event: ArrWebhookEvent, payload: dict) -> tuple[str, list[int], list[int]]:
    row = _forget(session, _file_of(event.source, payload))
    if row is None:
        return "file not in the library, or nothing to do", [], []
    return "file removed", [], [row.id]


HANDLERS = {"Download": _download, "Rename": _rename, "MovieFileDelete": _delete, "EpisodeFileDelete": _delete}


def _search(session: Session, media_file_ids: list[int]) -> None:
    """Facoltativo: i file appena importati sui tracker, come "Cerca ora"."""
    from nazgarr.integrations import adapter_factory
    from nazgarr.reseed import matching

    for tracker_row in session.query(Tracker).filter_by(enabled=True).all():
        adapter = None
        try:
            adapter = adapter_factory.build_tracker_adapter(tracker_row)
            matching.run_media_to_torrent_matching(
                session, tracker_row, adapter, only_media_file_ids=set(media_file_ids))
        except Exception:
            logger.warning("Ricerca dopo l'importazione fallita su %r", tracker_row.label, exc_info=True)
        finally:
            if adapter is not None:
                adapter_factory.close_adapter(adapter)


def _complete(session: Session, data_dir: str | None) -> None:
    """Titolo e poster dei contenuti appena arrivati, come alla fine della
    risoluzione di una scansione: dal poster che Radarr ha mandato, o da
    TMDB se c'è la chiave. Solo per quelli che non li hanno."""
    if data_dir is None:
        return
    from nazgarr.core import settings_repo
    from nazgarr.library.tmdb_client import TMDBClient

    key = settings_repo.get_setting(session, "tmdb_api_key")
    try:
        complete_media_items(session, os.path.join(data_dir, "posters"), None,
                             TMDBClient(api_key=key).details if key else None)
    except Exception:
        logger.warning("Titoli e poster dopo i webhook non completati", exc_info=True)


def process_pending(session: Session, now: datetime | None = None, data_dir: str | None = None) -> int:
    """Dallo scheduler: gli eventi in coda da almeno QUIET, uno alla volta.
    Mai durante una run (lo scheduler riprova al giro dopo). Poi titoli e
    poster dei contenuti nuovi, e i cambiamenti per la Dashboard."""
    from nazgarr.reseed import pipeline

    if pipeline.run_in_progress(session):
        return 0
    now = now or datetime.now(UTC)
    events = (
        session.query(ArrWebhookEvent)
        .filter(ArrWebhookEvent.status == "pending", ArrWebhookEvent.received_at <= now - QUIET)
        .order_by(ArrWebhookEvent.id).all()
    )
    imported: list[int] = []
    touched: dict[int, str] = {}
    for event in events:
        try:
            if event.event_type == "Test":
                event.detail = "connection works"
            else:
                payload = json.loads(event.payload_json)
                event.detail, ids, changed = HANDLERS[event.event_type](session, event, payload)
                imported.extend(ids)
                touched.update(dict.fromkeys(changed, event.source))
            event.status = "done"
        except Exception as exc:
            logger.exception("Evento %s %s (#%s) non applicato", event.source, event.event_type, event.id)
            session.rollback()
            event.status, event.detail = "failed", f"{type(exc).__name__}: {exc}"
        event.processed_at = datetime.now(UTC)
        session.commit()
    if imported:
        _complete(session, data_dir)
    if touched:
        try:
            file_changes.record_webhook_changes(session, touched)
        except Exception:
            logger.warning("Cambiamenti dei webhook non registrati per la Dashboard", exc_info=True)
            session.rollback()
    if imported and settings_registry.get_bool(session, SEARCH_SETTING):
        _search(session, imported)
    _prune(session)
    return len(events)


def _prune(session: Session) -> None:
    keep = session.query(ArrWebhookEvent.id).order_by(ArrWebhookEvent.id.desc()).offset(KEEP_EVENTS).first()
    if keep is not None:
        session.query(ArrWebhookEvent).filter(
            ArrWebhookEvent.id <= keep[0], ArrWebhookEvent.status != "pending",
        ).delete(synchronize_session=False)
        session.commit()
