"""Esecuzione di un job di upload approvato (docs/SPEC.md §9 "Upload flow
v2"), nel worker "heavy", un job alla volta nell'ordine della coda.

L'approvazione al secondo punto è la conferma umana obbligatoria: da qui si
va fino in fondo senza chiedere altro. Una volta per job:
- gli hash dei piece (torf), riusati per il .torrent di ogni tracker: stesso
  contenuto, announce e campo source diversi, quindi info hash diversi;
- screenshot e upload sull'image host, mediainfo (già letto nell'analisi).

Poi per ogni tracker, in ordine, la sua azione:
- upload: .torrent del tracker, descrizione dal suo template, invio, e il
  torrent aggiunto al client del tracker senza il suo recheck, dopo aver
  controllato che i file siano al loro posto (vedi _add_to_client);
- reseed: il .torrent già sul tracker, i file sistemati con i nomi che si
  aspetta, e l'aggiunta al client con recheck forzato.

Dove seedano: hardlink nella cartella per gli upload del disco
(disk.effective_upload_rel_path: upload_rel_path, o la prima cartella di seeding),
decisione dell'utente del 2026-09-30. Se la sorgente è già lì dentro con lo
stesso layout del torrent, si fa seed sul posto. Stesso filesystem o errore
esplicito, come nel reseeding. Un tracker che fallisce non ferma gli altri.
"""

import contextlib
import io
import json
import logging
import os
import re
import shutil
from datetime import UTC, datetime

import torf
from sqlalchemy.orm import Session

from nazgarr.adapters.image_host.base import ImageHostError
from nazgarr.adapters.tracker.base import UploadFields
from nazgarr.core import settings_registry
from nazgarr.core.file_types import is_video
from nazgarr.core.fs_scope import ScopeViolation, resolve_scoped
from nazgarr.core.models import TorrentClient, TrackerUploadProfile, UploadJob, UploadTarget
from nazgarr.core.version import CREATED_BY
from nazgarr.integrations import adapter_factory
from nazgarr.integrations.adapter_factory import ImageHostConfigError
from nazgarr.library import disk_folders, hardlinks
from nazgarr.library import mediainfo as mediainfo_util
from nazgarr.reseed.executor import client_visible_path
from nazgarr.torrents import client_labels
from nazgarr.torrents.metainfo import compute_info_hash, parse_torrent_info
from nazgarr.upload import file_names as upload_file_names
from nazgarr.upload import inventory as upload_inventory
from nazgarr.upload import jobs as upload_jobs
from nazgarr.upload import pack as upload_pack
from nazgarr.upload import screenshots
from nazgarr.upload import watch as upload_watch
from nazgarr.upload.description import render_description
from nazgarr.upload.jobs import UploadJobError
from nazgarr.upload.verify import build_locator

logger = logging.getLogger(__name__)

# File che non finiscono mai nel torrent di un upload.
PROGRESS_EVERY_SECONDS = 1.0
SD_RESOLUTIONS = {"480p", "480i", "576p", "576i"}


# --- dove seedare -------------------------------------------------------------


def seed_root(job: UploadJob) -> str:
    disk = job.disk
    if disk is None:
        raise UploadJobError("upload_disk_missing")
    rel = disk.effective_upload_rel_path
    if not rel:
        raise UploadJobError("upload_no_seed_folder", disk=disk.label)
    try:
        root = resolve_scoped(disk.root_path, rel)
    except ScopeViolation as exc:
        raise UploadJobError("path_outside_scope", path=exc.candidate) from exc
    if not os.path.isdir(root):
        raise UploadJobError("upload_seed_folder_missing", path=root)
    return root


def seeding_area(job: UploadJob) -> list[str]:
    """Le cartelle di seeding del disco (nazgarr/library/disk_folders.py)."""
    if job.disk is None:
        return []
    return disk_folders.absolute(job.disk, "seeding")


def _inside(path: str, folder: str | list[str] | None) -> bool:
    if folder is None:
        return False
    if isinstance(folder, list):
        return any(_inside(path, one) for one in folder)
    real, base = os.path.realpath(path), os.path.realpath(folder)
    return real.startswith(base + os.sep)


