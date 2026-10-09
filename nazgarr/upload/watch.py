"""La cartella osservata per le release (decisione dell'utente, 2026-10-02):
ogni file video o cartella nuovo nella cartella "watch" di un disco fa
partire da solo un upload verso i tracker con un profilo di upload, con il
nome del releaser come gruppo e il match TMDB automatico sopra soglia
(nazgarr/upload/identify.py). Arriva fino alla decisione e lì aspetta l'utente:
mai un upload, un hardlink o un torrent senza la sua approvazione.

Un elemento è pronto appena nessuno ci scrive più (decisione dell'utente,
2026-10-02: la cartella è solo di passaggio, si parte subito): niente file
parziali dentro, e l'ultima modifica è di almeno QUIET_SECONDS fa. Un file
spostato dentro (stessa data di modifica di prima) parte al primo giro; una
copia, che aggiorna la data mentre scrive, QUIET_SECONDS dopo la fine. La
dimensione deve anche essere quella del giro prima. Si controlla ogni
INTERVAL_SECONDS: niente eventi del filesystem (inotify), che sulle share
FUSE di Unraid, su NFS e SMB non arrivano sempre. Ogni elemento visto è in
watch_entry: una release fa partire un solo upload, anche dopo aver
cancellato il suo job.

Il percorso passa sempre da resolve_scoped (nazgarr/core/fs_scope.py); i link
simbolici non si seguono."""

import logging
import os
from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from nazgarr.core import events, settings_repo
from nazgarr.core.file_types import is_video
from nazgarr.core.fs_scope import ScopeViolation, resolve_scoped
from nazgarr.core.models import Disk, WatchEntry
from nazgarr.upload import jobs as upload_jobs
from nazgarr.upload.jobs import UploadJobError

logger = logging.getLogger(__name__)

INTERVAL_SECONDS = 10
QUIET_SECONDS = 15
RELEASER_SETTING = "upload_releaser_name"
# File ancora in scrittura (download, copia) secondo i client e i programmi più comuni.
PARTIAL_SUFFIXES = (".part", ".!qb", ".!ut", ".tmp", ".crdownload", ".partial", ".filepart")


def releaser_name(session: Session) -> str | None:
    return (settings_repo.get_setting(session, RELEASER_SETTING) or "").strip() or None


def is_partial(name: str) -> bool:
    return name.lower().endswith(PARTIAL_SUFFIXES)


def _signature(path: str) -> tuple[int, float, bool]:
    """(byte totali, ultima modifica, ha file parziali) di un file o di una cartella."""
    if not os.path.isdir(path):
        st = os.stat(path, follow_symlinks=False)
        return st.st_size, st.st_mtime, is_partial(path)
    size, mtime, partial = 0, os.stat(path, follow_symlinks=False).st_mtime, False
    for folder, dirs, files in os.walk(path, followlinks=False):
        for name in dirs:
            mtime = max(mtime, os.stat(os.path.join(folder, name), follow_symlinks=False).st_mtime)
        for name in files:
            st = os.stat(os.path.join(folder, name), follow_symlinks=False)
            size += st.st_size
            mtime = max(mtime, st.st_mtime)
            partial = partial or is_partial(name)
    return size, mtime, partial


def _entries(root: str) -> list[str]:
    """Gli elementi di primo livello: cartelle e file video, niente nascosti né link."""
    out = []
    with os.scandir(root) as listing:
        for entry in listing:
            if entry.name.startswith(".") or entry.is_symlink():
                continue
            if entry.is_dir(follow_symlinks=False) or (entry.is_file(follow_symlinks=False) and is_video(entry.name)):
                out.append(entry.path)
    return sorted(out)


def watch_root(disk: Disk) -> str | None:
    if not disk.watch_rel_path:
        return None
    try:
        root = resolve_scoped(disk.root_path, disk.watch_rel_path)
    except ScopeViolation:
        logger.warning("Cartella osservata fuori dal disco %r: %s", disk.label, disk.watch_rel_path)
        return None
    return root if os.path.isdir(root) else None


def scan(session: Session, kick: Callable[[int, str], None] | None = None, now: datetime | None = None) -> list[int]:
    """Un giro su tutte le cartelle osservate: gli id dei job creati."""
    now = now or datetime.now(UTC)
    releaser = releaser_name(session)
    created = []
    for disk in session.query(Disk).filter(Disk.watch_rel_path.isnot(None)).all():
        root = watch_root(disk)
        if root is None:
            continue
        known = {row.relative_path: row for row in session.query(WatchEntry).filter(WatchEntry.disk_id == disk.id)}
        present = set()
        for path in _entries(root):
            relative = os.path.relpath(path, disk.root_path)
            present.add(relative)
            row = known.get(relative)
            if row is not None and row.started_at is not None:
                continue  # upload già partito: niente da rileggere (la firma visita tutto l'albero)
            try:
                size, mtime, partial = _signature(path)
            except OSError:
                continue  # sparito o illeggibile mentre lo si leggeva: al prossimo giro
            if row is None:
                row = WatchEntry(disk_id=disk.id, relative_path=relative, size_bytes=size, mtime=mtime,
                                 stable_since=now)
                session.add(row)
                session.flush()
                unchanged = True  # visto ora: pronto se nessuno ci scrive (spostato dentro)
            else:
                unchanged = (row.size_bytes, row.mtime) == (size, mtime)
                if not unchanged:
                    row.size_bytes, row.mtime, row.stable_since = size, mtime, now
            quiet = now.timestamp() - mtime >= QUIET_SECONDS
            if partial or size == 0 or not unchanged or not quiet:
                continue
            session.commit()  # gli elementi nuovi di questo giro restano anche se l'upload non parte
            try:
                job = upload_jobs.create_job(
                    session, disk, relative, overrides={"group": releaser} if releaser else None, origin="watch"
                )
            except UploadJobError as exc:
                if exc.code == "upload_no_trackers":
                    continue  # nessun tracker con un profilo, per ora: si riprova
                session.rollback()
                row = session.get(WatchEntry, row.id)
                row.started_at, row.error_message = now, exc.code
                logger.warning("Cartella osservata: upload di %r non creato (%s)", relative, exc.code)
                continue
            except ScopeViolation:
                session.rollback()
                continue
            row = session.get(WatchEntry, row.id)
            row.job_id, row.started_at = job.id, now
            created.append(job.id)
            logger.info("Cartella osservata: upload #%s avviato per %r", job.id, relative)
            events.emit(session, "upload.detected", {"upload_id": job.id, "path": relative, "disk": disk.label})
            session.commit()
            if kick is not None:
                kick(job.id, job.status)
        # Uscito prima di partire (spostato, cancellato): se torna, si riparte da capo.
        for relative, row in known.items():
            if relative not in present and row.started_at is None:
                session.delete(row)
        session.commit()
    return created
