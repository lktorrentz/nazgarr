"""Le cartelle media e di seeding di un disco (decisione dell'utente,
2026-10-03): più di una per tipo, in disk_folder.

Una cartella si aggiunge solo se:
- è dentro il disco (resolve_scoped), esiste, e non è il disco intero;
- sta sullo stesso filesystem del disco: gli hardlink non attraversano i
  filesystem, e una cartella montata altrove va aggiunta come disco a sé;
- non coincide con un'altra cartella del disco, non ne contiene una e non
  sta dentro un'altra (una media dentro una di seeding conterebbe due volte
  gli stessi file);
- non sta dentro la cartella osservata e non coincide con essa (da dove le
  release se ne vanno); contenerla sì: una sottocartella dedicata (es.
  torrents/watch) è il caso normale.

Lo stesso vale per la cartella dei nuovi hardlink di un reseed e per quella
degli upload (validate_target), e al contrario la cartella osservata non
può contenere nessuna di queste (nazgarr/api/disks.py::_watch_folder).

Togliere una cartella non tocca niente sul disco: i suoi file escono dalla
libreria alla scansione successiva.
"""

import contextlib
import os
import secrets

from sqlalchemy.orm import Session

from nazgarr.core import mounts
from nazgarr.core.errors import CodedError
from nazgarr.core.fs_scope import ScopeViolation, resolve_scoped
from nazgarr.core.models import DISK_FOLDER_KINDS, Disk, DiskFolder
from nazgarr.library import hardlinks


class FolderError(CodedError):
    pass


def _inside_or_same(path: str, other: str) -> bool:
    return path == other or path.startswith(other + os.sep)


def validate(disk: Disk, kind: str, relative: str) -> str:
    """Il percorso relativo normalizzato, o FolderError con il motivo."""
    if kind not in DISK_FOLDER_KINDS:
        raise FolderError("folder_kind_invalid", kind=kind)
    try:
        path = resolve_scoped(disk.root_path, relative)
    except ScopeViolation as exc:
        raise FolderError("path_outside_scope", path=relative) from exc
    root = os.path.realpath(disk.root_path)
    if path == root:
        raise FolderError("folder_is_disk_root", path=relative)
    if not os.path.isdir(path):
        raise FolderError("folder_not_found", path=relative)
    if os.stat(path).st_dev != os.stat(root).st_dev:
        raise FolderError("folder_other_filesystem", path=relative)
    normalized = os.path.relpath(path, root)
    for folder in disk.folders:
        other = os.path.realpath(os.path.join(root, folder.relative_path))
        if folder.kind == kind and other == path:
            raise FolderError("folder_already_added", path=normalized)
        if _inside_or_same(path, other) or _inside_or_same(other, path):
            raise FolderError("folder_overlaps", path=normalized, other=folder.relative_path, kind=folder.kind)
    _outside_watch(disk, root, path, normalized)
    return normalized


def validate_target(disk: Disk, relative: str) -> str:
    """La cartella dei nuovi hardlink di un reseed o quella degli upload:
    esistente, sullo stesso filesystem del disco e dentro (o uguale a) una
    cartella dei torrent, se no i file che il client mette in seed lì non
    si collegherebbero a niente. Prima si salvava qualunque valore e
    l'errore arrivava solo all'esecuzione."""
    try:
        path = resolve_scoped(disk.root_path, relative)
    except ScopeViolation as exc:
        raise FolderError("path_outside_scope", path=relative) from exc
    root = os.path.realpath(disk.root_path)
    if not os.path.isdir(path):
        raise FolderError("folder_not_found", path=relative)
    if os.stat(path).st_dev != os.stat(root).st_dev:
        raise FolderError("folder_other_filesystem", path=relative)
    normalized = os.path.relpath(path, root)
    seeding = [os.path.realpath(os.path.join(root, f)) for f in disk.seeding_folders]
    if not any(_inside_or_same(path, folder) for folder in seeding):
        raise FolderError("folder_not_in_seeding", path=normalized)
    _outside_watch(disk, root, path, normalized)
    return normalized


