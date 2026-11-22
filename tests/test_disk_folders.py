import os
import shutil
import types
from pathlib import Path

import pytest
from sqlalchemy import text

from nazgarr.core import db as db_module
from nazgarr.core import mounts
from nazgarr.core.models import Disk, MediaFile, SeedFile
from nazgarr.library import disk_folders, scanner
from nazgarr.library.disk_folders import FolderError
from nazgarr.library.scan_state import is_current, latest_scan_by_disk
from nazgarr.reseed import pipeline


@pytest.fixture
def disk(db_session, tmp_path):
    root = tmp_path / "data"
    for folder in ("movies", "tv", "torrents", "cross-seed", "releases", "torrents/watch"):
        (root / folder).mkdir(parents=True, exist_ok=True)
    disk = Disk(label="d", root_path=str(root))
    db_session.add(disk)
    db_session.commit()
    return disk


def test_more_media_and_seeding_folders_on_one_disk(db_session, disk):
    for kind, path in (("media", "movies"), ("media", "tv"), ("seeding", "torrents"), ("seeding", "cross-seed")):
        disk_folders.add(db_session, disk, kind, path)
    assert (disk.media_folders, disk.seeding_folders) == (["movies", "tv"], ["torrents", "cross-seed"])
    # Le cartelle nuove vanno nella prima di seeding, se non c'è una cartella apposta.
    assert disk.effective_new_torrent_rel_path == disk.effective_upload_rel_path == "torrents"


@pytest.mark.parametrize(("kind", "path", "code"), [
    ("media", "../outside", "path_outside_scope"),
    ("media", "", "folder_is_disk_root"),
    ("media", "missing", "folder_not_found"),
    ("media", "movies", "folder_already_added"),
    ("seeding", "movies", "folder_overlaps"),          # la stessa cartella come media e come seeding
    ("media", "movies/sub", "folder_overlaps"),       # dentro un'altra
    ("seeding", "torrents/watch", "folder_overlaps"),  # è la cartella osservata (né dentro, né la stessa)
    ("other", "tv", "folder_kind_invalid"),
])
def test_a_folder_is_refused_when(db_session, disk, kind, path, code):
    (disk_root := disk.root_path) and os.makedirs(os.path.join(disk_root, "movies", "sub"), exist_ok=True)
    disk_folders.add(db_session, disk, "media", "movies")
    disk.watch_rel_path = "torrents/watch"
    db_session.commit()
    with pytest.raises(FolderError) as exc:
        disk_folders.add(db_session, disk, kind, path)
    assert exc.value.code == code


def test_a_folder_on_another_filesystem_is_refused(db_session, disk, monkeypatch):
    real_stat = os.stat

    def fake_stat(path, *args, **kwargs):
        st = real_stat(path, *args, **kwargs)
        return types.SimpleNamespace(st_dev=st.st_dev + 1, st_mode=st.st_mode) if str(path).endswith("tv") else st

    monkeypatch.setattr(disk_folders.os, "stat", fake_stat)
    with pytest.raises(FolderError) as exc:
        disk_folders.add(db_session, disk, "media", "tv")
    assert exc.value.code == "folder_other_filesystem"


def test_the_old_single_folders_move_to_the_new_table(db_session, disk):
    disk.media_rel_path, disk.torrents_rel_path = "movies", "torrents"
    db_session.commit()
    engine = db_session.get_bind()

    assert db_module.migrate_disk_folders(engine) == 2
    assert db_module.migrate_disk_folders(engine) == 0  # idempotente
    db_session.expire_all()
    assert (disk.media_folders, disk.seeding_folders) == (["movies"], ["torrents"])
    assert disk.media_rel_path is None and disk.torrents_rel_path is None
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM disk_folder")).scalar() == 2


def _current(db_session, model):
    latest = latest_scan_by_disk(db_session, model)
    return sorted(row.relative_path for row in db_session.query(model).all() if is_current(row, latest))


def test_the_scan_reads_every_folder_and_keeps_an_unreachable_one(db_session, disk):
    root = disk.root_path
    for kind, path in (("media", "movies"), ("media", "tv"), ("seeding", "torrents"), ("seeding", "cross-seed")):
        disk_folders.add(db_session, disk, kind, path)
    Path(root, "movies", "a.mkv").write_bytes(b"a" * 100)
    Path(root, "tv", "b.mkv").write_bytes(b"b" * 100)
    Path(root, "torrents", "old.mkv").write_bytes(b"o" * 100)
    os.link(os.path.join(root, "tv", "b.mkv"), os.path.join(root, "cross-seed", "b.mkv"))
    Path(root, "releases", "not-a-folder.mkv").write_bytes(b"r" * 100)

    scanner.scan_disk(db_session, disk, pipeline.start_run(db_session, "manual"))

    assert _current(db_session, MediaFile) == ["movies/a.mkv", "tv/b.mkv"]
    assert _current(db_session, SeedFile) == ["cross-seed/b.mkv", "torrents/old.mkv"]
    linked = db_session.query(SeedFile).filter_by(relative_path="cross-seed/b.mkv").one()
    assert linked.media_file.relative_path == "tv/b.mkv"  # hardlink fra cartelle diverse

    # La share "tv" non è montata, e un file sparisce da "movies": i file di
    # "tv" restano, quello sparito no.
    os.rename(os.path.join(root, "tv"), os.path.join(root, "tv-offline"))
    os.remove(os.path.join(root, "movies", "a.mkv"))
    scanner.scan_disk(db_session, disk, pipeline.start_run(db_session, "manual"))
    assert _current(db_session, MediaFile) == ["tv/b.mkv"]

    # Una cartella tolta dalla configurazione: i suoi file escono dalla libreria.
    folder = next(f for f in disk.folders if f.relative_path == "cross-seed")
    disk_folders.remove(db_session, disk, folder.id)
    scanner.scan_disk(db_session, disk, pipeline.start_run(db_session, "manual"))
    assert _current(db_session, SeedFile) == ["torrents/old.mkv"]


