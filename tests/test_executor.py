import hashlib
import os
from datetime import UTC, datetime

import pytest

from nazgarr.adapters.torrent_client.base import TorrentStatus
from nazgarr.core.models import (
    Candidate,
    Disk,
    DiskTorrentClient,
    MatchReview,
    MediaFile,
    MediaItem,
    SeedFile,
    SeedJob,
    TorrentClient,
    Tracker,
)
from nazgarr.reseed import executor, pipeline
from nazgarr.torrents.metainfo import compute_info_hash
from tests.fakes import RecordingClient
from tests.test_season_pack import _bencode


def _movie_torrent(content: bytes = b"content", name: str = "Movie.2024.mkv") -> bytes:
    return _bencode({"info": {"name": name, "piece length": 16, "length": len(content),
                              "pieces": hashlib.sha1(content).digest()}})


# La funzione vera, prima che il fixture qui sotto la sostituisca in ogni test.
_REAL_DOWNLOAD_TORRENT = executor._download_torrent


@pytest.fixture(autouse=True)
def tracker_torrent(monkeypatch):
    """Il .torrent che l'esecutore scarica dal tracker (mai il link al
    client): quello del film dei test, salvo dove un test lo cambia."""
    served = {"content": _movie_torrent(), "links": []}

    def download(candidate):
        served["links"].append(candidate.download_link)
        return served["content"]

    monkeypatch.setattr(executor, "_download_torrent", download)
    return served


class ReseedClient(RecordingClient):
    """Il client di questi test: RecordingClient (tests/fakes.py), il
    contratto come lo seguono gli adapter inclusi (aggiunge fermo, sa
    saltare il recheck, ha content_layout), con l'info hash "deadbeef".
    Prima era un finto suo, fuori dal contratto e senza content_layout né
    stopped: l'esecutore prendeva la strada dei plugin vecchi, che un
    adapter incluso non prende mai (audit 2026-10-18, finding 151).

    Le aggiunte sono in added (AddedTorrent). Lo stato, se status non è
    dato, è quello di un torrent aggiunto fermo: fermo dopo il recheck
    (recheck_status, progress) finché start(), che start_error fa fallire."""

    def __init__(self, info_hash="deadbeef", recheck_status="ok", progress=1.0, start_error=None, **kwargs):
        super().__init__(info_hash=info_hash, **kwargs)
        self.recheck_status, self.progress, self.start_error = recheck_status, progress, start_error

    def get_torrent_status(self, info_hash):
        if self.status is not None:
            return self.status
        done = self.progress >= 1.0
        state = ("uploading" if done else "downloading") if self.started else ("stoppedUP" if done else "stoppedDL")
        return TorrentStatus(info_hash=info_hash, state=state, recheck_status=self.recheck_status,
                             progress=self.progress, incomplete=not done, amount_left=0)

    def start(self, info_hash):
        if self.start_error is not None:
            raise self.start_error
        super().start(info_hash)


def _make_disk_media_path(tmp_path):
    root = tmp_path / "disk1"
    (root / "media" / "movies").mkdir(parents=True)
    (root / "torrents").mkdir(parents=True)
    return root


def _base_setup(db_session, tmp_path):
    root = _make_disk_media_path(tmp_path)
    disk = Disk(label="d", root_path=str(root), media_rel_path="media/movies", torrents_rel_path="torrents")
    db_session.add(disk)
    db_session.commit()
    tracker = Tracker(label="t", adapter_type="unit3d", base_url="https://t.example", api_token="x")
    db_session.add(tracker)
    db_session.commit()
    item = MediaItem(content_type="movie", tmdb_id=1)
    db_session.add(item)
    db_session.commit()
    run = pipeline.start_run(db_session, "manual")
    return root, disk, tracker, item, run


def test_execute_media_to_torrent_creates_hardlink_and_adds_torrent(db_session, tmp_path):
    root, disk, tracker, item, run = _base_setup(db_session, tmp_path)

    media_file_path = root / "media" / "movies" / "Movie.2024.mkv"
    media_file_path.write_bytes(b"content")
    media_file = MediaFile(
        disk_id=disk.id, relative_path="media/movies/Movie.2024.mkv", size_bytes=7,
        st_dev=1, inode=1, media_item_id=item.id, last_scan_id=run.id, last_seen_at=datetime.now(UTC),
    )
    db_session.add(media_file)
    db_session.commit()

    candidate = Candidate(
        media_item_id=item.id, tracker_id=tracker.id, torrent_id_remote="1", name="Movie.2024.mkv",
        size_bytes=7, source="catalog_search", direction="media_to_torrent", confidence=1.0,
        download_link="https://t.example/dl/1", file_list_json='["Movie.2024.mkv"]',
    )
    db_session.add(candidate)
    db_session.commit()
    match_review = MatchReview(candidate_id=candidate.id, media_file_id=media_file.id, status="approved")
    db_session.add(match_review)
    db_session.commit()

    adapter = ReseedClient()
    seed_job = executor.execute_review(db_session, match_review, adapter)

    expected_link = root / "torrents" / "Movie.2024.mkv"
    assert expected_link.is_file()
    assert os.path.samefile(media_file_path, expected_link)
    assert seed_job.hardlink_created_at is not None
    assert seed_job.info_hash == "deadbeef"
    assert seed_job.recheck_status == "pending"
    assert len(adapter.added) == 1 and adapter.added[0].options["stopped"] is True  # fermo fino al recheck


def _movie_review(db_session, tmp_path, folder=None):
    root, disk, tracker, item, run = _base_setup(db_session, tmp_path)
    (root / "media" / "movies" / "Movie.2024.mkv").write_bytes(b"content")
    media_file = MediaFile(
        disk_id=disk.id, relative_path="media/movies/Movie.2024.mkv", size_bytes=7,
        st_dev=1, inode=1, media_item_id=item.id, last_scan_id=run.id, last_seen_at=datetime.now(UTC),
    )
    db_session.add(media_file)
    db_session.commit()
    candidate = Candidate(
        media_item_id=item.id, tracker_id=tracker.id, torrent_id_remote="1", name="Movie.2024.mkv",
        size_bytes=7, source="catalog_search", direction="media_to_torrent", confidence=1.0,
        download_link="https://t.example/dl/1", file_list_json='["Movie.2024.mkv"]', folder=folder,
    )
    db_session.add(candidate)
    db_session.commit()
    review = MatchReview(candidate_id=candidate.id, media_file_id=media_file.id, status="approved")
    db_session.add(review)
    db_session.commit()
    return root, review


def test_a_client_that_creates_subfolders_gets_the_file_inside_its_folder(db_session, tmp_path):
    # Il caso segnalato: qBittorrent con "Crea sottocartella" cercava il file
    # in torrents/Movie.2024/, l'hardlink era in torrents/ e partiva il download.
    root, review = _movie_review(db_session, tmp_path)
    adapter = ReseedClient(layout="Subfolder")  # "Layout del contenuto" scelto dall'utente

    executor.execute_review(db_session, review, adapter)

    assert (root / "torrents" / "Movie.2024" / "Movie.2024.mkv").is_file()
    assert not (root / "torrents" / "Movie.2024.mkv").exists()
    assert adapter.added[0].save_path == str(root / "torrents" / "Movie.2024")
    assert adapter.added[0].options["content_layout"] == "Original"


def test_without_subfolders_the_root_folder_of_the_torrent_is_left_out(db_session, tmp_path):
    root, review = _movie_review(db_session, tmp_path, folder="Release.Name")
    adapter = ReseedClient(layout="NoSubfolder")

    executor.execute_review(db_session, review, adapter)

    assert (root / "torrents" / "Movie.2024.mkv").is_file()
    call = adapter.added[0]
    assert (call.save_path, call.options["content_layout"]) == (str(root / "torrents"), "NoSubfolder")


def test_a_torrent_with_its_own_folder_ignores_the_subfolder_preference(db_session, tmp_path):
    root, review = _movie_review(db_session, tmp_path, folder="Release.Name")
    adapter = ReseedClient(layout="Subfolder")

    executor.execute_review(db_session, review, adapter)

    assert (root / "torrents" / "Release.Name" / "Movie.2024.mkv").is_file()
    assert adapter.added[0].save_path == str(root / "torrents")
    assert adapter.added[0].options["content_layout"] == "Original"


def test_execute_media_to_torrent_respects_candidate_folder(db_session, tmp_path):
    root, disk, tracker, item, run = _base_setup(db_session, tmp_path)
    media_file_path = root / "media" / "movies" / "Movie.2024.mkv"
    media_file_path.write_bytes(b"content")
    media_file = MediaFile(
        disk_id=disk.id, relative_path="media/movies/Movie.2024.mkv", size_bytes=7,
        st_dev=1, inode=1, media_item_id=item.id, last_scan_id=run.id, last_seen_at=datetime.now(UTC),
    )
    db_session.add(media_file)
    db_session.commit()
    candidate = Candidate(
        media_item_id=item.id, tracker_id=tracker.id, torrent_id_remote="1", name="Movie.2024.mkv",
        size_bytes=7, source="catalog_search", direction="media_to_torrent", confidence=1.0,
        download_link="https://t.example/dl/1", file_list_json='["Movie.2024.mkv"]', folder="Release.Name",
    )
    db_session.add(candidate)
    db_session.commit()
    match_review = MatchReview(candidate_id=candidate.id, media_file_id=media_file.id, status="approved")
    db_session.add(match_review)
    db_session.commit()

    executor.execute_review(db_session, match_review, ReseedClient())

    assert (root / "torrents" / "Release.Name" / "Movie.2024.mkv").is_file()


