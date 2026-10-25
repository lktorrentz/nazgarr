from datetime import UTC, datetime

import pytest

from nazgarr.adapters.tracker.base import TorrentCandidate
from nazgarr.core.models import Candidate, Disk, MediaFile, MediaItem, SeedFile, Tracker
from nazgarr.reseed import matching, pipeline


def _tc(**overrides) -> TorrentCandidate:
    defaults = dict(
        torrent_id_remote="1", info_hash=None, name="Movie.2024.mkv", size_bytes=1000,
        file_list=["Movie.2024.mkv"], mediainfo_unique_id=None, download_link=None,
    )
    defaults.update(overrides)
    return TorrentCandidate(**defaults)


class FakeTrackerAdapter:
    def __init__(self, candidates):
        self._candidates = candidates

    def search_by_tmdb(self, tmdb_id):
        return self._candidates


def _anchor_ctx(db_session, candidates, size=1000, path="movies/Movie (2024)/Movie.2024.mkv"):
    tracker = Tracker(label="t", adapter_type="unit3d", base_url="https://t.example", api_token="x")
    disk = Disk(label="d", root_path="/media", media_rel_path="movies")
    db_session.add_all([tracker, disk])
    db_session.commit()
    run = pipeline.start_run(db_session, "manual")
    item = MediaItem(content_type="movie", tmdb_id=157336)
    db_session.add(item)
    db_session.commit()
    anchor = MediaFile(
        disk_id=disk.id, relative_path=path, size_bytes=size, st_dev=1, inode=1,
        media_item_id=item.id, last_scan_id=run.id, last_seen_at=datetime.now(UTC),
    )
    db_session.add(anchor)
    db_session.commit()
    ctx = matching.MatchContext(db_session, tracker, FakeTrackerAdapter(candidates), "media_to_torrent")
    return ctx, anchor


def _match_one(db_session, tc, **kwargs):
    ctx, anchor = _anchor_ctx(db_session, [tc], **kwargs)
    (candidate,) = matching.match_file(ctx, anchor, anchor.media_item_id, 157336)
    return candidate


def test_size_only_match(db_session, monkeypatch):
    monkeypatch.setattr(matching, "compute_unique_id", lambda path: None)
    c = _match_one(db_session, _tc(size_bytes=1000))
    assert c.confidence == matching.CONFIDENCE_SIZE_ONLY
    assert c.size_match is True
    assert c.mediainfo_match is None


def test_no_match_on_size_mismatch(db_session):
    c = _match_one(db_session, _tc(size_bytes=999))
    assert c.confidence == matching.CONFIDENCE_NO_MATCH
    assert c.size_match is False


def test_mediainfo_match_raises_confidence(db_session, monkeypatch):
    monkeypatch.setattr(matching, "compute_unique_id", lambda path: "abc123")
    c = _match_one(db_session, _tc(size_bytes=1000, mediainfo_unique_id="abc123"))
    assert c.confidence == matching.CONFIDENCE_SIZE_AND_MEDIAINFO_MATCH
    assert c.mediainfo_match is True


def test_mediainfo_mismatch_zeroes_confidence(db_session, monkeypatch):
    monkeypatch.setattr(matching, "compute_unique_id", lambda path: "different")
    c = _match_one(db_session, _tc(size_bytes=1000, mediainfo_unique_id="abc123"))
    assert c.confidence == matching.CONFIDENCE_NO_MATCH
    assert c.mediainfo_match is False
    assert c.ambiguity_reason == "mediainfo_mismatch"