def link_files(pairs: list[tuple[str, str]], root: str) -> list[str]:
    """Crea gli hardlink (sorgente, destinazione); una destinazione che è già
    lo stesso file (un tentativo precedente) va bene, un file diverso no.
    Tutto si controlla prima di creare il primo (hardlinks.check_link): dentro
    la cartella di seed, sorgenti che sono file veri sullo stesso disco. Se
    un hardlink fallisce a metà, quelli appena creati si tolgono.
    Restituisce gli hardlink creati."""
    codes = {"source_not_a_file": "upload_source_not_a_file", "cross_device": "upload_cross_device",
             "target_exists": "upload_seed_path_exists"}
    planned: list[tuple[str, str]] = []
    for source, target in pairs:
        try:
            resolved = hardlinks.check_link(source, target, root)
        except hardlinks.LinkProblem as problem:
            raise UploadJobError(codes[problem.code], path=problem.path) from problem
        if resolved is not None:
            planned.append((source, resolved))
    return hardlinks.create_links(planned)


# --- una volta per job --------------------------------------------------------


def _job_dir(worker, job: UploadJob) -> str:
    path = upload_jobs.job_folder(worker.data_dir, job.id)
    os.makedirs(path, exist_ok=True)
    return path


def _progress(session: Session, job: UploadJob, stage: str, done: int | None = None, total: int | None = None):
    job.stage, job.progress_done, job.progress_total = stage, done, total
    session.commit()


def prepare_content(session: Session, job: UploadJob, ctx: dict) -> str:
    """Il percorso da cui creare il torrent. Con i nomi della sorgente
    (upload_file_names "original") è la sorgente stessa; con nomi nuovi
    (dal torrent in hardlink, o generati) si creano prima gli hardlink con
    quei nomi nella cartella di seed, e il torrent nasce da lì: quello
    pubblicato e quello in seed sono per forza gli stessi file.

    Un file solo tolto dalla sua cartella (plan.single_file) va negli
    hardlink anche con i nomi originali: nella cartella di seed, dentro la
    sua cartella se resta (plan.folder). Se la sorgente è già nella cartella
    di seeding, seeda sul posto, dentro la sua cartella. ctx["save_path"] è
    dove il client lo cercherà."""
    plan = upload_file_names.plan(session, job)
    upload_jobs.log_event(session, job, "file_names", mode=plan.mode, name=plan.content_name,
                          **({"single_file": True, "folder": plan.folder} if plan.single_file else {}))
    session.commit()
    # Un pack di file scelti a mano non ha una cartella sua: sempre hardlink.
    # Dalla cartella osservata la release se ne va anche con i nomi originali
    # (decisione dell'utente, 2026-10-05): hardlink con quei nomi nella
    # cartella di seed, prima di pubblicare, come per i nomi nuovi.
    if not plan.renamed and not plan.single_file and not upload_pack.is_pack(job) and job.origin != "watch":
        refresh_mediainfo(session, job, plan, None)
        return job.source_path
    if not plan.renamed and _seeds_in_place(job, ctx):
        source = plan.files[0][0]
        ctx["save_path"] = os.path.dirname(source)
        refresh_mediainfo(session, job, plan, None)
        return source
    root = ctx["seed_root"]()
    base = os.path.join(root, *plan.folder.split("/")) if plan.folder else root
    pairs = [(source, os.path.join(base, *target.split("/"))) for source, target in plan.files]
    ctx["created_links"] = link_files(pairs, root)
    ctx["save_path"] = base
    refresh_mediainfo(session, job, plan, base)
    return os.path.join(base, *plan.content_name.split("/"))


def _seeds_in_place(job: UploadJob, ctx: dict) -> bool:
    """La sorgente è già nella cartella di seeding (o in quella per gli
    upload). Mai per la cartella osservata: da lì la release se ne va."""
    if job.origin == "watch" or upload_pack.is_pack(job):
        return False
    if _inside(job.source_path, seeding_area(job)):
        return True
    try:
        return _inside(job.source_path, ctx["seed_root"]())
    except UploadJobError:
        return False


_COMPLETE_NAME = re.compile(r"^(Complete name\s*:\s*).*$", re.MULTILINE)


def refresh_mediainfo(session: Session, job: UploadJob, plan, root: str | None) -> None:
    """Il MediaInfo per il tracker, dal file del torrent: rinominare cambia
    solo "Complete name" (i flussi sono gli stessi byte), che ora è il
    percorso dentro il torrent, non quello locale (le tue cartelle non escono
    verso il tracker). Il testo dell'analisi con la sola riga corretta, senza
    rileggere il file; letto dal file solo se l'analisi non l'ha."""
    main_video = (json.loads(job.layout_json or "{}").get("main_video")) or job.source_path
    target = next((t for source, t in plan.files if source == main_video), None)
    if target is None:
        return
    text = job.mediainfo_text
    if not text and root is not None:
        text = mediainfo_util.extract_full_text(os.path.join(root, *target.split("/")))
    if not text:
        return
    job.mediainfo_text = _COMPLETE_NAME.sub(lambda m: m.group(1) + target, text, count=1)
    session.commit()