def test_execute_media_to_torrent_fails_on_cross_device(db_session, tmp_path, monkeypatch):
    root, disk, tracker, item, run = _base_setup(db_session, tmp_path)
    media_file_path = root / "media" / "movies" / "Movie.2024.mkv"
    media_file_path.write_bytes(b"content")
    media_file = MediaFile(
        disk_id=disk.id, relative_path="media/movies/Movie.2024.mkv", size_bytes=7,
        st_dev=1, inode=1, media_item_id=item.id, last_scan_id=run.id, last_seen_at=datetime.now(UTC),
    )
    db_session.add(media_file)
    db_session.commit()
    candidate = Candidate(
        media_item_id=item.id, tracker_id=tracker.id, torrent_id_remote="1", name="Movie.2024.mkv",
        size_bytes=7, source="catalog_search", direction="media_to_torrent", confidence=1.0,
        download_link="https://t.example/dl/1", file_list_json='["Movie.2024.mkv"]',
    )
    db_session.add(candidate)
    db_session.commit()
    match_review = MatchReview(candidate_id=candidate.id, media_file_id=media_file.id, status="approved")
    db_session.add(match_review)
    db_session.commit()

    real_stat = os.stat

    def fake_stat(path, *a, **kw):
        result = real_stat(path, *a, **kw)
        if str(path) == str(media_file_path):
            # os.path.isfile() (chiamato prima dei controlli di hardlinks.check_link) usa
            # anche st_mode: serve un os.stat_result completo, non un oggetto
            # con solo st_dev, altrimenti fallisce lì invece che dove vogliamo.
            seq = list(result)
            seq[2] = result.st_dev + 1  # indice 2 = st_dev nella sequenza stat_result
            return os.stat_result(seq)
        return result

    monkeypatch.setattr(executor.os, "stat", fake_stat)

    with pytest.raises(executor.ExecutionError, match="different devices"):
        executor.execute_review(db_session, match_review, ReseedClient())


def test_execute_torrent_to_client_adds_existing_file_without_new_hardlink(db_session, tmp_path):
    root, disk, tracker, item, run = _base_setup(db_session, tmp_path)
    existing_file = root / "torrents" / "Standalone.2024.mkv"
    existing_file.write_bytes(b"content")
    media_file = MediaFile(
        disk_id=disk.id, relative_path="media/movies/Standalone.2024.mkv", size_bytes=7,
        st_dev=1, inode=1, media_item_id=item.id, last_scan_id=run.id, last_seen_at=datetime.now(UTC),
    )
    db_session.add(media_file)
    db_session.commit()
    seed_file = SeedFile(
        disk_id=disk.id, relative_path="torrents/Standalone.2024.mkv", size_bytes=7, st_dev=1, inode=2,
        media_file_id=media_file.id, last_scan_id=run.id, last_seen_at=datetime.now(UTC),
    )
    db_session.add(seed_file)
    db_session.commit()
    candidate = Candidate(
        media_item_id=item.id, tracker_id=tracker.id, torrent_id_remote="1", name="Standalone.2024.mkv",
        size_bytes=7, source="catalog_search", direction="torrent_to_client", confidence=1.0,
        download_link="https://t.example/dl/1",
    )
    db_session.add(candidate)
    db_session.commit()
    match_review = MatchReview(candidate_id=candidate.id, seed_file_id=seed_file.id, status="approved")
    db_session.add(match_review)
    db_session.commit()

    adapter = ReseedClient()
    seed_job = executor.execute_review(db_session, match_review, adapter)

    assert seed_job.hardlink_created_at is None  # nessun nuovo hardlink, il file c'era già
    assert seed_job.info_hash == "deadbeef"
    assert adapter.added[0].save_path == str(root / "torrents")


def test_execute_without_download_link_fails_explicitly(db_session, tmp_path):
    root, disk, tracker, item, run = _base_setup(db_session, tmp_path)
    existing_file = root / "torrents" / "Standalone.2024.mkv"
    existing_file.write_bytes(b"content")
    media_file = MediaFile(
        disk_id=disk.id, relative_path="media/movies/x.mkv", size_bytes=7,
        st_dev=1, inode=1, media_item_id=item.id, last_scan_id=run.id, last_seen_at=datetime.now(UTC),
    )
    db_session.add(media_file)
    db_session.commit()
    seed_file = SeedFile(
        disk_id=disk.id, relative_path="torrents/Standalone.2024.mkv", size_bytes=7, st_dev=1, inode=2,
        media_file_id=media_file.id, last_scan_id=run.id, last_seen_at=datetime.now(UTC),
    )
    db_session.add(seed_file)
    db_session.commit()
    candidate = Candidate(
        media_item_id=item.id, tracker_id=tracker.id, torrent_id_remote="1", name="x",
        size_bytes=7, source="catalog_search", direction="torrent_to_client", confidence=1.0,
        download_link=None,
    )
    db_session.add(candidate)
    db_session.commit()
    match_review = MatchReview(candidate_id=candidate.id, seed_file_id=seed_file.id, status="approved")
    db_session.add(match_review)
    db_session.commit()

    with pytest.raises(executor.ExecutionError, match="download_link"):
        executor.execute_review(db_session, match_review, ReseedClient())


def test_reconcile_seed_job_updates_status_to_seeding(db_session, tmp_path):
    _root, _disk, tracker, item, _run = _base_setup(db_session, tmp_path)
    candidate = Candidate(
        media_item_id=item.id, tracker_id=tracker.id, torrent_id_remote="1", name="x", size_bytes=1,
        source="catalog_search", direction="media_to_torrent", confidence=1.0,
    )
    db_session.add(candidate)
    db_session.commit()
    from nazgarr.core.models import SeedJob

    seed_job = SeedJob(
        candidate_id=candidate.id, final_status="in_progress", info_hash="deadbeef", recheck_status="pending"
    )
    db_session.add(seed_job)
    db_session.commit()

    adapter = ReseedClient(recheck_status="ok")
    executor.reconcile_seed_job(db_session, seed_job, adapter)

    assert seed_job.final_status == "seeding" and adapter.started == ["deadbeef"]


def test_add_torrent_uses_per_client_root_path_override(db_session, tmp_path):
    # Il motivo per cui l'override vive su (disk, torrent_client) e non sul
    # disco: due client diversi sullo stesso disco possono vederlo montato
    # a path diversi nei rispettivi container — qui solo uno dei due ha un
    # override, l'altro deve continuare a vedere il path locale invariato.
    root, disk, tracker, item, run = _base_setup(db_session, tmp_path)
    media_file_path = root / "media" / "movies" / "Movie.2024.mkv"
    media_file_path.write_bytes(b"content")
    media_file = MediaFile(
        disk_id=disk.id, relative_path="media/movies/Movie.2024.mkv", size_bytes=7,
        st_dev=1, inode=1, media_item_id=item.id, last_scan_id=run.id, last_seen_at=datetime.now(UTC),
    )
    db_session.add(media_file)
    db_session.commit()
    candidate = Candidate(
        media_item_id=item.id, tracker_id=tracker.id, torrent_id_remote="1", name="Movie.2024.mkv",
        size_bytes=7, source="catalog_search", direction="media_to_torrent", confidence=1.0,
        download_link="https://t.example/dl/1", file_list_json='["Movie.2024.mkv"]',
    )
    db_session.add(candidate)
    db_session.commit()

    tc_with_override = TorrentClient(label="qbt-a", adapter_type="qbittorrent", base_url="http://a")
    tc_without_override = TorrentClient(label="qbt-b", adapter_type="qbittorrent", base_url="http://b")
    db_session.add_all([tc_with_override, tc_without_override])
    db_session.commit()
    db_session.add(DiskTorrentClient(
        disk_id=disk.id, torrent_client_id=tc_with_override.id, torrent_client_root_path="/downloads",
    ))
    db_session.commit()

    match_review_a = MatchReview(candidate_id=candidate.id, media_file_id=media_file.id, status="approved")
    db_session.add(match_review_a)
    db_session.commit()
    adapter_a = ReseedClient()
    executor.execute_review(db_session, match_review_a, adapter_a, tc_with_override.id)
    assert adapter_a.added[0].save_path == "/downloads/torrents"

    candidate_b = Candidate(
        media_item_id=item.id, tracker_id=tracker.id, torrent_id_remote="2", name="Movie.2024.mkv",
        size_bytes=7, source="catalog_search", direction="torrent_to_client", confidence=1.0,
        download_link="https://t.example/dl/2",
    )
    db_session.add(candidate_b)
    db_session.commit()
    seed_file = SeedFile(
        disk_id=disk.id, relative_path="torrents/Movie.2024.mkv", size_bytes=7, st_dev=1, inode=2,
        media_file_id=media_file.id, last_scan_id=run.id, last_seen_at=datetime.now(UTC),
    )
    db_session.add(seed_file)
    db_session.commit()
    match_review_b = MatchReview(candidate_id=candidate_b.id, seed_file_id=seed_file.id, status="approved")
    db_session.add(match_review_b)
    db_session.commit()
    adapter_b = ReseedClient()
    executor.execute_review(db_session, match_review_b, adapter_b, tc_without_override.id)
    assert adapter_b.added[0].save_path == str(root / "torrents")


