"""Webhook di Radarr/Sonarr (nazgarr/integrations/arr_webhooks.py): un disco vero in
tmp_path, scansionato due volte come farebbe la pipeline, poi gli eventi che
Radarr/Sonarr manderebbero, con i loro percorsi (diversi da quelli di Nazgarr)."""

import base64
import json
import os
from datetime import UTC, datetime, timedelta

from nazgarr.core.models import (
    ArrWebhookEvent,
    Disk,
    FileChange,
    MediaFile,
    RadarrInstance,
    SeedFile,
    SonarrInstance,
)
from nazgarr.integrations import arr_webhooks
from nazgarr.library import file_changes, resolution, scanner
from nazgarr.library.scan_state import is_current, latest_scan_by_disk
from nazgarr.reseed import pipeline

LATER = datetime.now(UTC) + timedelta(minutes=1)  # oltre la quiete di QUIET


def _finish(session, run):
    run.finished_at = datetime.now(UTC)
    session.commit()


def _library(db_session, tmp_path):
    root = tmp_path / "disk"
    (root / "media" / "movies").mkdir(parents=True)
    (root / "media" / "tv").mkdir(parents=True)
    (root / "torrents").mkdir()
    disk = Disk(label="d", root_path=str(root), media_rel_path="media", torrents_rel_path="torrents")
    db_session.add(disk)
    db_session.commit()
    for _ in range(2):  # due scansioni: un file sparito torna a quella prima
        _scan(db_session, disk)
    return root, disk


def _scan(db_session, disk):
    """Una scansione con la sua fotografia per la Dashboard, come la pipeline."""
    run = pipeline.start_run(db_session, "manual")
    scanner.scan_disk(db_session, disk, run)
    _finish(db_session, run)
    file_changes.record_changes(db_session, run)
    return run


def _radarr(db_session):
    instance = RadarrInstance(label="r", base_url="http://radarr:7878", api_key="k", webhook_token="secret-token")
    db_session.add(instance)
    db_session.commit()
    return instance


def _event(db_session, source, instance, payload):
    arr_webhooks.receive(db_session, source, instance, payload)
    arr_webhooks.process_pending(db_session, now=LATER)
    return db_session.query(ArrWebhookEvent).order_by(ArrWebhookEvent.id.desc()).first()


def _current(db_session, disk, relative):
    row = db_session.query(MediaFile).filter_by(disk_id=disk.id, relative_path=relative).one_or_none()
    return row is not None and is_current(row, latest_scan_by_disk(db_session, MediaFile))


def _download(path, size, tmdb=603, deleted=None):
    return {
        "eventType": "Download", "isUpgrade": bool(deleted),
        "movie": {"id": 1, "title": "The Matrix", "year": 1999, "tmdbId": tmdb,
                  "folderPath": "/movies/The Matrix (1999)"},
        "movieFile": {"path": path, "relativePath": os.path.basename(path), "size": size},
        "deletedFiles": deleted or [],
    }


def test_an_import_enters_the_library_identified_and_linked_to_its_torrent(db_session, tmp_path):
    root, disk = _library(db_session, tmp_path)
    # Il torrent era già in seed (scansionato), Radarr lo importa con un hardlink.
    torrent_file = root / "torrents" / "The.Matrix.1999.1080p.BluRay-GRP.mkv"
    torrent_file.write_bytes(b"x" * 100)
    _scan(db_session, disk)
    (root / "media" / "movies" / "The Matrix (1999)").mkdir()
    library_file = root / "media" / "movies" / "The Matrix (1999)" / "The Matrix (1999).mkv"
    os.link(torrent_file, library_file)

    event = _event(db_session, "radarr", _radarr(db_session),
                   _download("/movies/The Matrix (1999)/The Matrix (1999).mkv", 100))

    assert event.status == "done", event.detail
    media_file = db_session.query(MediaFile).filter_by(
        relative_path="media/movies/The Matrix (1999)/The Matrix (1999).mkv").one()
    assert _current(db_session, disk, media_file.relative_path)
    assert (media_file.media_item.tmdb_id, media_file.resolver_source) == (603, "radarr")
    seed = db_session.query(SeedFile).filter_by(relative_path="torrents/The.Matrix.1999.1080p.BluRay-GRP.mkv").one()
    assert seed.media_file_id == media_file.id  # non più un torrent orfano