def hash_pieces(session: Session, job: UploadJob, path: str | None = None) -> torf.Torrent:
    root = path or job.source_path
    # Fuori i file che la regola di analisi e nomi esclude
    # (nazgarr/upload/inventory.py), uno per uno: mai dei glob sul percorso,
    # che toglievano un film con "Sample" nel titolo. Per nome esatto nel
    # torrent (cartella/percorso), così la struttura resta quella della
    # cartella anche se rimane un file solo.
    name = os.path.basename(root.rstrip(os.sep))
    excluded = [
        "^" + re.escape(f"{name}/{f.relative}") + "$"
        for f in (upload_inventory.walk(root) if os.path.isdir(root) else [])
        if not upload_inventory.in_torrent(f.relative, f.size)
    ]
    torrent = torf.Torrent(path=root, private=True, exclude_regexs=excluded, created_by=CREATED_BY)
    if not torrent.files:
        raise UploadJobError("no_video_files")

    def callback(_torrent, _path, done, total):
        _progress(session, job, "hashing", done, total)
        session.refresh(job)
        # Annullato a metà: torf si ferma se il callback restituisce qualcosa.
        return True if job.status == "cancelled" else None

    _progress(session, job, "hashing", 0, None)
    torrent.generate(callback=callback, interval=PROGRESS_EVERY_SECONDS)
    session.refresh(job)
    if job.status == "cancelled":
        raise _Cancelled()
    return torrent


def take_screenshots(session: Session, job: UploadJob, worker, count: int) -> list[str]:
    if count <= 0:
        return []
    layout = json.loads(job.layout_json or "{}")
    main_video = layout.get("main_video") or job.source_path
    tonemap = settings_registry.get_bool(session, "upload_tonemap_hdr")
    _progress(session, job, "screenshots", 0, count)
    try:
        chain = adapter_factory.build_image_host_chain(session)
    except ImageHostConfigError as exc:
        raise UploadJobError(exc.code, **exc.params) from exc
    try:
        paths = screenshots.generate_screenshots(
            main_video, os.path.join(_job_dir(worker, job), "screenshots"), count=count, tonemap=tonemap
        )
    except screenshots.ScreenshotError as exc:
        raise UploadJobError("upload_screenshots_failed", error=str(exc)) from exc
    urls = []
    for i, path in enumerate(paths, start=1):
        try:
            urls.append(chain.upload(path))
        except ImageHostError:
            logger.warning("Upload dello screenshot %r fallito", path, exc_info=True)
        _progress(session, job, "screenshots", i, count)
    if not urls:
        raise UploadJobError("upload_screenshots_failed", error="image host")
    return urls


# --- per tracker --------------------------------------------------------------


def _imdb_number(imdb_id: str | None) -> str:
    match = re.search(r"(\d+)", imdb_id or "")
    return match.group(1) if match else "0"


def upload_fields(job: UploadJob, target: UploadTarget, description: str, resolution_key: str | None) -> UploadFields:
    flags = json.loads(target.flags_json or "{}")
    seasons = json.loads(job.seasons_json or "[]")
    tv = job.content_type == "tv"
    return UploadFields(
        name=target.approved_name,
        description=description,
        mediainfo=job.mediainfo_text or "",
        category_id=target.category_id,
        type_id=target.type_id,
        resolution_id=target.resolution_id,
        tmdb_id=job.tmdb_id,
        imdb_id=_imdb_number(job.imdb_id),
        tvdb_id=job.tvdb_id or 0,
        mal_id=job.mal_id or 0,
        season_number=(min(seasons) if seasons else 0) if tv else None,
        episode_number=(job.episode if job.kind == "episode" else 0) if tv else None,
        anonymous=bool(flags.get("anonymous")),
        personal_release=bool(flags.get("personal_release")),
        internal=bool(flags.get("internal")),
        stream=bool(flags.get("stream")),
        free=int(flags.get("freeleech") or 0),
        sd=resolution_key in SD_RESOLUTIONS,
    )


def _client_row(session: Session, target: UploadTarget) -> TorrentClient | None:
    if target.torrent_client_id is None:
        return None
    row = session.get(TorrentClient, target.torrent_client_id)
    return row if row is not None and row.enabled else None


def _client(session: Session, target: UploadTarget):
    """(adapter, id del client) da chiudere dopo l'uso, o (None, None)."""
    row = _client_row(session, target)
    if row is None:
        return None, None
    return adapter_factory.build_torrent_client_adapter(row), row.id


