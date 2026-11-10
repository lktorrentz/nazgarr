"""Passo 'analyzing' del flusso di upload v2 (docs/SPEC.md §9): dopo il
match confermato, prima dei tracker, tutto quello che si può sapere in
locale. Porta il job al secondo punto di approvazione, awaiting_decision.

1. mediainfo del video principale, una volta sola per tutti i tracker;
2. i client: la sorgente è già in seed? Dall'indice dell'ultima scan
   (client_torrent_file), non con una chiamata a ogni client: stesso inode
   (hardlink, gli stessi byte) o stessa dimensione di un video;
3. Radarr/Sonarr, se configurati: se la history dice che il file è stato
   scaricato da un tracker, non è una release propria dell'utente;
   e gli altri upload non finiti con gli stessi video (other_jobs);
4. per ogni tracker, il dupe check (nazgarr/upload/dupes.py) e l'azione
   suggerita. Un tracker che non risponde non ferma gli altri.
"""

import json
import logging
import os
import threading
import time

from sqlalchemy.orm import Session

from nazgarr.adapters.tracker.base import catalog_truncated
from nazgarr.core import settings_registry
from nazgarr.core.errors import CodedError
from nazgarr.core.file_types import is_video
from nazgarr.core.models import (
    ClientTorrent,
    ClientTorrentFile,
    RadarrInstance,
    SeedFile,
    SonarrInstance,
    TorrentClient,
    TrackerUploadProfile,
    UploadEvent,
    UploadJob,
)
from nazgarr.integrations import adapter_factory, arr
from nazgarr.library import mediainfo as mediainfo_util
from nazgarr.reseed import download_links
from nazgarr.torrents import catalog_categories
from nazgarr.upload import auto as upload_auto
from nazgarr.upload import decision as upload_decision
from nazgarr.upload import dovi_probe
from nazgarr.upload import dupes as upload_dupes
from nazgarr.upload import file_names as upload_file_names
from nazgarr.upload import inventory as upload_inventory
from nazgarr.upload import jobs as upload_jobs
from nazgarr.upload import naming as upload_naming
from nazgarr.upload import pack as upload_pack
from nazgarr.upload.dupes import SourceSummary, check
from nazgarr.upload.naming import dv_profile

logger = logging.getLogger(__name__)

# Un video più piccolo di così non dice niente se ha la stessa dimensione
# di un altro (sample, extra): non conta per il confronto con i client.
MIN_MATCH_BYTES = 50 * 1024 * 1024

# L'indice Radarr/Sonarr legge tutta la history: costruito una volta ogni
# tanto, non per ogni upload in coda.
ARR_INDEX_TTL_SECONDS = 600
_arr_cache: tuple[float, arr.ArrIndex] | None = None
_arr_lock = threading.Lock()


def arr_index(session: Session) -> arr.ArrIndex:
    global _arr_cache
    with _arr_lock:
        if _arr_cache is not None and time.monotonic() - _arr_cache[0] < ARR_INDEX_TTL_SECONDS:
            return _arr_cache[1]
    index = arr.build_arr_index(session)
    with _arr_lock:
        _arr_cache = (time.monotonic(), index)
    return index


def arr_index_if_configured(session: Session) -> arr.ArrIndex | None:
    """L'indice solo se c'è almeno un'istanza abilitata; None anche se non
    si riesce a costruirlo: Radarr/Sonarr restano sempre opzionali."""
    enabled = sum(session.query(m).filter_by(enabled=True).count() for m in (RadarrInstance, SonarrInstance))
    if enabled == 0:
        return None
    try:
        return arr_index(session)
    except Exception:
        logger.warning("Indice Radarr/Sonarr non disponibile per l'upload", exc_info=True)
        return None


def source_files(job: UploadJob) -> list[tuple[str, int]]:
    """(percorso assoluto, dimensione) dei file che entrano nel torrent:
    sample e spazzatura esclusi, come nell'hashing (nazgarr/upload/inventory.py).
    Il dupe check confronta dimensioni e video con i torrent del tracker:
    contarli falsava il confronto."""
    return [(f.path, f.size) for f in upload_inventory.job_files(job)]