def _single_file_review(db_session, root, disk, tracker, item, run):
    media_file_path = root / "media" / "movies" / "Movie.2024.mkv"
    media_file_path.write_bytes(b"content")
    media_file = MediaFile(
        disk_id=disk.id, relative_path="media/movies/Movie.2024.mkv", size_bytes=7,
        st_dev=1, inode=1, media_item_id=item.id, last_scan_id=run.id, last_seen_at=datetime.now(UTC),
    )
    db_session.add(media_file)
    db_session.commit()
    candidate = Candidate(
        media_item_id=item.id, tracker_id=tracker.id, torrent_id_remote="2", name="Movie.2024.mkv",
        size_bytes=7, source="catalog_search", direction="media_to_torrent", confidence=1.0,
        download_link="https://t.example/dl/2", file_list_json='["Movie.2024.mkv"]',
    )
    db_session.add(candidate)
    db_session.commit()
    match_review = MatchReview(candidate_id=candidate.id, media_file_id=media_file.id, status="approved")
    db_session.add(match_review)
    db_session.commit()
    return media_file_path, match_review


def test_a_cross_seed_reuses_the_hardlink_already_there(db_session, tmp_path):
    # La stessa release, con lo stesso nome, già in seed per un altro tracker:
    # lo stesso inode si riusa invece di fallire.
    root, disk, tracker, item, run = _base_setup(db_session, tmp_path)
    media_file_path, match_review = _single_file_review(db_session, root, disk, tracker, item, run)
    existing = root / "torrents" / "Movie.2024.mkv"
    os.link(media_file_path, existing)

    adapter = ReseedClient()
    seed_job = executor.execute_review(db_session, match_review, adapter)

    assert os.path.samefile(media_file_path, existing)
    assert seed_job.info_hash == "deadbeef" and len(adapter.added) == 1


def test_another_file_at_the_destination_is_never_overwritten(db_session, tmp_path):
    root, disk, tracker, item, run = _base_setup(db_session, tmp_path)
    _media_file_path, match_review = _single_file_review(db_session, root, disk, tracker, item, run)
    (root / "torrents" / "Movie.2024.mkv").write_bytes(b"something else")

    with pytest.raises(executor.ExecutionError, match="already exists"):
        executor.execute_review(db_session, match_review, ReseedClient())
    assert (root / "torrents" / "Movie.2024.mkv").read_bytes() == b"something else"


@pytest.mark.parametrize("points_to", ["nowhere", "hardlink"])
def test_a_symlink_at_the_destination_is_never_followed(db_session, tmp_path, points_to):
    # Audit 2026-10-18: l'esecutore risolveva la destinazione prima di
    # check_link. Un link simbolico pendente faceva nascere l'hardlink dove
    # puntava (il percorso approvato restava un link), uno verso un hardlink
    # già lì passava per un cross-seed da riusare, e il client seedava
    # attraverso un link che lo scanner salta.
    root, disk, tracker, item, run = _base_setup(db_session, tmp_path)
    media_file_path, match_review = _single_file_review(db_session, root, disk, tracker, item, run)
    pointed = root / "torrents" / "other" / "elsewhere.mkv"
    pointed.parent.mkdir()
    if points_to == "hardlink":
        os.link(media_file_path, pointed)
    (root / "torrents" / "Movie.2024.mkv").symlink_to(pointed)

    adapter = ReseedClient()
    with pytest.raises(executor.ExecutionError, match="already exists"):
        executor.execute_review(db_session, match_review, adapter)
    assert os.path.lexists(pointed) is (points_to == "hardlink")  # niente creato dove punta il link
    assert adapter.added == []


def test_a_symlinked_folder_inside_the_torrent_is_out_of_scope(db_session, tmp_path):
    # Anche una cartella del torrent che è un link (qui dentro la cartella
    # dei torrent): il client vedrebbe i file da un'altra parte.
    root, review = _movie_review(db_session, tmp_path, folder="Release.Name")
    (root / "torrents" / "real").mkdir()
    (root / "torrents" / "Release.Name").symlink_to(root / "torrents" / "real")

    adapter = ReseedClient()
    with pytest.raises(executor.ExecutionError, match="outside the allowed scope"):
        executor.execute_review(db_session, review, adapter)
    assert os.listdir(root / "torrents" / "real") == [] and adapter.added == []


@pytest.mark.parametrize("target", ["outside", "inside"])
def test_a_library_file_replaced_by_a_symlink_is_never_linked(db_session, tmp_path, target):
    # Fra la scansione e l'esecuzione il file in libreria è diventato un
    # link simbolico (fuori dal disco o anche dentro): niente hardlink, e
    # niente torrent nel client.
    root, disk, tracker, item, run = _base_setup(db_session, tmp_path)
    media_file_path, match_review = _single_file_review(db_session, root, disk, tracker, item, run)
    elsewhere = (tmp_path / "secret.mkv") if target == "outside" else (root / "media" / "other.mkv")
    elsewhere.write_bytes(b"content")
    media_file_path.unlink()
    media_file_path.symlink_to(elsewhere)

    adapter = ReseedClient()
    with pytest.raises(executor.ExecutionError, match=r"scope|symbolic link"):
        executor.execute_review(db_session, match_review, adapter)
    assert not os.path.lexists(root / "torrents" / "Movie.2024.mkv")
    assert adapter.added == []


@pytest.mark.parametrize("can_skip", [True, False])
def test_the_recheck_is_skipped_only_by_a_client_that_can(can_skip):
    from types import SimpleNamespace

    adapter, seed_job = ReseedClient(), SimpleNamespace(recheck_skipped=None)
    adapter.can_skip_recheck = can_skip
    torrent = executor._CheckedTorrent(content=_movie_torrent(), info_hash="h1")

    executor._add_to_client(adapter, torrent, "/torrents", seed_job, skip_recheck=True)

    # Transmission e rTorrent ricontrollano comunque: il seed job non lo
    # registra come saltato, e il torrent entra fermo come ogni recheck.
    options = adapter.added[0].options
    assert (options["skip_check_verified"], options["stopped"]) == (can_skip, not can_skip)
    assert seed_job.recheck_skipped is (True if can_skip else None)


def test_a_torrent_stopped_after_a_good_recheck_is_started(db_session, tmp_path):
    # qBittorrent con la condizione di arresto "file controllati" (qui non la
    # lascia cambiare all'aggiunta): il recheck riesce e il torrent resta fermo.
    _root, review = _movie_review(db_session, tmp_path)
    adapter = ReseedClient(status=TorrentStatus(info_hash="deadbeef", state="stoppedUP", recheck_status="ok",
                                               progress=1.0))
    seed_job = executor.execute_review(db_session, review, adapter)
    executor.reconcile_seed_job(db_session, seed_job, adapter)

    assert seed_job.final_status == "seeding"
    assert adapter.started == ["deadbeef"]


def test_a_reseed_uses_the_client_labels_chosen_on_its_review(db_session, tmp_path):
    from nazgarr.reseed import review as review_module

    _root, review = _movie_review(db_session, tmp_path)
    client = TorrentClient(label="qb", adapter_type="qbittorrent", base_url="http://qb.test", category_movie="film",
                           tags_reseed="nazgarr,reseed")
    db_session.add(client)
    db_session.commit()

    # Scelti a mano come per un upload: vincono sui default del client.
    review_module.set_client_labels(db_session, review, "4k-movies", " cross , mine ")
    adapter = ReseedClient()
    executor.execute_review(db_session, review, adapter, client.id)
    options = adapter.added[0].options
    assert (options["category"], options["tags"]) == ("4k-movies", ["cross", "mine"])


def test_without_a_choice_a_reseed_takes_the_client_defaults_and_empty_means_none(db_session, tmp_path):
    from nazgarr.reseed import review as review_module

    _root, review = _movie_review(db_session, tmp_path)
    client = TorrentClient(label="qb", adapter_type="qbittorrent", base_url="http://qb.test", category_movie="film",
                           tags_reseed="reseed")
    db_session.add(client)
    db_session.commit()

    labels = executor._labels(db_session, client.id, review.candidate, review)
    assert labels == {"category": "film", "tags": ["reseed"]}
    review_module.set_client_labels(db_session, review, "", None)  # nessuna categoria, i tag di sempre
    assert executor._labels(db_session, client.id, review.candidate, review) == {"tags": ["reseed"]}


