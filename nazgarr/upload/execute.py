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
  aspetta, e l'aggiunta al client con recheck forzato, fermo: parte solo a
  recheck riuscito (decisione dell'utente, 2026-10-19; reconcile_held_seeds),
  e con i soli extra mancanti solo se ogni altro piece l'ha verificato
  Nazgarr (2026-10-21, nazgarr/upload/extras_check.py).

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
import stat
import time
from datetime import UTC, datetime

import httpx
import torf
from sqlalchemy import or_
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from nazgarr.adapters.image_host.base import ImageHostError
from nazgarr.adapters.torrent_client.base import TorrentAlreadyInClientError, is_stopped_state, stopped_kwargs
from nazgarr.adapters.tracker.base import UploadFields, catalog_truncated
from nazgarr.core import settings_registry
from nazgarr.core.db_utils import as_utc
from nazgarr.core.errors import CodedError
from nazgarr.core.file_types import is_video
from nazgarr.core.fs_scope import ScopeViolation, resolve_scoped
from nazgarr.core.logs import safe_error
from nazgarr.core.models import (
    CandidateFile,
    ClientTorrentFile,
    MediaFile,
    SeedFile,
    SeedJob,
    TorrentClient,
    TrackerUploadProfile,
    UploadJob,
    UploadTarget,
)
from nazgarr.core.version import CREATED_BY
from nazgarr.integrations import adapter_factory
from nazgarr.integrations.adapter_factory import ImageHostConfigError
from nazgarr.library import disk_folders, hardlinks, seed_refresh
from nazgarr.library import mediainfo as mediainfo_util
from nazgarr.reseed import resurrection
from nazgarr.reseed.executor import (
    MISSING_TORRENT_GRACE,
    can_look_up_torrent,
    client_visible_path,
    recheck_outcome,
    start_torrent,
    stop_torrent,
    torrent_missing,
)
from nazgarr.torrents import client_labels, partial_files
from nazgarr.torrents.layout import expected_missing_bytes
from nazgarr.torrents.metainfo import TorrentMetainfoError, compute_info_hash, parse_torrent_info
from nazgarr.upload import extras_check as upload_extras
from nazgarr.upload import file_names as upload_file_names
from nazgarr.upload import inventory as upload_inventory
from nazgarr.upload import jobs as upload_jobs
from nazgarr.upload import pack as upload_pack
from nazgarr.upload import screenshots
from nazgarr.upload import watch as upload_watch
from nazgarr.upload.decision import MAX_SCREENSHOTS
from nazgarr.upload.description import render_description
from nazgarr.upload.jobs import UploadJobError
from nazgarr.upload.verify import build_locator, dupe_torrent

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


def _link_identities(paths: list[str]) -> dict[str, tuple[int, int]]:
    """(dispositivo, inode) di ogni hardlink appena creato, visto con lstat:
    la pulizia toglie solo un file che è ancora quello (_cleanup_unused_links)."""
    out = {}
    for path in paths:
        with contextlib.suppress(OSError):
            st = os.lstat(path)
            if stat.S_ISREG(st.st_mode):
                out[path] = (st.st_dev, st.st_ino)
    return out


def _still_our_link(path: str, recorded: tuple[int, int] | None) -> bool | None:
    """L'hardlink creato da questo job è ancora al suo posto: un file vero
    (non un link simbolico), con il dispositivo e l'inode di quando è nato.
    None se non c'è più (niente da togliere), False se al suo posto c'è
    altro: un file con lo stesso nome messo lì da qualcun altro non si
    tocca (audit 2026-10-18)."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError:
        return False
    return recorded is not None and stat.S_ISREG(st.st_mode) and (st.st_dev, st.st_ino) == recorded


# Gli hardlink creati da un job per i nomi del torrent (prepare_content),
# anche in un file del job (audit 2026-10-18, finding 115): un'esecuzione
# fermata da un riavvio, o una pulizia non riuscita, li lasciava senza che
# niente li sapesse suoi. Una ripresa li riusa (check_link: lo stesso file
# va bene) senza crearli, e se poi falliva non li toglieva; eliminare il job
# non li toglieva mai. Percorso, dispositivo e inode di quando sono nati.
_LINKS_JOURNAL = "links.json"


def _journaled_links(job_dir: str | None) -> tuple[dict[str, tuple[int, int]], datetime | None]:
    """Gli hardlink scritti nel file del job, e quando è nato il primo; vuoto
    se manca o non si legge (al massimo un link resta, mai uno tolto senza
    sapere che è suo)."""
    if not job_dir:
        return {}, None
    try:
        with open(os.path.join(job_dir, _LINKS_JOURNAL)) as f:
            data = json.load(f)
        links = {str(path): (int(dev), int(ino)) for path, dev, ino in data.get("links") or []}
        at = datetime.fromisoformat(data["at"]) if data.get("at") else None
    except (OSError, ValueError, TypeError, AttributeError, KeyError):
        return {}, None
    return links, as_utc(at)


def _write_links_journal(job_dir: str, links: dict[str, tuple[int, int]], at: datetime | None) -> None:
    path = os.path.join(job_dir, _LINKS_JOURNAL)
    with contextlib.suppress(OSError):
        if not links:
            os.unlink(path)
            return
        with open(path, "w") as f:
            json.dump({"at": at.isoformat() if at else None,
                       "links": [[link, dev, ino] for link, (dev, ino) in links.items()]}, f)


def _journal_links(job_dir: str, created: dict[str, tuple[int, int]], at: datetime) -> None:
    """Aggiunge gli hardlink appena creati a quelli già nel file del job."""
    if not created:
        return
    known, since = _journaled_links(job_dir)
    _write_links_journal(job_dir, {**known, **created}, min(since, at) if since else at)


# I file nella cartella di seed da cui nasce il torrent di un'esecuzione
# (prepare_content), creati o riusati: un altro job può riusare un link
# lasciato da questo (check_link: lo stesso file va bene) senza crearlo, e
# dall'hash fino a quando un suo tracker lo mette in seed (_remember_seeded)
# niente altro dice che è suo (_used_by_other_uploads).
_CONTENT_JOURNAL = "content.json"


def _journal_content(job_dir: str, paths: list[str]) -> None:
    with contextlib.suppress(OSError):
        with open(os.path.join(job_dir, _CONTENT_JOURNAL), "w") as f:
            json.dump({"files": [os.path.realpath(path) for path in paths]}, f)


def _journaled_content(job_dir: str) -> set[str]:
    """Vuoto se manca o non si legge."""
    try:
        with open(os.path.join(job_dir, _CONTENT_JOURNAL)) as f:
            return {str(path) for path in json.load(f).get("files") or []}
    except (OSError, ValueError, TypeError, AttributeError):
        return set()


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
    # Da qui un reseed del motore approvato nel frattempo può riusarli (_used_elsewhere).
    ctx["links_at"] = datetime.now(UTC)
    ctx["created_links"] = link_files(pairs, root)
    ctx["created_ids"] = _link_identities(ctx["created_links"])
    if ctx.get("dir"):
        _journal_links(ctx["dir"], ctx["created_ids"], ctx["links_at"])
        _journal_content(ctx["dir"], [target for _source, target in pairs])
    ctx["save_path"] = base
    # Nel torrent solo questi: la cartella può avere già altro (hash_pieces).
    ctx["planned"] = [target for _source, target in pairs]
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
    target = upload_file_names.main_video_target(job, plan)
    if target is None:
        return
    text = job.mediainfo_text
    if not text and root is not None:
        text = mediainfo_util.extract_full_text(os.path.join(root, *target.split("/")))
    if not text:
        return
    job.mediainfo_text = _COMPLETE_NAME.sub(lambda m: m.group(1) + target, text, count=1)
    session.commit()


# Thread di hashing di torf: di default uno per core dell'host (in Docker
# tutti, anche con --cpus), e ognuno tiene in memoria il suo piece da 16 MiB.
# Quattro leggono già più veloce di qualunque disco (audit 2026-10-18).
HASH_THREADS = 4


def _exclude_exact(relative: str) -> str:
    """Il regex che toglie dal torrent proprio quel file ("Nome/percorso").
    torf lo confronta con il percorso preceduto dalla cartella sopra quella
    in comune fra tutti i file: con un file solo, o tutti nella stessa
    sottocartella, "Nome/Nome/percorso" invece di "Nome/percorso", e un
    "^Nome/percorso$" non toglieva niente (audit 2026-10-18). Per questo
    l'inizio vale anche dopo una "/"; il controllo dei file del torrent in
    hash_pieces ferma comunque un torrent diverso dal previsto."""
    return "(?:^|/)" + re.escape(relative) + "$"


def _torrent_files(torrent: torf.Torrent) -> set[str]:
    return {"/".join(f.parts) for f in torrent.files}


def _expected_files(root: str, planned: list[str] | None) -> set[str]:
    """I file che il torrent deve avere, come "Nome/percorso": gli hardlink
    appena creati (planned), o quelli della sorgente che la regola di
    analisi e nomi fa entrare (nazgarr/upload/inventory.py). Mai un file
    vuoto: torf non li mette, e non hanno dati da condividere."""
    parent = os.path.dirname(root.rstrip(os.sep))
    if planned is not None:
        out = set()
        for path in planned:
            with contextlib.suppress(OSError):
                if os.path.getsize(path) == 0:
                    continue
            out.add(os.path.relpath(path, parent).replace(os.sep, "/"))  # uno che manca resta: lo dice il controllo
        return out
    name = os.path.basename(root.rstrip(os.sep))
    if not os.path.isdir(root):
        return {name} if os.path.isfile(root) and os.path.getsize(root) > 0 else set()
    return {f"{name}/{f.relative}" for f in upload_inventory.torrent_files(root) if f.size > 0}


def hash_pieces(session: Session, job: UploadJob, path: str | None = None,
                planned: list[str] | None = None) -> torf.Torrent:
    """planned: i file (assoluti) degli hardlink appena creati da
    prepare_content, gli unici che entrano nel torrent.

    Il torrent ha esattamente i file previsti (audit 2026-10-18): la
    cartella con quel nome nella cartella di seed può avere già altro (la
    release originale con la promo di un altro tracker, un upload precedente
    con un sottotitolo tolto da allora), che finiva pubblicato e in seed
    senza che la decisione lo mostrasse. Quello in più resta fuori, e il
    registro lo dice; se torf non riesce a toglierlo, o manca un file
    previsto, l'upload si ferma prima di arrivare al tracker."""
    root = path or job.source_path
    if planned is None and (os.path.islink(root.rstrip(os.sep)) or upload_jobs.has_symlinks(root)):
        # La sorgente stessa: torf segue i link simbolici, e il file a cui
        # puntano finirebbe nel torrent pubblicato. Controllato alla
        # creazione del job, e di nuovo qui: fra le due possono passare giorni.
        raise UploadJobError("upload_source_has_symlinks", path=job.relative_path)
    expected = _expected_files(root, planned)
    if not expected:
        raise UploadJobError("no_video_files")
    # Fuori i file che la regola di analisi e nomi esclude
    # (nazgarr/upload/inventory.py), uno per uno: mai dei glob sul percorso,
    # che toglievano un film con "Sample" nel titolo. Per nome esatto nel
    # torrent (cartella/percorso), così la struttura resta quella della
    # cartella anche se rimane un file solo.
    name = os.path.basename(root.rstrip(os.sep))
    excluded = [
        _exclude_exact(f"{name}/{f.relative}")
        for f in (upload_inventory.walk(root) if os.path.isdir(root) else [])
        if not upload_inventory.in_torrent(f.relative, f.size)
    ]
    torrent = torf.Torrent(path=root, private=True, exclude_regexs=excluded, created_by=CREATED_BY)
    extras = sorted(_torrent_files(torrent) - expected)
    if extras:
        # Un Torrent nuovo, con tutte le esclusioni: riassegnare
        # exclude_regexs con quelle che ha già fallisce dentro torf.
        torrent = torf.Torrent(path=root, private=True, exclude_regexs=[*excluded, *map(_exclude_exact, extras)],
                               created_by=CREATED_BY)
    actual = _torrent_files(torrent)
    missing = sorted(expected - actual)
    if missing and planned is not None:
        raise UploadJobError("upload_planned_files_missing", files=", ".join(missing[:5]))
    if actual != expected:
        raise UploadJobError("upload_torrent_files_mismatch", files=", ".join(sorted(actual ^ expected)[:5]))
    if extras and planned is not None:
        upload_jobs.log_event(session, job, "upload_extra_files_left_out", level="warning", count=len(extras),
                              files=extras[:10])
        session.commit()

    def callback(_torrent, _path, done, total):
        _progress(session, job, "hashing", done, total)
        session.refresh(job)
        # Annullato a metà: torf si ferma se il callback restituisce qualcosa.
        return True if job.status == "cancelled" else None

    _progress(session, job, "hashing", 0, None)
    torrent.generate(threads=min(HASH_THREADS, os.cpu_count() or 1), callback=callback,
                     interval=PROGRESS_EVERY_SECONDS)
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

    def cancelled() -> bool:
        # Come durante l'hash (hash_pieces): un annullamento ferma il
        # lavoro fra una cattura e l'altra, non solo alla fine (audit 2026-10-18).
        session.refresh(job)
        return job.status == "cancelled"

    try:
        paths = screenshots.generate_screenshots(
            main_video, os.path.join(_job_dir(worker, job), "screenshots"), count=count, tonemap=tonemap,
            should_stop=cancelled,
        )
    except screenshots.ScreenshotsStopped as exc:
        raise _Cancelled() from exc
    except screenshots.ScreenshotError as exc:
        raise UploadJobError("upload_screenshots_failed", error=str(exc)) from exc
    urls = []
    for i, path in enumerate(paths, start=1):
        if cancelled():
            raise _Cancelled()
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
    suo percorso nel torrent), con la sua dimensione esatta. Un link
    simbolico no: il client seederebbe il file a cui punta, e senza recheck."""
    for file in torrent.files:
        path = os.path.join(save_path, *file.parts)
        if os.path.islink(path) or not os.path.isfile(path) or os.path.getsize(path) != file.size:
            return False
    return True


def _add_to_client(
    session: Session, job: UploadJob, target: UploadTarget, torrent_file: str, save_path: str,
    torrent: torf.Torrent | None = None, hold: bool = False, allowed: int = 0, missing: list[str] | None = None,
):
    """hold: il .torrent del tracker sui file di Nazgarr (un reseed, o
    "Riprova il seed"): entra fermo, con il recheck, e parte solo quando il
    recheck è riuscito (decisione dell'utente, 2026-10-19, come i reseed del
    motore): avviato, con un recheck fallito scaricava i piece mancanti
    dentro gli hardlink, cioè nei file della libreria. Il target resta
    SEED_RECHECK_PENDING finché reconcile_held_seeds non legge l'esito;
    allowed: i byte degli extra che si sa di non avere, che il client
    scarica dopo l'avvio (expected_missing_bytes); missing: i loro percorsi
    nel torrent, i soli piece che quel margine copre (2026-10-21,
    nazgarr/upload/extras_check.py).

    Senza hold, senza il recheck del client (decisione dell'utente, 2026-09-30): il
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
        skip = not hold and torrent is not None and can_skip and files_in_place(torrent, save_path)
        client = target.torrent_client.label
        # L'info hash di questi byte (audit 2026-10-18): senza, qBittorrent e
        # qui prendevano per il nostro un torrent qualunque comparso nel
        # frattempo (un *arr, cross-seed), e il controllo del percorso dopo
        # un'aggiunta senza recheck guardava quello.
        expected = _info_hash_of(torrent_file)
        # Fermo se l'adapter lo sa fare (ogni adapter incluso); un plugin che
        # non lo sa lo aggiunge avviato, e un recheck fallito lo ferma.
        extra = stopped_kwargs(adapter) if hold else {}
        try:
            info_hash = adapter.add_torrent(
                torrent_file, save_path=visible, force_recheck=True, expected_info_hash=expected,
                skip_check_verified=skip, **extra,
                **client_labels.add_kwargs(target.client_category, target.client_tags),
            )
        except TorrentAlreadyInClientError:
            info_hash = _keep_existing(session, job, target, adapter, expected, visible, client, hold=hold)
            _register_seed(session, job, target, adapter, info_hash)
            if hold:
                _hold(session, job, target, "already_in_client_held", client, save_path, allowed, missing)
            return info_hash
        if hold:
            # Dopo la registrazione: il rollback di un DB occupato lì dentro
            # non deve perdere l'attesa, se no il torrent restava fermo per
            # sempre senza nessuno che lo avviasse. Il commit è del chiamante.
            _register_seed(session, job, target, adapter, info_hash)
            _hold(session, job, target, "added_to_client_held", client, save_path, allowed, missing)
            return info_hash
        info = None
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
        _register_seed(session, job, target, adapter, info_hash, info)
        return info_hash
    finally:
        adapter_factory.close_adapter(adapter)


def _register_seed(session: Session, job: UploadJob, target: UploadTarget, adapter, info_hash: str,
                   info=None) -> None:
    """Il torrent appena aggiunto, e i suoi file nelle cartelle di seeding,
    subito fra quelli indicizzati (nazgarr/library/seed_refresh.py), come
    fanno l'esecutore dopo un reseed e il webhook di Radarr/Sonarr (audit
    2026-10-18). Prima restavano fuori fino alla run dopo: il file
    dell'upload risultava non in seed in libreria, e "Cerca ora" poteva
    trovare sul tracker l'upload stesso e proporlo come reseed. Un errore
    qui si scrive solo nel log: il torrent è nel client, lo vedrà la
    prossima indicizzazione. Anche il commit iniziale: con il DB occupato
    oltre il busy_timeout (una run che indicizza) l'upload riuscito
    risultava seed_failed, con il torrent già in seed."""
    job_id = job.id  # dopo un rollback rileggerlo vorrebbe dire un'altra query
    try:
        session.commit()  # quello che l'aggiunta ha già scritto resta, qualunque cosa succeda dopo
    except OperationalError:
        session.rollback()
        logger.warning("Upload %s: torrent %s nel client, il DB non si scrive: non registrato subito", job_id,
                       info_hash, exc_info=True)
        return
    try:
        info = info if getattr(info, "files", None) else adapter.get_torrent_info(info_hash)
        if info is not None:
            seed_refresh.register_client_torrent(session, target.torrent_client, info)
    except Exception:
        session.rollback()
        logger.warning("Upload %s: torrent %s nel client ma non registrato subito", job_id, info_hash,
                       exc_info=True)


def _info_hash_of(torrent_file: str) -> str:
    """Dai byte esatti del file (lo span grezzo di "info"), come lo calcola il client."""
    with open(torrent_file, "rb") as f:
        return compute_info_hash(f.read())


def _keep_existing(session: Session, job: UploadJob, target: UploadTarget, adapter, info_hash: str, visible: str,
                   client: str, hold: bool = False) -> str:
    """Il client ha già questo torrent: un tentativo precedente arrivato fin
    lì (un errore dopo l'aggiunta, un riavvio), o aggiunto a mano. Si tiene
    quello, con un recheck, se usa proprio il percorso di seed; altrove no:
    niente si sposta da solo. Con hold solo il recheck, fermo: lo avvia
    reconcile_held_seeds se riesce."""
    info = adapter.get_torrent_info(info_hash)
    seen = os.path.normpath(info.save_path) if info is not None and info.save_path else None
    if seen != os.path.normpath(visible):
        raise UploadJobError("upload_already_in_client", client=client, path=seen or "?")
    if hold:
        _stop_before_recheck(adapter, info_hash, job.id, client)
    adapter.recheck(info_hash)
    if not hold:
        adapter.start(info_hash)
        upload_jobs.log_event(session, job, "already_in_client", target=target, client=client)
    return info_hash


def _stop_before_recheck(adapter, info_hash: str, job_id: int, client: str) -> None:
    """Prima del recheck di un torrent in attesa (hold) che il client ha
    già: uno avviato (aggiunto a mano, da un tentativo di prima del
    2026-10-19, che lo aggiungeva avviato, o avviato a mano dopo un recheck
    fallito) con il recheck fallito riprenderebbe subito a scaricare dentro
    gli hardlink, fino al reconcile dopo (audit 2026-10-18). Se non si
    ferma lo dice il log; il reconcile riprova a fermarlo se il recheck
    fallisce."""
    try:
        state = adapter.get_torrent_status(info_hash).state
    except Exception:
        state = None  # non si sa: fermarlo non costa niente
    if is_stopped_state(state):
        return
    result, error = stop_torrent(adapter, info_hash)
    if result != "stopped":
        logger.warning("Upload %s: %s non fermato in %s prima del recheck (%s)", job_id, info_hash, client, result,
                       exc_info=error)


# Un torrent aggiunto fermo dal flusso degli upload (hold), in attesa
# dell'esito del suo recheck: il target è 'done' (il reseed o l'upload sono
# fatti) e reconcile_held_seeds lo avvia a recheck riuscito. Fallito, resta
# fermo e "Riprova il seed" lo fa ricontrollare.
SEED_RECHECK_PENDING = upload_jobs.SEED_RECHECK_PENDING
SEED_RECHECK_FAILED = "seed_recheck_failed"
# Gli eventi di un'aggiunta in attesa: i loro parametri (save_path, allowed,
# missing) servono al reconcile e al nuovo tentativo.
_HOLD_EVENTS = ("added_to_client_held", "already_in_client_held")


def _hold(session: Session, job: UploadJob, target: UploadTarget, code: str, client: str, save_path: str,
          allowed: int, missing: list[str] | None = None) -> None:
    target.error_message = SEED_RECHECK_PENDING
    upload_extras.forget(target.id)  # una nuova attesa: la verifica dei piece ricomincia
    extra = {"missing": sorted(missing)} if missing is not None else {}
    upload_jobs.log_event(session, job, code, target=target, client=client, save_path=save_path,
                          allowed=int(allowed or 0), **extra)


def _hold_params(job: UploadJob, target: UploadTarget) -> dict:
    """I parametri dell'ultima aggiunta in attesa di questo target."""
    for event in sorted(job.events, key=lambda e: e.id, reverse=True):
        if event.target_id == target.id and event.code in _HOLD_EVENTS:
            try:
                params = json.loads(event.params_json or "{}")
            except ValueError:
                return {}
            return params if isinstance(params, dict) else {}
    return {}


# Gli errori di un upload pubblicato ma non in seed, da cui si può riprovare;
# SEED_RECHECK_FAILED anche per un reseed.
SEED_RETRYABLE = ("seed_failed", "no_client", SEED_RECHECK_FAILED)


def _seeds_from_source(job: UploadJob) -> bool:
    """La sorgente è già nella cartella di seeding (o in quella per gli
    upload) e il torrent ha i suoi nomi: lì seeda anche nel flusso normale
    (_upload_pairs, _seeds_in_place). Mai una cartella della libreria."""
    if job.origin == "watch" or upload_pack.is_pack(job):
        return False
    if _inside(job.source_path, seeding_area(job)):
        return True
    try:
        return _inside(job.source_path, seed_root(job))
    except UploadJobError:
        return False


def _seed_candidates(session: Session, job: UploadJob) -> list[str]:
    """Dove possono stare i file di un upload pubblicato: la cartella di
    seed (con la cartella dei nomi nuovi, se c'è), o accanto alla sorgente
    solo se lì seeda anche il flusso normale. Prima accanto alla sorgente
    sempre: un nuovo tentativo poteva puntare il client alla cartella della
    libreria (audit 2026-10-18), e un "rimuovi con i file" nel client
    cancellava i file della libreria."""
    candidates = []
    with contextlib.suppress(UploadJobError):
        root = seed_root(job)
        with contextlib.suppress(Exception):
            folder = upload_file_names.plan(session, job).folder
            if folder:
                candidates.append(os.path.join(root, *folder.split("/")))
        candidates.append(root)
    if _seeds_from_source(job):
        candidates.append(os.path.dirname(job.source_path.rstrip(os.sep)))
    return candidates


def _relink_for_seed(session: Session, job: UploadJob, torrent: torf.Torrent) -> str:
    """Nessun posto ha più i file del torrent (gli hardlink tolti, o la
    cartella di seed creata dopo l'errore): si rifanno nella cartella di
    seed come all'upload, con gli stessi nomi. Restituisce dove seeda."""
    root = seed_root(job)
    if not os.path.exists(job.source_path):
        raise UploadJobError("upload_seed_files_missing")
    try:
        plan = upload_file_names.plan(session, job)
    except Exception as exc:  # la sorgente cambiata da allora: i nomi non si rifanno
        raise UploadJobError("upload_seed_files_missing") from exc
    if plan.renamed or plan.single_file or upload_pack.is_pack(job) or job.origin == "watch":
        base = os.path.join(root, *plan.folder.split("/")) if plan.folder else root
        pairs = [(source, os.path.join(base, *target.split("/"))) for source, target in plan.files]
    else:
        base, pairs = root, _upload_pairs(job, torrent, root)
    if not pairs or any(not os.path.isfile(source) for source, _target in pairs):
        raise UploadJobError("upload_seed_files_missing")
    created = link_files(pairs, root)
    if not files_in_place(torrent, base):
        hardlinks.remove_links(created)
        raise UploadJobError("upload_seed_files_missing")
    if created:
        upload_jobs.log_event(session, job, "seed_files_linked", count=len(created))
    return base


def _tracker_torrent(session: Session, job: UploadJob, target: UploadTarget, data_dir: str | None) -> str:
    """Il .torrent come l'ha salvato il tracker: il suo info hash è quello
    che il tracker conosce (UNIT3D riscrive il campo source). La copia
    scaricata dopo l'upload; se manca (il seed si è fermato prima, o un
    riavvio), si riscarica dal tracker. Quello creato da Nazgarr solo se il
    tracker non sa ridarlo, e il registro lo dice."""
    job_dir = upload_jobs.job_folder(data_dir, job.id) if data_dir else (
        os.path.dirname(target.torrent_path) if target.torrent_path else None)
    seed_copy = os.path.join(job_dir, f"tracker-{target.tracker_id}-seed.torrent") if job_dir else None
    if seed_copy and os.path.isfile(seed_copy):
        target.torrent_path = seed_copy
        return seed_copy
    if seed_copy and target.torrent_id_remote:
        with adapter_factory.tracker(target.tracker) as adapter:
            detail = getattr(adapter, "get_torrent_detail", None)
            if callable(detail):
                try:
                    link = detail(target.torrent_id_remote).download_link
                    if not link:
                        raise ValueError("no download link")
                    content = adapter.download_torrent(link)
                    torf.Torrent.read_stream(io.BytesIO(content))
                except Exception as exc:
                    raise UploadJobError("upload_tracker_torrent_unavailable", error=safe_error(exc)) from exc
                os.makedirs(job_dir, exist_ok=True)
                with open(seed_copy, "wb") as f:
                    f.write(content)
                target.torrent_path = seed_copy
                return seed_copy
    if not target.torrent_path or not os.path.isfile(target.torrent_path):
        raise UploadJobError("upload_tracker_torrent_unavailable")
    upload_jobs.log_event(session, job, "seed_retry_own_torrent", level="warning", target=target)
    return target.torrent_path


def retry_seed(session: Session, job: UploadJob, target: UploadTarget, data_dir: str | None = None) -> None:
    """Di nuovo solo l'aggiunta al client di un upload già pubblicato (il
    .torrent salvato dal tracker), mai l'upload. Con il recheck del client:
    i file potrebbero essere cambiati da quando Nazgarr li ha letti. Fermo,
    come un reseed (decisione dell'utente, 2026-10-19): parte a recheck
    riuscito (reconcile_held_seeds).

    Dopo un recheck fallito (SEED_RECHECK_FAILED, anche di un reseed) il
    torrent è ancora nel client, fermo: si chiede un nuovo recheck, mai un
    avvio, come il retry di un reseed del motore (executor.adopt_for_retry).
    Il client non ricontrolla da solo un torrent fermo. Se non c'è più, si
    riaggiunge."""
    retryable = target.error_message in SEED_RETRYABLE and (
        target.action == "upload" or target.error_message == SEED_RECHECK_FAILED)
    if target.status != "done" or not retryable:
        raise UploadJobError("upload_seed_retry_unavailable")
    if target.error_message == SEED_RECHECK_FAILED and _recheck_held(session, job, target):
        return
    if _client_row(session, target) is None:
        # Senza client all'upload (no_client) o con quello di allora tolto:
        # quello che il tracker userebbe oggi.
        target.torrent_client_id = upload_jobs.default_client_id(session, target.tracker)
    if target.action != "upload":
        _readd_reseed(session, job, target)
        return
    torrent_path = _tracker_torrent(session, job, target, data_dir)
    try:
        torrent = torf.Torrent.read(torrent_path)
    except torf.TorfError as exc:
        raise UploadJobError("upload_tracker_torrent_unavailable", error=safe_error(exc)) from exc
    save_path = next((p for p in _seed_candidates(session, job) if files_in_place(torrent, p)), None)
    if save_path is None:
        save_path = _relink_for_seed(session, job, torrent)
    # Il client scelto e il .torrent nel DB prima di toccare il client: se
    # dopo l'aggiunta il DB non si scrive (_register_seed torna indietro),
    # restano quelli, non il client di prima (magari nessuno).
    session.commit()
    _retry_add(session, job, target, torrent_path, save_path, allowed=0)


def _retry_add(session: Session, job: UploadJob, target: UploadTarget, torrent_path: str, save_path: str,
               allowed: int, missing: list[str] | None = None) -> None:
    """L'aggiunta di un nuovo tentativo: ferma, con il recheck, e il target
    in attesa del suo esito (_add_to_client con hold)."""
    previous = target.error_message
    try:
        info_hash = _add_to_client(session, job, target, torrent_path, save_path, hold=True, allowed=allowed,
                                   missing=missing)
    except Exception as exc:
        session.rollback()
        target.error_message = previous if previous in SEED_RETRYABLE else "seed_failed"
        upload_jobs.log_event(session, job, "seed_failed", level="error", target=target,
                              error=upload_jobs.error_text(exc))
        session.commit()
        raise UploadJobError("upload_seed_retry_failed", error=upload_jobs.error_text(exc)) from exc
    if info_hash is None:  # nessun client: _add_to_client l'ha già scritto
        session.commit()
        raise UploadJobError("upload_seed_retry_failed", error="no_client")
    target.info_hash = info_hash
    upload_jobs.log_event(session, job, "seed_retried", target=target, client=target.torrent_client.label)
    session.commit()


def _recheck_held(session: Session, job: UploadJob, target: UploadTarget) -> bool:
    """Il nuovo tentativo dopo un recheck fallito, se il client ha ancora il
    torrent: un nuovo recheck, fermo, e il target di nuovo in attesa. False
    se il client non l'ha più (o il client non c'è più): va riaggiunto. Il
    client che rifiuta il recheck è un nuovo tentativo fallito."""
    row = _client_row(session, target)
    if row is None or not target.info_hash:
        return False
    adapter = adapter_factory.build_torrent_client_adapter(row)
    try:
        try:
            # Un adapter che non sa cercare un torrent solo (un plugin col
            # default di get_torrent_info) direbbe sempre "non c'è": lì solo
            # il recheck, e un torrent che non c'è più lo rifiuta il client.
            if can_look_up_torrent(adapter) and adapter.get_torrent_info(target.info_hash) is None:
                return False
            recheck = getattr(adapter, "recheck", None)
            if not callable(recheck):
                raise NotImplementedError
            _stop_before_recheck(adapter, target.info_hash, job.id, row.label)
            recheck(target.info_hash)
        except NotImplementedError as exc:
            raise UploadJobError("upload_seed_retry_failed",
                                 error="this client cannot be asked for a recheck: recheck it in the client") from exc
        except Exception as exc:
            raise UploadJobError("upload_seed_retry_failed", error=upload_jobs.error_text(exc)) from exc
    finally:
        adapter_factory.close_adapter(adapter)
    target.error_message = SEED_RECHECK_PENDING
    upload_extras.forget(target.id)  # sui file di adesso
    upload_jobs.log_event(session, job, "seed_recheck_requested", target=target, client=row.label)
    session.commit()
    return True


def _readd_reseed(session: Session, job: UploadJob, target: UploadTarget) -> None:
    """Un reseed fallito al recheck il cui torrent non è più nel client: lo
    stesso .torrent del tracker, nella stessa cartella di prima (dentro le
    cartelle di seeding), di nuovo fermo e col recheck. Niente si ricrea:
    se i file non sono più giusti il recheck fallisce di nuovo, fermo."""
    params = _hold_params(job, target)
    save_path = params.get("save_path")
    if not target.torrent_path or not os.path.isfile(target.torrent_path) or not isinstance(save_path, str):
        raise UploadJobError("upload_seed_retry_unavailable")
    if not os.path.isdir(save_path) or not _inside(os.path.join(save_path, "x"), seeding_area(job)):
        raise UploadJobError("upload_seed_files_missing")
    session.commit()
    missing = params.get("missing")
    _retry_add(session, job, target, target.torrent_path, save_path, allowed=int(params.get("allowed") or 0),
               missing=[p for p in missing if isinstance(p, str)] if isinstance(missing, list) else None)


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
        raise UploadJobError("upload_tracker_torrent_unavailable", error=safe_error(exc)) from exc
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
        # Da qui la richiesta parte: un errore non dice più se il tracker
        # l'ha ricevuto (_run_target, upload_outcome_unknown).
        ctx.setdefault("sent", set()).add(target.id)
        uploaded = adapter.upload_torrent(fields, torrent_path)
        # Da qui il torrent è pubblicato: qualunque cosa succeda dopo, mai
        # più un target 'failed' né un secondo invio (handle, reset_interrupted).
        ctx.setdefault("published", {})[target.id] = uploaded.torrent_id_remote
        _journal_published(ctx["dir"], target, uploaded.torrent_id_remote)
        _commit_published(session, job, target, uploaded.torrent_id_remote)

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
            _remember_seeded(ctx, target, [os.path.join(save_path, *file.parts) for file in torrent.files])
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
            session.rollback()  # un errore del DB lascerebbe la sessione inutilizzabile
            target.error_message = "seed_failed"
            upload_jobs.log_event(session, job, "seed_failed", level="error", target=target,
                                  error=upload_jobs.error_text(exc))


# Un torrent appena pubblicato si scrive nel DB anche se è occupato: altri
# tentativi prima di arrendersi (ognuno aspetta già il busy_timeout di SQLite).
PUBLISHED_COMMIT_ATTEMPTS = 3


def _journal_published(job_dir: str, target: UploadTarget, torrent_id_remote: str) -> None:
    """L'id del torrent pubblicato anche in un file del job, prima del DB:
    se il processo si ferma (o il DB non si scrive) proprio adesso, al
    riavvio reset_interrupted sa che il tracker l'ha ricevuto. Solo l'id,
    mai il link di download (ha la passkey)."""
    with contextlib.suppress(OSError):
        with open(upload_jobs.published_journal(job_dir, target.id), "w") as f:
            json.dump({"torrent_id_remote": torrent_id_remote}, f)


def _commit_published(session: Session, job: UploadJob, target: UploadTarget, torrent_id_remote: str) -> None:
    for attempt in range(PUBLISHED_COMMIT_ATTEMPTS):
        target.torrent_id_remote = torrent_id_remote
        upload_jobs.log_event(session, job, "uploaded", target=target, torrent=torrent_id_remote)
        try:
            session.commit()
            return
        except OperationalError:
            session.rollback()
            if attempt == PUBLISHED_COMMIT_ATTEMPTS - 1:
                raise
            logger.warning("Upload %s: torrent %s pubblicato, il DB non si scrive: si riprova", job.id,
                           torrent_id_remote, exc_info=True)
            time.sleep(1 + attempt)


def run_reseed(session: Session, job: UploadJob, target: UploadTarget, ctx: dict) -> None:
    dupes = json.loads(target.dupes_json or "[]")
    dupe = next((d for d in dupes if d["torrent_id_remote"] == target.reseed_torrent_id), None)
    if dupe is None or not dupe.get("download_link"):
        raise UploadJobError("upload_dupe_no_download_link")
    upload_jobs.set_target_status(target, upload_jobs.TargetStatus.PREPARING)
    session.commit()
    with adapter_factory.tracker(target.tracker) as adapter:
        content = dupe_torrent(adapter, dupe)  # la chiave del tracker rimessa qui
        # Il controllo completo vale per il .torrent che ha letto: quello che si
        # aggiunge al client dev'essere lo stesso, non uno riscaricato e cambiato
        # nel frattempo sul tracker.
        verified = (dupe.get("verification") or {})
        if verified.get("status") != "passed" or verified.get("info_hash") != compute_info_hash(content):
            raise UploadJobError("upload_reseed_torrent_changed", tracker=target.tracker.label)
        # Prima di ogni file e del client: il claim vale solo finché il torrent è morto.
        _claim_resurrection(session, job, target, dupe, adapter)
    torrent_path = os.path.join(ctx["dir"], f"reseed-{target.tracker_id}.torrent")
    with open(torrent_path, "wb") as f:
        f.write(content)
    parsed = parse_torrent_info(content)
    target.torrent_path = torrent_path

    upload_jobs.set_target_status(target, upload_jobs.TargetStatus.SEEDING)
    session.commit()
    locate = build_locator(job, parsed)
    located, missing = [], []
    for entry in parsed.files:
        local, _where = locate(entry)
        if local is None:
            if is_video(entry.path):
                raise UploadJobError("upload_reseed_missing_video", path=entry.path)
            missing.append(entry)  # un extra mancante lo scarica il client dopo il recheck
            continue
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
    _remember_seeded(ctx, target, [dst for _src, dst in destinations(save_path)])
    allowed = expected_missing_bytes(sum(e.length for e in missing), len(missing), parsed.piece_length)
    target.info_hash = _add_to_client(session, job, target, torrent_path, save_path, hold=True, allowed=allowed,
                                      missing=[e.path for e in missing])
    upload_jobs.log_event(session, job, "reseeded", target=target, torrent=target.reseed_torrent_id)


def _claim_resurrection(session: Session, job: UploadJob, target: UploadTarget, dupe: dict, adapter) -> None:
    """Il claim della resurrection di un torrent morto che questo reseed
    rimette in seed, con la sessione web salvata sul tracker (decisione
    dell'utente, 2026-11-10): prima che il client lo annunci, perché UNIT3D
    lo accetta solo con 0 seeder. Il reseed entra fermo e parte dopo il
    recheck, ma il claim va prima di tutto: se non riesce, niente si tocca.

    - nessun cookie (o un adapter che non sa farlo): niente, il claim resta
      a mano come nella decisione;
    - fatto, non offerto dalla pagina (vivo, troppo recente, già in attesa:
      anche uno fatto a mano), o rifiutato (un tuo upload): il reseed va avanti;
    - sessione scaduta o un errore: il reseed si ferma qui con
      upload_resurrection_failed, così l'occasione non si perde; "Riprova"
      lo rifà, dopo il claim a mano o un cookie nuovo.

    Solo per un torrent morto quando l'ha letto il dupe check: per uno vivo
    nessuna richiesta al sito."""
    if dupe.get("seeders") != 0:
        return
    torrent = str(target.reseed_torrent_id)
    result = resurrection.claim_before_seed(target.tracker, adapter, torrent, seeders=0)
    if result is None:
        return
    detail = result.detail or ""
    if result.status in resurrection.GO_ON:
        upload_jobs.log_event(session, job, f"resurrection_{result.status}",
                              level="warning" if result.status == "refused" else "info", target=target,
                              torrent=torrent, detail=detail)
        session.commit()
        return
    expired = result.status == "session_expired"
    upload_jobs.log_event(session, job, "resurrection_session_expired" if expired else "resurrection_failed",
                          level="error", target=target, torrent=torrent, detail=detail)
    session.commit()
    raise UploadJobError("upload_resurrection_failed", tracker=target.tracker.label,
                         reason="the website session expired" if expired else (detail or result.status))


# Gli stati in cui un passo del worker lavora il job: gli stati del worker
# meno 'queued', dove il job aspetta soltanto il suo turno nella coda.
_WORKER_RUNNING = tuple(s for s in upload_jobs.WORKER_STATES if s != "queued")


def reconcile_held_seeds(session: Session, data_dir: str | None = None) -> int:
    """L'esito del recheck dei torrent aggiunti fermi dal flusso degli upload
    (hold: i reseed e "Riprova il seed"; decisione dell'utente, 2026-10-19),
    dallo scheduler ogni HELD_SEEDS_INTERVAL_SECONDS: riuscito, il torrent
    parte; fallito, resta fermo, e "Riprova il seed" lo fa ricontrollare.
    Ogni esito, una volta, diventa anche l'evento upload.seed_checked per
    webhook e notifiche (decisione dell'utente, 2026-10-21).
    Mai mentre il worker lavora quel job (lo stesso target, due sessioni).
    Un job solo in coda sì (audit 2026-10-18): "Riprova" di un altro
    tracker, o una ripresa, lo rimette in fondo alla coda, dietro upload che
    hashano per ore, e l'esecuzione non tocca mai un target già 'done'; prima
    il torrent, con il recheck già riuscito, restava fermo per tutto il tempo.
    Ritorna quanti target ha guardato."""
    targets = (
        session.query(UploadTarget).join(UploadJob, UploadJob.id == UploadTarget.job_id)
        .filter(UploadTarget.error_message == SEED_RECHECK_PENDING, UploadTarget.status == "done",
                UploadTarget.info_hash.isnot(None), UploadJob.status.notin_(_WORKER_RUNNING))
        .order_by(UploadTarget.id).all()
    )
    for target in targets:
        try:
            _reconcile_held(session, target.job, target, data_dir)
        except Exception:
            session.rollback()
            logger.warning("Upload %s: esito del recheck di %s non letto", target.job_id, target.info_hash,
                           exc_info=True)
    return len(targets)


def _reconcile_held(session: Session, job: UploadJob, target: UploadTarget, data_dir: str | None) -> None:
    row = _client_row(session, target)
    if row is None:
        if target.torrent_client_id is None:  # il client tolto da Nazgarr: nessuno lo seguirà più
            target.error_message = SEED_RECHECK_FAILED
            upload_jobs.log_event(session, job, "seed_recheck_no_client", level="error", target=target)
            upload_jobs.emit_seed_checked(session, job, target, "gone")
            session.commit()
        return  # spento: si aspetta che torni
    client = row.label
    adapter = adapter_factory.build_torrent_client_adapter(row)
    try:
        try:
            status = adapter.get_torrent_status(target.info_hash)
        except Exception:
            if not torrent_missing(adapter, target.info_hash):
                logger.warning("Upload %s: stato di %s non letto in %s", job.id, target.info_hash, client,
                               exc_info=True)
            elif not _missing_long_enough(job, target):
                logger.info("Upload %s: %s non (ancora) in %s, riprovo dopo", job.id, target.info_hash, client)
            else:
                _missing_since.pop(target.id, None)
                upload_extras.forget(target.id)
                target.error_message = SEED_RECHECK_FAILED
                upload_jobs.log_event(session, job, "seed_recheck_torrent_gone", level="error", target=target,
                                      client=client)
                upload_jobs.emit_seed_checked(session, job, target, "gone", client=client)
                session.commit()
            return
        _missing_since.pop(target.id, None)
        params = _hold_params(job, target)
        outcome, reason = recheck_outcome(status, params.get("allowed")), None
        if outcome == "ok" and (status.recheck_status == "failed" or upload_extras.is_open(target.id)):
            outcome, reason = _extras_outcome(session, job, target, adapter, client, status, params)
        if outcome == "pending":
            return
        if outcome == "ok":
            result, error = ("started", None)
            if is_stopped_state(status.state):
                result, error = start_torrent(adapter, target.info_hash)
            if result == "failed":  # si riprova al giro dopo, con la verifica dei piece già fatta
                logger.warning("Upload %s: recheck di %s riuscito, avvio rifiutato da %s", job.id,
                               target.info_hash, client, exc_info=error)
                return
            upload_extras.forget(target.id)
            target.error_message = None
            upload_jobs.log_event(session, job, "seed_recheck_passed" if result == "started"
                                  else "seed_recheck_passed_start_by_hand", target=target, client=client)
            upload_jobs.emit_seed_checked(session, job, target, "started" if result == "started" else "start_by_hand",
                                          client=client)
            session.commit()
            return
        upload_extras.forget(target.id)
        stopped = "stopped" if is_stopped_state(status.state) else stop_torrent(adapter, target.info_hash)[0]
        restored = _restore_partial_names(job, target, adapter, data_dir)
        target.error_message = SEED_RECHECK_FAILED
        if stopped != "stopped":
            code = "seed_recheck_failed_running"
        else:  # decisa da Nazgarr, piece per piece (_extras_outcome): il perché
            code = "seed_pieces_failed" if reason else "seed_recheck_failed"
        upload_jobs.log_event(session, job, code, level="error", target=target, client=client, state=status.state,
                              **({"reason": reason} if reason else {}))
        if restored:
            upload_jobs.log_event(session, job, "seed_partial_names_restored", level="warning", target=target,
                                  client=client, count=len(restored))
        upload_jobs.emit_seed_checked(session, job, target, "recheck_failed", client=client, state=status.state,
                                      stopped=stopped == "stopped")
        session.commit()
        logger.warning("Upload %s: recheck di %s fallito in %s (%s), lasciato fermo", job.id, target.info_hash,
                       client, status.state)
    finally:
        adapter_factory.close_adapter(adapter)


def _verification(target: UploadTarget) -> dict | None:
    """Il full hash check del dupe che questo reseed rimette in seed."""
    try:
        dupes = json.loads(target.dupes_json or "[]")
    except ValueError:
        return None
    dupe = next((d for d in dupes if isinstance(d, dict) and d.get("torrent_id_remote") == target.reseed_torrent_id),
                None)
    return dupe.get("verification") if dupe else None


def _extras_outcome(session: Session, job: UploadJob, target: UploadTarget, adapter, client: str, status,
                    params: dict) -> tuple[str, str | None]:
    """Un recheck finito entro il margine degli extra mancanti (o un 100%
    mentre una verifica è aperta) è riuscito solo se ogni piece che non
    tocca quegli extra l'ha verificato Nazgarr (2026-10-21, la regola dei
    reseed della coda: nazgarr/upload/extras_check.py). Mentre la verifica
    gira il torrent resta fermo: uno che gira (un plugin senza
    can_add_stopped, o avviato a mano) si ferma, come dopo un recheck
    fallito. Ritorna (esito, perché)."""
    save_path = params.get("save_path")
    missing = params.get("missing")
    missing = {p for p in missing if isinstance(p, str)} if isinstance(missing, list) else None
    if not isinstance(save_path, str) or not target.torrent_path:
        return "failed", "Nazgarr does not know where this reseed seeds, so it could not check its pieces"
    try:
        with open(target.torrent_path, "rb") as f:
            content = f.read()
        decided = upload_extras.decide(
            target.id, content, target.info_hash, save_path, missing,
            upload_extras.verified_record(_verification(target), target.info_hash), partial_files.suffix_of(adapter),
        )
    except (OSError, TorrentMetainfoError) as exc:
        return "failed", f"the saved .torrent cannot be read ({safe_error(exc)}), so its pieces could not be checked"
    except Exception:
        # Mai un avvio senza la verifica: si riprova al reconcile dopo.
        logger.warning("Upload %s: piece di %s fuori dagli extra mancanti non controllati", job.id, target.info_hash,
                       exc_info=True)
        decided = upload_extras.Outcome("pending")
    if decided.started:
        upload_jobs.log_event(session, job, "seed_pieces_checking", target=target, client=client)
    if decided.outcome == "pending" and not is_stopped_state(status.state):
        result, error = stop_torrent(adapter, target.info_hash)
        if result == "stopped":
            upload_jobs.log_event(session, job, "seed_stopped_while_checking", level="warning", target=target,
                                  client=client)
        else:
            logger.warning("Upload %s: %s non fermato in %s durante la verifica dei piece (%s)", job.id,
                           target.info_hash, client, result, exc_info=error)
    if decided.outcome == "pending":
        session.commit()
    return decided.outcome, decided.reason


# Da quando questo processo vede mancare dal client il torrent di un
# target in attesa (id del target -> momento). Sparito solo dopo
# MISSING_TORRENT_GRACE di fila, contati anche dall'ultima messa in attesa,
# come i reseed del motore (audit 2026-10-18): un client appena riavviato
# (un riavvio di tutto il compose, con il reconcile dopo 30 secondi) può
# non aver ancora ricaricato i suoi torrent, e il target restava fallito
# con un recheck che sarebbe riuscito. Solo in memoria: dopo un riavvio di
# Nazgarr si riparte da capo, e aspettare costa solo l'attesa (il torrent
# è fermo).
_missing_since: dict[int, datetime] = {}
# Gli eventi dopo cui il torrent di un target è (di nuovo) in attesa.
_HELD_AGAIN_EVENTS = (*_HOLD_EVENTS, "seed_recheck_requested")


def _missing_long_enough(job: UploadJob, target: UploadTarget) -> bool:
    now = datetime.now(UTC)
    since = _missing_since.setdefault(target.id, now)
    held = max((e.created_at for e in job.events if e.target_id == target.id and e.code in _HELD_AGAIN_EVENTS
                and e.created_at is not None), default=None)
    if held is not None:
        since = max(since, as_utc(held))
    return now - since >= MISSING_TORRENT_GRACE


def _restore_partial_names(job: UploadJob, target: UploadTarget, adapter, data_dir: str | None) -> list[str]:
    """Dopo un recheck fallito, il nome ai file di questo target che il
    client ha rinominato perché incompleti (Transmission, "<nome>.part";
    decisione dell'utente, 2026-10-19): solo quelli registrati quando il
    target li ha messi in seed (_remember_seeded: percorso, dispositivo e
    inode), dentro le cartelle di seeding (nazgarr/torrents/partial_files.py)."""
    suffix = partial_files.suffix_of(adapter)
    if suffix is None:
        return []
    job_dir = upload_jobs.job_folder(data_dir, job.id) if data_dir else (
        os.path.dirname(target.torrent_path) if target.torrent_path else None)
    seeded = _journaled_seeded(job_dir, target.id)
    folders = list(seeding_area(job))
    with contextlib.suppress(UploadJobError):
        folders.append(seed_root(job))
    return partial_files.restore_names(list(seeded.values()), set(seeded), suffix, folders)


def _remember_seeded(ctx: dict, target: UploadTarget, paths: list[str]) -> None:
    """I file che un tracker di questo job mette in seed (dispositivo, inode,
    percorso), per target: contano solo quelli dei tracker andati a buon
    fine (_seeded_by_done). Dalla cartella osservata si toglie solo un file
    la cui copia è fra questi (_clear_watch_source), e la pulizia degli
    hardlink non tocca mai un file che seedano (_cleanup_unused_links).

    Anche in un file del job (_seeded_journal), come l'id di un torrent
    pubblicato: la release resta nella cartella osservata finché un tracker
    è fallito, e se ne va dopo un nuovo tentativo riuscito (o un riavvio),
    in un'esecuzione che non ha più in memoria le copie messe in seed da
    questa."""
    seeded = ctx.setdefault("seeded", {}).setdefault(target.id, {})
    for path in paths:
        with contextlib.suppress(OSError):
            st = os.stat(path)
            seeded[(st.st_dev, st.st_ino)] = os.path.realpath(path)
    if ctx.get("dir"):
        with contextlib.suppress(OSError):
            with open(_seeded_journal(ctx["dir"], target.id), "w") as f:
                json.dump({"files": [[dev, ino, path] for (dev, ino), path in seeded.items()]}, f)


def _seeded_journal(job_dir: str, target_id: int) -> str:
    return os.path.join(job_dir, f"seeded-{target_id}.json")


def _forget_seeded(ctx: dict, target: UploadTarget) -> None:
    """Un tracker che parte (di nuovo, es. dopo "Cambia match"): le copie in
    seed registrate da un'esecuzione precedente non sono più le sue."""
    if ctx.get("dir"):
        with contextlib.suppress(OSError):
            os.unlink(_seeded_journal(ctx["dir"], target.id))


def _journaled_seeded(job_dir: str | None, target_id: int) -> dict[tuple[int, int], str]:
    """Le copie in seed scritte nel file del job; vuoto se manca o non si
    legge: al massimo un file resta nella cartella osservata, mai uno tolto
    senza la sua copia."""
    if not job_dir:
        return {}
    try:
        with open(_seeded_journal(job_dir, target_id)) as f:
            files = json.load(f).get("files") or []
        return {(int(dev), int(ino)): str(path) for dev, ino, path in files}
    except (OSError, ValueError, TypeError, AttributeError):
        return {}


def _seeded_by_done(job: UploadJob, ctx: dict) -> dict[tuple[int, int], set[str]]:
    """(dispositivo, inode) -> i percorsi da cui seedano i tracker di questo
    job finiti 'done', in questa esecuzione o in una precedente (il file del
    job: un nuovo tentativo, un riavvio). Un reseed fallito dopo aver
    registrato i suoi file non conta."""
    seeded = ctx.get("seeded") or {}
    out: dict[tuple[int, int], set[str]] = {}
    for target in job.targets:
        if target.status != "done":
            continue
        records = {**_journaled_seeded(ctx.get("dir"), target.id), **(seeded.get(target.id) or {})}
        for key, path in records.items():
            out.setdefault(key, set()).add(path)
    return out


class _Cancelled(Exception):
    pass


def _cleanup_unused_links(session: Session, job: UploadJob, ctx: dict, overrides: dict) -> None:
    """Gli hardlink creati per dare al torrent i suoi nomi (prepare_content)
    si tolgono se non seedano niente: "non mettere in seed", o nessun
    upload riuscito. Solo quelli creati da questo job, e le cartelle che
    restano vuote.

    Mai uno da cui seeda un altro tracker del job (audit 2026-10-18): un
    reseed con lo stesso nome e lo stesso layout riusa proprio quel link
    (hardlinks.check_link), e toglierlo lasciava il suo torrent senza dati;
    dalla cartella osservata poi se ne andava anche l'originale.

    Ognuno si ricontrolla subito prima (audit 2026-10-18): deve essere
    ancora un file vero con l'inode di quando è nato (_still_our_link), e
    si toglie con hardlinks.remove_links, che non segue mai un link
    simbolico nelle cartelle sopra e ricontrolla quell'inode nella cartella
    da cui toglie il file. Prima un unlink per percorso: un altro
    file messo con lo stesso nome nel frattempo, o una cartella diventata
    un link simbolico, portavano a cancellare un file non nostro.

    Anche quelli creati da un'esecuzione precedente dello stesso job (il
    file del job, _journaled_links: un riavvio a metà, una pulizia non
    riuscita), che questa ha riusato senza crearli (audit 2026-10-18)."""
    journal, journal_at = _journaled_links(ctx.get("dir"))
    created = list(dict.fromkeys([*(ctx.get("created_links") or []), *journal]))
    uploaded = any(t.action == "upload" and t.status == "done" for t in job.targets)
    if not created or (uploaded and not overrides.get("no_seed")):
        return
    recorded = {**journal, **(ctx.get("created_ids") or {})}
    since = min((at for at in (journal_at, ctx.get("links_at")) if at is not None), default=None)
    keep = {path for paths in _seeded_by_done(job, ctx).values() for path in paths}
    unused = [path for path in created if os.path.realpath(path) not in keep]
    if unused:
        used = _used_elsewhere(session, job, unused, since, ctx.get("data_dir"))
        unused = [path for path in unused if path not in used]
    if not unused:
        return
    root = os.path.realpath(ctx["seed_root"]())
    removable, changed = [], []
    for path in unused:
        ours = _still_our_link(path, recorded.get(path))
        if ours:
            removable.append(path)
        elif ours is False:
            changed.append(path)
    # Con l'identità di quando sono nati anche lì (expected): fra il
    # controllo qui sopra e l'unlink un altro file può prendere il posto di
    # un link, e remove_links lo guarda di nuovo nella cartella da cui lo
    # toglie. Quelli che non ha potuto togliere (lo scrive nel log) non contano.
    removed = hardlinks.remove_links(removable, prune_up_to=root, expected=recorded)
    if ctx.get("dir"):
        # Nel file del job restano solo quelli ancora suoi e al loro posto.
        gone = set(removed) | set(changed)
        _write_links_journal(ctx["dir"], {path: ids for path, ids in recorded.items() if path not in gone
                                          and _still_our_link(path, ids)}, since)
    if changed:
        upload_jobs.log_event(session, job, "file_links_changed", level="warning", count=len(changed),
                              files=[os.path.relpath(path, root) for path in changed[:10]])
    if removed:
        upload_jobs.log_event(session, job, "file_links_removed", count=len(removed))
    session.commit()


_LIVE_SEED_JOBS = ("in_progress", "seeding")


def _used_elsewhere(session: Session, job: UploadJob, paths: list[str], since: datetime | None,
                    data_dir: str | None = None) -> set[str]:
    """Dei link creati da questo job, quelli che usa già qualcun altro: un
    torrent del client (l'indice, nazgarr/torrents/indexer.py), un reseed
    del motore o un altro upload (_used_by_other_uploads). Il motore riusa
    una destinazione che è già lo stesso file (hardlinks.check_link): un suo
    reseed approvato durante i minuti di hash e screenshot, con lo stesso
    nome, seeda proprio da uno di questi, e toglierlo lo lasciava senza dati
    (audit 2026-10-18, finding 1). Nel dubbio un link resta: al massimo un
    hardlink orfano, mai un file tolto da sotto a un client."""
    used = _used_by_other_uploads(session, job, paths, data_dir)
    disk = job.disk
    if disk is None:
        _log_kept(session, job, used)
        return used
    base = os.path.realpath(disk.root_path)
    by_relative: dict[str, str] = {}
    by_inode: dict[int, list[str]] = {}
    for path in paths:
        real = os.path.realpath(path)
        if real.startswith(base + os.sep):
            by_relative[os.path.relpath(real, base)] = path
        with contextlib.suppress(OSError):
            by_inode.setdefault(os.stat(path).st_ino, []).append(path)
    if by_relative:
        relatives = list(by_relative)
        indexed = (
            session.query(SeedFile.relative_path)
            .join(ClientTorrentFile, ClientTorrentFile.seed_file_id == SeedFile.id)
            .filter(SeedFile.disk_id == disk.id, SeedFile.relative_path.in_(relatives))
        )
        # Un reseed dal file già nella cartella di seeding (torrent_to_client).
        reseeding = (
            session.query(SeedFile.relative_path)
            .join(CandidateFile, CandidateFile.seed_file_id == SeedFile.id)
            .join(SeedJob, SeedJob.candidate_id == CandidateFile.candidate_id)
            .filter(SeedFile.disk_id == disk.id, SeedFile.relative_path.in_(relatives),
                    SeedJob.final_status.in_(_LIVE_SEED_JOBS))
        )
        used |= {by_relative[row[0]] for row in indexed.union(reseeding).all() if row[0] in by_relative}
    if by_inode and since is not None:
        # Un reseed con hardlink dal file in libreria (media_to_torrent): la
        # sua destinazione non è nel DB, ma è lo stesso inode, e un reseed
        # nato dopo questi link può averne riusato uno.
        linked = (
            session.query(MediaFile.inode)
            .join(CandidateFile, CandidateFile.media_file_id == MediaFile.id)
            .join(SeedJob, SeedJob.candidate_id == CandidateFile.candidate_id)
            .filter(MediaFile.disk_id == disk.id, MediaFile.inode.in_(list(by_inode)),
                    SeedJob.final_status.in_(_LIVE_SEED_JOBS),
                    or_(SeedJob.hardlink_created_at.is_(None), SeedJob.hardlink_created_at >= since))
        )
        for (inode,) in linked.all():
            used.update(by_inode.get(inode, ()))
    _log_kept(session, job, used)
    return used


def _log_kept(session: Session, job: UploadJob, used: set[str]) -> None:
    if used:
        upload_jobs.log_event(session, job, "file_links_kept", count=len(used))
        session.commit()


def _used_by_other_uploads(session: Session, job: UploadJob, paths: list[str], data_dir: str | None) -> set[str]:
    """Dei link di questo job, quelli da cui seeda un altro upload (i suoi
    tracker finiti o in seed, il loro file nel job: _remember_seeded) o da
    cui un'esecuzione in corso sta creando il suo torrent (_CONTENT_JOURNAL).
    Un link lasciato da questo job (un riavvio, una pulizia non riuscita)
    lo riusa un altro upload con gli stessi nomi (hardlinks.check_link: lo
    stesso file va bene) senza crearlo, e toglierlo eliminando o
    riprovando questo lasciava il suo torrent senza dati (revisione
    dell'audit 2026-10-18, finding 115). Senza la cartella dei dati non si
    sa: restano tutti."""
    if not paths:
        return set()
    if not data_dir:
        return set(paths)
    rows = (
        session.query(UploadTarget.id, UploadTarget.job_id, UploadTarget.status, UploadJob.status)
        .join(UploadJob, UploadJob.id == UploadTarget.job_id)
        .filter(UploadJob.id != job.id, UploadTarget.action.in_(("upload", "reseed")),
                or_(UploadTarget.status.in_(("done", *upload_jobs.BUSY_TARGET_STATES)),
                    UploadJob.status == "running"))
        .all()
    )
    theirs: set[str] = set()
    running: set[int] = set()
    for target_id, job_id, target_status, job_status in rows:
        job_dir = upload_jobs.job_folder(data_dir, job_id)
        theirs.update(_journaled_seeded(job_dir, target_id).values())
        if job_status == "running" or target_status in upload_jobs.BUSY_TARGET_STATES:
            running.add(job_id)
    for job_id in running:
        theirs |= _journaled_content(upload_jobs.job_folder(data_dir, job_id))
    return {path for path in paths if os.path.realpath(path) in theirs}


def _source_files(path: str) -> list[str]:
    if not os.path.isdir(path):
        return [path]
    return [os.path.join(folder, name) for folder, _dirs, files in os.walk(path, followlinks=False) for name in files]


# File che i sistemi operativi lasciano nelle cartelle: mai di un utente.
_SYSTEM_JUNK = ("Thumbs.db", "desktop.ini", ".DS_Store")


def _is_system_junk(name: str) -> bool:
    return name in _SYSTEM_JUNK or name.startswith("._")


def _has_seeding_copy(path: str, st: os.stat_result, copies: set[str] | None) -> bool:
    """Una delle copie registrate esiste ancora adesso: file regolare, stesso
    dispositivo e inode, percorso diverso; e il file ha davvero un secondo
    link. Mai fidarsi del solo registro: un link tolto nel frattempo (la
    pulizia, l'utente) lascerebbe l'originale come unica copia."""
    if st.st_nlink < 2:
        return False
    real = os.path.realpath(path)
    for copy in copies or ():
        try:
            cst = os.stat(copy, follow_symlinks=False)
        except OSError:
            continue
        if stat.S_ISREG(cst.st_mode) and (cst.st_dev, cst.st_ino) == (st.st_dev, st.st_ino) \
                and os.path.realpath(copy) != real:
            return True
    return False


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
    cartelle vuote spariscono. Solo se si mette in seed e se la sorgente è
    davvero dentro la cartella osservata.

    Solo quando ogni tracker (gli skip non contano) è andato (decisione
    dell'utente, 2026-10-18; prima bastava il primo): con uno fallito la
    release resta dov'è, così "Riprova" ha ancora i suoi file, e se ne va
    alla fine del tentativo che porta a 'done' anche l'ultimo. Le copie in
    seed dei tracker andati in un'esecuzione precedente contano come quelle
    di questa (_seeded_by_done, dal file del job).

    La copia si ricontrolla subito prima di togliere l'originale (audit
    2026-10-18): deve esistere ancora, essere un file vero con lo stesso
    inode in un altro percorso, e seedare per un tracker andato a buon fine;
    se no l'originale resta."""
    if job.origin != "watch" or overrides.get("no_seed") or job.disk is None:
        return
    targets = [t for t in job.targets if t.action != "skip"]
    if not any(t.status == "done" for t in targets):
        return
    watch = upload_watch.watch_root(job.disk)
    source = os.path.realpath(job.source_path)
    if watch is None or not source.startswith(os.path.realpath(watch) + os.sep):
        return
    waiting = [t for t in targets if t.status != "done"]
    if waiting:
        upload_jobs.log_event(session, job, "watch_source_kept",
                              trackers=", ".join(t.tracker.label for t in waiting))
        session.commit()
        return
    seeded = _seeded_by_done(job, ctx or {})
    moved, kept = [], []
    for path in _source_files(source):
        try:
            st = os.stat(path, follow_symlinks=False)
        except OSError:
            continue
        linked = not os.path.islink(path) and _has_seeding_copy(path, st, seeded.get((st.st_dev, st.st_ino)))
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
    """Un episodio di una stagione divisa, o una stagione di una cartella con
    più stagioni (nazgarr/upload/split.py): quando nella sua cartella della
    cartella osservata non resta più nessun episodio da caricare, la
    cartella si toglie con quello che resta (nfo, sample...), decisione
    dell'utente del 2026-10-09. Un video ancora lì (l'upload di un
    altro episodio non è finito, o è stato annullato) o un file ancora in
    scrittura la lasciano dov'è. Mai per un job con un tracker fallito: si
    arriva qui solo dopo che _clear_watch_source ha tolto la sorgente, cioè
    con ogni tracker andato (decisione dell'utente, 2026-10-18).

    Si tolgono solo i file appena controllati, poi le cartelle rimaste vuote
    (audit 2026-10-18): prima un rmtree, che cancellava anche un file
    arrivato nel frattempo e, se un file non si poteva togliere, faceva
    fallire un upload già pubblicato e in seed. Quello che resta si dice nel
    registro."""
    root = os.path.realpath(watch)
    relative = os.path.relpath(os.path.realpath(job.source_path), root)
    parts = relative.split(os.sep)
    if parts[0] in ("", ".", "..") or (len(parts) < 2 and not job.is_dir):
        return  # un file direttamente nella cartella osservata: nessuna cartella sua
    folder = os.path.join(root, parts[0])
    if os.path.islink(folder) or not os.path.isdir(folder):
        return
    left, paths = [], []
    for directory, _dirs, files in os.walk(folder, followlinks=False):
        for name in files:
            path = os.path.join(directory, name)
            inside = os.path.relpath(path, folder)
            try:
                size = os.lstat(path).st_size
            except OSError:
                continue
            if upload_watch.is_partial(name) or (is_video(name) and upload_inventory.in_torrent(inside, size)):
                return
            left.append(inside)
            paths.append(path)
    errors = []
    for path in paths:
        try:
            os.unlink(path)
        except OSError as exc:
            errors.append(str(exc))
    for directory, _dirs, _files in sorted(os.walk(folder, followlinks=False), key=lambda entry: -len(entry[0])):
        with contextlib.suppress(OSError):
            os.rmdir(directory)  # solo se vuota
    if os.path.lexists(folder):
        remaining = sorted(os.path.relpath(os.path.join(d, n), folder)
                           for d, dirs, files in os.walk(folder, followlinks=False) for n in (*files, *dirs))
        upload_jobs.log_event(session, job, "watch_folder_remove_failed", level="warning", folder=parts[0],
                              count=len(remaining), files=remaining[:10], error=errors[0] if errors else None)
    else:
        upload_jobs.log_event(session, job, "watch_folder_removed", folder=parts[0], count=len(left),
                              files=left[:10])
    session.commit()


def _recheck_dupes(session: Session, job: UploadJob, targets: list[UploadTarget]) -> None:
    """Un upload programmato parte ore o giorni dopo la decisione: prima di
    tutto il dupe check di ogni tracker si rifà, e un torrent comparso nel
    frattempo (identico, o nello stesso slot) ferma l'upload su quel tracker.
    Quelli che c'erano già alla decisione li ha visti l'utente. Un reseed no:
    il torrent è già quello del tracker. Se il tracker non risponde si va
    avanti: l'invio fallirebbe comunque, e il registro lo dice.

    Lo stesso per un nuovo tentativo (upload_jobs.retry_failed): anche il
    nostro torrent di un invio interrotto ferma quel tracker. Sempre i
    risultati di adesso, non quelli tenuti in cache dalla ricerca
    dell'analisi (10 minuti)."""
    from nazgarr.upload import analysis as upload_analysis
    from nazgarr.upload import dupes as upload_dupes

    uploads = [t for t in targets if t.action == "upload" and t.status == "approved"]
    if not uploads:
        return
    try:
        files = upload_analysis.source_files(job)
    except OSError:
        return  # se ne accorge la preparazione, con il suo errore
    summary = upload_analysis.summary_of(job, files, json.loads(job.analysis_json or "{}"),
                                         upload_analysis.source_values(session, job))
    threshold = settings_registry.get_int(session, upload_dupes.THRESHOLD_SETTING)
    for target in uploads:
        known = {d.get("torrent_id_remote") for d in json.loads(target.dupes_json or "[]")}
        try:
            categories, keys = upload_analysis.profile_maps(session, target.tracker_id)
            with adapter_factory.tracker(target.tracker) as adapter:
                _forget_search(adapter, job.tmdb_id)
                found = adapter.search_by_tmdb(job.tmdb_id)
                results, _suggested = upload_dupes.check(
                    upload_analysis.same_content(job, found, categories), summary, keys, threshold)
        except Exception as exc:
            logger.warning("Upload %s: dupe check prima dell'invio fallito su %s", job.id, target.tracker.label,
                           exc_info=True)
            upload_jobs.log_event(session, job, "dupe_recheck_failed", level="warning", target=target,
                                  error=safe_error(exc))
            continue
        if catalog_truncated(found):
            # Come alla decisione (analysis.py): il tracker ha più risultati di
            # quelli letti, e un dupe oltre il limite non si vede (audit
            # 2026-10-18). Si va avanti, ma il registro lo dice.
            upload_jobs.log_event(session, job, "dupe_recheck_truncated", level="warning", target=target,
                                  results=len(found))
        # Un identico, o un dupe vivo (come suggest): un dupe morto, un trump o
        # un possibile dupe non fermano l'upload, come alla decisione.
        new = [r for r in results if upload_dupes.blocks_upload(r) and r["torrent_id_remote"] not in known]
        if new:
            upload_jobs.set_target_status(target, upload_jobs.TargetStatus.FAILED)
            target.error_message = "upload_dupe_appeared"
            target.finished_at = datetime.now(UTC)
            upload_jobs.log_event(session, job, "upload_dupe_appeared", level="error", target=target,
                                  names=[r["name"] for r in new[:3]])
    session.commit()


def _forget_search(adapter, tmdb_id: int | None) -> None:
    """Toglie dalla cache dell'adapter (UNIT3D: _search_cache, comune agli
    adapter dello stesso tracker) la ricerca di questo contenuto."""
    cache = getattr(adapter, "_search_cache", None)
    if isinstance(cache, dict):
        cache.pop(tmdb_id, None)


def _prepare(session: Session, job: UploadJob, worker, ctx: dict, targets: list[UploadTarget]) -> None:
    """Una volta per job, se c'è almeno un upload: il torrent (hash dei
    piece) e gli screenshot. Senza, nessun upload può partire; i reseed sì."""
    if not any(t.action == "upload" and t.status == "approved" for t in targets):
        return
    overrides = ctx["overrides"]
    try:
        content = prepare_content(session, job, ctx)
        ctx["torrent"] = hash_pieces(session, job, content, ctx.get("planned"))
        # Per la barra degli step (ProgressStep): i passaggi conclusi restano segnati.
        upload_jobs.log_event(session, job, "torrent_created")
        session.commit()
        stored = json.loads(job.screenshot_urls_json or "[]") if ctx.get("retried") else []
        if stored:
            # Un nuovo tentativo: quelli già sull'image host valgono ancora,
            # niente di nuovo da caricare per un tracker che era giù.
            ctx["screenshots"] = stored
        else:
            ctx["screenshots"] = take_screenshots(session, job, worker, _screenshot_count(session, overrides))
            job.screenshot_urls_json = json.dumps(ctx["screenshots"])
        upload_jobs.log_event(session, job, "screenshots_done", count=len(ctx["screenshots"]),
                              **({"reused": True} if stored else {}))
        session.commit()
    except _Cancelled:
        raise
    except Exception as exc:
        # Un errore qualunque (un OSError creando gli hardlink, torf che non
        # legge il disco, un plugin dell'image host), non solo i nostri:
        # prima gli altri uscivano da qui e il job finiva senza dire perché.
        if not isinstance(exc, UploadJobError):
            logger.error("Upload %s: preparazione fallita", job.id, exc_info=exc)
        _fail_approved(session, job, targets, exc, actions=("upload",), default="upload_prepare_failed")


def _screenshot_count(session: Session, overrides: dict) -> int:
    count = overrides.get("screenshot_count")
    if count is None:
        return settings_registry.get_int(session, "upload_screenshot_count")
    try:
        return max(0, min(int(count), MAX_SCREENSHOTS))
    except (TypeError, ValueError):
        return settings_registry.get_int(session, "upload_screenshot_count")


def _fail_approved(session: Session, job: UploadJob, targets: list[UploadTarget], exc: Exception,
                   actions: tuple[str, ...], default: str) -> None:
    """I target ancora da fare (di quelle azioni) falliscono tutti con
    l'errore che li ferma, nel registro con i suoi parametri."""
    session.rollback()
    code, params = upload_jobs.error_of(exc)
    if not isinstance(exc, CodedError):
        code = default
    for target in targets:
        if target.action in actions and target.status == "approved":
            upload_jobs.set_target_status(target, upload_jobs.TargetStatus.FAILED)
            target.error_message = code
            target.finished_at = datetime.now(UTC)
            upload_jobs.log_event(session, job, code, level="error", target=target, **params)
    session.commit()


def _run_target(session: Session, job: UploadJob, target: UploadTarget, ctx: dict) -> None:
    _progress(session, job, f"tracker:{target.tracker.label}")
    _forget_seeded(ctx, target)
    try:
        if target.action == "upload":
            run_upload(session, job, target, ctx)
        else:
            run_reseed(session, job, target, ctx)
        upload_jobs.set_target_status(target, upload_jobs.TargetStatus.DONE)
    except Exception as exc:
        session.rollback()
        logger.warning("Upload %s: %s fallito su %s", job.id, target.action, target.tracker.label, exc_info=True)
        code, params = upload_jobs.error_of(exc)
        published = (ctx.get("published") or {}).get(target.id)
        if published is not None:
            # Il tracker l'ha ricevuto (anche se il DB non l'ha ancora
            # scritto): pubblicato, non in seed. Mai 'failed', che si potrebbe
            # riprovare con un secondo upload; il seed si riprova da solo.
            target.torrent_id_remote = published
            upload_jobs.set_target_status(target, upload_jobs.TargetStatus.DONE)
            target.error_message = "seed_failed"
            upload_jobs.log_event(session, job, "seed_failed", level="error", target=target, error=code)
        elif target.action == "upload" and target.id in (ctx.get("sent") or ()) and _maybe_received(exc):
            # La richiesta era partita: il tracker può averlo salvato anche
            # senza una risposta buona. Si riprova solo dopo averlo
            # controllato sul tracker (upload_jobs.needs_confirmation), come
            # un invio fermato da un riavvio (audit 2026-10-18).
            upload_jobs.set_target_status(target, upload_jobs.TargetStatus.FAILED)
            target.error_message = "upload_outcome_unknown"
            upload_jobs.log_event(session, job, "upload_outcome_unknown", level="error", target=target,
                                  error=code, **{k: v for k, v in params.items() if k != "error"})
        else:
            upload_jobs.set_target_status(target, upload_jobs.TargetStatus.FAILED)
            target.error_message = code
            upload_jobs.log_event(session, job, f"{target.action}_failed", level="error", target=target,
                                  error=code, **{k: v for k, v in params.items() if k != "error"})
    target.finished_at = datetime.now(UTC)
    session.commit()


def _maybe_received(exc: BaseException) -> bool:
    """Un errore dopo che la richiesta di upload era partita: un timeout
    sulla risposta, un 5xx dopo il salvataggio, una risposta che non si
    legge ("nessun id riconoscibile" arriva con success: true) possono
    venire da un torrent che il tracker ha salvato. Dicono di no con
    certezza solo una connessione mai aperta e un 4xx (il tracker l'ha
    rifiutata), quando l'adapter li lascia vedere (raise ... from exc,
    come UNIT3D); un plugin che non lo fa resta fra gli incerti."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)):
            return False
        if isinstance(current, httpx.HTTPStatusError) and 400 <= current.response.status_code < 500:
            return False
        current = current.__cause__ or current.__context__
    return True


def _housekeeping(session: Session, job: UploadJob, ctx: dict, overrides: dict, cancelled: bool = False) -> None:
    """Gli hardlink che non seedano niente e la cartella osservata. Mai un
    motivo per cambiare l'esito del job (audit 2026-10-18): un upload
    pubblicato e in seed finiva 'failed' se un file della cartella osservata
    non si poteva togliere. Annullato: solo gli hardlink (la cartella
    osservata resta com'è)."""
    steps = [("links", lambda: _cleanup_unused_links(session, job, ctx, overrides))]
    if not cancelled:
        steps.append(("watch", lambda: _clear_watch_source(session, job, overrides, ctx)))
    for what, step in steps:
        try:
            step()
        except Exception as exc:
            session.rollback()
            logger.warning("Upload %s: pulizia (%s) non riuscita", job.id, what, exc_info=True)
            with contextlib.suppress(Exception):
                upload_jobs.log_event(session, job, "upload_cleanup_failed", level="warning", step=what,
                                      error=upload_jobs.error_text(exc))
                session.commit()


def release_links(session: Session, job: UploadJob, data_dir: str) -> None:
    """Prima di eliminare un job (upload_jobs.delete_job): gli hardlink che ha
    creato per i nomi del torrent (il file del job) e che non seedano niente
    si tolgono, con le regole della pulizia di fine esecuzione
    (_cleanup_unused_links: mai con un upload riuscito in seed, mai uno da
    cui seeda un altro tracker o un torrent del client, solo se è ancora lo
    stesso file). Prima restavano nella cartella di seed per sempre. Un
    errore qui si scrive nel log: il job si elimina lo stesso."""
    job_dir = upload_jobs.job_folder(data_dir, job.id)
    journal, at = _journaled_links(job_dir)
    if not journal:
        return
    ctx = {"dir": job_dir, "data_dir": data_dir, "seed_root": lambda: seed_root(job),
           # Senza un'ora (un file di prima), ogni reseed del motore vivo con quell'inode conta.
           "links_at": at or datetime(1970, 1, 1, tzinfo=UTC)}
    try:
        _cleanup_unused_links(session, job, ctx, json.loads(job.overrides_json or "{}"))
    except Exception:
        session.rollback()
        logger.warning("Upload %s: hardlink del job non tolti all'eliminazione", job.id, exc_info=True)


def _context(session: Session, job: UploadJob, worker, overrides: dict) -> dict:
    ctx: dict = {"dir": _job_dir(worker, job), "data_dir": worker.data_dir, "overrides": overrides,
                 "screenshots": []}
    root_cache: list[str] = []

    def lazy_seed_root() -> str:
        if not root_cache:
            root_cache.append(seed_root(job))
        return root_cache[0]

    ctx["seed_root"] = lazy_seed_root
    return ctx


def handle(session: Session, job: UploadJob, worker) -> None:
    if not upload_jobs.transition(session, job, "queued", "running", queue_position=None):
        return
    retried = upload_jobs.is_retry(job)
    upload_jobs.log_event(session, job, "execution_started")
    session.commit()
    targets = [t for t in job.targets if t.status == "approved"]
    if job.scheduled_at is not None or retried:
        _recheck_dupes(session, job, targets)
    overrides = json.loads(job.overrides_json or "{}")
    try:
        ctx = _context(session, job, worker, overrides)
        ctx["retried"] = retried
    except Exception as exc:  # la cartella del job non si crea: niente può partire
        logger.error("Upload %s: cartella del job non disponibile", job.id, exc_info=exc)
        _fail_approved(session, job, targets, exc, actions=("upload", "reseed"), default="upload_prepare_failed")
        ctx = {"overrides": overrides}
    else:
        try:
            _prepare(session, job, worker, ctx, targets)
            for target in targets:
                session.refresh(job)
                if job.status == "cancelled":
                    raise _Cancelled()
                if target.status == "approved":
                    _run_target(session, job, target, ctx)
        except _Cancelled:
            # Annullato a metà: gli hardlink creati e non usati si tolgono
            # come alla fine (audit 2026-10-18), prima restavano nella
            # cartella di seed senza che nessuno li seedasse.
            _housekeeping(session, job, ctx, overrides, cancelled=True)
            return
        _housekeeping(session, job, ctx, overrides)
    final = upload_jobs.outcome(job)
    if upload_jobs.transition(session, job, "running", final, stage=None, progress_done=None, progress_total=None):
        upload_jobs.log_event(session, job, f"job_{final}", level="info" if final == "done" else "warning")
        session.commit()