def _client_matches(session: Session, files: list[tuple[str, int]]) -> list[dict]:
    videos = [(p, s) for p, s in files if is_video(p) and s >= MIN_MATCH_BYTES]
    if not videos:
        return []
    by_torrent: dict[int, dict] = {}

    # Stessi byte sul disco: un file della sorgente è hardlink di un file in seed.
    for path, _size in videos:
        st = os.stat(path)
        rows = (
            session.query(ClientTorrentFile.client_torrent_id)
            .join(SeedFile, SeedFile.id == ClientTorrentFile.seed_file_id)
            .filter(SeedFile.st_dev == st.st_dev, SeedFile.inode == st.st_ino)
            .all()
        )
        for (torrent_id,) in rows:
            entry = by_torrent.setdefault(torrent_id, {"hardlinked": set(), "same_size": set()})
            entry["hardlinked"].add(path)

    sizes = {s for _p, s in videos}
    rows = (
        session.query(ClientTorrentFile.client_torrent_id, ClientTorrentFile.size_bytes)
        .filter(ClientTorrentFile.size_bytes.in_(sizes))
        .all()
    )
    for torrent_id, size in rows:
        entry = by_torrent.setdefault(torrent_id, {"hardlinked": set(), "same_size": set()})
        entry["same_size"].update(p for p, s in videos if s == size)

    matches = []
    for torrent_id, entry in by_torrent.items():
        torrent = session.get(ClientTorrent, torrent_id)
        client = session.get(TorrentClient, torrent.torrent_client_id)
        matched = entry["hardlinked"] | entry["same_size"]
        matches.append({
            "client_id": torrent.torrent_client_id,
            "client": client.label if client else None,
            "name": torrent.name,
            "info_hash": torrent.info_hash,
            "tracker_host": arr.host_of(torrent.tracker_url),
            "category": torrent.category,
            "state": torrent.state,
            "match": "hardlink" if entry["hardlinked"] else "same_size",
            "videos_matched": len(matched),
            "videos_hardlinked": len(entry["hardlinked"]),
            "videos_total": len(videos),
        })
    matches.sort(key=lambda m: (m["match"] != "hardlink", -m["videos_matched"]))
    return matches


def _video_ids(paths) -> set[tuple[int, int]]:
    """(dispositivo, inode) dei video fra i percorsi dati; un file che non
    si legge non conta."""
    out = set()
    for path in paths:
        if not is_video(path):
            continue
        try:
            st = os.stat(path, follow_symlinks=False)
        except OSError:
            continue
        out.add((st.st_dev, st.st_ino))
    return out


def other_jobs(session: Session, job: UploadJob, files: list[tuple[str, int]]) -> list[dict]:
    """Gli altri upload non finiti con gli stessi video (stessi byte: lo
    stesso inode, anche da un altro percorso), audit 2026-10-18: una stagione
    di una cartella divisa confermata a mano come la cartella intera, un
    upload creato a mano per una release che la cartella osservata ha già.
    Arrivavano alla decisione senza che niente dicesse che un altro upload
    pubblicava quei file. Mai il pack da cui viene una divisione, né quelli
    nati da questo."""
    mine = _video_ids(path for path, _size in files)
    if not mine:
        return []
    others = (
        session.query(UploadJob)
        .filter(UploadJob.id != job.id, UploadJob.status.notin_(upload_jobs.FINAL_STATES))
        .order_by(UploadJob.id)
        .all()
    )
    found = []
    for other in others:
        if other.id == job.split_from_id or other.split_from_id == job.id:
            continue
        try:
            shared = mine & _video_ids(f.path for f in upload_inventory.job_files(other))
            if shared:
                found.append({
                    "id": other.id, "status": other.status, "kind": other.kind, "title": other.title,
                    "seasons": json.loads(other.seasons_json or "[]"), "episode": other.episode,
                    "videos": len(shared),
                })
        except (OSError, ValueError, CodedError) as exc:
            # La sua sorgente non si legge (un file del pack sparito, il suo
            # disco tolto, un symlink): lo dirà la sua analisi. Mai un errore
            # di un altro job che fa fallire questa.
            logger.debug("upload %s: sorgente dell'upload %s non leggibile (%s)", job.id, other.id,
                         type(exc).__name__)
    return found