def _failed_recheck_job(db_session, tmp_path):
    from nazgarr.core.models import SeedJob

    _root, _disk, tracker, item, _run = _base_setup(db_session, tmp_path)
    candidate = Candidate(
        media_item_id=item.id, tracker_id=tracker.id, torrent_id_remote="1", name="x", size_bytes=1,
        source="catalog_search", direction="media_to_torrent", confidence=1.0,
    )
    db_session.add(candidate)
    db_session.commit()
    seed_job = SeedJob(candidate_id=candidate.id, final_status="in_progress", info_hash="deadbeef",
                       recheck_status="pending")
    db_session.add(seed_job)
    db_session.commit()
    return seed_job


class _IncompleteAfterRecheck(ReseedClient):
    """Un recheck fallito di un torrent che gira (state), o fermo."""

    def __init__(self, state="stalledDL", stop_error=None):
        super().__init__(status=TorrentStatus(info_hash="deadbeef", state=state, recheck_status="failed",
                                              progress=0.97, incomplete=True, amount_left=1000))
        self.stop_error = stop_error

    def stop(self, info_hash):
        if self.stop_error is not None:
            raise self.stop_error
        super().stop(info_hash)


def test_a_failed_recheck_is_stopped_when_it_is_reconciled(db_session, tmp_path):
    # Avviato, il client scaricava i piece mancanti negli hardlink: gli stessi
    # byte dei file in libreria, senza alcuna approvazione. Un reseed entra
    # fermo (decisione dell'utente, 2026-10-18); uno che gira lo stesso (un
    # plugin senza can_add_stopped, o avviato a mano) si ferma al reconcile.
    seed_job = _failed_recheck_job(db_session, tmp_path)
    adapter = _IncompleteAfterRecheck()

    executor.reconcile_seed_job(db_session, seed_job, adapter)

    assert seed_job.final_status == "failed" and adapter.stopped == ["deadbeef"]
    assert "stopped in the client" in seed_job.error_message


def test_a_torrent_already_stopped_is_not_stopped_again(db_session, tmp_path):
    seed_job = _failed_recheck_job(db_session, tmp_path)
    adapter = _IncompleteAfterRecheck(state="stoppedDL")

    executor.reconcile_seed_job(db_session, seed_job, adapter)

    assert seed_job.final_status == "failed" and adapter.stopped == []
    assert "left stopped in the client" in seed_job.error_message


@pytest.mark.parametrize(("error", "expected"), [
    (NotImplementedError(), "cannot be told to stop it"), (RuntimeError("down"), "could not be stopped"),
])
def test_a_torrent_that_cannot_be_stopped_says_to_stop_it_by_hand(db_session, tmp_path, error, expected):
    seed_job = _failed_recheck_job(db_session, tmp_path)

    executor.reconcile_seed_job(db_session, seed_job, _IncompleteAfterRecheck(stop_error=error))

    assert seed_job.final_status == "failed"
    assert expected in seed_job.error_message and "by hand" in seed_job.error_message


def test_a_plugin_adapter_without_stop_keeps_the_safe_default():
    from nazgarr.sdk import TorrentClientAdapter

    class Plugin(TorrentClientAdapter):
        def add_torrent(self, *a, **kw):
            return "h"

        def get_torrent_status(self, info_hash):
            raise NotImplementedError

        def list_torrents(self, on_progress=None):
            return []

    with pytest.raises(NotImplementedError):
        Plugin().stop("h")


def test_the_client_gets_the_bytes_nazgarr_downloaded_never_the_tracker_link(db_session, tmp_path, tracker_torrent):
    # Con il link il client scaricava la sua copia: un tracker ostile poteva
    # servire a Nazgarr il torrent giusto e al client un altro.
    _root, review = _movie_review(db_session, tmp_path)
    adapter = ReseedClient()

    executor.execute_review(db_session, review, adapter)

    call = adapter.added[0]
    assert call.source != "https://t.example/dl/1" and call.content == _movie_torrent()
    assert call.options["expected_info_hash"] == compute_info_hash(_movie_torrent())
    assert tracker_torrent["links"] == ["https://t.example/dl/1"]
    assert review.candidate.info_hash == compute_info_hash(_movie_torrent())  # per l'attesa e i retry
    assert not os.path.exists(call.source)  # il file temporaneo non resta


def test_a_tracker_serving_another_torrent_than_the_matched_one_gets_nothing(db_session, tmp_path, tracker_torrent):
    root, review = _movie_review(db_session, tmp_path)
    review.candidate.info_hash = compute_info_hash(_movie_torrent())  # letto dal matching o dal controllo
    db_session.commit()
    tracker_torrent["content"] = _movie_torrent(b"CONTENT")  # stessi nomi, altri piece
    adapter = ReseedClient()

    with pytest.raises(executor.ExecutionError, match=r"different \.torrent"):
        executor.execute_review(db_session, review, adapter)

    assert adapter.added == []
    assert not (root / "torrents" / "Movie.2024.mkv").exists()  # nessun hardlink prima del controllo
    from nazgarr.core.models import SeedJob
    job = db_session.query(SeedJob).one()
    assert job.final_status == "failed" and "different .torrent" in job.error_message


def test_a_torrent_that_cannot_be_downloaded_is_a_failed_job_to_retry(db_session, tmp_path, monkeypatch):
    root, review = _movie_review(db_session, tmp_path)

    def down(candidate):
        raise executor.ExecutionError("Could not download the .torrent from t: tracker down")

    monkeypatch.setattr(executor, "_download_torrent", down)
    adapter = ReseedClient()

    with pytest.raises(executor.ExecutionError, match="tracker down"):
        executor.execute_review(db_session, review, adapter)

    from nazgarr.core.models import SeedJob
    job = db_session.query(SeedJob).one()
    assert (job.final_status, job.info_hash) == ("failed", None)  # visibile fra le esecuzioni fallite
    assert adapter.added == [] and not (root / "torrents" / "Movie.2024.mkv").exists()


def test_a_download_link_that_is_a_local_path_is_never_read_or_sent(db_session, tmp_path, tracker_torrent):
    # Gli adapter mandano per contenuto un file locale: un link del tracker come
    # /proc/self/environ veniva letto e spedito al client.
    _root, review = _movie_review(db_session, tmp_path)
    secret = tmp_path / "secret.env"
    secret.write_bytes(b"APP_SECRET_KEY=hunter2")
    review.candidate.download_link = str(secret)
    db_session.commit()
    adapter = ReseedClient()

    executor.execute_review(db_session, review, adapter)

    assert adapter.added[0].content == _movie_torrent()
    assert tracker_torrent["links"] == [str(secret)]  # chiesto al tracker, mai aperto qui


def _client_down() -> ReseedClient:
    """add_torrent fallisce, e il client risponde che quel torrent non ce l'ha."""
    return ReseedClient(add_error=RuntimeError("client down"))


def test_a_single_file_add_that_fails_removes_only_the_hardlink_it_created(db_session, tmp_path):
    # Prima il percorso a file singolo lasciava l'hardlink, quello dei pack
    # lo toglieva: ora una regola sola.
    root, disk, tracker, item, run = _base_setup(db_session, tmp_path)
    media_file_path, review = _single_file_review(db_session, root, disk, tracker, item, run)

    with pytest.raises(executor.ExecutionError, match="client down"):
        executor.execute_review(db_session, review, _client_down())

    assert not (root / "torrents" / "Movie.2024.mkv").exists()
    assert media_file_path.read_bytes() == b"content"  # il file in libreria non si tocca mai


def test_a_reused_hardlink_stays_when_the_add_fails(db_session, tmp_path):
    root, disk, tracker, item, run = _base_setup(db_session, tmp_path)
    media_file_path, review = _single_file_review(db_session, root, disk, tracker, item, run)
    os.link(media_file_path, root / "torrents" / "Movie.2024.mkv")  # un cross-seed già in seed

    with pytest.raises(executor.ExecutionError):
        executor.execute_review(db_session, review, _client_down())

    assert os.path.samefile(media_file_path, root / "torrents" / "Movie.2024.mkv")


def test_a_seed_file_linked_outside_the_disk_is_refused_before_the_client(db_session, tmp_path):
    root, disk, tracker, item, run = _base_setup(db_session, tmp_path)
    outside = tmp_path / "elsewhere.mkv"
    outside.write_bytes(b"content")
    (root / "torrents" / "Standalone.2024.mkv").symlink_to(outside)
    seed_file = SeedFile(
        disk_id=disk.id, relative_path="torrents/Standalone.2024.mkv", size_bytes=7, st_dev=1, inode=2,
        last_scan_id=run.id, last_seen_at=datetime.now(UTC),
    )
    db_session.add(seed_file)
    db_session.commit()
    candidate = Candidate(
        media_item_id=item.id, tracker_id=tracker.id, torrent_id_remote="1", name="Standalone.2024.mkv",
        size_bytes=7, source="catalog_search", direction="torrent_to_client", confidence=1.0,
        download_link="https://t.example/dl/1",
    )
    db_session.add(candidate)
    db_session.commit()
    review = MatchReview(candidate_id=candidate.id, seed_file_id=seed_file.id, status="approved")
    db_session.add(review)
    db_session.commit()
    adapter = ReseedClient()

    with pytest.raises(executor.ExecutionError, match="scope"):
        executor.execute_review(db_session, review, adapter)
    assert adapter.added == []


