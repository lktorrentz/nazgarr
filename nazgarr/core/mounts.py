"""Le cartelle di dati montate nel container, e il confine dei dischi
(decisione dell'utente, 2026-10-05).

Dentro Docker /proc/self/mountinfo elenca esattamente quello che l'utente
ha montato nel template: per ogni mount il percorso nel container, il
filesystem (xfs, btrfs, ext4, fuse.shfs per la user share di Unraid...) e
il dispositivo (major:minor: due mount con lo stesso sono lo stesso
filesystem). Tolti quelli di sistema e la cartella di Nazgarr stessa,
restano le cartelle dei dati: il confine entro cui si può registrare un
disco, che quindi decide il template Docker e non un'impostazione
modificabile da un browser.

disk_scan_root in config.yaml resta, facoltativo: se c'è, è il confine (le
installazioni di prima non cambiano). Fuori da un container (pipx, macOS)
lo scrive `nazgarr init`; senza, /data.

Due avvisi, perché sono errori di montaggio che Nazgarr vede e l'utente no:
- split_mounts: due mount dello stesso filesystem (es. /data/media e
  /data/torrents montati separatamente, anche dalla share di Unraid). Linux
  rifiuta un hardlink fra due mount diversi anche sullo stesso disco: va
  montata la cartella comune;
- share_and_disks: la user share di Unraid (shfs) e dei dischi singoli
  montati insieme: gli stessi file si vedrebbero due volte. Gli hardlink
  sulla share invece funzionano (shfs li crea sullo stesso disco fisico).
"""

import os
import re
from dataclasses import dataclass, field

MOUNTINFO = "/proc/self/mountinfo"
DEFAULT_ROOT = "/data"

SYSTEM_FSTYPES = {
    "proc", "sysfs", "tmpfs", "devtmpfs", "devpts", "mqueue", "cgroup", "cgroup2", "securityfs", "debugfs",
    "tracefs", "pstore", "bpf", "autofs", "hugetlbfs", "configfs", "fusectl", "overlay", "nsfs", "binfmt_misc",
    "ramfs", "rpc_pipefs", "efivarfs", "selinuxfs",
}
SYSTEM_PREFIXES = ("/proc", "/sys", "/dev", "/run", "/etc", "/usr", "/bin", "/sbin", "/lib", "/app")


@dataclass(frozen=True)
class Mount:
    mount_point: str
    root: str  # la cartella della sorgente montata qui (es. /data dentro una share)
    fstype: str
    source: str
    device: str  # major:minor

    @property
    def is_unraid_share(self) -> bool:
        return self.fstype == "fuse.shfs" or self.source == "shfs"


def _inside(path: str, folder: str) -> bool:
    return path == folder or path.startswith(folder.rstrip(os.sep) + os.sep)


@dataclass
class Scope:
    roots: list[str]  # dove può stare un disco
    source: str  # "config" (disk_scan_root) | "mounts" (rilevati) | "default"
    mounts: list[Mount] = field(default_factory=list)  # le cartelle di dati rilevate
    warnings: list[dict] = field(default_factory=list)
    # Le cartelle di Nazgarr (config.yaml e secret.key, data_dir col DB), risolte.
    own_dirs: list[str] = field(default_factory=list)
    data_dir: str | None = None

    def contains(self, path: str) -> bool:
        real = os.path.realpath(path)
        return any(_inside(real, os.path.realpath(root)) for root in self.roots)

    def app_folder_in(self, path: str) -> str | None:
        """La cartella di Nazgarr che un disco con questa radice
        esporrebbe (al browser, a un upload che la mette in un torrent):
        una cartella sua uguale alla radice o dentro, o la data_dir che
        contiene la radice (è tutta di Nazgarr). Con disk_scan_root ampio
        (la home, per un'installazione pipx) un disco poteva contenerle
        (audit 2026-10-18). Una radice dentro la cartella del config resta
        possibile: quella cartella può essere una qualunque (config.yaml
        nella cartella di lavoro)."""
        real = os.path.realpath(path)
        for own in self.own_dirs:
            if _inside(own, real):
                return own
        if self.data_dir and _inside(real, self.data_dir):
            return self.data_dir
        return None


def _unescape(value: str) -> str:
    # mountinfo codifica spazi, tab e barre rovesciate in ottale: \040 = spazio.
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), value)