def files_in_place(torrent: torf.Torrent, save_path: str) -> bool:
    """Ogni file del torrent è dove il client lo cercherà (save_path più il
    suo percorso nel torrent), con la sua dimensione esatta."""
    for file in torrent.files:
        path = os.path.join(save_path, *file.parts)
        if not os.path.isfile(path) or os.path.getsize(path) != file.size:
            return False
    return True


def _add_to_client(
    session: Session, job: UploadJob, target: UploadTarget, torrent_file: str, save_path: str,
    torrent: torf.Torrent | None = None,
):
    """Senza il recheck del client (decisione dell'utente, 2026-09-30): il
    torrent l'ha appena creato Nazgarr leggendo ogni piece di questi file (o
    dei loro hardlink, gli stessi byte), rileggerli tutti non verifica niente
    di nuovo. Quello che il recheck verificherebbe davvero è che il client li
    trovi: prima si controlla che ogni file sia nel percorso di seed con la
    sua dimensione, dopo che il client usi proprio quel percorso. Se una delle
    due non torna, il recheck normale, così un percorso sbagliato (mount
    diversi fra i container) risulta in file mancanti invece di un torrent
    annunciato completo senza i dati. Un reseed (torrent=None, il .torrent è
    del tracker) ha sempre il recheck."""
    adapter, client_id = _client(session, target)
    if adapter is None:
        target.error_message = "no_client"
        upload_jobs.log_event(session, job, "no_client", level="warning", target=target)
        return None
    try:
        visible = client_visible_path(session, job.disk, client_id, save_path)
        # Transmission e rTorrent non sanno aggiungere un torrent senza
        # ricontrollarlo: lì il recheck c'è sempre.
        can_skip = getattr(adapter, "can_skip_recheck", True)
        skip = torrent is not None and can_skip and files_in_place(torrent, save_path)
        info_hash = adapter.add_torrent(
            torrent_file, save_path=visible, force_recheck=True, skip_check_verified=skip,
            **client_labels.add_kwargs(target.client_category, target.client_tags),
        )
        client = target.torrent_client.label
        if skip:
            info = adapter.get_torrent_info(info_hash)
            seen = os.path.normpath(info.save_path) if info is not None and info.save_path else None
            if seen != os.path.normpath(visible):
                adapter.recheck(info_hash)
                upload_jobs.log_event(session, job, "recheck_after_path_mismatch", level="warning", target=target,
                                      client=client, expected=visible, seen=seen)
                skip = False
        elif torrent is not None and can_skip:
            upload_jobs.log_event(session, job, "recheck_files_not_in_place", level="warning", target=target,
                                  path=save_path)
        # Due codici, due messaggi: con il recheck del client o senza.
        upload_jobs.log_event(session, job, "added_to_client_verified" if skip else "added_to_client", target=target,
                              client=client)
        return info_hash
    finally:
        adapter_factory.close_adapter(adapter)


# Gli errori di un upload pubblicato ma non in seed, da cui si può riprovare.
SEED_RETRYABLE = ("seed_failed", "no_client")


def _seed_candidates(session: Session, job: UploadJob) -> list[str]:
    """Dove possono stare i file di un upload pubblicato: la cartella di
    seed (con la cartella dei nomi nuovi, se c'è) o accanto alla sorgente."""
    candidates = []
    with contextlib.suppress(UploadJobError):
        root = seed_root(job)
        with contextlib.suppress(Exception):
            folder = upload_file_names.plan(session, job).folder
            if folder:
                candidates.append(os.path.join(root, *folder.split("/")))
        candidates.append(root)
    candidates.append(os.path.dirname(job.source_path.rstrip(os.sep)))
    return candidates


def retry_seed(session: Session, job: UploadJob, target: UploadTarget) -> None:
    """Di nuovo solo l'aggiunta al client di un upload già pubblicato (il
    .torrent salvato dal tracker), mai l'upload. Con il recheck del client:
    i file potrebbero essere cambiati da quando Nazgarr li ha letti."""
    if target.action != "upload" or target.status != "done" or target.error_message not in SEED_RETRYABLE:
        raise UploadJobError("upload_seed_retry_unavailable")
    if not target.torrent_path or not os.path.isfile(target.torrent_path):
        raise UploadJobError("upload_tracker_torrent_unavailable")
    if _client_row(session, target) is None:
        # Senza client all'upload (no_client) o con quello di allora tolto:
        # quello che il tracker userebbe oggi.
        target.torrent_client_id = upload_jobs.default_client_id(session, target.tracker)
    torrent = torf.Torrent.read(target.torrent_path)
    save_path = next((p for p in _seed_candidates(session, job) if files_in_place(torrent, p)), None)
    if save_path is None:
        raise UploadJobError("upload_seed_files_missing")
    try:
        info_hash = _add_to_client(session, job, target, target.torrent_path, save_path)
    except Exception as exc:
        session.rollback()
        upload_jobs.log_event(session, job, "seed_failed", level="error", target=target,
                              error=getattr(exc, "code", None) or str(exc))
        session.commit()
        raise UploadJobError("upload_seed_retry_failed", error=getattr(exc, "code", None) or str(exc)) from exc
    if info_hash is None:  # nessun client: _add_to_client l'ha già scritto
        session.commit()
        raise UploadJobError("upload_seed_retry_failed", error="no_client")
    target.info_hash = info_hash
    target.error_message = None
    upload_jobs.log_event(session, job, "seed_retried", target=target, client=target.torrent_client.label)
    session.commit()