class _DownloadingTracker:
    def __init__(self, result):
        self.result = result
        self.links = []

    def download_torrent(self, url):
        self.links.append(url)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _with_tracker_adapter(monkeypatch, fake):
    from contextlib import contextmanager

    from nazgarr.integrations import adapter_factory

    @contextmanager
    def tracker(row):
        yield fake

    monkeypatch.setattr(adapter_factory, "tracker", tracker)


def test_the_real_download_goes_through_the_candidates_tracker_adapter(monkeypatch):
    # Il fixture autouse sostituisce _download_torrent in ogni altro test.
    fake = _DownloadingTracker(b"d4:infod4:name1:xee")
    _with_tracker_adapter(monkeypatch, fake)
    link = "https://t.example/torrent/download/1.abcdef"
    candidate = Candidate(tracker=Tracker(label="t", adapter_type="unit3d", base_url="https://t.example"),
                          download_link=link)

    assert _REAL_DOWNLOAD_TORRENT(candidate) == b"d4:infod4:name1:xee"
    assert fake.links == [link]


def test_a_tracker_without_download_torrent_or_a_failed_download_is_an_execution_error(monkeypatch):
    from nazgarr.sdk import NotSupportedError

    # Un plugin senza download_torrent (ora serve ai reseed, docs/SDK.md), e
    # un errore che porta il link con la chiave: mai salvata in chiaro.
    tracker = Tracker(label="Plugin tracker", adapter_type="plugin", base_url="https://t.example")
    for error, expected in (
        (NotSupportedError("MyTracker non supporta download_torrent()"), "non supporta download_torrent"),
        (RuntimeError("GET https://t.example/torrent/download/7.secretkey123 failed"), "7.<redacted> failed"),
    ):
        _with_tracker_adapter(monkeypatch, _DownloadingTracker(error))
        candidate = Candidate(tracker=tracker, download_link="https://t.example/torrent/download/7.secretkey123")
        with pytest.raises(executor.ExecutionError) as info:
            _REAL_DOWNLOAD_TORRENT(candidate)
        assert str(info.value).startswith("Could not download the .torrent from Plugin tracker: ")
        assert expected in str(info.value) and "secretkey123" not in str(info.value)


def test_a_candidate_whose_tracker_is_gone_cannot_be_downloaded(monkeypatch):
    fake = _DownloadingTracker(b"never")
    _with_tracker_adapter(monkeypatch, fake)

    with pytest.raises(executor.ExecutionError, match="tracker no longer exists"):
        _REAL_DOWNLOAD_TORRENT(Candidate(tracker=None, download_link="https://t.example/torrent/download/1.k"))
    assert fake.links == []


def _catalog_only_review(db_session, tmp_path):
    """Un candidato a file singolo il cui .torrent il matching non ha letto:
    cartella e nome solo dal catalogo, nessun info hash."""
    from nazgarr.core.models import CandidateFile

    root, review = _movie_review(db_session, tmp_path)
    review.candidate.files = [CandidateFile(torrent_path="Movie.2024.mkv", size_bytes=7, is_video=True,
                                            media_file_id=review.media_file_id)]
    db_session.commit()
    return root, review


def _folder_torrent(name: str, folder: str = "Movie.2024.1080p-GRP") -> bytes:
    return _bencode({"info": {"name": folder, "piece length": 16, "pieces": hashlib.sha1(b"content").digest(),
                              "files": [{"length": 7, "path": [name]}]}})


def test_a_catalogue_only_structure_takes_the_folder_from_the_torrent_before_any_hardlink(
        db_session, tmp_path, tracker_torrent):
    # Con la verifica spenta, cartella e nomi del catalogo arrivavano fino agli
    # hardlink (audit 2026-10-18, finding 146).
    root, review = _catalog_only_review(db_session, tmp_path)
    tracker_torrent["content"] = _folder_torrent("Movie.2024.mkv")
    adapter = ReseedClient(info_hash=compute_info_hash(tracker_torrent["content"]))

    executor.execute_review(db_session, review, adapter)

    assert (root / "torrents" / "Movie.2024.1080p-GRP" / "Movie.2024.mkv").is_file()
    assert review.candidate.folder == "Movie.2024.1080p-GRP"
    assert len(tracker_torrent["links"]) == 1  # scaricato una volta sola


def test_a_catalogue_name_the_torrent_does_not_have_creates_nothing(db_session, tmp_path, tracker_torrent):
    root, review = _catalog_only_review(db_session, tmp_path)
    tracker_torrent["content"] = _folder_torrent("Something.Else.mkv")
    adapter = ReseedClient()

    with pytest.raises(executor.ExecutionError, match="not the ones the tracker's catalogue listed"):
        executor.execute_review(db_session, review, adapter)

    assert not any((root / "torrents").rglob("*.mkv"))
    assert adapter.added == []


def test_a_known_info_hash_does_not_skip_the_structure_check(db_session, tmp_path, tracker_torrent):
    # Il controllo completo impara l'info hash e ritrova i file anche per
    # nome: prima, con l'info hash noto, gli hardlink seguivano lo stesso i
    # nomi del catalogo. Conta se la struttura è mai stata letta da un .torrent.
    root, review = _catalog_only_review(db_session, tmp_path)
    tracker_torrent["content"] = _folder_torrent("Something.Else.mkv")
    review.candidate.info_hash = compute_info_hash(tracker_torrent["content"])
    db_session.commit()
    adapter = ReseedClient(info_hash=review.candidate.info_hash)

    with pytest.raises(executor.ExecutionError, match="not the ones the tracker's catalogue listed"):
        executor.execute_review(db_session, review, adapter)

    assert not any((root / "torrents").rglob("*.mkv"))
    assert adapter.added == []


def test_an_execution_error_carries_the_english_text_never_the_bare_code(db_session, tmp_path, monkeypatch):
    # Il retry mostrava "Retry failed: client_cannot_see_path", senza percorso,
    # mentre il seed_job aveva il testo giusto (audit 2026-10-18, finding 119).
    from nazgarr.core.models import SeedJob
    from nazgarr.torrents.client_paths import ClientPathError

    _root, review = _catalog_only_review(db_session, tmp_path)

    def cannot_see(*args):
        raise ClientPathError("client_cannot_see_path", path="/data/torrents", folder="/data/media",
                              client_root="/downloads")

    monkeypatch.setattr(executor, "client_visible_path", cannot_see)
    with pytest.raises(executor.ExecutionError) as info:
        executor.execute_review(db_session, review, ReseedClient())

    assert str(info.value).startswith("The client cannot see /data/torrents")
    assert str(info.value) == db_session.query(SeedJob).one().error_message


class _StoppedAdds(ReseedClient):
    """Un client che aggiunge fermo (come ogni adapter incluso, decisione
    dell'utente del 2026-10-18): resta fermo dopo il recheck, finché start().
    È ReseedClient, che ora lo fa già; il nome dice cosa contano i test qui
    sotto."""


def test_a_reseed_enters_the_client_stopped_and_starts_only_after_a_good_recheck(db_session, tmp_path):
    _root, review = _movie_review(db_session, tmp_path)
    adapter = _StoppedAdds()

    seed_job = executor.execute_review(db_session, review, adapter)

    assert [a.options["stopped"] for a in adapter.added] == [True] and adapter.started == []
    executor.reconcile_seed_job(db_session, seed_job, adapter)
    assert seed_job.final_status == "seeding" and adapter.started == ["deadbeef"]


def test_a_failed_recheck_never_starts_the_reseed(db_session, tmp_path):
    # Fermo, il client non scarica niente dentro gli hardlink della libreria.
    _root, review = _movie_review(db_session, tmp_path)
    adapter = _StoppedAdds(recheck_status="failed", progress=0.97)

    seed_job = executor.execute_review(db_session, review, adapter)
    executor.reconcile_seed_job(db_session, seed_job, adapter)
    executor.reconcile_seed_job(db_session, seed_job, adapter)  # il job fallito non si muove più

    assert seed_job.final_status == "failed" and adapter.started == [] and adapter.stopped == []
    assert "left stopped in the client" in seed_job.error_message
    assert "Retry once the files are right" in seed_job.error_message


def test_a_retry_of_a_stopped_failed_reseed_asks_for_a_recheck_never_a_start(db_session, tmp_path):
    # Fermo, il client non lo ricontrolla da solo: rileggerlo diceva "failed"
    # anche con i file sistemati. Il retry è l'approvazione di un recheck.
    _root, review = _movie_review(db_session, tmp_path)
    adapter = _StoppedAdds(recheck_status="failed", progress=0.97)
    seed_job = executor.execute_review(db_session, review, adapter)
    executor.reconcile_seed_job(db_session, seed_job, adapter)
    assert seed_job.final_status == "failed"

    executor.adopt_for_retry(db_session, seed_job, adapter)

    assert adapter.rechecked == ["deadbeef"] and adapter.started == [] and adapter.stopped == []
    assert (seed_job.final_status, seed_job.recheck_status, seed_job.error_message) == ("in_progress", "pending", None)
    adapter.recheck_status, adapter.progress = "ok", 1.0  # i file ora sono giusti
    executor.reconcile_seed_job(db_session, seed_job, adapter)
    assert seed_job.final_status == "seeding" and adapter.started == ["deadbeef"]


