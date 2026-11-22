"""Cartelle di dati montate nel container e confine dei dischi (nazgarr/core/mounts.py)."""

from nazgarr.core import mounts


def _line(mount_point, fstype="xfs", source="/dev/md1p1", device="9:1", root="/"):
    return f"100 50 {device} {root} {mount_point} rw,relatime master:1 - {fstype} {source} rw"


SYSTEM = [
    "20 1 0:30 / / rw - overlay overlay rw,lowerdir=/x",
    "21 20 0:31 / /proc rw - proc proc rw",
    "22 20 0:32 / /dev rw - tmpfs tmpfs rw",
    _line("/etc/hosts", device="9:9", root="/containers/hosts"),
]


def test_mountinfo_is_parsed_with_its_escapes():
    [m] = mounts.parse_mountinfo(_line("/data/My\\040Disk", fstype="fuse.shfs", source="shfs", device="0:50",
                                       root="/data"))
    assert (m.mount_point, m.root, m.fstype, m.source, m.device) == ("/data/My Disk", "/data", "fuse.shfs",
                                                                       "shfs", "0:50")
    assert m.is_unraid_share


def test_only_the_data_folders_are_kept(tmp_path):
    data, config = tmp_path / "data", tmp_path / "config"
    data.mkdir()
    config.mkdir()
    parsed = mounts.parse_mountinfo("\n".join([*SYSTEM, _line(str(data)), _line(str(config), device="9:2")]))

    kept = mounts.data_mounts(parsed, own_dirs=[str(config)])

    assert [m.mount_point for m in kept] == [str(data)]


def test_trash_single_mount_is_the_scope(tmp_path):
    """(i) Un solo mount, /data: è il confine, e un disco basta."""
    data = tmp_path / "data"
    data.mkdir()
    scope = mounts.scope(None, mounts=mounts.parse_mountinfo(_line(str(data))), container=True)
    assert (scope.source, scope.roots, scope.warnings) == ("mounts", [str(data)], [])
    assert scope.contains(str(data / "torrents")) and not scope.contains(str(tmp_path))


def test_unraid_disks_are_two_roots(tmp_path):
    """(iii) /mnt/disk1 e /mnt/disk2: due confini, nessun disk_scan_root da scrivere."""
    disk1, disk2 = tmp_path / "disk1", tmp_path / "disk2"
    disk1.mkdir()
    disk2.mkdir()
    text = "\n".join([_line(str(disk1), device="9:1"), _line(str(disk2), device="9:2")])
    scope = mounts.scope(None, mounts=mounts.parse_mountinfo(text), container=True)
    assert (scope.roots, scope.warnings) == ([str(disk1), str(disk2)], [])


def test_media_and_torrents_mounted_separately_are_flagged(tmp_path):
    """Lo stesso filesystem in due mount: Linux rifiuta l'hardlink fra mount diversi."""
    media, torrents = tmp_path / "media", tmp_path / "torrents"
    media.mkdir()
    torrents.mkdir()
    text = "\n".join([_line(str(media), device="8:1", root="/data/media"),
                      _line(str(torrents), device="8:1", root="/data/torrents")])
    scope = mounts.scope(None, mounts=mounts.parse_mountinfo(text), container=True)
    assert scope.warnings == [{"code": "split_mounts", "paths": [str(media), str(torrents)]}]


def test_the_unraid_share_in_one_mount_is_fine_but_not_split_or_with_the_disks(tmp_path):
    share, disk1, media, torrents = (tmp_path / n for n in ("share", "disk1", "media", "torrents"))
    for folder in (share, disk1, media, torrents):
        folder.mkdir()

    def share_line(path, root="/data"):
        return _line(str(path), fstype="fuse.shfs", source="shfs", device="0:50", root=root)

    # Un mount della share: gli hardlink funzionano (shfs li crea sullo stesso disco). Nessun avviso.
    alone = mounts.scope(None, mounts=mounts.parse_mountinfo(share_line(share)), container=True)
    assert alone.warnings == []
    # Due cartelle della share in due mount: fra mount diversi il kernel rifiuta l'hardlink.
    split = mounts.scope(None, mounts=mounts.parse_mountinfo(
        "\n".join([share_line(media, "/data/media"), share_line(torrents, "/data/torrents")])), container=True)
    assert [w["code"] for w in split.warnings] == ["split_mounts"]
    both = mounts.scope(None, mounts=mounts.parse_mountinfo("\n".join([share_line(share), _line(str(disk1))])),
                        container=True)
    assert both.warnings == [{"code": "share_and_disks", "share": [str(share)], "disks": [str(disk1)]}]