def _outside_watch(disk: Disk, root: str, path: str, normalized: str) -> None:
    """Mai dentro la cartella osservata, né la stessa (audit 2026-10-18): ogni
    reseed o upload lì dentro partirebbe come una release, e approvarla
    toglierebbe i file da cui un torrent seeda."""
    if disk.watch_rel_path:
        watch = os.path.realpath(os.path.join(root, disk.watch_rel_path))
        if _inside_or_same(path, watch):
            raise FolderError("folder_overlaps", path=normalized, other=disk.watch_rel_path, kind="watch")


def add(session: Session, disk: Disk, kind: str, relative: str) -> DiskFolder:
    _materialize_legacy(disk)
    normalized = validate(disk, kind, relative)
    folder = DiskFolder(disk=disk, kind=kind, relative_path=normalized)
    session.add(folder)
    session.commit()
    return folder


def remove(session: Session, disk: Disk, folder_id: int) -> bool:
    folder = next((f for f in disk.folders if f.id == folder_id), None)
    if folder is None:
        return False
    disk.folders.remove(folder)
    session.commit()
    return True


def replace(session: Session, disk: Disk, kind: str, relative: str | None) -> None:
    """Le API di prima (media_rel_path, torrents_rel_path): una sola cartella
    di quel tipo al posto di quelle che c'erano, o nessuna con "" / None."""
    _materialize_legacy(disk)
    kept = [f for f in disk.folders if f.kind != kind]
    previous = [f for f in disk.folders if f.kind == kind]
    disk.folders = kept
    session.flush()
    if relative:
        try:
            normalized = validate(disk, kind, relative)
        except FolderError:
            disk.folders = kept + previous
            raise
        disk.folders.append(DiskFolder(kind=kind, relative_path=normalized))
    session.commit()


def _materialize_legacy(disk: Disk) -> None:
    """Un disco non ancora migrato (creato in codice con i campi vecchi)
    porta in disk_folder le sue cartelle prima di cambiarle."""
    for kind, legacy in (("media", disk.media_rel_path), ("seeding", disk.torrents_rel_path)):
        if legacy and not any(f.kind == kind for f in disk.folders):
            disk.folders.append(DiskFolder(kind=kind, relative_path=legacy))
    disk.media_rel_path = disk.torrents_rel_path = None


def absolute(disk: Disk, kind: str) -> list[str]:
    """I percorsi assoluti (risolti) delle cartelle di quel tipo che stanno
    dentro il disco; una fuori scope si salta."""
    out = []
    for relative in disk.media_folders if kind == "media" else disk.seeding_folders:
        try:
            out.append(resolve_scoped(disk.root_path, relative))
        except ScopeViolation:
            continue
    return out


# --- prova del disco ----------------------------------------------------------

TEST_PREFIX = ".nazgarr-link-test-"


def _check(code: str, level: str = "ok", **params) -> dict:
    return {"code": code, "level": level, "params": params}


# Dove gli hardlink riescono ma i due nomi hanno inode diversi, e cosa fare:
# Nazgarr riconosce gli hardlink dall'inode (segnalato da un utente,
# 2026-11-22, prima era un "not the same file" che non diceva niente).
_SMB_FSTYPES = {"cifs", "smb3", "smbfs"}
_VIRTUAL_FSTYPES = {"9p", "virtiofs", "fakeowner"}


def _inode_hint(fstype: str | None) -> str:
    """smb: montata senza serverino (o il kernel l'ha spento); mergerfs:
    inodecalc=path-hash; fuse: un FUSE o un filesystem virtuale che non
    mantiene gli inode; other: non si sa, i casi noti nel messaggio."""
    if fstype in _SMB_FSTYPES:
        return "smb"
    if fstype == "fuse.mergerfs":
        return "mergerfs"
    if fstype and (fstype == "fuse" or fstype.startswith("fuse.") or fstype in _VIRTUAL_FSTYPES):
        return "fuse"
    return "other"


def _inode_mismatch(source: str, relative: str, folder: str) -> dict:
    """Il link c'è (link() riuscito), ma il filesystem mostra i due nomi con
    inode diversi: per Nazgarr non sarebbero lo stesso file."""
    fstype = mounts.fstype_of(folder)
    return _check("hardlink_inode_mismatch", "error", source=source, folder=relative, fstype=fstype or "",
                  hint=_inode_hint(fstype))