def test_an_upgrade_replaces_the_old_file_and_a_delete_removes_it(db_session, tmp_path):
    root, disk = _library(db_session, tmp_path)
    folder = root / "media" / "movies" / "The Matrix (1999)"
    folder.mkdir()
    (folder / "old.mkv").write_bytes(b"o" * 50)
    radarr = _radarr(db_session)
    _event(db_session, "radarr", radarr, _download("/movies/The Matrix (1999)/old.mkv", 50))
    (folder / "old.mkv").unlink()
    (folder / "new.mkv").write_bytes(b"n" * 80)

    event = _event(db_session, "radarr", radarr, _download(
        "/movies/The Matrix (1999)/new.mkv", 80,
        deleted=[{"path": "/movies/The Matrix (1999)/old.mkv", "size": 50}]))

    assert "replaced" in event.detail
    assert _current(db_session, disk, "media/movies/The Matrix (1999)/new.mkv")
    assert not _current(db_session, disk, "media/movies/The Matrix (1999)/old.mkv")

    (folder / "new.mkv").unlink()
    _event(db_session, "radarr", radarr, {"eventType": "MovieFileDelete", "movie": {"tmdbId": 603},
                                          "movieFile": {"path": "/movies/The Matrix (1999)/new.mkv", "size": 80}})
    assert not _current(db_session, disk, "media/movies/The Matrix (1999)/new.mkv")


def test_a_sonarr_rename_keeps_the_identity_under_the_new_name(db_session, tmp_path):
    root, disk = _library(db_session, tmp_path)
    season = root / "media" / "tv" / "Show" / "Season 01"
    season.mkdir(parents=True)
    (season / "Show.S01E01.mkv").write_bytes(b"e" * 30)
    sonarr = SonarrInstance(label="s", base_url="http://sonarr:8989", api_key="k", webhook_token="t")
    db_session.add(sonarr)
    db_session.commit()
    series = {"title": "Show", "tmdbId": 1399, "titleSlug": "show", "path": "/tv/Show"}
    _event(db_session, "sonarr", sonarr, {
        "eventType": "Download", "series": series, "episodes": [{"seasonNumber": 1, "episodeNumber": 1}],
        "episodeFile": {"path": "/tv/Show/Season 01/Show.S01E01.mkv", "size": 30}})
    os.rename(season / "Show.S01E01.mkv", season / "Show - S01E01 - Pilot.mkv")

    event = _event(db_session, "sonarr", sonarr, {"eventType": "Rename", "series": series, "renamedEpisodeFiles": [{
        "path": "/tv/Show/Season 01/Show - S01E01 - Pilot.mkv", "size": 30,
        "previousPath": "/tv/Show/Season 01/Show.S01E01.mkv"}]})

    assert event.detail == "1 of 1 renamed file(s) updated"
    renamed = db_session.query(MediaFile).filter_by(
        relative_path="media/tv/Show/Season 01/Show - S01E01 - Pilot.mkv").one()
    assert (renamed.media_item.tmdb_id, renamed.media_item.season_number, renamed.media_item.episode_number) == (
        1399, 1, 1)
    assert not _current(db_session, disk, "media/tv/Show/Season 01/Show.S01E01.mkv")


def test_a_path_from_the_webhook_never_leaves_the_media_folders(db_session, tmp_path):
    root, disk = _library(db_session, tmp_path)
    (root / "secret.mkv").write_bytes(b"s" * 10)  # sul disco, fuori dalle cartelle media
    os.symlink(root / "secret.mkv", root / "media" / "movies" / "link.mkv")

    assert arr_webhooks.locate(db_session, "/movies/../../secret.mkv", 10) is None
    assert arr_webhooks.locate(db_session, "/movies/link.mkv", 10) is None


def test_events_wait_while_a_run_is_in_progress(db_session, tmp_path):
    _library(db_session, tmp_path)
    radarr = _radarr(db_session)
    arr_webhooks.receive(db_session, "radarr", radarr, {"eventType": "Test"})
    pipeline.start_run(db_session, "manual")  # una run in corso

    assert arr_webhooks.process_pending(db_session, now=LATER) == 0
    assert db_session.query(ArrWebhookEvent).one().status == "pending"


def _basic(password):
    return {"Authorization": "Basic " + base64.b64encode(f"radarr:{password}".encode()).decode()}


def test_the_endpoint_wants_the_password_of_that_instance(client):
    created = client.post("/api/radarr-instances", json={
        "label": "r", "base_url": "http://radarr:7878", "api_key": "k"}).json()
    setup = client.post(f"/api/radarr-instances/{created['id']}/webhook").json()
    assert setup["path"] == f"/api/arr-hooks/radarr/{created['id']}"
    anonymous = {"Authorization": ""}  # niente login di Nazgarr: solo la password del webhook

    assert client.post(setup["path"], json={"eventType": "Test"}, headers=anonymous).status_code == 401
    assert client.post(setup["path"], json={"eventType": "Test"}, headers=_basic("wrong")).status_code == 401
    assert client.post(setup["path"], json={"eventType": "Test"}, headers=_basic(setup["token"])).json() == {
        "status": "accepted"}
    assert client.post(f"{setup['path']}?token={setup['token']}", json={"eventType": "Grab"},
                       headers=anonymous).json() == {"status": "ignored"}

    [instance] = client.get("/api/radarr-instances").json()
    assert instance["webhook_enabled"] is True
    assert instance["webhook_last_event"]["event_type"] == "Grab"
    # Rigenerata: la password vecchia non vale più.
    client.post(f"/api/radarr-instances/{created['id']}/webhook")
    assert client.post(setup["path"], content=json.dumps({"eventType": "Test"}),
                       headers=_basic(setup["token"])).status_code == 401