def test_a_retry_without_a_recheck_in_the_adapter_only_reads_it_again(db_session, tmp_path):
    seed_job = _failed_recheck_job(db_session, tmp_path)
    adapter = _StoppedAdds(recheck_status="failed", progress=0.97)
    adapter.recheck = None  # un plugin senza recheck
    executor.reconcile_seed_job(db_session, seed_job, adapter)

    executor.adopt_for_retry(db_session, seed_job, adapter)

    assert seed_job.final_status == "failed" and adapter.started == []


def test_a_recheck_the_client_refuses_is_a_failed_retry(db_session, tmp_path):
    seed_job = _failed_recheck_job(db_session, tmp_path)
    adapter = _StoppedAdds(recheck_status="failed", progress=0.97)
    executor.reconcile_seed_job(db_session, seed_job, adapter)

    def refuse(info_hash):
        raise RuntimeError("client down")

    adapter.recheck = refuse
    with pytest.raises(executor.ExecutionError, match="did not take the recheck"):
        executor.adopt_for_retry(db_session, seed_job, adapter)
    db_session.refresh(seed_job)
    assert seed_job.final_status == "failed" and adapter.started == []


def _other_pieces_verified(monkeypatch, outcome=("ok", None)):
    """La verifica di Nazgarr dei piece fuori dagli extra mancanti (la
    vera è in tests/test_season_pack.py, con i file): qui il suo esito."""
    calls = []
    monkeypatch.setattr(executor, "_extras_gate", lambda session, seed_job, adapter: calls.append(seed_job.id)
                        or outcome)
    return calls


def test_the_known_missing_extras_still_count_as_a_good_recheck(db_session, tmp_path, monkeypatch):
    # Solo dopo la verifica di Nazgarr dei piece fuori dagli extra
    # (decisione dell'utente, 2026-10-21).
    seed_job = _failed_recheck_job(db_session, tmp_path)
    seed_job.expected_missing_bytes = 10
    adapter = _StoppedAdds(recheck_status="failed", progress=0.99)
    calls = _other_pieces_verified(monkeypatch)

    executor.reconcile_seed_job(db_session, seed_job, adapter)

    assert calls == [seed_job.id]
    assert seed_job.final_status == "seeding" and adapter.started == ["deadbeef"]  # il client scarica gli extra


@pytest.mark.parametrize("outcome", ["pending", "failed"])
def test_within_the_allowance_nothing_starts_before_nazgarrs_own_check_passes(db_session, tmp_path, monkeypatch,
                                                                              outcome):
    seed_job = _failed_recheck_job(db_session, tmp_path)
    seed_job.expected_missing_bytes = 10
    adapter = _StoppedAdds(recheck_status="failed", progress=0.99)
    _other_pieces_verified(monkeypatch, (outcome, "the reason"))

    executor.reconcile_seed_job(db_session, seed_job, adapter)

    assert adapter.started == []
    if outcome == "pending":
        assert (seed_job.final_status, seed_job.recheck_status, seed_job.error_message) == (
            "in_progress", "pending", "the reason")
    else:
        assert seed_job.final_status == "failed"
        assert seed_job.error_message.startswith("Recheck failed, the reason (client state: stoppedDL); left stopped")


def test_a_recheck_beyond_the_allowance_never_asks_nazgarrs_check(db_session, tmp_path, monkeypatch):
    seed_job = _failed_recheck_job(db_session, tmp_path)
    seed_job.expected_missing_bytes = 10
    adapter = _IncompleteAfterRecheck(state="stoppedDL")  # mancano 1000 byte
    calls = _other_pieces_verified(monkeypatch)

    executor.reconcile_seed_job(db_session, seed_job, adapter)

    assert calls == [] and seed_job.final_status == "failed" and adapter.started == []


def test_a_start_the_client_refuses_keeps_the_job_in_progress_and_is_tried_again(db_session, tmp_path):
    # Prima diventava "seeding" con il torrent fermo per sempre.
    seed_job = _failed_recheck_job(db_session, tmp_path)
    adapter = _StoppedAdds(start_error=RuntimeError("refused"))

    executor.reconcile_seed_job(db_session, seed_job, adapter)
    assert seed_job.final_status == "in_progress" and seed_job.recheck_status == "ok"
    assert "did not start the torrent" in seed_job.error_message

    adapter.start_error = None
    executor.reconcile_seed_job(db_session, seed_job, adapter)
    assert seed_job.final_status == "seeding" and adapter.started == ["deadbeef"]
    assert seed_job.error_message is None


@pytest.mark.parametrize("missing", ["no method", "not implemented"])
def test_a_client_that_cannot_start_it_ends_seeding_with_the_reason(db_session, tmp_path, missing):
    # Un plugin senza la classe base, o con start() non implementato: prima
    # restava in corso per sempre, con un avviso a ogni reconcile.
    seed_job = _failed_recheck_job(db_session, tmp_path)
    adapter = _StoppedAdds()
    if missing == "no method":
        adapter.start = None
    else:
        adapter.start_error = NotImplementedError()

    executor.reconcile_seed_job(db_session, seed_job, adapter)

    assert seed_job.final_status == "seeding"
    assert seed_job.error_message == executor.CANNOT_START


def test_a_deluge_reseed_missing_only_its_extras_starts_after_its_recheck(db_session, tmp_path, monkeypatch):
    # Come lo legge Deluge dopo force_recheck su un torrent in pausa: rimesso
    # in pausa, al 99%, con is_auto_managed (l'opzione salvata) vero. Prima
    # era "pending" per sempre: mai avviato, mai in seed.
    from tests.test_torrent_client_deluge import _adapter as deluge_adapter
    from tests.test_torrent_client_deluge import _DelugeMock

    _other_pieces_verified(monkeypatch)
    seed_job = _failed_recheck_job(db_session, tmp_path)
    seed_job.expected_missing_bytes = 2000
    mock = _DelugeMock(torrents={"deadbeef": {"state": "Paused", "progress": 99.0, "paused": True,
                                              "is_auto_managed": True, "total_wanted": 100_000,
                                              "total_done": 99_000}})

    executor.reconcile_seed_job(db_session, seed_job, deluge_adapter(mock))

    assert seed_job.final_status == "seeding"
    assert mock.called("core.resume_torrent") == [[["deadbeef"]]]


def test_a_reseed_verified_at_100_percent_starts_right_away():
    # L'eccezione al recheck (decisione dell'utente, 2026-09-29): parte subito, come prima.
    from types import SimpleNamespace

    adapter = _StoppedAdds()
    torrent = executor._CheckedTorrent(content=_movie_torrent(), info_hash="h1")

    executor._add_to_client(adapter, torrent, "/torrents", SimpleNamespace(recheck_skipped=None), skip_recheck=True)

    options = adapter.added[0].options
    assert (options["skip_check_verified"], options["stopped"]) == (True, False)


def test_a_client_that_cannot_add_stopped_gets_the_add_of_before():
    # Un plugin scritto prima di can_add_stopped, o che ridefinisce add_torrent
    # senza il parametro: nessun argomento che non conosce.
    from types import SimpleNamespace

    from nazgarr.adapters.torrent_client.base import stopped_kwargs

    class OldPlugin(ReseedClient):
        can_add_stopped = True  # ereditato da un adapter incluso

        def add_torrent(self, torrent_file_or_url, save_path, force_recheck=True, expected_info_hash=None):
            return super().add_torrent(torrent_file_or_url, save_path, force_recheck, expected_info_hash)

    torrent = executor._CheckedTorrent(content=_movie_torrent(), info_hash="h1")
    plain = ReseedClient()
    plain.can_add_stopped = False  # un plugin scritto prima di can_add_stopped
    for adapter in (plain, OldPlugin()):
        assert stopped_kwargs(adapter) == {}
        executor._add_to_client(adapter, torrent, "/torrents", SimpleNamespace(recheck_skipped=None),
                                skip_recheck=False)
        assert [a.options["stopped"] for a in adapter.added] == [False]


class _RenamesPartialFiles(_StoppedAdds):
    """Come Transmission con rename-partial-files: dice il suffisso e dove
    cerca i file del torrent (decisione dell'utente, 2026-10-19)."""

    def __init__(self):
        super().__init__(recheck_status="failed", progress=0.97)
        self.save_path = None

    def add_torrent(self, torrent_file_or_url, save_path, force_recheck=True, expected_info_hash=None, **kwargs):
        self.save_path = save_path
        return super().add_torrent(torrent_file_or_url, save_path, force_recheck, expected_info_hash, **kwargs)

    def partial_file_suffix(self):
        return ".part"

    def get_torrent_info(self, info_hash):
        from nazgarr.adapters.torrent_client.base import ClientTorrentFileInfo, ClientTorrentInfo

        return ClientTorrentInfo(info_hash=info_hash, name="Movie.2024.mkv", save_path=self.save_path,
                                 state="stoppedDL", files=[ClientTorrentFileInfo("Movie.2024.mkv", 7)])