def test_a_configured_scan_root_wins_and_says_what_is_left_out(tmp_path):
    data, other = tmp_path / "data", tmp_path / "other"
    data.mkdir()
    other.mkdir()
    text = "\n".join([_line(str(data), device="9:1"), _line(str(other), device="9:2")])
    scope = mounts.scope(str(data), mounts=mounts.parse_mountinfo(text), container=True)
    assert (scope.source, scope.roots) == ("config", [str(data)])
    assert scope.warnings == [{"code": "mounts_outside_scan_root", "scan_root": str(data), "paths": [str(other)]}]


def test_outside_a_container_nothing_is_detected():
    scope = mounts.scope(None, mounts=mounts.parse_mountinfo(_line("/data")), container=False)
    assert (scope.source, scope.roots, scope.mounts) == ("default", ["/data"], [])


def test_nested_disks_are_refused(client):
    root = client.scan_root / "data"
    (root / "torrents").mkdir(parents=True)
    assert client.post("/api/disks", json={"label": "main", "root_path": str(root)}).status_code == 201
    nested = client.post("/api/disks", json={"label": "inner", "root_path": str(root / "torrents")})
    assert nested.status_code == 400 and nested.json()["detail"]["code"] == "disk_root_nested"
    body = client.get("/api/disks/available-mounts").json()
    assert body["scope_source"] == "config" and body["mounts"] == []


def test_nazgarr_own_folders_are_never_inside_a_disk(tmp_path):
    home = tmp_path / "home"
    config, data = home / ".config" / "nazgarr", home / ".local" / "share" / "nazgarr"
    for folder in (config, data, home / "media"):
        folder.mkdir(parents=True)
    scope = mounts.scope(str(home), [str(config)], container=False, data_dir=str(data))

    assert scope.app_folder_in(str(home)) == str(config.resolve())  # la radice le contiene
    assert scope.app_folder_in(str(home / ".local")) == str(data.resolve())
    assert scope.app_folder_in(str(data / "posters")) == str(data.resolve())  # dentro data_dir: è tutta sua
    assert scope.app_folder_in(str(home / "media")) is None
    assert scope.contains(str(home / "media"))


def test_the_data_folder_given_apart_is_still_left_out_of_the_mounts(tmp_path):
    data, media = tmp_path / "appdata", tmp_path / "media"
    data.mkdir()
    media.mkdir()
    text = "\n".join([_line(str(data), device="9:1"), _line(str(media), device="9:2")])
    scope = mounts.scope(None, [], mounts=mounts.parse_mountinfo(text), container=True, data_dir=str(data))
    assert [m.mount_point for m in scope.mounts] == [str(media)]


def test_the_filesystem_of_a_path_is_the_one_of_the_deepest_mount_holding_it(tmp_path):
    media = tmp_path / "mnt" / "media"
    (media / "DownloadArr").mkdir(parents=True)
    parsed = mounts.parse_mountinfo("\n".join([
        *SYSTEM, _line(str(tmp_path / "mnt"), fstype="ext4", device="8:1"),
        _line(str(media), fstype="cifs", source="//nas/data", device="0:60"),
    ]))
    assert mounts.fstype_of(str(media / "DownloadArr"), parsed) == "cifs"
    assert mounts.fstype_of(str(tmp_path / "mnt" / "other"), parsed) == "ext4"
    assert mounts.fstype_of(str(tmp_path / "mnt" / "media2"), parsed) == "ext4"  # non è dentro /mnt/media
    assert mounts.fstype_of(str(media), []) is None