def _arr_grabs(index, files: list[tuple[str, int]]) -> list[dict]:
    if index is None:
        return []
    grabs, seen = [], set()
    for path, size in files:
        if not is_video(path):
            continue
        grab = index.grab_for(path, size)
        if grab is None:
            # Il file importato in libreria: la history conosce il percorso di
            # download, non quello in libreria. Nessun modo affidabile da qui.
            continue
        key = (grab.tracker_host, grab.torrent_id_remote)
        if key not in seen:
            seen.add(key)
            grabs.append({
                "tracker_host": grab.tracker_host, "torrent_id_remote": grab.torrent_id_remote,
                "info_hash": grab.info_hash, "indexer": grab.indexer,
            })
    return grabs


def _strip_video_ext(name: str) -> str:
    base, _ext = os.path.splitext(name)
    return base if is_video(name) else name


def name_source(job: UploadJob, files: list[tuple[str, int]], client_matches: list[dict], index) -> dict:
    """Da quale nome leggere risoluzione, sorgente, codec e gruppo per il
    nome della release. Chi parte da un file in libreria ha il nome
    rinominato da Radarr/Sonarr: meglio il nome del torrent che fa già seed
    di quei byte (hardlink), poi il nome originale che Radarr/Sonarr hanno
    registrato, e solo in ultimo il nome della sorgente.

    Per un pack di file scelti a mano (nazgarr/upload/pack.py) gli stessi
    nomi, ma quelli di un episodio: diventano il nome della stagione
    ("Show.S01E01..." -> "Show.S01..."); in ultimo il nome del pack."""
    if upload_pack.is_pack(job):
        found = _single_name_source(job, files, client_matches, index)
        seasons = json.loads(job.seasons_json or "[]")
        if found is None:
            # Le stagioni scelte di una cartella: quello che i nomi degli
            # episodi non dicono, dal nome della cartella.
            folder = upload_pack.from_folder(job)
            fallback = os.path.basename(folder["source_path"].rstrip(os.sep)) if folder else None
            return {"name": upload_pack.name(job), "origin": "source", **({"fallback": fallback} if fallback else {})}
        return {"name": upload_pack.season_name(found["name"], seasons), "fallback": upload_pack.name(job),
                "origin": found["origin"]}
    found = _single_name_source(job, files, client_matches, index)
    if found is not None:
        return found
    source_name = os.path.basename(job.source_path.rstrip(os.sep))
    # Una cartella con un solo contenuto: prima il nome del suo video, la
    # cartella per quello che manca (nazgarr/upload/file_names.py name_detected).
    layout = json.loads(job.layout_json or "{}")
    main = layout.get("main_video")
    if job.is_dir and job.kind in ("movie", "episode") and main:
        return {"name": _strip_video_ext(os.path.basename(main)), "fallback": source_name, "origin": "source"}
    return {"name": source_name, "origin": "source"}


def _single_name_source(job: UploadJob, files: list[tuple[str, int]], client_matches: list[dict], index) -> dict | None:
    hardlinked = next((m for m in client_matches if m["match"] == "hardlink"), None)
    if hardlinked is not None:
        return {"name": _strip_video_ext(hardlinked["name"]), "origin": "hardlink"}
    if index is not None:
        for path, size in sorted(files, key=lambda f: -f[1]):
            if not is_video(path):
                continue
            identity = index.identity_for(path, size)
            if identity is not None and identity.scene_name:
                return {"name": identity.scene_name, "origin": identity.source}
    return None


def summary_of(
    job: UploadJob, files: list[tuple[str, int]], analysis: dict | None = None, values: dict | None = None
) -> SourceSummary:
    """analysis: per leggere i tratti dal nome scelto per la release
    (name_source: il torrent in seed, il nome originale di Radarr/Sonarr) e
    dal MediaInfo, invece che dal nome della cartella. values: i valori del
    nome della release con gli override (source_values): tipo, risoluzione
    e codec come andranno al tracker. Senza file (un nuovo verdetto dei
    risultati salvati, upload_dupes.rejudge) le dimensioni non servono."""
    analysis = analysis or {}
    mediainfo = analysis.get("mediainfo") or {}
    video = mediainfo.get("video") or {}
    values = values or {}
    overrides = json.loads(job.overrides_json or "{}")
    if overrides.get("hdr"):
        hdr = upload_dupes.hdr_family(overrides["hdr"])
    elif video:
        hdr = upload_dupes.hdr_family(upload_naming.mi_hdr(video))
    else:
        hdr = None
    return SourceSummary(
        name=(analysis.get("name_source") or {}).get("name") or upload_pack.name(job),
        resolution=values.get("resolution") or (upload_naming.mi_resolution(video) if video else None),
        total_size_bytes=sum(s for _p, s in files),
        video_sizes=tuple(s for p, s in files if is_video(p)),
        kind=job.kind or "movie",
        seasons=frozenset(json.loads(job.seasons_json or "[]")),
        episode=job.episode,
        release_type=values.get("type"),
        codec=upload_dupes.codec_family(values.get("video_codec")) or upload_dupes.codec_family(video.get("format")),
        hdr=hdr,
        languages=upload_dupes.audio_languages(mediainfo.get("audio")) if mediainfo else None,
    )