def _tracked(db_session, root, review):
    st = os.stat(root / "media" / "movies" / "Movie.2024.mkv")
    review.media_file.st_dev, review.media_file.inode = st.st_dev, st.st_ino
    db_session.commit()


def test_after_a_failed_recheck_the_hardlink_the_client_renamed_gets_its_name_back(db_session, tmp_path):
    root, review = _movie_review(db_session, tmp_path)
    _tracked(db_session, root, review)
    adapter = _RenamesPartialFiles()
    seed_job = executor.execute_review(db_session, review, adapter)
    link = root / "torrents" / "Movie.2024.mkv"
    os.rename(link, str(link) + ".part")  # come Transmission, durante il recheck

    executor.reconcile_seed_job(db_session, seed_job, adapter)

    assert seed_job.final_status == "failed" and adapter.started == []
    assert link.is_file() and os.path.samefile(link, root / "media" / "movies" / "Movie.2024.mkv")
    assert not os.path.exists(str(link) + ".part")
    assert "1 file(s) the client had renamed to *.part got their name back" in seed_job.error_message


def test_a_renamed_file_that_is_not_ours_keeps_its_name(db_session, tmp_path):
    root, review = _movie_review(db_session, tmp_path)
    _tracked(db_session, root, review)
    adapter = _RenamesPartialFiles()
    seed_job = executor.execute_review(db_session, review, adapter)
    link = root / "torrents" / "Movie.2024.mkv"
    os.unlink(link)
    (root / "torrents" / "Movie.2024.mkv.part").write_bytes(b"another file")  # un altro inode

    executor.reconcile_seed_job(db_session, seed_job, adapter)

    assert not link.exists() and (root / "torrents" / "Movie.2024.mkv.part").is_file()
    assert "got their name back" not in seed_job.error_message


def test_a_client_that_renames_nothing_is_not_asked_where_its_files_are(db_session, tmp_path):
    _root, review = _movie_review(db_session, tmp_path)
    adapter = _RenamesPartialFiles()
    adapter.partial_file_suffix = lambda: None
    adapter.get_torrent_info = None  # mai chiamato
    seed_job = executor.execute_review(db_session, review, adapter)

    executor.reconcile_seed_job(db_session, seed_job, adapter)

    assert seed_job.final_status == "failed"


def test_an_error_in_nazgarrs_own_check_never_starts_the_torrent(db_session, tmp_path, monkeypatch):
    seed_job = _failed_recheck_job(db_session, tmp_path)
    seed_job.expected_missing_bytes = 10
    adapter = _StoppedAdds(recheck_status="failed", progress=0.99)
    monkeypatch.setattr(executor.full_check, "verified_for",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("disk asleep")))

    executor.reconcile_seed_job(db_session, seed_job, adapter)

    assert (seed_job.final_status, seed_job.error_message) == ("in_progress", executor.EXTRAS_NOT_CHECKED)
    assert adapter.started == []


def _foreign_torrent(save_path="/other/downloads", state="downloading"):
    from nazgarr.adapters.torrent_client.base import ClientTorrentInfo

    return ClientTorrentInfo(info_hash=compute_info_hash(_movie_torrent()), name="Movie.2024.mkv",
                             save_path=save_path, state=state)


def test_a_retry_never_takes_over_a_torrent_another_program_had_added(db_session, tmp_path):
    """Revisione dell'audit 2026-10-18 (R5): l'aggiunta fallisce perché il
    client ha già il torrent (un cross-seed, autobrr, l'utente), e il retry
    lo seguiva come suo: in download lo fermava, fermo al 100% lo avviava."""
    from nazgarr.adapters.torrent_client.base import TorrentAlreadyInClientError

    root, review = _movie_review(db_session, tmp_path)
    foreign = _foreign_torrent()
    adapter = ReseedClient(add_error=TorrentAlreadyInClientError("already there"), torrents=[foreign],
                           status=TorrentStatus(foreign.info_hash, "downloading", "failed", 0.4, incomplete=True))
    with pytest.raises(executor.ExecutionError, match="already had this torrent"):
        executor.execute_review(db_session, review, adapter)
    seed_job = db_session.query(SeedJob).one()
    assert not (root / "torrents" / "Movie.2024.mkv").exists()  # i suoi hardlink tolti

    with pytest.raises(executor.RetryRefused) as refused:
        executor.adopt_for_retry(db_session, seed_job, adapter)

    assert refused.value.code == "seed_job_torrent_not_ours"
    assert refused.value.params == {"id": seed_job.id, "info_hash": foreign.info_hash}
    assert (adapter.stopped, adapter.started, adapter.rechecked) == ([], [], [])
    db_session.refresh(seed_job)
    assert (seed_job.final_status, seed_job.info_hash) == ("failed", None)

    adapter.torrents.clear()  # l'utente lo toglie dal client: il retry riparte da zero
    assert executor.adopt_for_retry(db_session, seed_job, adapter) is None


def test_a_retry_never_takes_over_a_torrent_after_a_failure_before_the_add(db_session, tmp_path, tracker_torrent):
    # Il .torrent del tracker non è quello verificato: niente mandato al
    # client. Un torrent con quell'hash che compare dopo è di qualcun altro.
    from nazgarr.adapters.torrent_client.base import ClientTorrentInfo

    _root, review = _movie_review(db_session, tmp_path)
    review.candidate.info_hash = "ab" * 20
    db_session.commit()
    tracker_torrent["content"] = _movie_torrent(b"other!!")
    adapter = ReseedClient()
    with pytest.raises(executor.ExecutionError, match=r"different \.torrent"):
        executor.execute_review(db_session, review, adapter)
    seed_job = db_session.query(SeedJob).one()
    assert adapter.added == [] and seed_job.error_message.endswith(executor.NOTHING_SENT)

    adapter.torrents["ab" * 20] = ClientTorrentInfo(info_hash="ab" * 20, name="Movie.2024.mkv",
                                                   save_path="/other/downloads", state="stoppedUP")
    with pytest.raises(executor.RetryRefused):
        executor.adopt_for_retry(db_session, seed_job, adapter)
    assert (adapter.started, adapter.rechecked, seed_job.info_hash) == ([], [], None)


def test_an_execution_failed_with_the_old_duplicate_text_is_not_taken_over_either(db_session, tmp_path):
    _root, review = _movie_review(db_session, tmp_path)
    foreign = _foreign_torrent(state="stoppedUP")
    seed_job = SeedJob(
        candidate_id=review.candidate_id, source_media_file_id=review.media_file_id, final_status="failed",
        error_message=f"Torrent {foreign.info_hash} is already in the client (possibly with another save path): "
                      "nothing added",
    )
    review.candidate.info_hash = foreign.info_hash
    db_session.add(seed_job)
    db_session.commit()
    adapter = ReseedClient(torrents=[foreign])

    with pytest.raises(executor.RetryRefused):
        executor.adopt_for_retry(db_session, seed_job, adapter)
    assert adapter.started == []


def test_a_retry_of_an_execution_whose_torrent_left_the_client_adds_it_again(db_session, tmp_path):
    """Audit 2026-10-18, finding 113 (residuo): il retry che il messaggio
    "no longer in the client" proponeva finiva in un ValueError (un 500), e
    restava solo l'eliminazione."""
    _root, review = _movie_review(db_session, tmp_path)

    class Gone(ReseedClient):
        def get_torrent_status(self, info_hash):
            raise ValueError(f"Torrent {info_hash} not found in the client")

    seed_job = SeedJob(
        candidate_id=review.candidate_id, source_media_file_id=review.media_file_id, final_status="failed",
        info_hash="deadbeef", error_message="The torrent is no longer in the client: retry it to add it again, "
                                            "or delete it",
    )
    db_session.add(seed_job)
    db_session.commit()
    adapter = Gone()

    assert executor.adopt_for_retry(db_session, seed_job, adapter) is None  # da rifare
    again = executor.retry_from_scratch(db_session, seed_job, adapter)

    assert again.id != seed_job.id and len(adapter.added) == 1
    assert (again.final_status, again.info_hash) == ("in_progress", "deadbeef")


def test_a_retry_when_the_client_does_not_answer_is_a_failed_retry_not_a_crash(db_session, tmp_path):
    seed_job = _failed_recheck_job(db_session, tmp_path)
    seed_job.final_status = "failed"
    db_session.commit()

    class Down(ReseedClient):
        def get_torrent_status(self, info_hash):
            raise ConnectionError("connection refused")

        def get_torrent_info(self, info_hash):
            raise ConnectionError("connection refused")

    with pytest.raises(executor.ExecutionError, match="Could not read torrent deadbeef"):
        executor.adopt_for_retry(db_session, seed_job, Down())
    assert seed_job.final_status == "failed"