def _upload_pairs(job: UploadJob, torrent: torf.Torrent, root: str) -> list[tuple[str, str]]:
    """(file locale, destinazione) per ogni file del torrent. Vuoto se la
    sorgente è già nella cartella di seeding (o in quella per gli upload):
    il torrent ha il suo stesso nome, quindi seed sul posto."""
    if _inside(job.source_path, seeding_area(job)) or _inside(job.source_path, root):
        return []
    pairs = []
    for file in torrent.files:
        relative = os.path.join(*file.parts[1:]) if job.is_dir else ""
        source = os.path.join(job.source_path, relative) if relative else job.source_path
        pairs.append((source, os.path.join(root, *file.parts)))
    return pairs


def _tracker_copy(adapter, uploaded, torrent: torf.Torrent, job_dir: str, tracker_id: int) -> tuple[str, bool]:
    """Il .torrent come l'ha salvato il tracker (UploadedTorrent: il suo info
    hash è quello che il tracker conosce), e se ha gli stessi piece e gli
    stessi file di quello creato da Nazgarr."""
    if not uploaded.download_link:
        raise UploadJobError("upload_tracker_torrent_unavailable")
    try:
        content = adapter.download_torrent(uploaded.download_link)
        theirs = torf.Torrent.read_stream(io.BytesIO(content))
    except Exception as exc:
        raise UploadJobError("upload_tracker_torrent_unavailable", error=str(exc)) from exc
    path = os.path.join(job_dir, f"tracker-{tracker_id}-seed.torrent")
    with open(path, "wb") as f:
        f.write(content)
    mine_info, their_info = torrent.metainfo["info"], theirs.metainfo["info"]
    keys = ("pieces", "piece length", "name", "files", "length")
    same = all(mine_info.get(key) == their_info.get(key) for key in keys)
    return path, same


def run_upload(session: Session, job: UploadJob, target: UploadTarget, ctx: dict) -> None:
    tracker = target.tracker
    if not tracker.announce_url:
        raise UploadJobError("tracker_missing_announce_url", tracker=tracker.label)
    profile = session.get(TrackerUploadProfile, tracker.id)
    if profile is None:
        raise UploadJobError("tracker_no_upload_profile", tracker=tracker.label)

    upload_jobs.set_target_status(target, upload_jobs.TargetStatus.PREPARING)
    session.commit()
    torrent: torf.Torrent = ctx["torrent"]
    torrent.trackers = [tracker.announce_url]
    torrent.source = tracker.label
    torrent_path = os.path.join(ctx["dir"], f"tracker-{tracker.id}.torrent")
    torrent.write(torrent_path, overwrite=True)
    target.torrent_path, target.info_hash = torrent_path, torrent.infohash
    overrides = ctx["overrides"]
    description = render_description(
        session, profile, job.mediainfo_text or "", ctx["screenshots"], notes=overrides.get("notes", "")
    )
    target.description_rendered = description
    resolutions = json.loads(profile.resolution_id_map_json or "{}")
    resolution_key = next((k for k, v in resolutions.items() if v == target.resolution_id), None)

    upload_jobs.set_target_status(target, upload_jobs.TargetStatus.UPLOADING)
    session.commit()
    with adapter_factory.tracker(tracker) as adapter:
        fields = upload_fields(job, target, description, resolution_key)
        uploaded = adapter.upload_torrent(fields, torrent_path)
        target.torrent_id_remote = uploaded.torrent_id_remote
        upload_jobs.log_event(session, job, "uploaded", target=target, torrent=target.torrent_id_remote)
        session.commit()

        if overrides.get("no_seed"):
            upload_jobs.log_event(session, job, "not_seeded", target=target)
            return
        upload_jobs.set_target_status(target, upload_jobs.TargetStatus.SEEDING)
        session.commit()
        try:
            root = ctx["seed_root"]()
            if ctx.get("save_path"):  # già in seed con i nomi del torrent (prepare_content)
                save_path = ctx["save_path"]
            else:
                pairs = _upload_pairs(job, torrent, root)
                link_files(pairs, root)
                save_path = root if pairs else os.path.dirname(job.source_path.rstrip(os.sep))
            _remember_seeded(ctx, [os.path.join(save_path, *file.parts) for file in torrent.files])
            seed_path, same_content = _tracker_copy(adapter, uploaded, torrent, ctx["dir"], tracker.id)
            target.torrent_path = seed_path
            target.info_hash = torf.Torrent.read(seed_path).infohash
            session.commit()
            # Gli stessi piece di quelli appena calcolati: niente recheck (vedi
            # _add_to_client); se il tracker ha cambiato il contenuto, recheck.
            _add_to_client(session, job, target, seed_path, save_path, torrent if same_content else None)
        except Exception as exc:
            # L'upload è andato: mai ripeterlo per un problema del client.
            logger.warning("Upload %s su %s riuscito ma il seed no", job.id, tracker.label, exc_info=True)
            target.error_message = "seed_failed"
            upload_jobs.log_event(
                session, job, "seed_failed", level="error", target=target, error=getattr(exc, "code", None) or str(exc)
            )