def _link_test(anchor_relative: str, anchor: str, targets: list[tuple[str, str]]) -> list[dict]:
    """Un file vuoto nella prima cartella e un hardlink verso ogni altra:
    la prova che gli hardlink funzionano davvero fra quelle cartelle (uno
    stesso st_dev da solo non lo garantisce: un mount di rete o un bind di
    un'altra cartella). Sulla user share di Unraid (/mnt/user, shfs)
    funzionano: shfs crea il link sullo stesso disco fisico del file. File
    nascosti, tolti subito, e solo quelli creati qui (audit 2026-10-18:
    prima un link fallito con EEXIST toglieva il file che c'era già, e due
    cartelle salvate con nomi diversi per la stessa cartella toglievano il
    file di prova prima del tempo, con un 500)."""
    name = TEST_PREFIX + secrets.token_hex(4)
    source = os.path.join(anchor, name)
    checks = []
    try:
        with open(source, "x"):
            pass
    except OSError as exc:
        return [_check("link_test_write_failed", "error", folder=anchor_relative, error=exc.strerror or str(exc))]
    anchor_real = os.path.realpath(anchor)
    try:
        for relative, folder in targets:
            same_folder = os.path.realpath(folder) == anchor_real
            target = os.path.join(os.path.realpath(folder), name + "-link" if same_folder else name)
            created: list[str] = []
            try:
                # Attraverso hardlinks.py come ogni hardlink (mai un link simbolico seguito).
                created = hardlinks.create_links([(source, target)])
                if os.stat(target).st_ino == os.stat(source).st_ino:
                    checks.append(_check("hardlink_ok", source=anchor_relative, folder=relative, error=None))
                else:
                    checks.append(_inode_mismatch(anchor_relative, relative, folder))
            except OSError as exc:
                checks.append(_check("hardlink_failed", "error", source=anchor_relative, folder=relative,
                                     error=exc.strerror or str(exc)))
            finally:
                hardlinks.remove_links(created)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(source)
    return checks


def test_disk(session: Session, disk: Disk) -> dict:
    """La prova del disco, chiesta dall'utente (Configurazione › Storage):
    - il disco è raggiungibile;
    - ogni cartella esiste e sta sul filesystem del disco;
    - un hardlink di prova dalla prima cartella di seeding (o media) verso
      ogni altra cartella, o dentro la stessa se è l'unica, funziona;
    - st_dev rispetto a quello registrato: su FUSE cambia a ogni rimontaggio,
      quindi da solo è solo un avviso; se gli hardlink funzionano, il valore
      registrato si aggiorna.
    Scrive solo file vuoti nascosti, tolti subito."""
    root = os.path.realpath(disk.root_path)
    if not os.path.isdir(root):
        return {"ok": False, "checks": [_check("disk_unreachable", "error", path=disk.root_path)]}
    root_dev = os.stat(root).st_dev
    checks: list[dict] = []
    usable: list[tuple[str, str]] = []
    for kind, folders in (("seeding", disk.seeding_folders), ("media", disk.media_folders)):
        for relative in folders:
            try:
                path = resolve_scoped(disk.root_path, relative)  # una cartella salvata prima delle regole
            except ScopeViolation:
                path = None
            if path is None or not os.path.isdir(path):
                checks.append(_check("folder_missing", "error", folder=relative, kind=kind))
            elif os.stat(path).st_dev != root_dev:
                checks.append(_check("folder_other_filesystem", "error", folder=relative, kind=kind))
            else:
                usable.append((relative, path))
    if usable:
        anchor_relative, anchor = usable[0]
        targets = usable[1:] or [usable[0]]
        checks.extend(_link_test(anchor_relative, anchor, targets))
    else:
        checks.append(_check("no_folders", "warning"))
    links_ok = usable and all(c["level"] != "error" for c in checks)
    if disk.st_dev is None:
        disk.st_dev = root_dev
    elif disk.st_dev != root_dev:
        updated = bool(links_ok)
        checks.append(_check("st_dev_changed", "warning", old=disk.st_dev, new=root_dev, updated=updated))
        if updated:
            disk.st_dev = root_dev
    session.commit()
    return {"ok": all(c["level"] != "error" for c in checks), "checks": checks}