def source_values(session: Session, job: UploadJob) -> dict:
    """I valori del nome della release senza le regole di un tracker: il
    tipo, la risoluzione e il codec che il dupe check confronta, letti come
    per il nome (MediaInfo, nome, override)."""
    analysis = json.loads(job.analysis_json or "{}")
    return upload_naming.release_values(
        job, upload_file_names.name_detected(session, job), analysis.get("mediainfo"),
        json.loads(job.overrides_json or "{}"), None)


def profile_maps(session: Session, tracker_id: int) -> tuple[dict[str, int], dict[int, tuple[str, ...]]]:
    """Dal profilo del tracker: le categorie (movie/tv -> id, per togliere i
    torrent dell'altro tipo con lo stesso id TMDB) e i tipi (type_id -> le
    chiavi dei profili, per il tipo dei torrent trovati)."""
    profile = session.get(TrackerUploadProfile, tracker_id)
    try:
        types = json.loads(profile.type_id_map_json or "{}") if profile is not None else {}
    except ValueError:
        types = {}
    return catalog_categories.category_map(session, tracker_id), upload_dupes.type_keys(types)


def same_content(job: UploadJob, found: list, categories: dict[str, int]) -> list:
    """I torrent trovati per l'id TMDB del job che sono del suo tipo: un
    film e una serie possono avere lo stesso id (nazgarr/torrents/catalog_categories.py)."""
    return [c for c in found
            if not catalog_categories.other_content_type(getattr(c, "category_id", None), job.content_type, categories)]


def seeding_here(target, client_matches: list[dict]) -> dict | None:
    """Gli stessi byte (hardlink) sono già in seed sul client di questo
    tracker, con un torrent di questo tracker: niente da fare, né upload né
    reseed."""
    hosts = {arr.host_of(target.tracker.announce_url), arr.host_of(target.tracker.base_url)} - {None}
    for match in client_matches:
        # Tutti i video, non uno: un episodio in seed col suo torrent non
        # mette in seed il pack che lo contiene.
        covers_all = match.get("videos_hardlinked", match["videos_matched"]) >= match["videos_total"]
        if (match["match"] == "hardlink" and covers_all and match["client_id"] == target.torrent_client_id
                and match["tracker_host"] in hosts):
            return match
    return None


def _still_mine(session: Session, job: UploadJob, run_id: int) -> bool:
    """Il job è ancora in analisi, e con questa analisi (audit 2026-10-18):
    annullato nel frattempo, i tracker che restano non si interrogano e
    nessun target si tocca più; ripreso, un'analisi partita dopo (il suo
    analysis_started) è quella che vale."""
    status = session.query(UploadJob.status).filter(UploadJob.id == job.id).scalar()
    newer = session.query(UploadEvent.id).filter(
        UploadEvent.job_id == job.id, UploadEvent.code == "analysis_started", UploadEvent.id > run_id).first()
    return status == "analyzing" and newer is None


def _drop_check(session: Session, job: UploadJob, target, previous: str | None) -> None:
    """Il dupe check di un'analisi che non è più di questo job (_still_mine)
    non lascia niente (audit 2026-10-18). Con il job annullato il target non
    resta 'checking': torna allo stato di prima. Se un'altra analisi è
    partita, i target sono suoi e non si toccano."""
    session.rollback()
    status = session.query(UploadJob.status).filter(UploadJob.id == job.id).scalar()
    if status == "analyzing" or previous is None:
        return
    session.refresh(target)
    if target.status == upload_jobs.TargetStatus.CHECKING:
        upload_jobs.set_target_status(target, previous)
        session.commit()