def _timed_out_execution(db_session, tmp_path):
    """Un'esecuzione la cui aggiunta scade (TorrentAddTimeoutError) prima
    che il torrent compaia nel client: fallita, senza info hash, gli
    hardlink lasciati al loro posto. Restituisce il seed job e il save path
    mandato al client."""
    from nazgarr.adapters.torrent_client.base import TorrentAddTimeoutError

    root, review = _movie_review(db_session, tmp_path)
    first = ReseedClient(add_error=TorrentAddTimeoutError("no new torrent within 30s"))
    with pytest.raises(executor.ExecutionError):
        executor.execute_review(db_session, review, first)
    seed_job = db_session.query(SeedJob).one()
    assert (seed_job.final_status, seed_job.info_hash) == ("failed", None)
    assert (root / "torrents" / "Movie.2024.mkv").is_file()
    return seed_job, first.added[0].save_path


def test_a_retry_follows_its_own_torrent_that_showed_up_late(db_session, tmp_path):
    """Revisione dell'audit 2026-10-18: l'aggiunta della prima esecuzione
    scade, il retry parte (il controllo completo dura minuti) e intanto il
    torrent della prima compare. L'aggiunta del retry trovava il client che
    l'aveva già e falliva con "l'ha messo un altro programma": il torrent di
    Nazgarr restava fermo, mai ricontrollato, e il retry dopo lo rifiutava."""
    from nazgarr.adapters.torrent_client.base import TorrentAlreadyInClientError

    seed_job, save_path = _timed_out_execution(db_session, tmp_path)
    late = _foreign_torrent(save_path=save_path, state="stoppedDL")
    adapter = ReseedClient(add_error=TorrentAlreadyInClientError("already there"), torrents=[late])

    again = executor.retry_from_scratch(db_session, seed_job, adapter)

    assert again.id != seed_job.id
    assert (again.final_status, again.info_hash, again.recheck_status) == ("in_progress", late.info_hash, "pending")
    assert adapter.rechecked == [late.info_hash] and adapter.started == [] and adapter.stopped == []


def test_a_torrent_elsewhere_after_an_earlier_timeout_is_still_not_followed(db_session, tmp_path):
    # Stesso hash, ma in un'altra cartella: non l'ha mandato la prima
    # esecuzione (ce l'ha messo un altro programma), e non si tocca.
    from nazgarr.adapters.torrent_client.base import TorrentAlreadyInClientError

    seed_job, _save_path = _timed_out_execution(db_session, tmp_path)
    foreign = _foreign_torrent(save_path="/other/downloads", state="downloading")
    adapter = ReseedClient(add_error=TorrentAlreadyInClientError("already there"), torrents=[foreign])

    with pytest.raises(executor.ExecutionError, match="already had this torrent"):
        executor.retry_from_scratch(db_session, seed_job, adapter)

    assert (adapter.rechecked, adapter.started, adapter.stopped) == ([], [], [])


def _gone_execution(db_session, tmp_path):
    """Un'esecuzione riuscita il cui torrent è poi sparito dal client
    (GONE_FROM_CLIENT, review.reconcile_pending_seed_jobs)."""
    _root, review = _movie_review(db_session, tmp_path)
    first = ReseedClient()
    seed_job = executor.execute_review(db_session, review, first)
    seed_job.final_status, seed_job.error_message = "failed", executor.GONE_FROM_CLIENT
    db_session.commit()
    return seed_job, first.added[0].save_path


def test_a_retry_never_follows_a_torrent_that_came_back_somewhere_else(db_session, tmp_path):
    """Revisione dell'audit 2026-10-18 (R5): il torrent di Nazgarr sparisce,
    un altro programma riaggiunge lo stesso hash nella sua cartella, e il
    retry lo seguiva: in download lo fermava, fermo al 100% lo avviava."""
    from nazgarr.adapters.torrent_client.base import ClientTorrentInfo

    seed_job, _save_path = _gone_execution(db_session, tmp_path)
    back = ClientTorrentInfo(info_hash="deadbeef", name="Movie.2024.mkv", save_path="/other/downloads",
                             state="downloading")
    adapter = ReseedClient(torrents=[back],
                           status=TorrentStatus("deadbeef", "downloading", "failed", 0.4, incomplete=True))

    with pytest.raises(executor.RetryRefused) as refused:
        executor.adopt_for_retry(db_session, seed_job, adapter)

    assert refused.value.code == "seed_job_torrent_not_ours"
    assert adapter.stopped == adapter.started == adapter.rechecked == []
    assert seed_job.final_status == "failed"


def test_a_retry_follows_its_torrent_back_where_it_was_added(db_session, tmp_path):
    from nazgarr.adapters.torrent_client.base import ClientTorrentInfo

    seed_job, save_path = _gone_execution(db_session, tmp_path)
    back = ClientTorrentInfo(info_hash="deadbeef", name="Movie.2024.mkv", save_path=save_path + "/",
                             state="stoppedDL")
    adapter = ReseedClient(torrents=[back], recheck_status="failed", progress=0.0)

    assert executor.adopt_for_retry(db_session, seed_job, adapter) is seed_job

    assert adapter.rechecked == ["deadbeef"] and adapter.started == []
    assert (seed_job.final_status, seed_job.recheck_status) == ("in_progress", "pending")


# --- claim della resurrection prima dell'aggiunta (decisione dell'utente, 2026-11-10)


class _WebTracker:
    """L'adapter del tracker per il claim: i seeder dall'API, poi il sito."""

    def __init__(self, seeders=0, result="claimed", client=None):
        self.seeders, self.result, self.client = seeders, result, client
        self.claims, self.details = [], []

    def get_torrent_detail(self, torrent_id):
        self.details.append(torrent_id)
        if isinstance(self.seeders, Exception):
            raise self.seeders
        return type("Detail", (), {"seeders": self.seeders})()

    def claim_resurrection(self, torrent_id):
        # Quante aggiunte al client e quanti hardlink c'erano al momento del claim.
        self.claims.append((torrent_id, len(self.client.added)))
        from nazgarr.adapters.tracker.base import TrackerWebResult
        return TrackerWebResult(self.result, "HTTP 503" if self.result == "failed" else None)

    def close(self):
        pass


def _with_web_tracker(monkeypatch, db_session, review, **kwargs):
    review.candidate.tracker.history_session_cookie = "remember_web_x=1"
    db_session.commit()
    adapter = ReseedClient()
    web = _WebTracker(client=adapter, **kwargs)
    monkeypatch.setattr(executor.adapter_factory, "build_tracker_adapter", lambda row: web)
    return adapter, web


def test_a_dead_torrent_is_claimed_before_the_hardlink_and_the_client(db_session, tmp_path, monkeypatch):
    root, review = _movie_review(db_session, tmp_path)
    adapter, web = _with_web_tracker(monkeypatch, db_session, review)
    links_at_claim = []
    claim = web.claim_resurrection
    web.claim_resurrection = lambda torrent_id: (
        links_at_claim.append((root / "torrents" / "Movie.2024.mkv").exists()), claim(torrent_id))[1]

    seed_job = executor.execute_review(db_session, review, adapter)

    assert web.details == ["1"] and web.claims == [("1", 0)] and links_at_claim == [False]
    assert seed_job.resurrection == "claimed" and len(adapter.added) == 1


def test_an_alive_torrent_is_not_claimed_and_the_site_is_not_asked(db_session, tmp_path, monkeypatch):
    _root, review = _movie_review(db_session, tmp_path)
    adapter, web = _with_web_tracker(monkeypatch, db_session, review, seeders=4)

    seed_job = executor.execute_review(db_session, review, adapter)

    assert web.claims == [] and seed_job.resurrection is None and len(adapter.added) == 1


def test_without_a_website_session_nothing_is_asked_to_the_tracker(db_session, tmp_path, monkeypatch):
    _root, review = _movie_review(db_session, tmp_path)
    monkeypatch.setattr(executor.adapter_factory, "build_tracker_adapter",
                        lambda row: pytest.fail("no session: no tracker adapter"))
    adapter = ReseedClient()

    seed_job = executor.execute_review(db_session, review, adapter)

    assert seed_job.resurrection is None and len(adapter.added) == 1


def test_when_the_api_does_not_answer_the_torrent_page_decides(db_session, tmp_path, monkeypatch):
    _root, review = _movie_review(db_session, tmp_path)
    adapter, web = _with_web_tracker(monkeypatch, db_session, review, seeders=RuntimeError("API down"),
                                     result="not_offered")

    seed_job = executor.execute_review(db_session, review, adapter)

    assert web.claims == [("1", 0)] and seed_job.resurrection == "not_offered" and len(adapter.added) == 1


@pytest.mark.parametrize("result", ["session_expired", "failed"])
def test_a_failed_claim_stops_the_execution_before_hardlink_and_client(db_session, tmp_path, monkeypatch, result):
    root, review = _movie_review(db_session, tmp_path)
    adapter, _web = _with_web_tracker(monkeypatch, db_session, review, result=result)

    with pytest.raises(executor.ExecutionError, match="resurrection claim on t did not go through"):
        executor.execute_review(db_session, review, adapter)

    seed_job = db_session.query(SeedJob).one()
    assert seed_job.final_status == "failed" and executor.NOTHING_SENT in seed_job.error_message
    assert not (root / "torrents" / "Movie.2024.mkv").exists() and adapter.added == []