def parse_mountinfo(text: str) -> list[Mount]:
    mounts = []
    for line in text.splitlines():
        left, sep, right = line.partition(" - ")
        if not sep:
            continue
        fields, tail = left.split(), right.split()
        if len(fields) < 5 or len(tail) < 2:
            continue
        mounts.append(Mount(mount_point=_unescape(fields[4]), root=_unescape(fields[3]), fstype=tail[0],
                            source=_unescape(tail[1]), device=fields[2]))
    return mounts


def fstype_of(path: str, mounts: list[Mount] | None = None) -> str | None:
    """Il filesystem su cui sta un percorso: quello del mount più profondo
    che lo contiene. None se non si sa (niente /proc, es. macOS)."""
    real = os.path.realpath(path)
    found = [m for m in (read_mounts() if mounts is None else mounts) if _inside(real, m.mount_point)]
    return max(found, key=lambda m: len(m.mount_point)).fstype if found else None


def in_container() -> bool:
    return os.environ.get("NAZGARR_CONTAINER") == "1" or os.path.exists("/.dockerenv")


def data_mounts(mounts: list[Mount], own_dirs: list[str] = ()) -> list[Mount]:
    """Le cartelle di dati: niente filesystem di sistema, niente cartelle
    di sistema o dell'app, niente file montati (es. /etc/hosts), niente
    cartella di Nazgarr (config e data_dir)."""
    own = [os.path.realpath(d) for d in own_dirs if d]
    out, seen = [], set()
    for m in mounts:
        point = m.mount_point
        if point == "/" or m.fstype in SYSTEM_FSTYPES or point in seen:
            continue
        if any(point == p or point.startswith(p + "/") for p in SYSTEM_PREFIXES):
            continue
        if any(point == d or point.startswith(d + "/") or d.startswith(point + "/") for d in own):
            continue
        if not os.path.isdir(point):
            continue
        seen.add(point)
        out.append(m)
    return sorted(out, key=lambda m: m.mount_point)


def mount_warnings(mounts: list[Mount]) -> list[dict]:
    warnings = []
    by_device: dict[str, list[Mount]] = {}
    for m in mounts:
        # Anche per la share di Unraid: dentro un suo mount gli hardlink
        # funzionano, ma fra due mount diversi no (il limite è del kernel).
        by_device.setdefault(m.device, []).append(m)
    for group in by_device.values():
        if len(group) > 1:
            warnings.append({"code": "split_mounts", "paths": [m.mount_point for m in group]})
    share = [m.mount_point for m in mounts if m.is_unraid_share]
    others = [m.mount_point for m in mounts if not m.is_unraid_share]
    if share and others:
        warnings.append({"code": "share_and_disks", "share": share, "disks": others})
    return warnings


def read_mounts() -> list[Mount]:
    try:
        with open(MOUNTINFO) as f:
            return parse_mountinfo(f.read())
    except OSError:
        return []


def scope(configured_root: str | None, own_dirs: list[str] = (), mounts: list[Mount] | None = None,
          container: bool | None = None, data_dir: str | None = None) -> Scope:
    """Il confine dei dischi: disk_scan_root se impostato, se no le cartelle
    di dati montate nel container, se no /data. I mount rilevati (e i loro
    avvisi) si riportano comunque: servono ai suggerimenti. own_dirs (e
    data_dir, che ne fa parte) sono le cartelle di Nazgarr: mai un disco
    che le contiene, in ogni modo (Scope.app_folder_in)."""
    container = in_container() if container is None else container
    every_own = [d for d in (*own_dirs, data_dir) if d]
    detected = data_mounts(read_mounts() if mounts is None else mounts, every_own) if container else []
    warnings = mount_warnings(detected)
    own = {"own_dirs": [os.path.realpath(d) for d in every_own],
           "data_dir": os.path.realpath(data_dir) if data_dir else None}
    if configured_root:
        outside = [m.mount_point for m in detected
                   if not Scope([configured_root], "config").contains(m.mount_point)]
        if outside:
            warnings.append({"code": "mounts_outside_scan_root", "scan_root": configured_root, "paths": outside})
        return Scope([configured_root], "config", detected, warnings, **own)
    if detected:
        return Scope([m.mount_point for m in detected], "mounts", detected, warnings, **own)
    return Scope([DEFAULT_ROOT], "default", detected, warnings, **own)