def _remember_rss_key(target, adapter) -> None:
    """La chiave dei link di download imparata dalle risposte del tracker
    resta sul tracker (cifrata): il dupe check salva i link senza, e il full
    hash check o il reseed li ricostruiscono con lei (verify.dupe_torrent,
    nazgarr/reseed/download_links.py)."""
    learned = getattr(adapter, "rss_key", None)
    if learned and learned != target.tracker.rss_key:
        target.tracker.rss_key = learned


def _check_trackers(
    session: Session, job: UploadJob, summary: SourceSummary, client_matches: list[dict], run_id: int
) -> tuple[dict[str, dict], dict[str, int]] | None:
    """Dupe check di ogni tracker; restituisce, per target, il torrent che fa
    già seed degli stessi byte sul suo client (vedi seeding_here) e quanti
    risultati si sono letti da un tracker che ne aveva di più (il catalogo
    tagliato: il dupe check può non essere completo). None se l'analisi non
    è più di questo job (_still_mine)."""
    seeding_by_target: dict[str, dict] = {}
    truncated_by_target: dict[str, int] = {}
    threshold = settings_registry.get_int(session, upload_dupes.THRESHOLD_SETTING)
    for target in job.targets:
        if not _still_mine(session, job, run_id):
            return None
        previous = target.status
        upload_jobs.set_target_status(target, upload_jobs.TargetStatus.CHECKING)
        session.commit()
        failure: Exception | None = None
        try:
            categories, keys = profile_maps(session, target.tracker_id)
            with adapter_factory.tracker(target.tracker) as adapter:
                found = adapter.search_by_tmdb(job.tmdb_id)
                _remember_rss_key(target, adapter)
                results, suggested = check(same_content(job, found, categories), summary, keys, threshold)
                for result in results:
                    # Mai la chiave personale nel DB (audit 2026-10-18).
                    result["download_link"] = download_links.stored_link(adapter, result["download_link"])
        except Exception as exc:
            failure = exc
        if not _still_mine(session, job, run_id):
            # Annullato (o ripreso da un'altra analisi) durante la ricerca:
            # il risultato non si scrive, e il target torna com'era.
            _drop_check(session, job, target, previous)
            return None
        if failure is not None:
            # L'utente decide lo stesso: senza dupe check, ma lo sa.
            logger.warning("Dupe check fallito su %s", target.tracker.label, exc_info=failure)
            target.dupes_json = None
            target.suggested_action = None
            target.error_message = "dupe_check_failed"
            upload_jobs.log_event(session, job, "dupe_check_failed", level="warning", target=target,
                                  error=str(failure))
        else:
            target.dupes_json = json.dumps(results)
            target.suggested_action = suggested
            target.error_message = None
            counts = {v: sum(1 for r in results if r["verdict"] == v) for v in ("identical", "same_slot")}
            upload_jobs.log_event(
                session, job, "dupe_check_done", target=target, results=len(results),
                identical=counts["identical"], same_slot=counts["same_slot"], suggested=suggested,
            )
            if catalog_truncated(found):
                # Il tracker ha più risultati di quelli letti (audit 2026-10-18):
                # un dupe oltre il limite non si vede, e la decisione lo dice.
                truncated_by_target[str(target.id)] = len(found)
                upload_jobs.log_event(session, job, "dupe_check_truncated", level="warning", target=target,
                                      results=len(found))
        seeding = seeding_here(target, client_matches)
        if seeding is not None:
            seeding_by_target[str(target.id)] = {"name": seeding["name"], "client": seeding["client"]}
            target.suggested_action = "skip"
            upload_jobs.log_event(session, job, "already_seeding_here", level="warning", target=target,
                                  torrent=seeding["name"], client=seeding["client"])
        upload_jobs.set_target_status(target, upload_jobs.TargetStatus.AWAITING_DECISION)
        session.commit()
    return seeding_by_target, truncated_by_target