def test_api_adds_and_removes_folders(client):
    root = client.scan_root / "disk1"
    for folder in ("movies", "tv", "torrents"):
        (root / folder).mkdir(parents=True)
    disk_id = client.post("/api/disks", json={"label": "Disk 1", "root_path": str(root)}).json()["id"]

    for kind, path in (("media", "movies"), ("media", "tv"), ("seeding", "torrents")):
        response = client.post(f"/api/disks/{disk_id}/folders", json={"kind": kind, "relative_path": path})
        assert response.status_code == 201, response.text
    body = response.json()
    assert (body["media_folders"], body["seeding_folders"]) == (["movies", "tv"], ["torrents"])
    assert body["media_rel_path"] == "movies"  # deprecato: la prima

    refused = client.post(f"/api/disks/{disk_id}/folders", json={"kind": "seeding", "relative_path": "movies"})
    assert refused.status_code == 400 and refused.json()["detail"]["code"] == "folder_overlaps"

    tv = next(f for f in body["folders"] if f["relative_path"] == "tv")
    body = client.delete(f"/api/disks/{disk_id}/folders/{tv['id']}").json()
    assert body["media_folders"] == ["movies"]
    assert client.delete(f"/api/disks/{disk_id}/folders/999").status_code == 404


def _codes(result):
    return [(c["code"], c["level"]) for c in result["checks"]]


def test_the_disk_test_links_a_file_between_every_folder_and_leaves_nothing(db_session, disk):
    for kind, path in (("seeding", "torrents"), ("seeding", "cross-seed"), ("media", "movies")):
        disk_folders.add(db_session, disk, kind, path)

    result = disk_folders.test_disk(db_session, disk)

    assert result["ok"] is True
    assert _codes(result) == [("hardlink_ok", "ok"), ("hardlink_ok", "ok")]
    assert [c["params"]["folder"] for c in result["checks"]] == ["cross-seed", "movies"]
    leftovers = [n for _d, _s, names in os.walk(disk.root_path) for n in names if n.startswith(".nazgarr-link-test")]
    assert leftovers == []


def test_the_disk_test_says_what_is_wrong(db_session, disk, monkeypatch):
    disk_folders.add(db_session, disk, "seeding", "torrents")
    disk_folders.add(db_session, disk, "media", "movies")
    os.rmdir(os.path.join(disk.root_path, "movies"))

    def no_links(*args, **kwargs):
        raise OSError(18, "Invalid cross-device link")

    result = disk_folders.test_disk(db_session, disk)
    assert ("folder_missing", "error") in _codes(result) and result["ok"] is False

    os.mkdir(os.path.join(disk.root_path, "movies"))
    monkeypatch.setattr(disk_folders.os, "link", no_links)
    result = disk_folders.test_disk(db_session, disk)
    failed = next(c for c in result["checks"] if c["code"] == "hardlink_failed")
    assert failed["params"] == {"source": "torrents", "folder": "movies", "error": "Invalid cross-device link"}


def _copy_instead_of_linking(pairs):
    """Come un filesystem che mostra inode diversi per due nomi dello stesso
    file (SMB senza serverino): link() riesce, gli inode non coincidono."""
    created = []
    for source, target in pairs:
        shutil.copyfile(source, target)
        created.append(target)
    return created


def test_a_link_with_different_inodes_says_why_from_the_filesystem(db_session, disk, monkeypatch):
    """Segnalato da un utente (2026-11-22): "not the same file" non diceva
    niente. Il link c'è, ma Nazgarr riconosce gli hardlink dall'inode."""
    disk_folders.add(db_session, disk, "seeding", "torrents")
    monkeypatch.setattr(disk_folders.hardlinks, "create_links", _copy_instead_of_linking)
    cifs = mounts.Mount(mount_point=disk.root_path, root="/", fstype="cifs", source="//nas/data", device="0:60")
    monkeypatch.setattr(disk_folders.mounts, "read_mounts", lambda: [cifs])

    result = disk_folders.test_disk(db_session, disk)

    assert result["ok"] is False
    [check] = [c for c in result["checks"] if c["code"] == "hardlink_inode_mismatch"]
    assert check["level"] == "error"
    assert check["params"] == {"source": "torrents", "folder": "torrents", "fstype": "cifs", "hint": "smb"}
    leftovers = [n for n in os.listdir(os.path.join(disk.root_path, "torrents")) if n.startswith(".nazgarr")]
    assert leftovers == []

    monkeypatch.setattr(disk_folders.mounts, "read_mounts", lambda: [])  # niente /proc: non si sa
    [check] = [c for c in disk_folders.test_disk(db_session, disk)["checks"] if c["code"] == "hardlink_inode_mismatch"]
    assert (check["params"]["fstype"], check["params"]["hint"]) == ("", "other")