def run_reseed(session: Session, job: UploadJob, target: UploadTarget, ctx: dict) -> None:
    dupes = json.loads(target.dupes_json or "[]")
    dupe = next((d for d in dupes if d["torrent_id_remote"] == target.reseed_torrent_id), None)
    if dupe is None or not dupe.get("download_link"):
        raise UploadJobError("upload_dupe_no_download_link")
    upload_jobs.set_target_status(target, upload_jobs.TargetStatus.PREPARING)
    session.commit()
    with adapter_factory.tracker(target.tracker) as adapter:
        content = adapter.download_torrent(dupe["download_link"])
    # Il controllo completo vale per il .torrent che ha letto: quello che si
    # aggiunge al client dev'essere lo stesso, non uno riscaricato e cambiato
    # nel frattempo sul tracker.
    verified = (dupe.get("verification") or {})
    if verified.get("status") != "passed" or verified.get("info_hash") != compute_info_hash(content):
        raise UploadJobError("upload_reseed_torrent_changed", tracker=target.tracker.label)
    torrent_path = os.path.join(ctx["dir"], f"reseed-{target.tracker_id}.torrent")
    with open(torrent_path, "wb") as f:
        f.write(content)
    parsed = parse_torrent_info(content)
    target.torrent_path = torrent_path

    upload_jobs.set_target_status(target, upload_jobs.TargetStatus.SEEDING)
    session.commit()
    locate = build_locator(job, parsed)
    located = []
    for entry in parsed.files:
        local, _where = locate(entry)
        if local is None:
            if is_video(entry.path):
                raise UploadJobError("upload_reseed_missing_video", path=entry.path)
            continue  # un extra mancante lo scarica il client dopo il recheck
        located.append((local, entry.path))

    def destinations(base: str) -> list[tuple[str, str]]:
        name = os.path.join(base, parsed.name)
        return [(local, os.path.join(name, path) if parsed.is_multi_file else name) for local, path in located]

    # Sul posto se la sorgente, dentro la cartella di seeding, ha già il nome
    # e il layout del torrent del tracker; altrimenti hardlink.
    parent = os.path.dirname(job.source_path.rstrip(os.sep))
    # Mai dalla cartella osservata: da lì la release se ne va.
    in_place = job.origin != "watch" and not upload_pack.is_pack(job) and _inside(
        job.source_path, seeding_area(job)) and all(
        os.path.exists(dst) and os.path.samefile(src, dst) for src, dst in destinations(parent)
    )
    if in_place:
        save_path = parent
    else:
        save_path = ctx["seed_root"]()
        link_files(destinations(save_path), save_path)
    _remember_seeded(ctx, [dst for _src, dst in destinations(save_path)])
    target.info_hash = _add_to_client(session, job, target, torrent_path, save_path)
    upload_jobs.log_event(session, job, "reseeded", target=target, torrent=target.reseed_torrent_id)


def _remember_seeded(ctx: dict, paths: list[str]) -> None:
    """I file che questo job ha messo in seed (dispositivo, inode, percorso):
    dalla cartella osservata si toglie solo un file la cui copia è fra questi
    (_clear_watch_source)."""
    seeded = ctx.setdefault("seeded", {})
    for path in paths:
        with contextlib.suppress(OSError):
            st = os.stat(path)
            seeded[(st.st_dev, st.st_ino)] = os.path.realpath(path)


