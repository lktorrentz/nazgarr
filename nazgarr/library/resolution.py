"""Risoluzione TMDB dei media_file (docs/SPEC.md sezione 6).

A differenza di scanner.py/torrent_indexer.py, qui non c'è un bulk upsert
possibile: ogni file richiede il proprio parsing filename + la propria
chiamata di rete a TMDB, quindi è legittimamente un'operazione per-file.
Il dedup avviene a livello di media_item (stesso tmdb_id/season/episode
riusa la stessa riga, get_or_create_media_item), non a livello di query.
"""

import logging
import os
from collections.abc import Callable

from sqlalchemy.orm import Session

from nazgarr.adapters.media_resolver.base import MediaResolverAdapter, ResolvedMedia
from nazgarr.core.db_utils import keep_loaded_on_commit
from nazgarr.core.file_types import is_video
from nazgarr.core.models import MediaFile, MediaItem
from nazgarr.core.run_progress import NULL_PROGRESS
from nazgarr.core.settings_repo import get_setting, set_setting
from nazgarr.library.exclusions import load_exclusions
from nazgarr.library.poster_cache import download_poster, poster_file
from nazgarr.library.scan_state import is_current, latest_scan_by_disk

logger = logging.getLogger(__name__)

# Versione delle regole con cui guessit + TMDB identificano un file. Quando
# cambia, i file identificati dal solo nome (resolver_source
# "filename_parser") si rileggono una volta: la v1 cercava su TMDB solo il
# titolo base (senza il sottotitolo che guessit mette in alternative_title)
# e con `year` invece di `primary_release_year` — "Mission Impossible
# Fallout (2018)" poteva diventare un altro film della saga. La v3: in
# "08 - Titolo 426.mkv" l'episodio è il numero davanti, non S04E26
# (nazgarr/library/guess.py, 2026-11-11).
IDENTITY_RULES_VERSION = "3"
IDENTITY_RULES_KEY = "identity_rules_version"
ARR_SOURCES = ("radarr", "sonarr")


def _apply_arr_link(item: MediaItem, source: str | None, instance_id: int | None, slug: str | None,
                    imdb_id: str | None) -> None:
    item.imdb_id = item.imdb_id or imdb_id
    if source in ("radarr", "sonarr") and instance_id is not None and slug:
        item.arr_kind, item.arr_instance_id, item.arr_slug = source, instance_id, slug


def get_or_create_media_item(session: Session, resolved: ResolvedMedia) -> MediaItem:
    query = session.query(MediaItem).filter_by(content_type=resolved.content_type, tmdb_id=resolved.tmdb_id)
    if resolved.content_type == "tv":
        query = query.filter_by(season_number=resolved.season_number, episode_number=resolved.episode_number)
    item = query.one_or_none()
    if item is not None:
        # Voci create prima che si salvassero titolo/anno/poster: si
        # completano con quello che la nuova risoluzione sa già.
        item.title = item.title or resolved.title
        item.year = item.year or resolved.year
        item.tmdb_poster_path = item.tmdb_poster_path or resolved.poster_path
        _apply_arr_link(item, resolved.source, resolved.arr_instance_id, resolved.arr_slug, resolved.imdb_id)
        return item

    item = MediaItem(
        content_type=resolved.content_type,
        tmdb_id=resolved.tmdb_id,
        season_number=resolved.season_number if resolved.content_type == "tv" else None,
        episode_number=resolved.episode_number if resolved.content_type == "tv" else None,
        tmdb_poster_path=resolved.poster_path,
        title=resolved.title,
        year=resolved.year,
    )
    _apply_arr_link(item, resolved.source, resolved.arr_instance_id, resolved.arr_slug, resolved.imdb_id)
    session.add(item)
    session.commit()
    return item


_LOAD_CHUNK = 500  # id per IN (...): sotto il limite di variabili dei SQLite vecchi


def _files_to_resolve(session: Session, arr_index, reread_parsed: bool) -> list[MediaFile]:
    """File ancora senza identità, più quelli da rileggere:
    - Radarr/Sonarr fanno fede: un file che conoscono ma identificato da
      altro (nome del file) si riallinea alla loro identità;
    - un cambio di IDENTITY_RULES_VERSION rilegge i file identificati dal nome.
    Solo i file ancora sul disco (audit 2026-10-18): uno sparito non ha niente
    da identificare, e prima tornava al resolver (e su TMDB) a ogni run. La
    sua riga tiene l'identità che ha: se il file torna, la ritrova. La scelta
    si fa sulle sole colonne che servono; oggetti ORM solo per i file scelti."""
    latest = latest_scan_by_disk(session, MediaFile)
    ids = []
    for mf in session.query(
        MediaFile.id, MediaFile.disk_id, MediaFile.relative_path, MediaFile.size_bytes, MediaFile.last_scan_id,
        MediaFile.media_item_id, MediaFile.resolver_source,
    ).order_by(MediaFile.id):
        if not is_video(mf.relative_path):  # nfo, sottotitoli, immagini: nessuna identità da cercare
            continue
        if not is_current(mf, latest):
            continue
        if mf.media_item_id is None:
            ids.append(mf.id)
        elif mf.resolver_source in ARR_SOURCES:
            continue
        elif reread_parsed and mf.resolver_source == "filename_parser":
            ids.append(mf.id)
        elif arr_index is not None and arr_index.identity_for(mf.relative_path, mf.size_bytes) is not None:
            ids.append(mf.id)
    files = []
    for start in range(0, len(ids), _LOAD_CHUNK):
        chunk = ids[start:start + _LOAD_CHUNK]
        files.extend(session.query(MediaFile).filter(MediaFile.id.in_(chunk)).order_by(MediaFile.id).all())
    return files