@pytest.mark.parametrize(("fstype", "hint"), [
    ("cifs", "smb"), ("smb3", "smb"), ("fuse.mergerfs", "mergerfs"), ("fuse.rclone", "fuse"),
    ("fuse.sshfs", "fuse"), ("9p", "fuse"), ("virtiofs", "fuse"), ("nfs4", "other"), ("xfs", "other"), (None, "other"),
])
def test_the_inode_hint_follows_the_filesystem(fstype, hint):
    assert disk_folders._inode_hint(fstype) == hint


def test_a_changed_st_dev_alone_is_only_a_warning_and_is_updated_when_links_work(db_session, disk):
    disk_folders.add(db_session, disk, "seeding", "torrents")
    disk.st_dev = 12345  # come dopo un rimontaggio FUSE
    db_session.commit()

    result = disk_folders.test_disk(db_session, disk)

    assert result["ok"] is True
    changed = next(c for c in result["checks"] if c["code"] == "st_dev_changed")
    assert changed["level"] == "warning" and changed["params"]["updated"] is True
    assert disk.st_dev == os.stat(disk.root_path).st_dev
    assert ("hardlink_ok", "ok") in _codes(result)  # l'unica cartella: un link dentro la stessa


def test_reseed_and_upload_folders_never_go_inside_the_watched_folder(db_session, disk):
    """Prima si controllava solo al momento di scegliere la cartella
    osservata, mai dopo (audit 2026-10-18): una cartella degli upload dentro
    quella osservata diventava una release, e approvarla toglieva i file da
    cui seedavano gli upload precedenti."""
    os.makedirs(os.path.join(disk.root_path, "torrents", "watch", "out"))
    disk_folders.add(db_session, disk, "seeding", "torrents")
    disk.watch_rel_path = "torrents/watch"
    db_session.commit()
    for path in ("torrents/watch", "torrents/watch/out"):
        with pytest.raises(FolderError) as exc:
            disk_folders.validate_target(disk, path)
        assert (exc.value.code, exc.value.params["kind"]) == ("folder_overlaps", "watch")
    assert disk_folders.validate_target(disk, "torrents") == "torrents"  # contenerla va bene


def test_the_disk_test_never_removes_a_file_it_did_not_create(db_session, disk, monkeypatch):
    """Un link fallito con EEXIST toglieva il file che c'era già (audit
    2026-10-18): si toglie solo quello che la prova ha creato."""
    disk_folders.add(db_session, disk, "seeding", "torrents")
    disk_folders.add(db_session, disk, "media", "movies")
    monkeypatch.setattr(disk_folders.secrets, "token_hex", lambda n: "deadbeef")
    planted = os.path.join(disk.root_path, "movies", disk_folders.TEST_PREFIX + "deadbeef")
    with open(planted, "w") as f:
        f.write("not nazgarr's")

    result = disk_folders.test_disk(db_session, disk)

    assert ("hardlink_failed", "error") in _codes(result)
    with open(planted) as f:
        assert f.read() == "not nazgarr's"
    leftovers = [n for n in os.listdir(os.path.join(disk.root_path, "torrents")) if n.startswith(".nazgarr")]
    assert leftovers == []


def test_the_same_folder_saved_twice_is_tested_without_an_error(db_session, disk):
    """Due righe vecchie per la stessa cartella ("torrents" e "torrents/",
    copiate dalla migrazione senza validate): la prova toglieva il proprio
    file prima del tempo e finiva in un 500."""
    from nazgarr.core.models import DiskFolder

    disk.folders.extend([DiskFolder(kind="seeding", relative_path="torrents"),
                         DiskFolder(kind="seeding", relative_path="torrents/")])
    db_session.commit()

    result = disk_folders.test_disk(db_session, disk)

    assert _codes(result) == [("hardlink_ok", "ok")]
    assert not [n for n in os.listdir(os.path.join(disk.root_path, "torrents")) if n.startswith(".nazgarr")]


def test_a_stored_folder_outside_the_disk_is_reported_not_written(db_session, disk, tmp_path):
    from nazgarr.core.models import DiskFolder

    (tmp_path / "elsewhere").mkdir()
    disk.folders.extend([DiskFolder(kind="seeding", relative_path="torrents"),
                         DiskFolder(kind="media", relative_path="../elsewhere")])
    db_session.commit()

    result = disk_folders.test_disk(db_session, disk)

    assert ("folder_missing", "error") in _codes(result)
    assert os.listdir(tmp_path / "elsewhere") == []