class _Cancelled(Exception):
    pass


def _cleanup_unused_links(session: Session, job: UploadJob, ctx: dict, overrides: dict) -> None:
    """Gli hardlink creati per dare al torrent i suoi nomi (prepare_content)
    si tolgono se non seedano niente: "non mettere in seed", o nessun
    upload riuscito. Solo quelli creati da questo job, e le cartelle che
    restano vuote."""
    created = ctx.get("created_links") or []
    uploaded = any(t.action == "upload" and t.status == "done" for t in job.targets)
    if not created or (uploaded and not overrides.get("no_seed")):
        return
    root = os.path.realpath(ctx["seed_root"]())
    for path in created:
        with contextlib.suppress(OSError):
            os.unlink(path)
        parent = os.path.dirname(path)
        while os.path.realpath(parent).startswith(root + os.sep):
            try:
                os.rmdir(parent)  # solo se vuota
            except OSError:
                break
            parent = os.path.dirname(parent)
    upload_jobs.log_event(session, job, "file_links_removed", count=len(created))
    session.commit()


def _source_files(path: str) -> list[str]:
    if not os.path.isdir(path):
        return [path]
    return [os.path.join(folder, name) for folder, _dirs, files in os.walk(path, followlinks=False) for name in files]


# File che i sistemi operativi lasciano nelle cartelle: mai di un utente.
_SYSTEM_JUNK = ("Thumbs.db", "desktop.ini", ".DS_Store")


def _is_system_junk(name: str) -> bool:
    return name in _SYSTEM_JUNK or name.startswith("._")


def _clear_watch_source(session: Session, job: UploadJob, overrides: dict, ctx: dict | None = None) -> None:
    """La cartella osservata è solo di passaggio (decisione dell'utente,
    2026-10-02): a upload fatto la release resta solo nella cartella delle
    release, dove seeda con i nomi del torrent (gli hardlink di questo job),
    e dalla cartella osservata si toglie. Un "move" fatto con un hardlink e
    poi la rimozione, mai prima: se qualcosa va storto la release resta lì.

    Si tolgono solo i file la cui copia in seed l'ha messa questo job (stesso
    inode, altro percorso: _remember_seeded), e i file di sistema
    (Thumbs.db, .DS_Store...). Prima bastava un secondo link qualunque: un
    file già collegato altrove (es. dalla cartella dei download) si
    cancellava anche se a seedare era proprio lui. Quello che il torrent non
    ha preso (es. un sample) resta, elencato nel registro del job, e le
    cartelle vuote spariscono. Solo se almeno un tracker è andato, se si
    mette in seed e se la sorgente è davvero dentro la cartella osservata."""
    if job.origin != "watch" or overrides.get("no_seed") or job.disk is None:
        return
    if not any(t.status == "done" and t.action != "skip" for t in job.targets):
        return
    watch = upload_watch.watch_root(job.disk)
    source = os.path.realpath(job.source_path)
    if watch is None or not source.startswith(os.path.realpath(watch) + os.sep):
        return
    seeded = (ctx or {}).get("seeded") or {}
    moved, kept = [], []
    for path in _source_files(source):
        try:
            st = os.stat(path, follow_symlinks=False)
        except OSError:
            continue
        copy = seeded.get((st.st_dev, st.st_ino))
        linked = not os.path.islink(path) and copy is not None and copy != os.path.realpath(path)
        if linked or _is_system_junk(os.path.basename(path)):
            with contextlib.suppress(OSError):
                os.unlink(path)
            if linked:
                moved.append(path)
        else:
            kept.append(os.path.relpath(path, os.path.dirname(source)))
    if os.path.isdir(source):
        for folder, _dirs, _files in sorted(os.walk(source), key=lambda entry: -len(entry[0])):
            with contextlib.suppress(OSError):
                os.rmdir(folder)  # solo se vuota
    if moved:
        upload_jobs.log_event(session, job, "watch_source_moved", count=len(moved))
    if kept:
        upload_jobs.log_event(session, job, "watch_source_leftovers", level="warning", count=len(kept),
                              files=kept[:10])
    session.commit()
    if job.split_from_id is not None:
        _remove_split_folder(session, job, watch)