def resolve_unmatched_media_files(
    session: Session, resolver: MediaResolverAdapter, posters_dir: str, progress=NULL_PROGRESS, arr_index=None
) -> dict[str, int]:
    """Per ogni media_file senza media_item_id ancora (o da rileggere, vedi
    _files_to_resolve), prova a risolverlo.
    Un fallimento di rete/resolver su un singolo file viene loggato e
    contato come unresolved — non deve mai far fallire l'intero giro; un
    file già identificato che non si riesce a rileggere tiene l'identità che ha.
    I file esclusi (Configuration > Exclusions) non vengono mai risolti:
    niente chiamate TMDB per sample, trailer e simili."""
    exclusions = load_exclusions(session)
    reread_parsed = get_setting(session, IDENTITY_RULES_KEY) != IDENTITY_RULES_VERSION
    videos = _files_to_resolve(session, arr_index, reread_parsed)
    excluded = sum(1 for mf in videos if exclusions.is_excluded(mf.relative_path))
    media_files = [mf for mf in videos if not exclusions.is_excluded(mf.relative_path)]
    progress.add_total(len(media_files))
    resolved = 0
    unresolved = 0
    corrected = 0

    # Un commit per file con tutti i file da risolvere caricati: senza, ogni
    # commit li scadrebbe tutti (nazgarr/core/db_utils.py).
    with keep_loaded_on_commit(session):
        for mf in media_files:
            progress.advance()
            abs_path = os.path.join(mf.disk.root_path, mf.relative_path)
            previous = mf.media_item_id
            try:
                result = resolver.resolve(abs_path)
            except Exception:
                logger.exception("Resolver fallito su %r", abs_path)
                if previous is None:
                    unresolved += 1
                continue

            if result is None:
                if previous is None:
                    unresolved += 1
                continue

            media_item = get_or_create_media_item(session, result)
            mf.media_item_id = media_item.id
            mf.resolver_source = result.source or resolver.SOURCE
            session.commit()
            if previous is None:
                resolved += 1
            elif previous != media_item.id:
                corrected += 1
                logger.info("Identità corretta per %r: media_item %s -> %s", mf.relative_path, previous, media_item.id)

            if result.poster_path:
                try:
                    download_poster(posters_dir, result.content_type, result.tmdb_id, result.poster_path)
                except Exception:
                    # Un poster mancante non è un errore di risoluzione — il contenuto
                    # resta identificato, mostrerà solo un placeholder in UI (§6).
                    logger.warning("Download poster fallito per tmdb_id=%s", result.tmdb_id, exc_info=True)

        if reread_parsed:
            set_setting(session, IDENTITY_RULES_KEY, IDENTITY_RULES_VERSION)
    return {"resolved": resolved, "unresolved": unresolved, "excluded": excluded, "corrected": corrected}


def complete_media_items(
    session: Session,
    posters_dir: str,
    arr_index=None,
    tmdb_details: Callable[[str, int], dict] | None = None,
    progress=NULL_PROGRESS,
) -> dict[str, int]:
    """Completa le voci senza titolo o senza poster in cache — create da
    versioni precedenti, o il cui poster non si era scaricato. Una sola
    richiesta per contenuto (tmdb_id), mai per episodio: prima Radarr/Sonarr
    (nessuna chiamata di rete verso TMDB), poi il dettaglio TMDB se c'è la
    chiave. Un errore su un contenuto lo lascia com'è per la run successiva."""
    by_content: dict[tuple[str, int], list[MediaItem]] = {}
    for item in session.query(MediaItem).all():
        missing_poster = not item.tmdb_poster_path or not os.path.exists(
            poster_file(posters_dir, item.content_type, item.tmdb_id)
        )
        if item.title is None or missing_poster or (arr_index is not None and item.arr_slug is None):
            by_content.setdefault((item.content_type, item.tmdb_id), []).append(item)
    progress.add_total(len(by_content))
    completed = failed = 0

    with keep_loaded_on_commit(session):  # un commit per contenuto, con tutti gli elementi caricati
        for (content_type, tmdb_id), items in by_content.items():
            progress.advance()
            known = next((i for i in items if i.title), None)
            title, year = (known.title, known.year) if known else (None, None)
            poster_path = next((i.tmdb_poster_path for i in items if i.tmdb_poster_path), None)
            identity = arr_index.details_for(content_type, tmdb_id) if arr_index is not None else None
            if identity is not None:
                title, year = title or identity.title, year or identity.year
                poster_path = poster_path or identity.poster_path
                for item in items:
                    _apply_arr_link(item, identity.source, identity.instance_id, identity.slug, identity.imdb_id)
            if (title is None or poster_path is None) and tmdb_details is not None:
                try:
                    details = tmdb_details(content_type, tmdb_id)
                except Exception:
                    logger.warning("Dettaglio TMDB fallito per %s %s", content_type, tmdb_id, exc_info=True)
                    failed += 1
                    continue
                title, year = title or details.get("title"), year or details.get("year")
                poster_path = poster_path or details.get("poster_path")
            for item in items:
                item.title = item.title or title
                item.year = item.year or year
                item.tmdb_poster_path = item.tmdb_poster_path or poster_path
            session.commit()
            if poster_path:
                try:
                    download_poster(posters_dir, content_type, tmdb_id, poster_path)
                except Exception:
                    logger.warning("Download poster fallito per %s %s", content_type, tmdb_id, exc_info=True)
            completed += 1
    return {"completed": completed, "failed": failed}