def _torrent_bytes(folder: str, files: dict[str, int]) -> bytes:
    from tests.test_season_pack import _bencode

    return _bencode({"info": {
        "name": folder, "piece length": 16, "pieces": b"\x00" * 20 * -(-sum(files.values()) // 16),
        "files": [{"length": size, "path": [name]} for name, size in files.items()],
    }})


def test_movie_with_an_nfo_takes_its_folder_and_names_from_the_torrent(db_session, monkeypatch):
    # Prima: qualunque torrent con più file finiva a 0 come "season pack".
    # E il catalogo UNIT3D spesso non riporta la cartella: la struttura vera
    # (cartella + nomi) si legge dal .torrent, o il seed finirebbe nel posto
    # sbagliato.
    monkeypatch.setattr(matching, "compute_unique_id", lambda path: None)
    torrent = _torrent_bytes("Movie.2024.1080p-GRP", {"Movie.2024.1080p-GRP.mkv": 1000, "Movie.2024.1080p-GRP.nfo": 50})
    ctx, anchor = _anchor_ctx(db_session, [_tc(
        size_bytes=1050, folder=None, download_link="https://t.example/torrent/download/1.k",
        file_list=["Movie.2024.1080p-GRP.mkv", "Movie.2024.1080p-GRP.nfo"],
        file_sizes={"Movie.2024.1080p-GRP.mkv": 1000, "Movie.2024.1080p-GRP.nfo": 50},
    )])
    ctx.tracker_adapter.download_torrent = lambda url: torrent

    (c,) = matching.match_file(ctx, anchor, anchor.media_item_id, 157336)

    assert c.folder == "Movie.2024.1080p-GRP"
    by_path = {f.torrent_path: f for f in c.files}
    assert by_path["Movie.2024.1080p-GRP.mkv"].media_file_id is not None
    assert by_path["Movie.2024.1080p-GRP.nfo"].media_file_id is None  # lo scaricherà il client


def test_a_single_file_inside_a_folder_takes_the_folder_from_the_torrent(db_session, monkeypatch):
    # Segnalato (2026-10-06): un file solo dentro una cartella, che il catalogo
    # non riporta; il .torrent si leggeva solo per i torrent con più file, e
    # l'hardlink finiva fuori dalla cartella col recheck del client che falliva.
    monkeypatch.setattr(matching, "compute_unique_id", lambda path: None)
    torrent = _torrent_bytes("Movie.2024.1080p-GRP", {"Movie.2024.1080p-GRP.mkv": 1000})
    ctx, anchor = _anchor_ctx(db_session, [_tc(
        size_bytes=1000, folder=None, download_link="https://t.example/torrent/download/2.k",
        file_list=["Movie.2024.1080p-GRP.mkv"], file_sizes={"Movie.2024.1080p-GRP.mkv": 1000},
    )])
    ctx.tracker_adapter.download_torrent = lambda url: torrent

    (c,) = matching.match_file(ctx, anchor, anchor.media_item_id, 157336)

    assert c.folder == "Movie.2024.1080p-GRP"
    assert [f.torrent_path for f in c.files] == ["Movie.2024.1080p-GRP.mkv"]


def test_a_single_file_without_a_readable_torrent_keeps_the_catalog_shape(db_session, monkeypatch):
    # Niente .torrent (link mancante o tracker giù): resta proponibile, il
    # controllo completo prima di eseguire ne correggerà la cartella.
    monkeypatch.setattr(matching, "compute_unique_id", lambda path: None)
    c = _match_one(db_session, _tc(
        size_bytes=1000, folder=None, download_link=None,
        file_list=["Movie.2024.1080p-GRP.mkv"], file_sizes={"Movie.2024.1080p-GRP.mkv": 1000},
    ))
    assert c.confidence > matching.CONFIDENCE_NO_MATCH and c.folder is None


def test_multi_file_candidate_without_a_readable_torrent_is_not_executable(db_session, monkeypatch):
    monkeypatch.setattr(matching, "compute_unique_id", lambda path: None)
    c = _match_one(db_session, _tc(
        size_bytes=1050, folder=None, download_link=None,
        file_list=["Movie.2024.1080p-GRP.mkv", "Movie.2024.1080p-GRP.nfo"],
        file_sizes={"Movie.2024.1080p-GRP.mkv": 1000, "Movie.2024.1080p-GRP.nfo": 50},
    ))
    assert (c.confidence, c.ambiguity_reason) == (matching.CONFIDENCE_NO_MATCH, "torrent_structure_unknown")


def test_season_pack_without_local_episodes_is_partial(db_session):
    ctx, anchor = _anchor_ctx(db_session, [], path="tv/Show/Season 01/Show - S01E01.mkv")
    anchor.media_item.content_type = "tv"
    anchor.media_item.season_number, anchor.media_item.episode_number = 1, 1
    db_session.commit()
    ctx.tracker_adapter = FakeTrackerAdapter([_tc(
        size_bytes=2000, folder="Show.S01", file_list=["Show.S01E01.mkv", "Show.S01E02.mkv"],
        file_sizes={"Show.S01E01.mkv": 1000, "Show.S01E02.mkv": 1000},
    )])

    (c,) = matching.match_file(ctx, anchor, anchor.media_item_id, 157336)

    assert c.confidence == matching.CONFIDENCE_NO_MATCH
    assert c.ambiguity_reason == "season_pack_partial"


def test_match_file_persists_one_candidate_per_torrent_candidate(db_session, monkeypatch):
    monkeypatch.setattr(matching, "compute_unique_id", lambda path: None)
    ctx, anchor = _anchor_ctx(
        db_session, [_tc(torrent_id_remote="1", size_bytes=1000), _tc(torrent_id_remote="2", size_bytes=999)]
    )

    candidates = matching.match_file(ctx, anchor, anchor.media_item_id, 157336)

    assert len(candidates) == 2
    assert db_session.query(Candidate).count() == 2
    by_remote = {c.torrent_id_remote: c for c in candidates}
    assert by_remote["1"].confidence == matching.CONFIDENCE_SIZE_ONLY
    assert by_remote["2"].confidence == matching.CONFIDENCE_NO_MATCH
    assert all(c.direction == "media_to_torrent" for c in candidates)


def _make_disk(db_session):
    disk = Disk(label="d", root_path="/mnt/d", media_rel_path="movies", torrents_rel_path="torrents")
    db_session.add(disk)
    db_session.commit()
    return disk


def test_orphan_media_files_excludes_hardlinked(db_session):
    disk = _make_disk(db_session)
    run = pipeline.start_run(db_session, "manual")
    item = MediaItem(content_type="movie", tmdb_id=1)
    db_session.add(item)
    db_session.commit()

    linked = MediaFile(
        disk_id=disk.id, relative_path="movies/linked.mkv", size_bytes=1,
        st_dev=1, inode=1, media_item_id=item.id, last_scan_id=run.id, last_seen_at=datetime.now(UTC),
    )
    orphan = MediaFile(
        disk_id=disk.id, relative_path="movies/orphan.mkv", size_bytes=1,
        st_dev=1, inode=2, media_item_id=item.id, last_scan_id=run.id, last_seen_at=datetime.now(UTC),
    )
    db_session.add_all([linked, orphan])
    db_session.commit()
    seed_file = SeedFile(
        disk_id=disk.id, relative_path="torrents/linked.mkv", size_bytes=1, st_dev=1, inode=1,
        media_file_id=linked.id, last_scan_id=run.id, last_seen_at=datetime.now(UTC),
    )
    db_session.add(seed_file)
    db_session.commit()
    # Un hardlink che nessun client segue (es. scaricato a mano e importato da
    # Radarr nella cartella torrent) non è un seed: il file si cerca lo stesso.
    assert {mf.id for mf in matching.orphan_media_files(db_session)} == {linked.id, orphan.id}

    _seed_in_client(db_session, seed_file, "https://a.example")

    orphans = matching.orphan_media_files(db_session)

    assert [mf.id for mf in orphans] == [orphan.id]


def _seed_in_client(db_session, seed_file, announce):
    from nazgarr.core.models import ClientTorrent, ClientTorrentFile, TorrentClient

    client = db_session.query(TorrentClient).first()
    if client is None:
        client = TorrentClient(label="q", adapter_type="qbittorrent", base_url="http://q")
        db_session.add(client)
        db_session.commit()
    torrent = ClientTorrent(torrent_client_id=client.id, info_hash=f"h{seed_file.id}{announce}", name="x",
                            save_path="/x", state="uploading", tracker_url=announce, last_polled_at=datetime.now(UTC))
    db_session.add(torrent)
    db_session.commit()
    db_session.add(ClientTorrentFile(client_torrent_id=torrent.id, path_in_torrent=seed_file.relative_path,
                                     size_bytes=1, seed_file_id=seed_file.id, last_scan_id=seed_file.last_scan_id))
    db_session.commit()


def test_a_file_seeding_on_one_tracker_is_searched_on_the_others(db_session):
    from nazgarr.core import settings_repo

    disk = _make_disk(db_session)
    run = pipeline.start_run(db_session, "manual")
    item = MediaItem(content_type="movie", tmdb_id=1)
    a = Tracker(label="A", adapter_type="unit3d", base_url="https://a.example", api_token="x")
    b = Tracker(label="B", adapter_type="unit3d", base_url="https://b.example", api_token="x")
    db_session.add_all([item, a, b])
    db_session.commit()
    mf = MediaFile(disk_id=disk.id, relative_path="movies/x.mkv", size_bytes=1, st_dev=1, inode=1,
                   media_item_id=item.id, last_scan_id=run.id, last_seen_at=datetime.now(UTC))
    db_session.add(mf)
    db_session.commit()
    sf = SeedFile(disk_id=disk.id, relative_path="torrents/x.mkv", size_bytes=1, st_dev=1, inode=1,
                  media_file_id=mf.id, last_scan_id=run.id, last_seen_at=datetime.now(UTC))
    db_session.add(sf)
    db_session.commit()
    _seed_in_client(db_session, sf, "https://www.a.example")

    assert matching.orphan_media_files(db_session, tracker=a) == []
    assert [f.id for f in matching.orphan_media_files(db_session, tracker=b)] == [mf.id]  # cross-seed

    settings_repo.set_setting(db_session, "cross_seed_search", "false")
    assert matching.orphan_media_files(db_session, tracker=b) == []


def test_the_matching_index_holds_only_files_from_the_latest_scan(db_session, tmp_path):
    """Un file sparito dal disco (visto solo da una scansione vecchia) non si
    propone come sorgente di un torrent."""
    from datetime import UTC, datetime

    from nazgarr.core.models import Disk, MediaFile, SeedFile
    from nazgarr.reseed import pipeline
    from nazgarr.torrents.layout import LocalFiles

    disk = Disk(label="d", root_path=str(tmp_path), media_rel_path="media", torrents_rel_path="torrents")
    db_session.add(disk)
    db_session.commit()
    old, new = pipeline.start_run(db_session, "manual"), pipeline.start_run(db_session, "manual")
    now = datetime.now(UTC)
    for path, scan, inode in (("media/Gone.mkv", old, 1), ("media/Here.mkv", new, 2)):
        db_session.add(MediaFile(disk_id=disk.id, relative_path=path, size_bytes=10, st_dev=1, inode=inode,
                                 last_scan_id=scan.id, last_seen_at=now))
    for path, scan, inode in (("torrents/Gone.mkv", old, 3), ("torrents/Here.mkv", new, 4)):
        db_session.add(SeedFile(disk_id=disk.id, relative_path=path, size_bytes=10, st_dev=1, inode=inode,
                                last_scan_id=scan.id, last_seen_at=now))
    disk.media_scan_id = disk.seed_scan_id = new.id
    db_session.commit()

    local = LocalFiles.load(db_session)

    assert [mf.relative_path for files in local.media_by_key.values() for mf in files] == ["media/Here.mkv"]
    assert list(local.seed_by_path) == [(disk.id, "torrents/Here.mkv")]


# --- Torznab: un catalogo che non elenca i file (issue #33) ------------------


class _MediaQueryAdapter(FakeTrackerAdapter):
    def __init__(self, candidates):
        super().__init__(candidates)
        self.queries = []

    def search_media(self, query):
        self.queries.append(query)
        return self._candidates


def test_search_media_receives_the_library_identity(db_session, monkeypatch):
    monkeypatch.setattr(matching, "compute_unique_id", lambda path: None)
    ctx, anchor = _anchor_ctx(db_session, [])
    anchor.media_item.title, anchor.media_item.year, anchor.media_item.imdb_id = "Movie", 2024, "tt0816692"
    ctx.tracker_adapter = _MediaQueryAdapter([])

    matching.match_file(ctx, anchor, anchor.media_item_id, 157336)

    (query,) = ctx.tracker_adapter.queries
    assert (query.tmdb_id, query.content_type, query.title, query.year, query.imdb_id, query.season) == (
        157336, "movie", "Movie", 2024, "tt0816692", None)


def test_an_unlisted_torrent_takes_its_structure_from_the_torrent(db_session, monkeypatch):
    # Il titolo Torznab non è un nome di file: la struttura (e la verifica)
    # vengono dal .torrent, scaricato perché la dimensione può contenere il file.
    monkeypatch.setattr(matching, "compute_unique_id", lambda path: None)
    torrent = _torrent_bytes("Movie.2024.1080p-GRP", {"Movie.2024.1080p-GRP.mkv": 1000, "Movie.2024.1080p-GRP.nfo": 50})
    ctx, anchor = _anchor_ctx(db_session, [_tc(
        name="Movie 2024 1080p GRP", size_bytes=1050, folder=None, file_list=None, file_sizes=None,
        download_link="https://prowlarr.example/1/download?apikey=k",
    )])
    downloads = []
    ctx.tracker_adapter.download_torrent = lambda url: downloads.append(url) or torrent

    (c,) = matching.match_file(ctx, anchor, anchor.media_item_id, 157336)

    assert downloads == ["https://prowlarr.example/1/download?apikey=k"]
    assert c.folder == "Movie.2024.1080p-GRP"
    assert {f.torrent_path for f in c.files} == {"Movie.2024.1080p-GRP.mkv", "Movie.2024.1080p-GRP.nfo"}


def test_an_unlisted_torrent_too_small_or_too_big_is_not_downloaded(db_session, monkeypatch):
    monkeypatch.setattr(matching, "compute_unique_id", lambda path: None)
    candidates = [
        _tc(torrent_id_remote="small", size_bytes=999, file_list=None, file_sizes=None, download_link="https://x/1"),
        _tc(torrent_id_remote="pack", size_bytes=10**10, file_list=None, file_sizes=None, download_link="https://x/2"),
    ]
    ctx, anchor = _anchor_ctx(db_session, candidates)
    ctx.tracker_adapter.download_torrent = lambda url: pytest.fail(f"scaricato {url}")

    assert matching.match_file(ctx, anchor, anchor.media_item_id, 157336, frozenset({matching.SINGLE})) == []


def test_an_unlisted_single_file_without_a_torrent_is_not_executable(db_session, monkeypatch):
    # Con file_count=1 si valuta dal catalogo, ma senza .torrent il titolo non
    # può fare da nome del file: niente proposta eseguibile.
    monkeypatch.setattr(matching, "compute_unique_id", lambda path: None)
    c = _match_one(db_session, _tc(
        name="Movie 2024 1080p GRP", size_bytes=1000, folder=None, file_list=None, file_sizes=None,
        file_count=1, download_link=None,
    ))
    assert c.confidence == matching.CONFIDENCE_NO_MATCH