def test_an_import_gets_its_poster_and_shows_on_the_dashboard_as_from_radarr(db_session, tmp_path, monkeypatch):
    # Segnalato (2026-10-07): un file importato dal webhook restava senza
    # poster, e la Dashboard non diceva che era arrivato.
    root, disk = _library(db_session, tmp_path)
    torrent_file = root / "torrents" / "Movie.2020.mkv"
    torrent_file.write_bytes(b"m" * 64)
    _scan(db_session, disk)
    (root / "media" / "movies" / "Movie (2020)").mkdir()
    os.link(torrent_file, root / "media" / "movies" / "Movie (2020)" / "Movie (2020).mkv")
    downloaded = []
    monkeypatch.setattr(resolution, "download_poster", lambda d, kind, tmdb, path: downloaded.append((tmdb, path)))
    payload = _download("/movies/Movie (2020)/Movie (2020).mkv", 64, tmdb=42)
    payload["movie"]["images"] = [{"coverType": "poster", "remoteUrl": "https://image.tmdb.org/t/p/original/abc.jpg"}]

    arr_webhooks.receive(db_session, "radarr", _radarr(db_session), payload)
    arr_webhooks.process_pending(db_session, now=LATER, data_dir=str(tmp_path / "data"))

    media_file = db_session.query(MediaFile).filter_by(
        relative_path="media/movies/Movie (2020)/Movie (2020).mkv").one()
    assert media_file.media_item.tmdb_poster_path == "/abc.jpg"
    assert downloaded == [(42, "/abc.jpg")]
    changes = {(c.side, c.change, c.origin) for c in db_session.query(FileChange).all()}
    assert ("media", "added", "radarr") in changes
    # La fotografia è aggiornata: la scansione dopo non lo conta di nuovo.
    run = _scan(db_session, disk)
    again = db_session.query(FileChange).filter_by(run_id=run.id).all()
    assert not any(c.relative_path.endswith("Movie (2020).mkv") for c in again)


def test_the_torrent_it_was_imported_from_counts_as_seeding_right_away(db_session, tmp_path, monkeypatch):
    # Segnalato (2026-10-09): dopo un import il file risultava non in seed
    # fino alla scansione, e la ricerca dopo l'importazione lo proponeva in reseed.
    from nazgarr.adapters.torrent_client.base import ClientTorrentFileInfo, ClientTorrentInfo
    from nazgarr.core.models import ClientTorrent, TorrentClient
    from nazgarr.integrations import adapter_factory
    from nazgarr.library.seeding import seeding_media_file_ids
    from nazgarr.reseed import matching

    root, disk = _library(db_session, tmp_path)
    # Scaricato dopo l'ultima scansione: né il file né il torrent sono ancora noti a Nazgarr.
    download = root / "torrents" / "Movie.2020.1080p.WEB-DL-GRP" / "Movie.2020.1080p.WEB-DL-GRP.mkv"
    download.parent.mkdir()
    download.write_bytes(b"m" * 64)
    (root / "media" / "movies" / "Movie (2020)").mkdir()
    os.link(download, root / "media" / "movies" / "Movie (2020)" / "Movie (2020).mkv")
    client = TorrentClient(label="qbit", adapter_type="qbittorrent", base_url="http://q", username="u", password="p")
    db_session.add(client)
    db_session.commit()
    info_hash = "ab" * 20
    asked = []

    class Client:
        def get_torrent_info(self, wanted):
            asked.append(wanted)
            return ClientTorrentInfo(
                info_hash=wanted, name="Movie.2020.1080p.WEB-DL-GRP", save_path=str(root / "torrents"),
                state="uploading", tracker_url="https://tracker.example/announce/key",
                files=[ClientTorrentFileInfo("Movie.2020.1080p.WEB-DL-GRP/Movie.2020.1080p.WEB-DL-GRP.mkv", 64)])

    monkeypatch.setattr(adapter_factory, "build_torrent_client_adapter", lambda row: Client())
    payload = _download("/movies/Movie (2020)/Movie (2020).mkv", 64, tmdb=42)
    payload["downloadId"] = info_hash.upper()  # come lo manda Radarr

    event = _event(db_session, "radarr", _radarr(db_session), payload)

    assert "its torrent is in qbit (1 file(s) linked)" in event.detail
    media_file = db_session.query(MediaFile).filter_by(
        relative_path="media/movies/Movie (2020)/Movie (2020).mkv").one()
    assert media_file.id in seeding_media_file_ids(db_session)
    assert media_file.id not in {mf.id for mf in matching.orphan_media_files(db_session)}  # niente reseed
    assert db_session.query(ClientTorrent).one().info_hash == info_hash

    # Un altro evento dello stesso download (un altro episodio, una rinomina): il client non si richiama.
    from nazgarr.integrations import arr_webhooks as hooks
    assert hooks._register_download(db_session, {"downloadId": info_hash.upper()}) is None
    assert asked == [info_hash]