def _remove_split_folder(session: Session, job: UploadJob, watch: str) -> None:
    """Un episodio di una stagione divisa (nazgarr/upload/split.py): quando
    nella sua cartella della cartella osservata non resta più nessun episodio
    da caricare, la cartella si toglie con quello che resta (nfo, sample...),
    decisione dell'utente del 2026-10-09. Un video ancora lì (l'upload di un
    altro episodio non è finito, o è stato annullato) o un file ancora in
    scrittura la lasciano dov'è."""
    root = os.path.realpath(watch)
    relative = os.path.relpath(os.path.realpath(job.source_path), root)
    parts = relative.split(os.sep)
    if len(parts) < 2 or parts[0] in ("", ".", ".."):
        return  # un file direttamente nella cartella osservata: nessuna cartella sua
    folder = os.path.join(root, parts[0])
    if os.path.islink(folder) or not os.path.isdir(folder):
        return
    left = []
    for directory, _dirs, files in os.walk(folder, followlinks=False):
        for name in files:
            path = os.path.join(directory, name)
            inside = os.path.relpath(path, folder)
            try:
                size = os.path.getsize(path)
            except OSError:
                continue
            if upload_watch.is_partial(name) or (is_video(name) and upload_inventory.in_torrent(inside, size)):
                return
            left.append(inside)
    shutil.rmtree(folder)
    upload_jobs.log_event(session, job, "watch_folder_removed", folder=parts[0], count=len(left), files=left[:10])
    session.commit()


def handle(session: Session, job: UploadJob, worker) -> None:
    if not upload_jobs.transition(session, job, "queued", "running", queue_position=None):
        return
    upload_jobs.log_event(session, job, "execution_started")
    session.commit()
    targets = [t for t in job.targets if t.status == "approved"]
    overrides = json.loads(job.overrides_json or "{}")
    ctx: dict = {"dir": _job_dir(worker, job), "overrides": overrides, "screenshots": []}

    root_cache: list[str] = []

    def lazy_seed_root() -> str:
        if not root_cache:
            root_cache.append(seed_root(job))
        return root_cache[0]

    ctx["seed_root"] = lazy_seed_root

    try:
        if any(t.action == "upload" for t in targets):
            ctx["torrent"] = hash_pieces(session, job, prepare_content(session, job, ctx))
            # Per la barra degli step (ProgressStep): i passaggi conclusi restano segnati.
            upload_jobs.log_event(session, job, "torrent_created")
            session.commit()
            count = overrides.get("screenshot_count")
            if count is None:
                count = settings_registry.get_int(session, "upload_screenshot_count")
            ctx["screenshots"] = take_screenshots(session, job, worker, count)
            job.screenshot_urls_json = json.dumps(ctx["screenshots"])
            upload_jobs.log_event(session, job, "screenshots_done", count=len(ctx["screenshots"]))
            session.commit()
    except _Cancelled:
        return
    except UploadJobError as exc:
        # Senza torrent o screenshot nessun upload può partire; i reseed sì.
        for target in targets:
            if target.action == "upload":
                target.status, target.error_message = "failed", exc.code
                upload_jobs.log_event(session, job, exc.code, level="error", target=target, **exc.params)
        session.commit()

    for target in targets:
        session.refresh(job)
        if job.status == "cancelled":
            return
        if target.status != "approved":
            continue
        _progress(session, job, f"tracker:{target.tracker.label}")
        try:
            if target.action == "upload":
                run_upload(session, job, target, ctx)
            else:
                run_reseed(session, job, target, ctx)
            upload_jobs.set_target_status(target, upload_jobs.TargetStatus.DONE)
        except Exception as exc:
            session.rollback()
            logger.warning("Upload %s: %s fallito su %s", job.id, target.action, target.tracker.label, exc_info=True)
            upload_jobs.set_target_status(target, upload_jobs.TargetStatus.FAILED)
            target.error_message = getattr(exc, "code", None) or str(exc)
            params = getattr(exc, "params", None) or {"error": str(exc)}
            upload_jobs.log_event(session, job, f"{target.action}_failed", level="error", target=target,
                                  error=target.error_message, **{k: v for k, v in params.items() if k != "error"})
        target.finished_at = datetime.now(UTC)
        session.commit()

    _cleanup_unused_links(session, job, ctx, overrides)
    _clear_watch_source(session, job, overrides, ctx)
    outcomes = [t.status for t in job.targets if t.action != "skip"]
    if all(s == "done" for s in outcomes):
        final = "done"
    elif all(s == "failed" for s in outcomes):
        final = "failed"
    else:
        final = "partial"
    if upload_jobs.transition(session, job, "running", final, stage=None, progress_done=None, progress_total=None):
        upload_jobs.log_event(session, job, f"job_{final}", level="info" if final == "done" else "warning")
        session.commit()