def handle(session: Session, job: UploadJob, worker) -> None:
    from nazgarr.upload import identify as upload_identify  # import qui: identify importa questo modulo

    # Una divisione fermata a metà da un riavvio (nazgarr/upload/identify.py):
    # un upload creato da lei si toglie, il pack la rifà prima dell'analisi.
    if upload_identify.drop_split_orphan(session, job) or upload_identify.resume_split(session, job, worker):
        return
    run = upload_jobs.log_event(session, job, "analysis_started")
    session.commit()
    try:
        files = source_files(job)
    except OSError as exc:
        raise upload_jobs.UploadJobError("upload_source_unreadable", error=str(exc)) from exc
    layout = json.loads(job.layout_json or "{}")
    main_video = layout.get("main_video") or job.source_path

    job.stage = "mediainfo"
    session.commit()
    try:
        job.mediainfo_text = mediainfo_util.extract_full_text(main_video)
    except Exception:
        logger.warning("mediainfo fallito per %r", main_video, exc_info=True)
        upload_jobs.log_event(session, job, "mediainfo_failed", level="warning")
    # Riepilogo strutturato: anteprima MediaInfo e segnaposto del nome.
    mediainfo_summary = mediainfo_util.extract_summary(main_video)
    # Il Dolby Vision che il contenitore non dichiara, letto dal flusso
    # (nazgarr/upload/dovi_probe.py): solo se MediaInfo non l'ha visto.
    if dovi_probe.complete(mediainfo_summary, main_video):
        upload_jobs.log_event(session, job, "dolby_vision_from_stream", level="warning",
                              profile=dv_profile(mediainfo_summary["video"]) or "?")

    job.stage = "local"
    session.commit()
    index = arr_index_if_configured(session)
    client_matches = _client_matches(session, files)
    analysis = {
        "total_size_bytes": sum(s for _p, s in files),
        "file_count": len(files),
        "client_matches": client_matches,
        "arr_grabs": _arr_grabs(index, files),
        "mediainfo": mediainfo_summary,
        "name_source": name_source(job, files, client_matches, index),
        "other_jobs": other_jobs(session, job, files),
    }
    if upload_pack.is_pack(job):
        job.stage = "pack"
        session.commit()
        analysis["pack_mixed"] = upload_pack.mixed(job)
        if analysis["pack_mixed"]:
            upload_jobs.log_event(session, job, "pack_mixed", level="warning",
                                  fields=", ".join(sorted(analysis["pack_mixed"])))
    if not _still_mine(session, job, run.id):
        # Annullato durante mediainfo e indici: niente analisi scritta su un
        # job che non è più in analisi (audit 2026-10-18).
        session.rollback()
        logger.info("Upload %s: analisi interrotta prima dei tracker", job.id)
        return
    job.analysis_json = json.dumps(analysis)
    if analysis["client_matches"]:
        upload_jobs.log_event(session, job, "already_on_client", level="warning", count=len(analysis["client_matches"]))
    if analysis["arr_grabs"]:
        upload_jobs.log_event(
            session, job, "grabbed_from_tracker", level="warning",
            trackers=[g["tracker_host"] for g in analysis["arr_grabs"]],
        )
    if analysis["other_jobs"]:
        upload_jobs.log_event(session, job, "other_upload_same_files", level="warning",
                              jobs=", ".join(f"#{o['id']}" for o in analysis["other_jobs"]))
    session.commit()

    job.stage = "trackers"
    session.commit()
    summary = summary_of(job, files, analysis, source_values(session, job))
    checked = _check_trackers(session, job, summary, client_matches, run.id)
    if checked is None or not _still_mine(session, job, run.id):
        logger.info("Upload %s: analisi interrotta (annullato, o ripresa da un'altra analisi)", job.id)
        return
    analysis["seeding_here"], truncated = checked
    if truncated:
        analysis["dupes_truncated"] = truncated
    job.analysis_json = json.dumps(analysis)
    session.commit()
    upload_decision.propose(session, job)

    if upload_jobs.transition(session, job, "analyzing", "awaiting_decision", stage=None):
        upload_jobs.log_event(session, job, "analysis_done")
        session.commit()
        # Un torrent identico: il full hash check e il reseed da soli, se
        # acceso (nazgarr/upload/auto.py). L'avviso "upload pronto" arriva da
        # lì, quando la decisione tocca all'utente.
        upload_auto.settle(session, job, worker, after_analysis=True)

