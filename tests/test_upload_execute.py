import io
import json
import os
import types
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import torf

from nazgarr.adapters.torrent_client.base import (
    ClientTorrentFileInfo,
    ClientTorrentInfo,
    TorrentAlreadyInClientError,
    TorrentClientAdapter,
    TorrentStatus,
)
from nazgarr.adapters.tracker.base import TrackerWebResult, UploadedTorrent, UploadError
from nazgarr.core import settings_repo
from nazgarr.core.db_utils import as_utc
from nazgarr.core.fs_scope import ScopeViolation
from nazgarr.core.models import Disk, TrackerUploadProfile
from nazgarr.torrents import create as torrent_create
from nazgarr.torrents.metainfo import compute_info_hash
from nazgarr.upload import auto as upload_auto
from nazgarr.upload import decision as upload_decision
from nazgarr.upload import execute as upload_execute
from nazgarr.upload import extras_check as upload_extras
from nazgarr.upload import jobs as upload_jobs
from nazgarr.upload import verify as upload_verify
from nazgarr.upload.jobs import UploadJobError
from nazgarr.upload.worker import UploadWorker
from tests.fakes import RecordingClient
from tests.upload_helpers import InlineExecutor, make_client, make_tracker, write_video

KB = 1024


def _rejected(message="rejected: validation", status=422):
    """Come UNIT3D: il tracker risponde con un 4xx, l'errore di httpx come causa."""
    request = httpx.Request("POST", "https://tracker/api/torrents/upload")
    error = UploadError(message)
    error.__cause__ = httpx.HTTPStatusError(f"Client error '{status}'", request=request,
                                            response=httpx.Response(status, request=request))
    return error


def _unreachable(message="connection refused"):
    """Il tracker non risponde: la richiesta non è mai partita."""
    error = UploadError(message)
    error.__cause__ = httpx.ConnectError(message)
    return error


class _Tracker:
    def __init__(self, torrent_id="100", error=None, torrent_bytes=None):
        self.torrent_id, self.error, self.torrent_bytes = torrent_id, error, torrent_bytes
        self.uploads = []
        self.changed_content = False
        self.catalog = []  # quello che la ricerca trova (il dupe check prima di un upload programmato)
        # Il claim della resurrection: l'esito, e i torrent chiesti con quante
        # aggiunte al client c'erano già in quel momento.
        self.claim_result = TrackerWebResult("no_session")
        self.claims = []
        self.client = None

    def claim_resurrection(self, torrent_id_remote):
        self.claims.append((torrent_id_remote, len(self.client.added) if self.client is not None else None))
        return self.claim_result

    def search_by_tmdb(self, tmdb_id):
        return self.catalog

    def upload_torrent(self, fields, torrent_path):
        if self.error:
            raise self.error
        self.uploads.append((fields, torrent_path))
        return UploadedTorrent(self.torrent_id, f"https://tracker/torrent/download/{self.torrent_id}.key")

    def download_torrent(self, url):
        if url.startswith("https://tracker/torrent/download/"):
            # Come UNIT3D: riscrive il campo source, quindi un altro info hash.
            with open(self.uploads[-1][1], "rb") as f:
                torrent = torf.Torrent.read_stream(io.BytesIO(f.read()))
            torrent.source = f"Tracker{self.torrent_id}"
            if self.changed_content:
                torrent.metainfo["info"]["name"] = "Renamed by the tracker"
            return torrent.dump()
        return self.torrent_bytes


class _Client(RecordingClient):
    """Il client dei test: RecordingClient (tests/fakes.py), il contratto
    come lo seguono gli adapter inclusi (la firma vera di add_torrent, il
    recheck sempre chiesto, aggiunge fermo, sa saltare il recheck, start,
    stop, recheck), più quello che guarda il flusso degli upload. Prima era
    un finto suo, fuori dal contratto: senza stop(), senza can_add_stopped
    né can_skip_recheck, il flusso prendeva le strade di un plugin vecchio
    (audit 2026-10-18, finding 151). Un test che vuole proprio quel plugin
    lo dice (can_add_stopped = False, stop = None).

    I torrent che conosce sono quelli in save_paths (info hash -> cartella):
    un test che ne toglie uno simula un torrent sparito dal client, e
    get_torrent_status allora solleva. reported_path è la cartella che il
    client dice di usare, se diversa; held_recheck l'esito del recheck ("ok"
    di default), e lo stato dice fermo finché start()."""

    held_recheck = "ok"

    def __init__(self):
        super().__init__()
        self.save_paths: dict[str, str] = {}
        self.reported_path = None
        self.files = {}  # info hash -> (nome, file del torrent)

    @property
    def skipped(self) -> list[bool]:  # per ogni aggiunta: senza il recheck del client?
        return [added.options["skip_check_verified"] for added in self.added]

    @property
    def stopped_adds(self) -> list[bool]:  # per ogni aggiunta: fermo (stopped=True)?
        return [added.options["stopped"] for added in self.added]

    @property
    def labels(self) -> list[tuple]:  # (categoria, tag) di ogni aggiunta
        return [(added.options["category"], added.options["tags"]) for added in self.added]

    def add_torrent(
        self, torrent_file_or_url: str, save_path: str, force_recheck: bool = True,
        expected_info_hash: str | None = None, skip_check_verified: bool = False,
        category: str | None = None, tags: list[str] | None = None, content_layout: str = "Original",
        stopped: bool = False,
    ) -> str:
        info_hash = super().add_torrent(
            torrent_file_or_url, save_path, force_recheck=force_recheck, expected_info_hash=expected_info_hash,
            skip_check_verified=skip_check_verified, category=category, tags=tags, content_layout=content_layout,
            stopped=stopped,
        )
        torrent = torf.Torrent.read(torrent_file_or_url)
        assert info_hash == torrent.infohash
        self.save_paths[info_hash] = save_path
        self.files[info_hash] = (torrent.name,
                                 [ClientTorrentFileInfo("/".join(f.parts), f.size) for f in torrent.files])
        return info_hash

    def get_torrent_info(self, info_hash):
        if info_hash not in self.save_paths:
            return None
        name, files = self.files.get(info_hash, ("?", []))
        save_path = self.reported_path or self.save_paths[info_hash]
        return ClientTorrentInfo(info_hash=info_hash, name=name, save_path=save_path, state="uploading",
                                 tracker_url="https://tracker/announce", files=files)

    def list_torrents(self, on_progress=None):
        return [self.get_torrent_info(info_hash) for info_hash in self.save_paths]

    def stop(self, info_hash):
        super().stop(info_hash)
        if info_hash in self.started:
            self.started.remove(info_hash)

    def get_torrent_status(self, info_hash):
        if info_hash not in self.save_paths:
            raise ValueError("not in the client")
        running = info_hash in self.started
        done = self.held_recheck == "ok"
        state = ("uploading" if done else "downloading") if running else ("stoppedUP" if done else "stoppedDL")
        return TorrentStatus(info_hash=info_hash, state=state, recheck_status=self.held_recheck,
                             progress=1.0 if done else 0.9, incomplete=not done, amount_left=0 if done else 1000)


def _adds(client) -> list[tuple[str, str]]:
    """(.torrent, cartella) di ogni aggiunta al client."""
    return [(added.source, added.save_path) for added in client.added]


class _Chain:
    def upload(self, path):
        return f"https://img.example/{os.path.basename(path)}"


@pytest.fixture
def env(db_session, tmp_path, monkeypatch):
    """Disco con libreria e cartella torrent, due tracker, un client, fake
    per tracker, client, screenshot e image host."""
    root = tmp_path / "disk"
    (root / "torrents").mkdir(parents=True)
    disk = Disk(label="d", root_path=str(root), media_rel_path="media", torrents_rel_path="torrents")
    db_session.add(disk)
    db_session.commit()
    client_row = make_client(db_session)
    trackers = {"a": _Tracker("100"), "b": _Tracker("200")}
    for label in trackers:
        tracker = make_tracker(db_session, label, torrent_client_id=client_row.id)
        profile = db_session.get(TrackerUploadProfile, tracker.id)
        profile.description_template = "{{ mediainfo }}{% for u in screenshot_urls %} [img]{{ u }}[/img]{% endfor %}"
    db_session.commit()
    client = _Client()
    for fake in trackers.values():
        fake.client = client
    monkeypatch.setattr(upload_execute.adapter_factory, "build_tracker_adapter", lambda t: trackers[t.label])
    monkeypatch.setattr(upload_execute.adapter_factory, "build_torrent_client_adapter", lambda row: client)
    monkeypatch.setattr(upload_execute.adapter_factory, "build_image_host_chain", lambda session: _Chain())

    def fake_screenshots(video, out_dir, count=4, tonemap=False, should_stop=None):
        os.makedirs(out_dir, exist_ok=True)
        paths = [os.path.join(out_dir, f"{i}.png") for i in range(count)]
        for p in paths:
            open(p, "wb").close()
        return paths

    monkeypatch.setattr(upload_execute.screenshots, "generate_screenshots", fake_screenshots)
    return {"root": root, "disk": disk, "trackers": trackers, "client": client, "client_row": client_row}


def _approved(db_session, env, relative_path, decisions, file_naming="original", scheduled_at=None, **job_values):
    job = upload_jobs.create_job(db_session, env["disk"], relative_path)
    # I test di prima dei nomi generati tengono i nomi della sorgente.
    if file_naming:
        job.overrides_json = json.dumps({"file_naming": file_naming})
    upload_jobs.transition(
        db_session, job, "identifying", "awaiting_decision", tmdb_id=603, imdb_id="tt0133093", title="The Matrix",
        year=1999, content_type=job_values.pop("content_type", "movie"), kind=job_values.pop("kind", "movie"),
        mediainfo_text="MI", **job_values,
    )
    for target in job.targets:
        target.status = "awaiting_decision"
    db_session.commit()
    upload_decision.approve(db_session, job, [
        {"target_id": t.id, **decisions[t.tracker.label]} for t in job.targets
    ], scheduled_at=scheduled_at)
    return job


def _upload(name, **extra):
    return {"action": "upload", "name": name, "category_id": 1, "type_id": 4, "resolution_id": 3,
            "flags": {"anonymous": True}, **extra}


def _run(db_session, tmp_path, job):
    factory = lambda: db_session.__class__(bind=db_session.get_bind())  # noqa: E731
    UploadWorker(
        factory, str(tmp_path / "data"), light_executor=InlineExecutor(), heavy_executor=InlineExecutor(),
        verify_executor=InlineExecutor(),
    ).kick(job.id, job.status)
    db_session.expire_all()
    return job


def test_uploads_to_every_tracker_and_seeds_from_hardlinks(db_session, tmp_path, env):
    video = write_video(env["root"] / "media" / "The.Matrix.1999.1080p.WEB-DL.H.264-GRP.mkv", 300 * KB)
    job = _approved(db_session, env, "media/" + video.name, {"a": _upload("Matrix A"), "b": _upload("Matrix B")})

    _run(db_session, tmp_path, job)

    assert job.status == "done"
    a, b = job.targets
    assert (a.status, a.torrent_id_remote, b.torrent_id_remote) == ("done", "100", "200")
    # Stessi piece, un .torrent per tracker: info hash diversi.
    ta, tb = torf.Torrent.read(a.torrent_path), torf.Torrent.read(b.torrent_path)
    assert ta.metainfo["info"]["pieces"] == tb.metainfo["info"]["pieces"]
    assert a.info_hash != b.info_hash
    fields, _ = env["trackers"]["a"].uploads[0]
    assert (fields.name, fields.imdb_id, fields.anonymous, fields.season_number) == ("Matrix A", "0133093", True, None)
    assert "https://img.example/0.png" in fields.description
    linked = env["root"] / "torrents" / video.name
    assert os.path.samefile(linked, video)
    assert _adds(env["client"]) == [(a.torrent_path, str(env["root"] / "torrents")),
                                   (b.torrent_path, str(env["root"] / "torrents"))]
    # Il torrent l'ha creato Nazgarr da quei file: niente recheck del client.
    assert env["client"].skipped == [True, True] and env["client"].rechecked == []
    # Nel client il .torrent del tracker (source riscritto), non quello inviato.
    seeded = torf.Torrent.read(a.torrent_path)
    assert seeded.source == "Tracker100" and a.info_hash == seeded.infohash
    assert seeded.infohash != torf.Torrent.read(env["trackers"]["a"].uploads[0][1]).infohash
    assert json.loads(job.screenshot_urls_json) == [f"https://img.example/{i}.png" for i in range(4)]


def test_the_uploaded_torrent_and_its_files_are_registered_right_away(db_session, tmp_path, env):
    """Audit 2026-10-18 (finding 171): il flusso degli upload aggiungeva il
    torrent al client senza registrarlo. Fino alla run dopo il file
    risultava non in seed, e "Cerca ora" poteva proporre l'upload stesso
    come reseed."""
    from nazgarr.core.models import ClientTorrent, ClientTorrentFile, MediaFile, RunLog, SeedFile
    from nazgarr.library import hardlinks

    video = write_video(env["root"] / "media" / "The.Matrix.1999.1080p.WEB-DL.H.264-GRP.mkv", 300 * KB)
    run = RunLog(run_type="manual", started_at=datetime.now(UTC), finished_at=datetime.now(UTC))
    db_session.add(run)
    db_session.commit()
    st = video.stat()
    media = MediaFile(disk_id=env["disk"].id, relative_path="media/" + video.name, size_bytes=st.st_size,
                      st_dev=st.st_dev, inode=st.st_ino, last_scan_id=run.id, last_seen_at=datetime.now(UTC))
    env["disk"].media_scan_id = env["disk"].seed_scan_id = run.id
    db_session.add(media)
    db_session.commit()
    job = _approved(db_session, env, "media/" + video.name, {"a": _upload("Matrix A"), "b": {"action": "skip"}})

    _run(db_session, tmp_path, job)

    assert job.status == "done", [e.code for e in job.events]
    target = job.targets[0]
    torrent = db_session.query(ClientTorrent).filter_by(info_hash=target.info_hash).one()
    seed = db_session.query(SeedFile).filter_by(relative_path="torrents/" + video.name).one()
    assert seed.media_file_id == media.id
    assert db_session.query(ClientTorrentFile).filter_by(client_torrent_id=torrent.id).one().seed_file_id == seed.id
    assert hardlinks.media_links(db_session, [media.id]) == {media.id: [(seed.id, seed.relative_path)]}


def test_a_failed_registration_never_fails_the_upload(db_session, tmp_path, env, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(upload_execute.seed_refresh, "register_client_torrent", broken)
    video = write_video(env["root"] / "media" / "The.Matrix.1999.1080p.WEB-DL.H.264-GRP.mkv", 300 * KB)
    job = _approved(db_session, env, "media/" + video.name, {"a": _upload("Matrix A"), "b": {"action": "skip"}})

    _run(db_session, tmp_path, job)

    assert job.status == "done"
    assert (job.targets[0].status, job.targets[0].error_message) == ("done", None)
    assert "added_to_client_verified" in [e.code for e in job.events]


def _locked_after_the_add(monkeypatch):
    """Il primo commit di _register_seed fallisce come con il DB occupato
    oltre il busy_timeout (una run che indicizza nello stesso momento)."""
    from sqlalchemy.exc import OperationalError

    original = upload_execute._register_seed

    def register(session, *args, **kwargs):
        def locked():
            del session.commit  # solo questa volta
            raise OperationalError("COMMIT", {}, Exception("database is locked"))

        session.commit = locked
        return original(session, *args, **kwargs)

    monkeypatch.setattr(upload_execute, "_register_seed", register)


def test_a_locked_db_right_after_the_add_never_fails_the_upload(db_session, tmp_path, env, monkeypatch):
    """Prima il commit iniziale di _register_seed era fuori dal suo try: il
    torrent in seed nel client e il target seed_failed."""
    _locked_after_the_add(monkeypatch)
    video = write_video(env["root"] / "media" / "The.Matrix.1999.1080p.WEB-DL.H.264-GRP.mkv", 300 * KB)
    job = _approved(db_session, env, "media/" + video.name, {"a": _upload("Matrix A"), "b": {"action": "skip"}})

    _run(db_session, tmp_path, job)

    target = job.targets[0]
    assert job.status == "done"
    assert (target.status, target.error_message) == ("done", None)
    assert _adds(env["client"]) == [(target.torrent_path, str(env["root"] / "torrents"))]


def test_a_locked_db_right_after_a_retried_add_keeps_the_chosen_client(db_session, tmp_path, env, monkeypatch):
    """Un nuovo tentativo senza client all'upload sceglie quello di oggi: si
    scrive prima dell'aggiunta, così il rollback dopo non lo perde."""
    video = write_video(env["root"] / "media" / "Movie.2024.mkv", 100 * KB)
    job = _approved(db_session, env, "media/" + video.name, {"a": _upload("A"), "b": {"action": "skip"}})
    client = env["client"]
    real_add = client.add_torrent
    monkeypatch.setattr(client, "add_torrent", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    _run(db_session, tmp_path, job)
    target = job.targets[0]
    target.torrent_client_id, target.error_message = None, "no_client"
    db_session.commit()
    monkeypatch.setattr(client, "add_torrent", real_add)
    _locked_after_the_add(monkeypatch)

    upload_execute.retry_seed(db_session, job, target)

    db_session.expire_all()
    # In attesa del recheck (decisione dell'utente, 2026-10-19): il rollback
    # del DB occupato non la perde.
    assert (target.torrent_client_id, target.error_message) == (env["client_row"].id, "seed_recheck_pending")
    assert _adds(client) == [(target.torrent_path, str(env["root"] / "torrents"))]
    assert {"seed_retried", "added_to_client_held"} <= {e.code for e in job.events}


def test_a_source_inside_the_seeding_folder_seeds_in_place(db_session, tmp_path, env):
    folder = env["root"] / "torrents" / "Show.S01.1080p-GRP"
    for e in (1, 2):
        write_video(folder / f"Show.S01E0{e}.mkv", 200 * KB)
    write_video(folder / "Sample" / "sample.mkv", 10 * KB)
    job = _approved(db_session, env, "torrents/Show.S01.1080p-GRP", {"a": _upload("Show S01"), "b": {"action": "skip"}},
                    content_type="tv", kind="season_pack", seasons_json="[1]")

    _run(db_session, tmp_path, job)

    assert job.status == "done"
    torrent = torf.Torrent.read(job.targets[0].torrent_path)
    assert sorted(str(f) for f in torrent.files) == ["Show.S01.1080p-GRP/Show.S01E01.mkv",
                                                     "Show.S01.1080p-GRP/Show.S01E02.mkv"]
    assert _adds(env["client"]) == [(job.targets[0].torrent_path, str(env["root"] / "torrents"))]
    fields, _ = env["trackers"]["a"].uploads[0]
    assert (fields.season_number, fields.episode_number) == (1, 0)


def _reseed_job(db_session, tmp_path, env, verified_hash=None, extras=None, recorded=True, seeders=None):
    """Un job col solo target "a" in reseed del torrent 7, già verificato
    (verified_hash: l'info hash letto dal controllo; di default quello del
    .torrent che il tracker restituisce). extras: {nome: byte} che il
    torrent del tracker ha e la sorgente no. recorded: il controllo vero
    (upload_verify.run_check), con l'identità dei file letti; False, come
    una verifica di prima del 2026-10-21, che non se la segnava."""
    video = write_video(env["root"] / "media" / "Matrix" / "matrix.mkv", 300 * KB)
    other = tmp_path / "tracker-layout" / "The.Matrix.1999.1080p-GRP"
    other.mkdir(parents=True)
    os.link(video, other / "The.Matrix.1999.1080p-GRP.mkv")
    for name, content in (extras or {}).items():
        (other / name).write_bytes(content)
    torrent_path, _ = torrent_create.create_torrent(str(other), "https://a/announce", str(tmp_path / "t.torrent"))
    with open(torrent_path, "rb") as f:
        env["trackers"]["a"].torrent_bytes = f.read()
    info_hash = verified_hash or compute_info_hash(env["trackers"]["a"].torrent_bytes)
    job = upload_jobs.create_job(db_session, env["disk"], "media/Matrix")
    verification = {"status": "passed", "info_hash": info_hash}
    if recorded and verified_hash is None:
        verification = upload_verify.run_check(job, env["trackers"]["a"].torrent_bytes)
        assert verification["status"] == "passed" and verification["files"]
    job.targets[0].dupes_json = json.dumps([{"torrent_id_remote": "7", "verdict": "identical",
                                             "download_link": "https://a/dl/7", "verification": verification,
                                             "seeders": seeders}])
    db_session.commit()
    upload_jobs.transition(db_session, job, "identifying", "awaiting_decision", tmdb_id=603, content_type="movie",
                           kind="movie")
    for target in job.targets:
        target.status = "awaiting_decision"
    db_session.commit()
    upload_decision.approve(db_session, job, [
        {"target_id": job.targets[0].id, "action": "reseed", "reseed_torrent_id": "7"},
        {"target_id": job.targets[1].id, "action": "skip"},
    ])
    return job, video


def test_reseed_links_the_files_with_the_tracker_names(db_session, tmp_path, env):
    job, video = _reseed_job(db_session, tmp_path, env)

    _run(db_session, tmp_path, job)

    assert job.status == "done", [e.code for e in job.events]
    linked = env["root"] / "torrents" / "The.Matrix.1999.1080p-GRP" / "The.Matrix.1999.1080p-GRP.mkv"
    assert os.path.samefile(linked, video)
    assert _adds(env["client"]) == [(job.targets[0].torrent_path, str(env["root"] / "torrents"))]
    assert env["client"].skipped == [False]  # il .torrent è del tracker: recheck del client
    assert env["trackers"]["a"].uploads == []  # un reseed non pubblica niente


# --- reseed del flusso degli upload: fermi fino al recheck riuscito (decisione dell'utente, 2026-10-19)


def _events(job, code):
    return [json.loads(e.params_json or "{}") for e in job.events if e.code == code]


@pytest.fixture(autouse=True)
def _no_missing_memory():
    """Da quando manca un torrent è in memoria nel modulo: da zero a ogni test."""
    upload_execute._missing_since.clear()
    yield
    upload_execute._missing_since.clear()


def _missing_for(db_session, job, target, minutes, since_hold=True):
    """Il torrent manca da `minutes` minuti per questo processo; since_hold:
    anche l'ultima messa in attesa è di allora."""
    past = datetime.now(UTC) - timedelta(minutes=minutes)
    upload_execute._missing_since[target.id] = past
    if since_hold:
        for event in job.events:
            if event.target_id == target.id:
                event.created_at = past.replace(tzinfo=None)
        db_session.commit()


def test_a_reseed_enters_the_client_stopped_and_starts_after_its_recheck(db_session, tmp_path, env):
    """Avviato, con un recheck fallito il client scaricava i piece mancanti
    dentro gli hardlink, cioè nei file della libreria."""
    job, _video = _reseed_job(db_session, tmp_path, env)
    client = env["client"]

    _run(db_session, tmp_path, job)

    target = job.targets[0]
    assert job.status == "done" and target.status == "done"
    assert client.stopped_adds == [True] and client.skipped == [False] and not getattr(client, "started", [])
    assert target.error_message == "seed_recheck_pending"
    assert _events(job, "added_to_client_held")[0]["allowed"] == 0

    upload_execute.reconcile_held_seeds(db_session, str(tmp_path / "data"))

    assert client.started == [target.info_hash] and target.error_message is None
    assert "seed_recheck_passed" in [e.code for e in job.events]
    assert upload_execute.reconcile_held_seeds(db_session) == 0  # niente più in attesa


def test_an_upload_of_a_new_torrent_still_starts_at_once(db_session, tmp_path, env):
    """Un upload (il torrent appena creato da Nazgarr) resta com'era."""
    video = write_video(env["root"] / "media" / "The.Matrix.1999.1080p.WEB-DL.H.264-GRP.mkv", 300 * KB)
    job = _approved(db_session, env, "media/" + video.name, {"a": _upload("Matrix A"), "b": {"action": "skip"}})

    _run(db_session, tmp_path, job)

    assert env["client"].stopped_adds == [False] and env["client"].skipped == [True]
    assert job.targets[0].error_message is None


def test_a_failed_recheck_leaves_the_reseed_stopped_and_retry_rechecks_it(db_session, tmp_path, env):
    job, _video = _reseed_job(db_session, tmp_path, env)
    client = env["client"]
    client.held_recheck = "failed"
    _run(db_session, tmp_path, job)
    target = job.targets[0]

    upload_execute.reconcile_held_seeds(db_session, str(tmp_path / "data"))

    assert target.error_message == "seed_recheck_failed" and not getattr(client, "started", [])
    assert _events(job, "seed_recheck_failed")[0]["state"] == "stoppedDL"
    assert job.status == "done"  # il reseed è nel client: fermo, da riprovare col seed
    upload_execute.reconcile_held_seeds(db_session)
    assert not getattr(client, "started", [])  # un esito fallito non si rilegge più

    # "Riprova il seed": un nuovo recheck del torrent fermo, mai un avvio né una seconda aggiunta.
    upload_execute.retry_seed(db_session, job, target)
    assert client.rechecked == [target.info_hash] and len(client.added) == 1
    assert target.error_message == "seed_recheck_pending" and not getattr(client, "started", [])
    assert "seed_recheck_requested" in [e.code for e in job.events]
    client.held_recheck = "ok"  # i file ora sono giusti
    upload_execute.reconcile_held_seeds(db_session)
    assert client.started == [target.info_hash] and target.error_message is None


# --- upload.seed_checked: l'esito del recheck per webhook e notifiche (decisione dell'utente, 2026-10-21)


def _seed_checked(db_session):
    from nazgarr.core.models import Event

    return [json.loads(e.payload_json) for e in db_session.query(Event).filter_by(name="upload.seed_checked")
            .order_by(Event.id)]


@pytest.fixture
def seed_checked_hook(db_session):
    from nazgarr.core.models import Webhook

    db_session.add(Webhook(name="hook", url="https://hooks.example/n", secret="s",
                           events_json=json.dumps(["upload.seed_checked", "upload.finished"])))
    db_session.commit()


@pytest.mark.parametrize("recheck", ["ok", "failed"])
def test_the_outcome_of_a_held_recheck_is_an_event_once(db_session, tmp_path, env, seed_checked_hook, recheck):
    """upload.finished arriva col target ancora in attesa; l'esito del
    recheck arriva dopo, una volta sola, come upload.seed_checked."""
    from nazgarr.core.models import Event

    job, _video = _reseed_job(db_session, tmp_path, env)
    client = env["client"]
    client.held_recheck = "pending"
    _run(db_session, tmp_path, job)
    target = job.targets[0]
    [finished] = [json.loads(e.payload_json) for e in db_session.query(Event).filter_by(name="upload.finished")]
    assert finished["targets"][0]["error"] == "seed_recheck_pending"

    upload_execute.reconcile_held_seeds(db_session)
    assert _seed_checked(db_session) == []  # ancora in corso: niente

    client.held_recheck = recheck
    upload_execute.reconcile_held_seeds(db_session)
    upload_execute.reconcile_held_seeds(db_session)

    [event] = _seed_checked(db_session)
    assert {k: event[k] for k in ("job_id", "target_id", "tracker", "client", "action", "torrent_id_remote",
                                  "info_hash")} == {
        "job_id": job.id, "target_id": target.id, "tracker": "a", "client": env["client_row"].label,
        "action": "reseed", "torrent_id_remote": "7", "info_hash": target.info_hash}
    if recheck == "ok":
        assert (event["outcome"], event["state"], event["stopped"]) == ("started", None, None)
    else:
        assert (event["outcome"], event["state"], event["stopped"]) == ("recheck_failed", "stoppedDL", True)


def test_a_held_torrent_gone_from_the_client_is_an_event(db_session, tmp_path, env, seed_checked_hook):
    job, _video = _reseed_job(db_session, tmp_path, env)
    client = env["client"]
    _run(db_session, tmp_path, job)
    target = job.targets[0]
    client.save_paths.pop(target.info_hash)  # tolto a mano dal client
    _missing_for(db_session, job, target, minutes=60)

    upload_execute.reconcile_held_seeds(db_session)

    assert target.error_message == "seed_recheck_failed"
    assert [(e["outcome"], e["client"]) for e in _seed_checked(db_session)] == [("gone", env["client_row"].label)]


def test_a_held_reseed_running_anyway_is_stopped_when_its_recheck_fails(db_session, tmp_path, env):
    """Un plugin senza can_add_stopped lo aggiunge avviato: fallito, si ferma."""
    job, _video = _reseed_job(db_session, tmp_path, env)
    client = env["client"]
    client.can_add_stopped, client.held_recheck = False, "failed"
    _run(db_session, tmp_path, job)
    target = job.targets[0]
    assert client.stopped_adds == [False] and target.error_message == "seed_recheck_pending"
    client.started = [target.info_hash]  # gira

    upload_execute.reconcile_held_seeds(db_session)

    assert client.started == [] and client.stopped == [target.info_hash]
    assert target.error_message == "seed_recheck_failed"
    assert "seed_recheck_failed" in [e.code for e in job.events]


def test_a_held_reseed_that_cannot_be_stopped_says_so(db_session, tmp_path, env):
    job, _video = _reseed_job(db_session, tmp_path, env)
    client = env["client"]
    client.can_add_stopped, client.held_recheck = False, "failed"
    client.stop = None  # un plugin senza stop()
    _run(db_session, tmp_path, job)
    client.started = [job.targets[0].info_hash]  # gira

    upload_execute.reconcile_held_seeds(db_session)

    assert _events(job, "seed_recheck_failed_running")[0]["client"] == env["client_row"].label


def test_the_extras_a_reseed_lacks_still_count_as_a_good_recheck(db_session, tmp_path, env, monkeypatch):
    """Un extra del tracker che la sorgente non ha lo scarica il client
    dopo l'avvio: il recheck a cui manca solo quello è riuscito. Il full
    hash check dell'upload ha già letto proprio questi file (gli hardlink
    hanno il loro inode): niente si rilegge (2026-10-21)."""
    job, _video = _reseed_job(db_session, tmp_path, env, extras={"The.Matrix.nfo": b"n" * 600})
    client = env["client"]
    client.held_recheck = "failed"  # al client mancano 1000 byte
    _run(db_session, tmp_path, job)
    target = job.targets[0]
    held = _events(job, "added_to_client_held")[0]
    assert held["allowed"] >= 1000 and held["missing"] == ["The.Matrix.nfo"]
    monkeypatch.setattr(upload_extras.full_check, "check_pieces_outside", _never_read)

    upload_execute.reconcile_held_seeds(db_session)

    assert client.started == [target.info_hash] and target.error_message is None
    assert "seed_pieces_checking" not in [e.code for e in job.events]


def _never_read(*args, **kwargs):
    raise AssertionError("the files were read again")


# --- la tolleranza degli extra mancanti vale solo per i loro piece (2026-10-21, come i reseed della coda)


@pytest.fixture
def inline_checks(monkeypatch):
    """La coda dei controlli completi nel thread del test."""
    monkeypatch.setattr(upload_extras.full_check, "_executor", InlineExecutor())


def _edit_in_place(path, offset=1000, byte=b"Z"):
    """Un byte cambiato sul posto, stessa dimensione (un header MKV ritoccato)."""
    with open(path, "r+b") as f:
        f.seek(offset)
        f.write(byte)
    st = os.stat(path)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))  # mai la stessa data, anche su FS grossolani


def test_a_video_piece_changed_since_the_check_never_starts_within_the_allowance(
        db_session, tmp_path, env, inline_checks):
    """Il client dice solo quanti byte mancano: un piece diverso del video
    che sta nel margine degli extra passava per un extra, e il torrent
    partiva a riscriverlo dentro gli hardlink, nel file della libreria.
    Ora Nazgarr rilegge (i file non sono più quelli verificati) e il
    torrent resta fermo; "Riprova il seed" ricontrolla sui file di adesso."""
    job, video = _reseed_job(db_session, tmp_path, env, extras={"The.Matrix.nfo": b"n" * 600})
    original = video.read_bytes()[1000:1001]
    client = env["client"]
    client.held_recheck = "failed"  # 1000 byte, dentro il margine
    _run(db_session, tmp_path, job)
    target = job.targets[0]
    _edit_in_place(video)

    upload_execute.reconcile_held_seeds(db_session)

    assert not getattr(client, "started", []) and target.error_message == "seed_recheck_failed"
    assert "seed_pieces_checking" in [e.code for e in job.events]
    [failed] = _events(job, "seed_pieces_failed")
    assert "do not match" in failed["reason"] and "The.Matrix.1999.1080p-GRP.mkv" in failed["reason"]

    _edit_in_place(video, byte=original)  # il file rimesso com'era
    upload_execute.retry_seed(db_session, job, target)
    upload_execute.reconcile_held_seeds(db_session)

    assert client.started == [target.info_hash] and target.error_message is None


def test_files_touched_since_the_check_are_read_again_before_starting(db_session, tmp_path, env, inline_checks):
    """Stessi byte ma un'altra data di modifica: Nazgarr rilegge i piece
    fuori dagli extra, e passati il torrent parte. Così anche una verifica
    di prima del 2026-10-21, che non si segnava i file."""
    job, _video = _reseed_job(db_session, tmp_path, env, extras={"The.Matrix.nfo": b"n" * 600}, recorded=False)
    client = env["client"]
    client.held_recheck = "failed"
    _run(db_session, tmp_path, job)
    target = job.targets[0]

    upload_execute.reconcile_held_seeds(db_session)

    assert client.started == [target.info_hash] and target.error_message is None
    assert [e.code for e in job.events].count("seed_pieces_checking") == 1


def test_an_old_hold_without_missing_never_counts_an_absent_video_as_an_extra(
        db_session, tmp_path, env, inline_checks):
    """Un'aggiunta di prima della regola non segnava gli extra mancanti: si
    ricavano dai file assenti nella cartella di seed, ma mai un video (come
    upload_reseed_missing_video). Contato come extra, ogni suo piece era
    scusato e la lettura "passava": il torrent partiva a scaricarlo."""
    job, _video = _reseed_job(db_session, tmp_path, env, extras={"The.Matrix.nfo": b"n" * 600})
    client = env["client"]
    client.held_recheck = "failed"  # al client mancano 1000 byte, dentro il margine
    _run(db_session, tmp_path, job)
    target = job.targets[0]
    [held] = [e for e in job.events if e.code == "added_to_client_held"]
    params = json.loads(held.params_json)
    del params["missing"]  # com'era scritto prima
    held.params_json = json.dumps(params)
    db_session.commit()
    (env["root"] / "torrents" / "The.Matrix.1999.1080p-GRP" / "The.Matrix.1999.1080p-GRP.mkv").unlink()

    upload_execute.reconcile_held_seeds(db_session)

    assert not getattr(client, "started", []) and target.error_message == "seed_recheck_failed"
    [failed] = _events(job, "seed_pieces_failed")
    assert "The.Matrix.1999.1080p-GRP.mkv" in failed["reason"]


class _Later:
    """La coda dei controlli completi, ferma finché il test non la fa girare."""

    def __init__(self):
        self.jobs = []

    def submit(self, fn, *args):
        self.jobs.append((fn, args))

    def run(self):
        while self.jobs:
            fn, args = self.jobs.pop(0)
            fn(*args)


def test_a_running_reseed_is_stopped_while_nazgarr_checks_and_a_100_percent_meanwhile_does_not_count(
        db_session, tmp_path, env, monkeypatch):
    """Un plugin senza can_add_stopped lo aggiunge avviato: mentre Nazgarr
    legge i piece si ferma, se no scaricherebbe i byte "mancanti", e con
    loro un piece diverso del video. Un 100% del client prima che la
    lettura passi non dice niente dei piece che aveva prima."""
    later = _Later()
    monkeypatch.setattr(upload_extras.full_check, "_executor", later)
    job, _video = _reseed_job(db_session, tmp_path, env, extras={"The.Matrix.nfo": b"n" * 600}, recorded=False)
    client = env["client"]
    client.can_add_stopped, client.held_recheck = False, "failed"
    _run(db_session, tmp_path, job)
    target = job.targets[0]
    client.started = [target.info_hash]  # gira

    upload_execute.reconcile_held_seeds(db_session)

    assert client.stopped == [target.info_hash] and client.started == []
    assert target.error_message == "seed_recheck_pending" and len(later.jobs) == 1
    assert {"seed_pieces_checking", "seed_stopped_while_checking"} <= {e.code for e in job.events}

    client.held_recheck = "ok"  # ha scaricato tutto, prima che Nazgarr finisse
    upload_execute.reconcile_held_seeds(db_session)
    assert client.started == [] and target.error_message == "seed_recheck_pending"

    later.run()
    upload_execute.reconcile_held_seeds(db_session)
    assert client.started == [target.info_hash] and target.error_message is None
    assert len(later.jobs) == 0  # una lettura sola


def test_a_start_refused_after_the_check_passed_does_not_read_again(db_session, tmp_path, env, monkeypatch,
                                                                    inline_checks):
    job, _video = _reseed_job(db_session, tmp_path, env, extras={"The.Matrix.nfo": b"n" * 600}, recorded=False)
    client = env["client"]
    client.held_recheck = "failed"
    _run(db_session, tmp_path, job)
    target = job.targets[0]
    reads = []
    real = upload_extras.full_check.check_pieces_outside
    monkeypatch.setattr(upload_extras.full_check, "check_pieces_outside",
                        lambda *a, **kw: reads.append(1) or real(*a, **kw))
    refused = [True]
    start = client.start

    def flaky_start(info_hash):
        if refused.pop() if refused else False:
            raise RuntimeError("client busy")
        start(info_hash)

    client.start = flaky_start

    upload_execute.reconcile_held_seeds(db_session)
    assert target.error_message == "seed_recheck_pending" and client.started == []
    upload_execute.reconcile_held_seeds(db_session)

    assert client.started == [target.info_hash] and target.error_message is None
    assert reads == [1]  # la verifica passata vale ancora: i file sono quelli che ha letto


def test_a_check_that_cannot_run_is_tried_again_then_fails(db_session, tmp_path, env, monkeypatch, inline_checks):
    job, _video = _reseed_job(db_session, tmp_path, env, extras={"The.Matrix.nfo": b"n" * 600}, recorded=False)
    client = env["client"]
    client.held_recheck = "failed"
    _run(db_session, tmp_path, job)
    target = job.targets[0]

    def broken(*args, **kwargs):
        raise OSError("disk not answering")

    monkeypatch.setattr(upload_extras.full_check, "check_pieces_outside", broken)
    for _ in range(upload_extras.MAX_CHECKS - 1):
        upload_execute.reconcile_held_seeds(db_session)
        assert target.error_message == "seed_recheck_pending"  # un errore, non un esito: si riprova
    upload_execute.reconcile_held_seeds(db_session)

    assert target.error_message == "seed_recheck_failed" and not getattr(client, "started", [])
    assert "disk not answering" in _events(job, "seed_pieces_failed")[0]["reason"]


def test_a_held_reseed_is_left_alone_while_its_job_runs(db_session, tmp_path, env):
    job, _video = _reseed_job(db_session, tmp_path, env)
    _run(db_session, tmp_path, job)
    job.status = "running"  # il worker sullo stesso job (un nuovo tentativo di un altro tracker)
    db_session.commit()

    assert upload_execute.reconcile_held_seeds(db_session) == 0
    assert job.targets[0].error_message == "seed_recheck_pending"


def test_a_held_reseed_starts_while_its_job_waits_in_the_queue(db_session, tmp_path, env):
    """Audit 2026-10-18 (R4): "Riprova" di un altro tracker rimette il job in
    coda, dietro upload che possono hashare per ore. Il reseed già in attesa,
    con il recheck riuscito, restava fermo per tutto quel tempo."""
    job, _video = _reseed_job(db_session, tmp_path, env)
    _run(db_session, tmp_path, job)
    target = job.targets[0]
    job.status = "queued"  # come dopo retry_failed di un altro tracker
    db_session.commit()

    assert upload_execute.reconcile_held_seeds(db_session) == 1
    assert target.error_message is None and target.info_hash in env["client"].started


def test_a_reseed_whose_torrent_left_the_client_is_added_again_on_retry(db_session, tmp_path, env):
    job, _video = _reseed_job(db_session, tmp_path, env)
    client = env["client"]
    _run(db_session, tmp_path, job)
    target = job.targets[0]
    del client.save_paths[target.info_hash]  # tolto a mano dal client

    # Prima, MISSING_TORRENT_GRACE di fila (audit 2026-10-18): un client
    # appena riavviato può non averlo ancora ricaricato.
    upload_execute.reconcile_held_seeds(db_session)
    assert target.error_message == "seed_recheck_pending"
    _missing_for(db_session, job, target, minutes=11)
    upload_execute.reconcile_held_seeds(db_session)
    assert target.error_message == "seed_recheck_failed"
    assert "seed_recheck_torrent_gone" in [e.code for e in job.events]

    upload_execute.retry_seed(db_session, job, target)

    assert _adds(client) == [(target.torrent_path, str(env["root"] / "torrents"))] * 2
    assert client.stopped_adds == [True, True] and target.error_message == "seed_recheck_pending"


def test_a_held_torrent_missing_right_after_a_restart_is_not_given_up(db_session, tmp_path, env):
    """Audit 2026-10-18: un riavvio di tutto il compose, il reconcile dopo
    30 secondi e il client che ricarica ancora i suoi torrent. Anche con
    l'aggiunta di un'ora fa, il torrent manca da poco per questo processo:
    resta in attesa, e quando il client l'ha ricaricato il recheck conta."""
    job, _video = _reseed_job(db_session, tmp_path, env)
    client = env["client"]
    _run(db_session, tmp_path, job)
    target = job.targets[0]
    save_path = client.save_paths.pop(target.info_hash)  # il client non l'ha ancora ricaricato
    _missing_for(db_session, job, target, minutes=60)
    upload_execute._missing_since.clear()  # Nazgarr riavviato: non l'aveva mai visto mancare

    upload_execute.reconcile_held_seeds(db_session)
    assert target.error_message == "seed_recheck_pending"
    assert "seed_recheck_torrent_gone" not in [e.code for e in job.events]

    client.save_paths[target.info_hash] = save_path  # ricaricato
    upload_execute.reconcile_held_seeds(db_session)
    assert target.error_message is None and client.started == [target.info_hash]
    assert target.id not in upload_execute._missing_since


class _PluginLikeClient(_Client):
    """Un plugin che get_torrent_info non ce l'ha: il None di default della
    classe base non chiede niente al client."""

    get_torrent_info = TorrentClientAdapter.get_torrent_info


def test_a_plugin_that_cannot_look_up_one_torrent_never_reads_as_gone(db_session, tmp_path, env):
    """Audit 2026-10-18: lì ogni errore passeggero di get_torrent_status
    diventava "il torrent non c'è più", e il nuovo tentativo lo riaggiungeva
    (upload_already_in_client)."""
    job, _video = _reseed_job(db_session, tmp_path, env)
    client = env["client"]
    client.held_recheck = "failed"
    _run(db_session, tmp_path, job)
    target = job.targets[0]
    client.__class__ = _PluginLikeClient
    save_path = client.save_paths.pop(target.info_hash)  # get_torrent_status solleva
    _missing_for(db_session, job, target, minutes=60)

    upload_execute.reconcile_held_seeds(db_session)
    assert target.error_message == "seed_recheck_pending"

    client.save_paths[target.info_hash] = save_path
    upload_execute.reconcile_held_seeds(db_session)
    assert target.error_message == "seed_recheck_failed"
    # "Riprova il seed": senza poter cercare il torrent, solo il recheck, mai una seconda aggiunta.
    upload_execute.retry_seed(db_session, job, target)
    assert client.rechecked == [target.info_hash] and len(client.added) == 1
    assert target.error_message == "seed_recheck_pending"


def test_a_running_torrent_already_in_the_client_is_stopped_before_its_recheck(
        db_session, tmp_path, env, monkeypatch):
    """Audit 2026-10-18: con hold, uno già nel client e avviato (aggiunto a
    mano, o da un tentativo di prima, che li aggiungeva avviati) veniva
    solo ricontrollato, e con il recheck fallito riprendeva subito a
    scaricare dentro gli hardlink."""
    job, _video = _reseed_job(db_session, tmp_path, env)
    client = env["client"]
    client.held_recheck = "failed"
    expected = compute_info_hash(env["trackers"]["a"].torrent_bytes)
    client.save_paths[expected] = str(env["root"] / "torrents")
    client.started = [expected]  # gira
    calls = []
    real_recheck = client.recheck

    def add(torrent_file, save_path, expected_info_hash=None, **kwargs):
        raise TorrentAlreadyInClientError("already there")

    def stop(info_hash):
        calls.append(("stop", info_hash))
        client.started.remove(info_hash)

    def recheck(info_hash):
        calls.append(("recheck", info_hash))
        real_recheck(info_hash)

    def start(info_hash):
        raise AssertionError("mai avviato in attesa del recheck")

    monkeypatch.setattr(client, "add_torrent", add)
    client.stop, client.recheck, client.start = stop, recheck, start

    _run(db_session, tmp_path, job)

    target = job.targets[0]
    assert calls == [("stop", expected), ("recheck", expected)]
    assert target.error_message == "seed_recheck_pending" and client.started == []
    upload_execute.reconcile_held_seeds(db_session)
    assert target.error_message == "seed_recheck_failed" and client.started == []


def test_a_job_with_a_torrent_waiting_for_its_recheck_is_not_deleted(db_session, tmp_path, env):
    """Audit 2026-10-18: eliminato il job, il torrent restava fermo nel
    client per sempre, senza più niente che lo seguisse."""
    job, _video = _reseed_job(db_session, tmp_path, env)
    _run(db_session, tmp_path, job)
    data_dir = str(tmp_path / "data")

    with pytest.raises(UploadJobError) as refused:
        upload_jobs.delete_job(db_session, job, data_dir)
    assert refused.value.code == "upload_seed_recheck_pending"
    assert refused.value.params == {"tracker": "a", "client": env["client_row"].label}

    upload_execute.reconcile_held_seeds(db_session)  # recheck riuscito: avviato
    upload_jobs.delete_job(db_session, job, data_dir)


def test_a_job_whose_held_torrent_is_in_a_disabled_client_can_be_deleted(db_session, tmp_path, env):
    """Con il client spento il reconcile non lo segue: il job resterebbe
    da non eliminare per sempre."""
    job, _video = _reseed_job(db_session, tmp_path, env)
    _run(db_session, tmp_path, job)
    env["client_row"].enabled = False
    db_session.commit()

    upload_jobs.delete_job(db_session, job, str(tmp_path / "data"))


def test_after_a_failed_recheck_the_files_the_client_renamed_get_their_name_back(db_session, tmp_path, env):
    """Transmission con rename-partial-files rinomina un file incompleto in
    "<nome>.part" già durante il recheck (decisione dell'utente,
    2026-10-19): solo i file che questo reseed ha messo in seed, con il loro
    inode, tornano al loro nome."""
    job, video = _reseed_job(db_session, tmp_path, env)
    client = env["client"]
    client.held_recheck = "failed"
    client.partial_file_suffix = lambda: ".part"
    _run(db_session, tmp_path, job)
    linked = env["root"] / "torrents" / "The.Matrix.1999.1080p-GRP" / "The.Matrix.1999.1080p-GRP.mkv"
    os.rename(linked, str(linked) + ".part")  # come Transmission
    stranger = env["root"] / "torrents" / "Other.mkv"
    stranger.write_bytes(b"x")
    os.rename(stranger, str(stranger) + ".part")  # non è un file di questo reseed

    upload_execute.reconcile_held_seeds(db_session, str(tmp_path / "data"))

    assert linked.is_file() and os.path.samefile(linked, video) and not os.path.exists(str(linked) + ".part")
    assert os.path.exists(str(stranger) + ".part") and not stranger.exists()
    assert _events(job, "seed_partial_names_restored")[0]["count"] == 1


def test_a_renamed_file_is_never_put_back_over_another(db_session, tmp_path, env):
    job, _video = _reseed_job(db_session, tmp_path, env)
    client = env["client"]
    client.held_recheck = "failed"
    client.partial_file_suffix = lambda: ".part"
    _run(db_session, tmp_path, job)
    linked = env["root"] / "torrents" / "The.Matrix.1999.1080p-GRP" / "The.Matrix.1999.1080p-GRP.mkv"
    os.rename(linked, str(linked) + ".part")
    linked.write_bytes(b"someone else's")  # un altro file ha preso il nome nel frattempo

    upload_execute.reconcile_held_seeds(db_session, str(tmp_path / "data"))

    assert linked.read_bytes() == b"someone else's" and os.path.exists(str(linked) + ".part")
    assert not _events(job, "seed_partial_names_restored")


def test_a_reseed_puts_the_trackers_key_back_into_the_stored_link(db_session, tmp_path, env):
    """Audit 2026-10-18 (finding 55): i link dei dupe si salvano senza la
    chiave personale del tracker; il reseed la rimette solo per scaricare."""
    job, _video = _reseed_job(db_session, tmp_path, env)
    target = job.targets[0]
    dupes = json.loads(target.dupes_json)
    dupes[0]["download_link"] = "https://a.example/torrent/download/7.rsskey"
    target.dupes_json = json.dumps(dupes)
    db_session.commit()
    tracker = env["trackers"]["a"]
    tracker.rss_key = "CurrentKey"
    tracker.rewrite_download_link = lambda url: url.replace(".rsskey", ".CurrentKey")
    seen, download = [], tracker.download_torrent
    tracker.download_torrent = lambda url: (seen.append(url), download(url))[1]

    _run(db_session, tmp_path, job)

    assert job.status == "done", [e.code for e in job.events]
    assert seen == ["https://a.example/torrent/download/7.CurrentKey"]
    assert "CurrentKey" not in job.targets[0].dupes_json


# --- claim della resurrection prima del reseed (decisione dell'utente, 2026-11-10)


def test_a_dead_torrent_is_claimed_before_anything_enters_the_client(db_session, tmp_path, env):
    """UNIT3D accetta il claim solo con 0 seeder: prima che il client annunci."""
    job, _video = _reseed_job(db_session, tmp_path, env, seeders=0)
    tracker = env["trackers"]["a"]
    tracker.claim_result = TrackerWebResult("claimed")

    _run(db_session, tmp_path, job)

    assert job.status == "done", [e.code for e in job.events]
    assert tracker.claims == [("7", 0)]  # nessuna aggiunta al client prima del claim
    assert len(_adds(env["client"])) == 1
    assert _events(job, "resurrection_claimed") == [{"torrent": "7", "detail": ""}]


def test_an_alive_torrent_is_never_claimed(db_session, tmp_path, env):
    job, _video = _reseed_job(db_session, tmp_path, env, seeders=3)
    env["trackers"]["a"].claim_result = TrackerWebResult("claimed")

    _run(db_session, tmp_path, job)

    assert job.status == "done" and env["trackers"]["a"].claims == []


@pytest.mark.parametrize("status", ["no_session", "not_offered", "refused"])
def test_a_claim_that_is_not_needed_or_refused_lets_the_reseed_go_on(db_session, tmp_path, env, status):
    """Senza cookie il claim resta a mano; non offerto (già in attesa, anche
    fatto a mano) o rifiutato (un tuo upload) non c'è niente da perdere."""
    job, _video = _reseed_job(db_session, tmp_path, env, seeders=0)
    env["trackers"]["a"].claim_result = TrackerWebResult(status, "why" if status == "refused" else None)

    _run(db_session, tmp_path, job)

    assert job.status == "done" and len(_adds(env["client"])) == 1
    assert bool(_events(job, f"resurrection_{status}")) == (status != "no_session")


@pytest.mark.parametrize(("status", "event"), [("session_expired", "resurrection_session_expired"),
                                               ("failed", "resurrection_failed")])
def test_a_failed_claim_stops_the_reseed_before_files_and_client(db_session, tmp_path, env, status, event):
    """Il claim si perde appena il client annuncia: meglio fermarsi, e
    "Riprova" dopo il claim a mano o un cookie nuovo."""
    job, _video = _reseed_job(db_session, tmp_path, env, seeders=0)
    tracker = env["trackers"]["a"]
    tracker.claim_result = TrackerWebResult(status, "HTTP 503")

    _run(db_session, tmp_path, job)

    target = job.targets[0]
    assert target.status == "failed" and target.error_message == "upload_resurrection_failed"
    assert _adds(env["client"]) == []
    assert not (env["root"] / "torrents" / "The.Matrix.1999.1080p-GRP").exists()
    assert _events(job, event) == [{"torrent": "7", "detail": "HTTP 503"}]

    # Fatto a mano nel frattempo: la pagina non lo offre più, e il reseed riparte.
    tracker.claim_result = TrackerWebResult("not_offered")
    upload_jobs.retry_failed(db_session, job)
    _run(db_session, tmp_path, job)

    assert job.status == "done", [e.code for e in job.events]
    assert len(_adds(env["client"])) == 1


# --- hash check e reseed da soli (upload_identical_auto, decisione dell'utente 2026-11-10)


def _identical_job(db_session, tmp_path, env, b_seeding=True):
    """Il job alla decisione: su "a" il torrent 7 identico e morto, non
    ancora verificato; "b" ha già quei file in seed con un suo torrent."""
    video = write_video(env["root"] / "media" / "Matrix" / "matrix.mkv", 300 * KB)
    other = tmp_path / "tracker-layout" / "The.Matrix.1999.1080p-GRP"
    other.mkdir(parents=True)
    os.link(video, other / "The.Matrix.1999.1080p-GRP.mkv")
    torrent_path, _ = torrent_create.create_torrent(str(other), "https://a/announce", str(tmp_path / "t.torrent"))
    with open(torrent_path, "rb") as f:
        env["trackers"]["a"].torrent_bytes = f.read()
    job = upload_jobs.create_job(db_session, env["disk"], "media/Matrix")
    job.targets[0].dupes_json = json.dumps([{"torrent_id_remote": "7", "verdict": "identical",
                                             "download_link": "https://a/dl/7", "seeders": 0}])
    job.targets[0].suggested_action = "reseed"
    upload_jobs.transition(db_session, job, "identifying", "awaiting_decision", tmdb_id=603, content_type="movie",
                           kind="movie")
    for target in job.targets:
        target.status = "awaiting_decision"
    seeding = {str(job.targets[1].id): {"name": "x", "client": "qbit"}} if b_seeding else {}
    job.analysis_json = json.dumps({"seeding_here": seeding})
    db_session.commit()
    return job


def _worker(db_session, tmp_path):
    factory = lambda: db_session.__class__(bind=db_session.get_bind())  # noqa: E731
    return UploadWorker(factory, str(tmp_path / "data"), light_executor=InlineExecutor(),
                        heavy_executor=InlineExecutor(), verify_executor=InlineExecutor())


def test_the_automatic_reseed_verifies_claims_and_reseeds_an_identical_torrent(
        db_session, tmp_path, env, monkeypatch):
    settings_repo.set_setting(db_session, "upload_identical_auto", "reseed")
    ready = []
    monkeypatch.setattr(upload_auto.events, "emit", lambda session, kind, data: ready.append(kind))
    job = _identical_job(db_session, tmp_path, env)
    tracker = env["trackers"]["a"]
    tracker.claim_result = TrackerWebResult("claimed")

    upload_auto.settle(db_session, job, _worker(db_session, tmp_path), after_analysis=True)
    db_session.expire_all()

    a, b = job.targets
    assert job.status == "done", [e.code for e in job.events]
    assert (a.action, a.reseed_torrent_id, b.action) == ("reseed", "7", "skip")
    assert json.loads(a.dupes_json)[0]["verification"]["status"] == "passed"
    assert tracker.claims == [("7", 0)] and len(_adds(env["client"])) == 1
    codes = [e.code for e in job.events]
    assert codes.index("auto_verify_started") < codes.index("verify_passed") < codes.index("auto_reseed_approved")
    assert "upload.ready" not in ready and "upload.finished" in ready  # niente da decidere, nessun "pronto"
    assert "ready_pending" not in json.loads(job.analysis_json)


def test_with_only_the_automatic_check_the_decision_stays_yours(db_session, tmp_path, env, monkeypatch):
    settings_repo.set_setting(db_session, "upload_identical_auto", "verify")
    ready = []
    monkeypatch.setattr(upload_auto.events, "emit", lambda session, kind, data: ready.append(kind))
    job = _identical_job(db_session, tmp_path, env)

    upload_auto.settle(db_session, job, _worker(db_session, tmp_path), after_analysis=True)
    db_session.expire_all()

    assert job.status == "awaiting_decision" and job.targets[0].action is None
    assert json.loads(job.targets[0].dupes_json)[0]["verification"]["status"] == "passed"
    assert ready == ["upload.ready"]  # dopo la verifica, una volta
    assert _adds(env["client"]) == []


def test_a_tracker_that_needs_you_keeps_the_automatic_reseed_waiting(db_session, tmp_path, env, monkeypatch):
    """"b" non ha né il torrent né i file in seed: un upload è una decisione tua."""
    settings_repo.set_setting(db_session, "upload_identical_auto", "reseed")
    ready = []
    monkeypatch.setattr(upload_auto.events, "emit", lambda session, kind, data: ready.append(kind))
    job = _identical_job(db_session, tmp_path, env, b_seeding=False)

    upload_auto.settle(db_session, job, _worker(db_session, tmp_path), after_analysis=True)
    db_session.expire_all()

    assert job.status == "awaiting_decision" and ready == ["upload.ready"]
    assert json.loads(job.targets[0].dupes_json)[0]["verification"]["status"] == "passed"
    assert "auto_reseed_approved" not in [e.code for e in job.events]


def test_with_the_setting_off_nothing_starts_and_the_job_is_ready_at_once(db_session, tmp_path, env, monkeypatch):
    ready = []
    monkeypatch.setattr(upload_auto.events, "emit", lambda session, kind, data: ready.append(kind))
    job = _identical_job(db_session, tmp_path, env)

    upload_auto.settle(db_session, job, _worker(db_session, tmp_path), after_analysis=True)
    db_session.expire_all()

    assert ready == ["upload.ready"] and "verify_started" not in [e.code for e in job.events]
    assert job.targets[0].status == "awaiting_decision"


def test_an_automatic_check_stopped_by_a_restart_starts_again(db_session, tmp_path, env, monkeypatch):
    """Il riavvio riporta il target alla decisione (verify_interrupted): la
    verifica partita da sola riparte, e l'avviso "upload pronto" arriva."""
    settings_repo.set_setting(db_session, "upload_identical_auto", "verify")
    ready = []
    monkeypatch.setattr(upload_auto.events, "emit", lambda session, kind, data: ready.append(kind))
    job = _identical_job(db_session, tmp_path, env)
    job.analysis_json = json.dumps({**json.loads(job.analysis_json), "ready_pending": True})
    db_session.commit()

    _worker(db_session, tmp_path).resume()
    db_session.expire_all()

    assert json.loads(job.targets[0].dupes_json)[0]["verification"]["status"] == "passed"
    assert ready == ["upload.ready"] and "ready_pending" not in json.loads(job.analysis_json)


def test_a_failed_automatic_check_moves_to_the_next_identical_torrent(db_session, tmp_path, env, monkeypatch):
    settings_repo.set_setting(db_session, "upload_identical_auto", "verify")
    monkeypatch.setattr(upload_auto.events, "emit", lambda session, kind, data: None)
    job = _identical_job(db_session, tmp_path, env)
    target = job.targets[0]
    dupes = json.loads(target.dupes_json)
    target.dupes_json = json.dumps([{**dupes[0], "torrent_id_remote": "6", "download_link": "https://a/dl/6"},
                                    dupes[0]])
    db_session.commit()
    good = env["trackers"]["a"].torrent_bytes
    env["trackers"]["a"].download_torrent = lambda url: good if url.endswith("/7") else b"not a torrent"

    upload_auto.settle(db_session, job, _worker(db_session, tmp_path), after_analysis=True)
    db_session.expire_all()

    statuses = [d["verification"]["status"] for d in json.loads(target.dupes_json)]
    assert statuses == ["error", "passed"]


def test_a_reseed_refuses_a_torrent_other_than_the_verified_one(db_session, tmp_path, env):
    # Il controllo completo ha letto un altro .torrent: quello riscaricato
    # non entra nel client, e niente hardlink.
    job, _video = _reseed_job(db_session, tmp_path, env, verified_hash="0" * 40)

    _run(db_session, tmp_path, job)

    target = job.targets[0]
    assert _adds(env["client"]) == []
    assert not (env["root"] / "torrents" / "The.Matrix.1999.1080p-GRP").exists()
    assert "upload_reseed_torrent_changed" in [e.code for e in job.events] + [target.error_message]


def test_a_client_that_cannot_skip_the_recheck_is_never_asked_to(db_session, tmp_path, env):
    # Transmission e rTorrent ricontrollano sempre: niente "verificato senza
    # recheck" nel registro, e nessun avviso di file fuori posto.
    video = write_video(env["root"] / "media" / "The.Matrix.1999.1080p.WEB-DL.H.264-GRP.mkv", 300 * KB)
    job = _approved(db_session, env, "media/" + video.name, {"a": _upload("Matrix A"), "b": {"action": "skip"}})
    env["client"].can_skip_recheck = False

    _run(db_session, tmp_path, job)

    assert job.status == "done"
    assert env["client"].skipped == [False]
    codes = [e.code for e in job.events]
    assert "added_to_client" in codes
    assert "added_to_client_verified" not in codes and "recheck_files_not_in_place" not in codes


def test_an_upload_seen_elsewhere_by_the_client_is_rechecked(db_session, tmp_path, env):
    video = write_video(env["root"] / "media" / "The.Matrix.1999.1080p.WEB-DL.H.264-GRP.mkv", 300 * KB)
    job = _approved(db_session, env, "media/" + video.name, {"a": _upload("Matrix A"), "b": {"action": "skip"}})
    env["client"].reported_path = "/somewhere/else"

    _run(db_session, tmp_path, job)

    assert job.status == "done"
    assert env["client"].skipped == [True]
    assert env["client"].rechecked == [job.targets[0].info_hash]
    assert "recheck_after_path_mismatch" in [e.code for e in job.events]


def test_one_tracker_failing_leaves_the_others_going(db_session, tmp_path, env):
    video = write_video(env["root"] / "media" / "Movie.2024.mkv", 100 * KB)
    env["trackers"]["a"].error = _rejected("rejected: dupe")
    job = _approved(db_session, env, "media/" + video.name, {"a": _upload("A"), "b": _upload("B")})

    _run(db_session, tmp_path, job)

    assert job.status == "partial"
    a, b = job.targets
    assert (a.status, a.error_message) == ("failed", "rejected: dupe")
    assert b.status == "done"
    assert "upload_failed" in [e.code for e in job.events]


def test_no_seed_and_existing_destination(db_session, tmp_path, env):
    video = write_video(env["root"] / "media" / "Movie.2024.mkv", 100 * KB)
    write_video(env["root"] / "torrents" / "Movie.2024.mkv", 50 * KB)  # un altro file con lo stesso nome
    job = _approved(db_session, env, "media/" + video.name, {"a": _upload("A"), "b": {"action": "skip"}})

    _run(db_session, tmp_path, job)

    # Upload riuscito, seed no: mai ripetere l'upload per un problema del client.
    target = job.targets[0]
    assert (job.status, target.status, target.error_message) == ("done", "done", "seed_failed")
    assert _adds(env["client"]) == []

    job2 = _approved(db_session, env, "media/" + video.name, {"a": _upload("A"), "b": {"action": "skip"}})
    job2.overrides_json = json.dumps({"no_seed": True})
    db_session.commit()
    _run(db_session, tmp_path, job2)
    assert "not_seeded" in [e.code for e in job2.events]


def test_screenshots_failing_blocks_uploads_but_not_reseeds(db_session, tmp_path, env, monkeypatch):
    def broken(*a, **k):
        raise upload_execute.screenshots.ScreenshotError("ffmpeg")

    monkeypatch.setattr(upload_execute.screenshots, "generate_screenshots", broken)
    video = write_video(env["root"] / "media" / "Movie.2024.mkv", 100 * KB)
    job = _approved(db_session, env, "media/" + video.name, {"a": _upload("A"), "b": _upload("B")})

    _run(db_session, tmp_path, job)

    assert job.status == "failed"
    assert [t.error_message for t in job.targets] == ["upload_screenshots_failed"] * 2
    assert env["trackers"]["a"].uploads == []


def test_zero_screenshots_is_a_choice(db_session, tmp_path, env):
    video = write_video(env["root"] / "media" / "Movie.2024.mkv", 100 * KB)
    job = upload_jobs.create_job(db_session, env["disk"], "media/" + video.name, overrides={"screenshot_count": 0})
    upload_jobs.transition(db_session, job, "identifying", "awaiting_decision", tmdb_id=1, content_type="movie",
                           kind="movie")
    for target in job.targets:
        target.status = "awaiting_decision"
    db_session.commit()
    upload_decision.approve(db_session, job, [{"target_id": t.id, **_upload(t.tracker.label)} for t in job.targets])

    _run(db_session, tmp_path, job)

    assert job.status == "done", [(e.code, e.params_json) for e in job.events]
    assert json.loads(job.screenshot_urls_json) == []


def test_files_in_place_needs_every_file_with_its_size(tmp_path):
    folder = tmp_path / "Show.S01"
    write_video(folder / "e1.mkv", 20 * KB)
    write_video(folder / "e2.mkv", 20 * KB)
    torrent = torf.Torrent(path=str(folder))

    assert upload_execute.files_in_place(torrent, str(tmp_path))
    (folder / "e2.mkv").write_bytes(b"short")
    assert not upload_execute.files_in_place(torrent, str(tmp_path))
    assert not upload_execute.files_in_place(torrent, str(tmp_path / "elsewhere"))


def test_a_torrent_changed_by_the_tracker_is_rechecked(db_session, tmp_path, env):
    video = write_video(env["root"] / "media" / "The.Matrix.1999.1080p.WEB-DL.H.264-GRP.mkv", 300 * KB)
    job = _approved(db_session, env, "media/" + video.name, {"a": _upload("Matrix A"), "b": {"action": "skip"}})
    env["trackers"]["a"].changed_content = True

    _run(db_session, tmp_path, job)

    assert job.status == "done"
    assert env["client"].skipped == [False]  # non più gli stessi file: recheck


def test_client_category_and_tags_follow_the_client_defaults_or_the_job(db_session, tmp_path, env):
    client = env["client_row"]
    client.category_movie, client.category_anime = "movie", "anime"
    client.tags_upload, client.tags_reseed = "release, nazgarr", "reseed"
    db_session.commit()
    video = write_video(env["root"] / "media" / "The.Matrix.1999.1080p.WEB-DL.H.264-GRP.mkv", 300 * KB)
    # "a" con i default del client; "b" con categoria e tag scelti nel job.
    job = _approved(db_session, env, "media/" + video.name, {
        "a": _upload("Matrix A"),
        "b": _upload("Matrix B", client_category="ebook", client_tags=""),
    })
    a, b = job.targets
    assert (a.client_category, a.client_tags) == ("movie", "release, nazgarr")
    assert (b.client_category, b.client_tags) == ("ebook", None)

    _run(db_session, tmp_path, job)

    assert env["client"].labels == [("movie", ["release", "nazgarr"]), ("ebook", None)]


def test_an_anime_takes_the_anime_category(db_session, env):
    from nazgarr.torrents import client_labels

    env["client_row"].category_tv, env["client_row"].category_anime = "tv", "anime"
    db_session.commit()
    write_video(env["root"] / "media" / "Show" / "e1.mkv", 20 * KB)
    job = upload_jobs.create_job(db_session, env["disk"], "media/Show")
    job.anime, job.content_type = True, "tv"
    db_session.commit()

    assert upload_decision.client_label_defaults(job, job.targets[0])["category"] == "anime"
    assert client_labels.is_anime({"genres": ["Animation", "Action"], "original_language": "ja"})
    assert not client_labels.is_anime({"genres": ["Animation"], "original_language": "en"})  # Pixar non è anime


def test_link_files_checks_everything_first_and_never_follows_symlinks(tmp_path):
    root = tmp_path / "torrents"
    root.mkdir()
    real = write_video(tmp_path / "media" / "a.mkv", 10 * KB)
    other = write_video(tmp_path / "media" / "b.mkv", 10 * KB)
    link = tmp_path / "media" / "link.mkv"
    link.symlink_to(real)

    with pytest.raises(UploadJobError) as exc:
        upload_execute.link_files([(str(real), str(root / "a.mkv")), (str(link), str(root / "l.mkv"))], str(root))
    assert exc.value.code == "upload_source_not_a_file"
    assert not (root / "a.mkv").exists()  # niente hardlink a metà

    # La guardia di traversal, non un errore qualunque prima (audit 2026-10-18, finding 141).
    with pytest.raises(ScopeViolation):
        upload_execute.link_files([(str(other), str(root / ".." / "escaped.mkv"))], str(root))
    assert not (tmp_path / "escaped.mkv").exists()


def test_a_library_file_is_uploaded_with_a_generated_release_name(db_session, tmp_path, env):
    video = write_video(env["root"] / "media" / "The Matrix (1999) {imdb-tt0133093}.mkv", 300 * KB)
    job = _approved(db_session, env, "media/" + video.name, {"a": _upload("Matrix A"), "b": {"action": "skip"}},
                    file_naming=None)  # il default: nessun hardlink, file della libreria -> generato

    _run(db_session, tmp_path, job)

    assert job.status == "done", [e.code for e in job.events]
    torrent = torf.Torrent.read(job.targets[0].torrent_path)
    assert torrent.name == "The.Matrix.1999.mkv"
    linked = env["root"] / "torrents" / "The.Matrix.1999.mkv"
    assert os.path.samefile(linked, video)  # in seed con quel nome, stessi byte
    assert _adds(env["client"]) == [(job.targets[0].torrent_path, str(env["root"] / "torrents"))]



def test_the_mediainfo_names_the_file_in_the_torrent_not_the_local_path(db_session, tmp_path, env, monkeypatch):
    # Rinominare cambia solo "Complete name"; il percorso locale non esce verso il tracker.
    monkeypatch.setattr("nazgarr.library.mediainfo.extract_full_text", lambda path: None)
    video = write_video(env["root"] / "media" / "The Matrix (1999) {imdb-tt0133093}.mkv", 300 * KB)
    job = _approved(db_session, env, "media/" + video.name, {"a": _upload("Matrix A"), "b": {"action": "skip"}},
                    file_naming=None)
    job.mediainfo_text = f"General\nComplete name                            : {video}\nFormat : Matroska\n"
    db_session.commit()

    _run(db_session, tmp_path, job)

    assert job.status == "done", [e.code for e in job.events]
    assert "Complete name                            : The.Matrix.1999.mkv\n" in job.mediainfo_text
    assert str(env["root"]) not in job.mediainfo_text
    assert "Format : Matroska" in job.mediainfo_text


def test_the_names_of_the_hardlinked_torrent_win(db_session, tmp_path, env):
    from nazgarr.core.models import ClientTorrent, ClientTorrentFile, RunLog, SeedFile

    video = write_video(env["root"] / "media" / "Matrix (1999).mkv", 300 * KB)
    release = env["root"] / "torrents" / "The.Matrix.1999.1080p.BluRay.x264-GRP.mkv"
    os.link(video, release)
    run = RunLog(run_type="manual", started_at=datetime.now(UTC))
    db_session.add(run)
    db_session.commit()
    st = os.stat(release)
    seed = SeedFile(disk_id=env["disk"].id, relative_path=f"torrents/{release.name}", size_bytes=st.st_size,
                    st_dev=st.st_dev, inode=st.st_ino, last_scan_id=run.id, last_seen_at=datetime.now(UTC))
    db_session.add(seed)
    db_session.commit()
    torrent_row = ClientTorrent(torrent_client_id=env["client_row"].id, info_hash="h", name=release.name,
                                save_path=str(release.parent), state="uploading", last_polled_at=datetime.now(UTC))
    db_session.add(torrent_row)
    db_session.commit()
    db_session.add(ClientTorrentFile(client_torrent_id=torrent_row.id, path_in_torrent=release.name,
                                     size_bytes=st.st_size, seed_file_id=seed.id, last_scan_id=run.id))
    db_session.commit()
    job = _approved(db_session, env, "media/" + video.name, {"a": _upload("Matrix A"), "b": {"action": "skip"}},
                    file_naming=None)

    _run(db_session, tmp_path, job)

    assert job.status == "done", [e.code for e in job.events]
    assert torf.Torrent.read(job.targets[0].torrent_path).name == "The.Matrix.1999.1080p.BluRay.x264-GRP.mkv"


def test_renamed_links_are_removed_when_nothing_is_seeded(db_session, tmp_path, env):
    video = write_video(env["root"] / "media" / "The Matrix (1999).mkv", 300 * KB)
    job = _approved(db_session, env, "media/" + video.name, {"a": _upload("Matrix A"), "b": {"action": "skip"}},
                    file_naming="generated")
    overrides = json.loads(job.overrides_json)
    job.overrides_json = json.dumps({**overrides, "no_seed": True})
    db_session.commit()

    _run(db_session, tmp_path, job)

    assert job.status == "done"
    assert not (env["root"] / "torrents" / "The.Matrix.1999.mkv").exists()
    assert video.exists()  # la libreria non si tocca


def _watched_job(db_session, env, name, decisions, **overrides):
    env["disk"].watch_rel_path = "releases"
    db_session.commit()
    video = write_video(env["root"] / "releases" / name, 300 * KB)
    job = _approved(db_session, env, "releases/" + name, decisions)
    job.origin = "watch"
    if overrides:
        job.overrides_json = json.dumps({**json.loads(job.overrides_json or "{}"), **overrides})
    db_session.commit()
    return video


def test_a_release_leaves_the_watched_folder_once_it_seeds(db_session, tmp_path, env):
    # La cartella osservata è solo di passaggio: la release resta dove seeda.
    video = _watched_job(db_session, env, "My.Movie.2024.1080p.WEB-DL-NZG.mkv",
                         {"a": _upload("Movie A"), "b": {"action": "skip"}})
    job = db_session.query(upload_jobs.UploadJob).one()

    _run(db_session, tmp_path, job)

    assert job.status == "done", [e.code for e in job.events]
    assert not video.exists()
    seeding = env["root"] / "torrents" / "My.Movie.2024.1080p.WEB-DL-NZG.mkv"
    assert seeding.is_file() and seeding.stat().st_nlink == 1  # ora l'unica copia, in seed
    assert "watch_source_moved" in [e.code for e in job.events]


def test_a_watched_folder_inside_the_seeding_folder_still_moves_with_its_original_names(db_session, tmp_path, env):
    """Decisione dell'utente, 2026-10-05: con i nomi originali la release
    lascia comunque la cartella osservata (prima seedava sul posto, dentro
    torrents/watch, e restava lì); i nomi non si toccano."""
    env["disk"].watch_rel_path = "torrents/watch"
    db_session.commit()
    video = write_video(env["root"] / "torrents" / "watch" / "Original.Name.2024.mkv", 300 * KB)
    job = _approved(db_session, env, "torrents/watch/" + video.name, {"a": _upload("Movie A"), "b": {"action": "skip"}})
    job.origin = "watch"
    db_session.commit()

    _run(db_session, tmp_path, job)

    assert job.status == "done", [e.code for e in job.events]
    assert not video.exists()
    seeding = env["root"] / "torrents" / "Original.Name.2024.mkv"  # la cartella degli upload, stesso nome
    assert seeding.is_file() and seeding.stat().st_nlink == 1


def test_a_watched_file_linked_elsewhere_is_kept_unless_this_upload_seeds_its_copy(db_session, tmp_path, env):
    """Prima bastava un secondo link qualunque per cancellare il file della
    cartella osservata, anche se a seedare era proprio lui."""
    from nazgarr.upload import execute

    video = _watched_job(db_session, env, "Linked.2024.mkv", {"a": _upload("Movie A"), "b": {"action": "skip"}})
    (env["root"] / "downloads").mkdir()
    os.link(video, env["root"] / "downloads" / "Linked.2024.mkv")  # un altro link, non di questo job
    job = db_session.query(upload_jobs.UploadJob).one()
    job.targets[0].status = "done"
    db_session.commit()

    execute._clear_watch_source(db_session, job, {}, {"seeded": {}})
    assert video.is_file()
    copy = env["root"] / "torrents" / "Linked.2024.mkv"
    os.link(video, copy)
    st = video.stat()
    seeded = {job.targets[0].id: {(st.st_dev, st.st_ino): str(copy)}}
    execute._clear_watch_source(db_session, job, {}, {"seeded": seeded})
    assert not video.exists() and copy.is_file()


def _watch_upload_and_reseed(db_session, tmp_path, env, name="My.Movie.2024.1080p.WEB-DL-NZG.mkv", **overrides):
    """Dalla cartella osservata: upload su "a", reseed su "b" del torrent
    identico (stesso nome, quindi lo stesso hardlink nella cartella di seed
    di quello creato per l'upload), già verificato."""
    env["disk"].watch_rel_path = "releases"
    db_session.commit()
    video = write_video(env["root"] / "releases" / name, 300 * KB)
    torrent_path, _ = torrent_create.create_torrent(str(video), "https://b/announce", str(tmp_path / "b.torrent"))
    with open(torrent_path, "rb") as f:
        env["trackers"]["b"].torrent_bytes = f.read()
    info_hash = compute_info_hash(env["trackers"]["b"].torrent_bytes)
    job = upload_jobs.create_job(db_session, env["disk"], "releases/" + name)
    job.origin = "watch"
    job.overrides_json = json.dumps({"file_naming": "original", **overrides})
    b = next(t for t in job.targets if t.tracker.label == "b")
    b.dupes_json = json.dumps([{"torrent_id_remote": "9", "verdict": "identical", "download_link": "https://b/dl/9",
                                "verification": {"status": "passed", "info_hash": info_hash}}])
    db_session.commit()
    upload_jobs.transition(db_session, job, "identifying", "awaiting_decision", tmdb_id=603, title="My Movie",
                           year=2024, content_type="movie", kind="movie", mediainfo_text="MI")
    for target in job.targets:
        target.status = "awaiting_decision"
    db_session.commit()
    decisions = {"a": _upload("Movie A"), "b": {"action": "reseed", "reseed_torrent_id": "9"}}
    upload_decision.approve(db_session, job, [{"target_id": t.id, **decisions[t.tracker.label]} for t in job.targets])
    return job, video


@pytest.mark.parametrize("failure", ["tracker", "screenshots"])
def test_a_failed_upload_never_removes_the_links_a_reseed_of_the_same_job_seeds(
        db_session, tmp_path, env, monkeypatch, failure):
    """Audit 2026-10-18 (finding 1): l'upload su "a" fallisce, il reseed su
    "b" riusa l'hardlink creato per l'upload. Prima la pulizia toglieva quel
    link e poi la cartella osservata toglieva l'originale: nessuna copia
    della release restava, e il client di "b" seedava un torrent senza dati."""
    if failure == "tracker":
        env["trackers"]["a"].error = _rejected("rejected: validation")
    else:
        monkeypatch.setattr(upload_execute.screenshots, "generate_screenshots",
                            lambda *a, **k: (_ for _ in ()).throw(upload_execute.screenshots.ScreenshotError("ffmpeg")))
    job, video = _watch_upload_and_reseed(db_session, tmp_path, env)

    _run(db_session, tmp_path, job)

    assert job.status == "partial", [e.code for e in job.events]
    seeding = env["root"] / "torrents" / video.name
    assert _adds(env["client"]) == [(job.targets[1].torrent_path, str(env["root"] / "torrents"))]
    assert seeding.is_file()  # il reseed su "b" ha i suoi dati
    assert "file_links_removed" not in [e.code for e in job.events]
    if video.exists():
        assert os.path.samefile(video, seeding)


def test_with_no_seed_the_links_a_reseed_seeds_are_kept(db_session, tmp_path, env):
    job, video = _watch_upload_and_reseed(db_session, tmp_path, env, no_seed=True)

    _run(db_session, tmp_path, job)

    assert job.status == "done", [e.code for e in job.events]
    assert (env["root"] / "torrents" / video.name).is_file()  # quello che "b" seeda
    assert video.is_file()  # "non mettere in seed": la cartella osservata non si svuota


def test_the_watched_file_is_kept_when_its_recorded_copy_is_gone(db_session, tmp_path, env):
    """La copia in seed si ricontrolla subito prima di togliere l'originale:
    stesso inode, un file vero, in un altro percorso."""
    from nazgarr.upload import execute

    video = _watched_job(db_session, env, "Gone.2024.mkv", {"a": _upload("Movie A"), "b": {"action": "skip"}})
    job = db_session.query(upload_jobs.UploadJob).one()
    job.targets[0].status = "done"
    db_session.commit()
    copy = env["root"] / "torrents" / "Gone.2024.mkv"
    os.link(video, copy)
    st = video.stat()
    ctx = {"seeded": {job.targets[0].id: {(st.st_dev, st.st_ino): str(copy)}}}
    os.unlink(copy)  # tolto dopo che il job l'ha registrato

    execute._clear_watch_source(db_session, job, {}, ctx)

    assert video.is_file()


def test_a_release_stays_in_the_watched_folder_when_nothing_seeds(db_session, tmp_path, env):
    video = _watched_job(db_session, env, "My.Movie.2024.mkv", {"a": _upload("Movie A"), "b": {"action": "skip"}},
                         no_seed=True)
    job = db_session.query(upload_jobs.UploadJob).one()

    _run(db_session, tmp_path, job)

    assert video.is_file()


def test_an_upload_by_hand_never_removes_its_source(db_session, tmp_path, env):
    video = write_video(env["root"] / "media" / "The.Matrix.1999.mkv", 300 * KB)
    job = _approved(db_session, env, "media/" + video.name, {"a": _upload("Matrix A"), "b": {"action": "skip"}})

    _run(db_session, tmp_path, job)

    assert job.status == "done" and video.is_file()


def test_a_release_folder_leaves_with_its_files_and_lists_what_the_torrent_did_not_take(db_session, tmp_path, env):
    # Una cartella con il film, un sample (fuori dal torrent) e un Thumbs.db.
    env["disk"].watch_rel_path = "releases"
    db_session.commit()
    folder = env["root"] / "releases" / "My Movie 2024"
    write_video(folder / "My.Movie.2024.1080p.WEB-DL-NZG.mkv", 300 * KB)
    write_video(folder / "Sample" / "sample.mkv", 10 * KB)
    (folder / "Thumbs.db").write_bytes(b"x")
    job = _approved(db_session, env, "releases/My Movie 2024", {"a": _upload("Movie A"), "b": {"action": "skip"}})
    job.origin = "watch"
    db_session.commit()

    _run(db_session, tmp_path, job)

    assert job.status == "done", [e.code for e in job.events]
    assert not (folder / "My.Movie.2024.1080p.WEB-DL-NZG.mkv").exists()
    assert not (folder / "Thumbs.db").exists()
    assert (folder / "Sample" / "sample.mkv").is_file()  # non era nel torrent: resta, e si dice
    leftovers = next(e for e in job.events if e.code == "watch_source_leftovers")
    assert json.loads(leftovers.params_json)["files"] == ["My Movie 2024/Sample/sample.mkv"]


def _single_file(db_session, choice):
    from nazgarr.core import settings_repo
    from nazgarr.upload import file_names as upload_file_names
    settings_repo.set_setting(db_session, upload_file_names.SINGLE_FILE_SETTING, "true")
    settings_repo.set_setting(db_session, upload_file_names.SINGLE_FILE_FOLDER_SETTING, choice)
    db_session.commit()


@pytest.mark.parametrize("choice", ["keep", "remove"])
def test_a_folder_with_one_file_becomes_a_single_file_torrent(db_session, tmp_path, env, choice):
    _single_file(db_session, choice)
    env["disk"].watch_rel_path = "releases"
    db_session.commit()
    folder = env["root"] / "releases" / "My Movie 2024"
    write_video(folder / "My.Movie.2024.1080p.WEB-DL-NZG.mkv", 300 * KB)
    write_video(folder / "Sample" / "sample.mkv", 10 * KB)  # mai nel torrent: non conta
    job = _approved(db_session, env, "releases/My Movie 2024", {"a": _upload("Movie A"), "b": {"action": "skip"}})
    job.origin = "watch"
    db_session.commit()

    _run(db_session, tmp_path, job)

    assert job.status == "done", [e.code for e in job.events]
    torrent = torf.Torrent.read(job.targets[0].torrent_path)
    assert torrent.mode == "singlefile" and torrent.name == "My.Movie.2024.1080p.WEB-DL-NZG.mkv"
    seeds_in = env["root"] / "torrents" / ("My Movie 2024" if choice == "keep" else "")
    assert (seeds_in / "My.Movie.2024.1080p.WEB-DL-NZG.mkv").is_file()
    assert _adds(env["client"]) == [(job.targets[0].torrent_path, str(seeds_in).rstrip(os.sep))]
    assert env["client"].skipped == [True]
    assert not (folder / "My.Movie.2024.1080p.WEB-DL-NZG.mkv").exists()  # spostato dalla cartella osservata


def test_a_single_file_already_in_the_seeding_folder_seeds_in_place(db_session, tmp_path, env):
    _single_file(db_session, "remove")
    video = write_video(env["root"] / "torrents" / "Movie.2024-GRP" / "Movie.2024-GRP.mkv", 300 * KB)
    job = _approved(db_session, env, "torrents/Movie.2024-GRP", {"a": _upload("Movie A"), "b": {"action": "skip"}})

    _run(db_session, tmp_path, job)

    assert job.status == "done", [e.code for e in job.events]
    assert torf.Torrent.read(job.targets[0].torrent_path).mode == "singlefile"
    assert _adds(env["client"]) == [(job.targets[0].torrent_path, str(video.parent))]
    assert video.stat().st_nlink == 1  # nessun hardlink nuovo


def test_a_folder_with_more_files_stays_a_folder(db_session, tmp_path, env):
    _single_file(db_session, "keep")
    folder = env["root"] / "media" / "Movie (2024)"
    write_video(folder / "Movie.mkv", 300 * KB)
    (folder / "Movie.nfo").write_text("nfo")
    job = _approved(db_session, env, "media/Movie (2024)", {"a": _upload("Movie A"), "b": {"action": "skip"}})

    _run(db_session, tmp_path, job)

    torrent = torf.Torrent.read(job.targets[0].torrent_path)
    assert torrent.mode == "multifile" and torrent.name == "Movie (2024)"


# --- pack di file scelti a mano (nazgarr/upload/pack.py) ----------------------


def _pack(db_session, env, files, kind, seasons, file_naming=None, **overrides):
    from dataclasses import asdict

    from nazgarr.upload import pack as upload_pack
    from nazgarr.upload.source import scan_source
    job = upload_pack.create_job(db_session, env["disk"], files)
    layout = scan_source(job.source_path, upload_pack.entries(job), upload_pack.name(job))
    assert (layout.kind, layout.seasons) == (kind, seasons)
    if file_naming or overrides:
        job.overrides_json = json.dumps({**({"file_naming": file_naming} if file_naming else {}), **overrides})
    upload_jobs.transition(
        db_session, job, "identifying", "awaiting_decision", tmdb_id=1399, title="Show", year=2020,
        content_type="tv", kind=kind, seasons_json=json.dumps(seasons), mediainfo_text="MI",
        layout_json=json.dumps(asdict(layout)),
    )
    for target in job.targets:
        target.status = "awaiting_decision"
    db_session.commit()
    return job


def _episodes(env, folder, season, count, size=200 * KB):
    """Gli episodi scaricati uno alla volta, ognuno nella cartella del suo torrent."""
    out = []
    for e in range(1, count + 1):
        name = f"Show.S{season:02d}E{e:02d}.1080p.WEB-DL-GRP"
        out.append(write_video(env["root"] / folder / name / f"{name}.mkv", size + e * KB))
    return out


def test_a_pack_of_episodes_already_seeding_becomes_one_season_torrent(db_session, tmp_path, env):
    episodes = _episodes(env, "torrents", 1, 3)
    (episodes[0].parent / "Show.S01E01.1080p.WEB-DL-GRP.it.srt").write_text("1")
    (episodes[0].parent / "other.nfo").write_text("x")  # non è un sottotitolo del video: resta fuori
    job = _pack(db_session, env, [str(p.relative_to(env["root"])) for p in episodes], "season_pack", [1],
                file_naming="original")
    from nazgarr.upload import pack as upload_pack
    assert upload_pack.name(job) == "Show.S01.1080p.WEB-DL-GRP"
    assert sorted(n for _p, n in upload_pack.entries(job))[0] == "Show.S01E01.1080p.WEB-DL-GRP.it.srt"
    upload_decision.approve(db_session, job, [
        {"target_id": t.id, **({"a": _upload("Show S01"), "b": {"action": "skip"}}[t.tracker.label])}
        for t in job.targets
    ])

    _run(db_session, tmp_path, job)

    assert job.status == "done", [e.code for e in job.events]
    torrent = torf.Torrent.read(job.targets[0].torrent_path)
    assert sorted(str(f) for f in torrent.files) == [
        "Show.S01.1080p.WEB-DL-GRP/Show.S01E01.1080p.WEB-DL-GRP.it.srt",
        "Show.S01.1080p.WEB-DL-GRP/Show.S01E01.1080p.WEB-DL-GRP.mkv",
        "Show.S01.1080p.WEB-DL-GRP/Show.S01E02.1080p.WEB-DL-GRP.mkv",
        "Show.S01.1080p.WEB-DL-GRP/Show.S01E03.1080p.WEB-DL-GRP.mkv",
    ]
    pack = env["root"] / "torrents" / "Show.S01.1080p.WEB-DL-GRP"
    assert os.path.samefile(pack / "Show.S01E02.1080p.WEB-DL-GRP.mkv", episodes[1])
    assert episodes[1].is_file()  # i torrent degli episodi restano come sono
    assert _adds(env["client"]) == [(job.targets[0].torrent_path, str(env["root"] / "torrents"))]


def test_a_complete_pack_puts_each_season_in_its_folder(db_session, tmp_path, env):
    files = _episodes(env, "torrents", 1, 2) + _episodes(env, "torrents", 2, 2)
    job = _pack(db_session, env, [str(p.relative_to(env["root"])) for p in files], "complete_pack", [1, 2],
                file_naming="original")
    from nazgarr.upload import file_names as upload_file_names
    plan = upload_file_names.plan(db_session, job)
    assert plan.content_name == "Show.S01-S02.1080p.WEB-DL-GRP"
    assert sorted(target for _s, target in plan.files) == [
        "Show.S01-S02.1080p.WEB-DL-GRP/Season 01/Show.S01E01.1080p.WEB-DL-GRP.mkv",
        "Show.S01-S02.1080p.WEB-DL-GRP/Season 01/Show.S01E02.1080p.WEB-DL-GRP.mkv",
        "Show.S01-S02.1080p.WEB-DL-GRP/Season 02/Show.S02E01.1080p.WEB-DL-GRP.mkv",
        "Show.S01-S02.1080p.WEB-DL-GRP/Season 02/Show.S02E02.1080p.WEB-DL-GRP.mkv",
    ]
    # Rinominati: ogni episodio col suo nome, sempre nella sua stagione.
    generated = upload_file_names.plan(db_session, job, "generated")
    assert all("/Season 0" in target for _s, target in generated.files)


def test_a_mixed_pack_waits_for_a_confirmation(db_session, tmp_path, env):
    files = _episodes(env, "torrents", 1, 2)
    job = _pack(db_session, env, [str(p.relative_to(env["root"])) for p in files], "season_pack", [1])
    job.analysis_json = json.dumps({"pack_mixed": {"group": ["GRP", "OTHER"]}})
    db_session.commit()
    decisions = [{"target_id": t.id, **_upload("Show S01")} for t in job.targets]

    with pytest.raises(UploadJobError) as exc:
        upload_decision.approve(db_session, job, decisions)
    assert exc.value.code == "upload_pack_mixed_unconfirmed"

    upload_decision.update_overrides(db_session, job, {"pack_mixed_confirmed": True})
    db_session.refresh(job)
    job.analysis_json = json.dumps({**json.loads(job.analysis_json), "pack_mixed": {"group": ["GRP", "OTHER"]}})
    for target in job.targets:
        target.status = "awaiting_decision"
    db_session.commit()
    upload_decision.approve(db_session, job, decisions)
    assert job.status == "queued"


def test_a_pack_checks_every_file(db_session, tmp_path, env):
    from nazgarr.upload import pack as upload_pack
    a, b = _episodes(env, "torrents", 1, 2)
    rel = lambda p: str(p.relative_to(env["root"]))  # noqa: E731
    with pytest.raises(UploadJobError, match="upload_pack_too_few_files"):
        upload_pack.create_job(db_session, env["disk"], [rel(a)])
    nfo = env["root"] / "torrents" / "x.nfo"
    nfo.write_text("x")
    with pytest.raises(UploadJobError, match="upload_pack_not_a_video"):
        upload_pack.create_job(db_session, env["disk"], [rel(a), rel(nfo)])
    twin = write_video(env["root"] / "media" / a.name)
    with pytest.raises(UploadJobError, match="upload_pack_duplicate_name"):
        upload_pack.create_job(db_session, env["disk"], [rel(a), rel(b), rel(twin)])
    os.symlink(b, env["root"] / "torrents" / "link.mkv")
    with pytest.raises(UploadJobError, match="upload_source_has_symlinks"):
        upload_pack.create_job(db_session, env["disk"], [rel(a), "torrents/link.mkv"])
    with pytest.raises(ScopeViolation):
        upload_pack.create_job(db_session, env["disk"], [rel(a), "../outside.mkv"])


def test_an_episode_seeding_alone_does_not_mark_the_pack_as_seeding():
    from nazgarr.upload.analysis import seeding_here
    target = types.SimpleNamespace(
        tracker=types.SimpleNamespace(announce_url="https://t.example/announce", base_url=None), torrent_client_id=1,
    )
    one = {"match": "hardlink", "client_id": 1, "tracker_host": "t.example", "videos_matched": 1,
           "videos_hardlinked": 1, "videos_total": 3}
    assert seeding_here(target, [one]) is None
    assert seeding_here(target, [{**one, "videos_matched": 3, "videos_hardlinked": 3}]) is not None


def test_the_season_name_of_an_episode():
    from nazgarr.upload.pack import season_name
    assert season_name("Show.S01E01.1080p.WEB-DL-GRP.mkv", [1]) == "Show.S01.1080p.WEB-DL-GRP"
    assert season_name("Show.S01E01E02.1080p-GRP", [1]) == "Show.S01.1080p-GRP"
    assert (season_name("Show (2020) - S02E05 - Title [WEBDL-1080p]", [1, 3])
            == "Show (2020) - S01-S03 - Title [WEBDL-1080p]")
    assert season_name("Show.Season.Pack", [1]) == "Show.Season.Pack"


def test_seeding_can_be_retried_without_uploading_again(db_session, tmp_path, env, monkeypatch):
    video = write_video(env["root"] / "media" / "The.Matrix.1999.1080p.WEB-DL.H.264-GRP.mkv", 300 * KB)
    job = _approved(db_session, env, "media/" + video.name, {"a": _upload("A"), "b": {"action": "skip"}})
    client = env["client"]
    real_add = client.add_torrent

    def broken(*args, **kwargs):
        raise RuntimeError("No such file or directory")

    monkeypatch.setattr(client, "add_torrent", broken)
    _run(db_session, tmp_path, job)
    target = job.targets[0]
    assert (target.status, target.error_message) == ("done", "seed_failed")
    uploads = len(env["trackers"]["a"].uploads)

    # Fallisce ancora: resta seed_failed, con il motivo.
    with pytest.raises(UploadJobError) as failed:
        upload_execute.retry_seed(db_session, job, target)
    assert failed.value.code == "upload_seed_retry_failed" and target.error_message == "seed_failed"

    monkeypatch.setattr(client, "add_torrent", real_add)
    upload_execute.retry_seed(db_session, job, target)

    assert target.error_message == "seed_recheck_pending"  # parte a recheck riuscito
    assert _adds(client) == [(target.torrent_path, str(env["root"] / "torrents"))]
    assert client.skipped == [False]  # un nuovo tentativo ha sempre il recheck del client
    assert len(env["trackers"]["a"].uploads) == uploads  # nessun nuovo upload
    assert "seed_retried" in [e.code for e in job.events]
    with pytest.raises(UploadJobError) as again:
        upload_execute.retry_seed(db_session, job, target)
    assert again.value.code == "upload_seed_retry_unavailable"


def test_seeding_is_not_retried_when_the_files_are_gone(db_session, tmp_path, env, monkeypatch):
    video = write_video(env["root"] / "media" / "Movie.2024.mkv", 100 * KB)
    job = _approved(db_session, env, "media/" + video.name, {"a": _upload("A"), "b": {"action": "skip"}})
    monkeypatch.setattr(env["client"], "add_torrent", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    _run(db_session, tmp_path, job)
    target = job.targets[0]
    os.unlink(env["root"] / "torrents" / video.name)
    os.unlink(video)

    with pytest.raises(UploadJobError) as missing:
        upload_execute.retry_seed(db_session, job, target)
    assert missing.value.code == "upload_seed_files_missing"


def test_a_scheduled_upload_waits_for_its_time(db_session, tmp_path, env):
    from datetime import timedelta

    video = write_video(env["root"] / "media" / "The.Matrix.1999.1080p.WEB-DL.H.264-GRP.mkv", 300 * KB)
    later = datetime.now(UTC) + timedelta(hours=3)
    job = _approved(db_session, env, "media/" + video.name, {"a": _upload("Matrix A"), "b": {"action": "skip"}},
                    scheduled_at=later)
    assert (job.status, as_utc(job.scheduled_at)) == ("queued", later)
    assert any(e.code == "job_scheduled" for e in job.events)

    _run(db_session, tmp_path, job)
    assert job.status == "queued" and env["trackers"]["a"].uploads == []  # non è ancora ora

    job.scheduled_at = datetime.now(UTC) - timedelta(seconds=1)
    db_session.commit()
    _run(db_session, tmp_path, job)
    assert job.status == "done" and len(env["trackers"]["a"].uploads) == 1


def test_a_time_already_gone_means_now_and_the_time_can_change(db_session, tmp_path, env):
    from datetime import timedelta

    video = write_video(env["root"] / "media" / "The.Matrix.1999.1080p.WEB-DL.H.264-GRP.mkv", 300 * KB)
    job = _approved(db_session, env, "media/" + video.name, {"a": _upload("Matrix A"), "b": {"action": "skip"}},
                    scheduled_at=datetime.now(UTC) - timedelta(minutes=5))
    assert job.scheduled_at is None

    upload_jobs.schedule(db_session, job, datetime.now(UTC) + timedelta(days=1))
    assert job.scheduled_at is not None
    upload_jobs.schedule(db_session, job, None)  # "Avvia ora"
    assert job.scheduled_at is None and job.events[-1].code == "job_start_now"


def test_a_dupe_that_appeared_after_the_decision_stops_that_tracker(db_session, tmp_path, env):
    from datetime import timedelta

    from nazgarr.adapters.tracker.base import TorrentCandidate

    video = write_video(env["root"] / "media" / "The.Matrix.1999.1080p.WEB-DL.H.264-GRP.mkv", 300 * KB)
    job = _approved(db_session, env, "media/" + video.name, {"a": _upload("Matrix A"), "b": _upload("Matrix B")},
                    scheduled_at=datetime.now(UTC) + timedelta(hours=1))
    # Su "a" nel frattempo qualcuno l'ha caricata; su "b" c'è solo quello che c'era alla decisione.
    same = TorrentCandidate(torrent_id_remote="77", info_hash=None, name="The.Matrix.1999.1080p.WEB-DL.H.264-OTHER",
                            size_bytes=300 * KB, file_list=None, mediainfo_unique_id=None)
    env["trackers"]["a"].catalog = [same]
    env["trackers"]["b"].catalog = [same]
    b = next(t for t in job.targets if t.tracker.label == "b")
    b.dupes_json = json.dumps([{"torrent_id_remote": "77", "verdict": "identical"}])
    job.scheduled_at = datetime.now(UTC) - timedelta(seconds=1)
    db_session.commit()

    _run(db_session, tmp_path, job)

    a = next(t for t in job.targets if t.tracker.label == "a")
    assert (a.status, a.error_message) == ("failed", "upload_dupe_appeared")
    assert env["trackers"]["a"].uploads == [] and len(env["trackers"]["b"].uploads) == 1
    assert job.status == "partial"


def test_a_dead_dupe_that_appeared_after_the_decision_does_not_stop_the_upload(db_session, tmp_path, env):
    """Un dupe senza seeder: il nuovo torrent lo sostituisce (decisione
    dell'utente, 2026-11-07), anche se compare dopo la decisione."""
    from datetime import timedelta

    from nazgarr.adapters.tracker.base import TorrentCandidate

    video = write_video(env["root"] / "media" / "The.Matrix.1999.1080p.WEB-DL.H.264-GRP.mkv", 300 * KB)
    job = _approved(db_session, env, "media/" + video.name, {"a": _upload("Matrix A"), "b": {"action": "skip"}},
                    scheduled_at=datetime.now(UTC) + timedelta(hours=1))
    dead = TorrentCandidate(torrent_id_remote="78", info_hash=None, name="The.Matrix.1999.1080p.WEB-DL.H.264-OTHER",
                            size_bytes=300 * KB + 1, file_list=None, mediainfo_unique_id=None, seeders=0)
    env["trackers"]["a"].catalog = [dead]
    job.scheduled_at = datetime.now(UTC) - timedelta(seconds=1)
    db_session.commit()

    _run(db_session, tmp_path, job)

    assert len(env["trackers"]["a"].uploads) == 1


def test_a_truncated_catalogue_before_sending_is_said(db_session, tmp_path, env):
    """Audit 2026-10-18 (finding 51): il dupe check prima dell'invio non
    diceva che il tracker aveva più risultati di quelli letti."""
    from datetime import timedelta

    from nazgarr.adapters.tracker.base import CatalogResults, TorrentCandidate

    video = write_video(env["root"] / "media" / "The.Matrix.1999.1080p.WEB-DL.H.264-GRP.mkv", 300 * KB)
    job = _approved(db_session, env, "media/" + video.name, {"a": _upload("Matrix A"), "b": {"action": "skip"}},
                    scheduled_at=datetime.now(UTC) + timedelta(hours=1))
    other = TorrentCandidate(torrent_id_remote="5", info_hash=None, name="The.Matrix.1999.2160p.BluRay-X",
                             size_bytes=1, file_list=None, mediainfo_unique_id=None)
    catalog = CatalogResults([other])
    catalog.truncated = True
    env["trackers"]["a"].catalog = catalog
    job.scheduled_at = datetime.now(UTC) - timedelta(seconds=1)
    db_session.commit()

    _run(db_session, tmp_path, job)

    assert job.status == "done"  # si va avanti
    event = next(e for e in job.events if e.code == "dupe_recheck_truncated")
    assert (event.level, json.loads(event.params_json)) == ("warning", {"results": 1})


# --- errori a metà esecuzione (audit 2026-10-18) -------------------------------


def test_an_unexpected_error_while_preparing_fails_the_uploads_with_it(db_session, tmp_path, env, monkeypatch, caplog):
    """Un errore che non è nostro (qui torf che non legge il disco) faceva
    finire il job con "passo non disponibile", senza log, e lasciava gli
    hardlink dei nomi nuovi nella cartella di seed."""
    import logging

    video = write_video(env["root"] / "media" / "The Matrix (1999).mkv", 300 * KB)
    job = _approved(db_session, env, "media/" + video.name, {"a": _upload("Matrix A"), "b": {"action": "skip"}},
                    file_naming="generated")
    monkeypatch.setattr(torf.Torrent, "generate", lambda self, **k: (_ for _ in ()).throw(OSError(5, "I/O error")))

    with caplog.at_level(logging.ERROR, logger=upload_execute.logger.name):
        _run(db_session, tmp_path, job)

    a = job.targets[0]
    assert (job.status, a.status, a.error_message) == ("failed", "failed", "upload_prepare_failed")
    event = next(e for e in job.events if e.code == "upload_prepare_failed")
    assert "I/O error" in json.loads(event.params_json)["error"]
    assert "preparazione fallita" in caplog.text
    assert not (env["root"] / "torrents" / "The.Matrix.1999.mkv").exists()


def test_a_cancel_while_hashing_removes_the_links_made_for_the_names(db_session, tmp_path, env, monkeypatch):
    video = write_video(env["root"] / "media" / "The Matrix (1999).mkv", 300 * KB)
    job = _approved(db_session, env, "media/" + video.name, {"a": _upload("Matrix A"), "b": {"action": "skip"}},
                    file_naming="generated")

    def cancel_midway(self, callback=None, **kwargs):
        other = db_session.__class__(bind=db_session.get_bind())
        upload_jobs.cancel_job(other, other.get(upload_jobs.UploadJob, job.id))
        other.close()
        return callback(self, "", 1, 2)

    monkeypatch.setattr(torf.Torrent, "generate", cancel_midway)
    _run(db_session, tmp_path, job)

    assert job.status == "cancelled"
    assert not (env["root"] / "torrents" / "The.Matrix.1999.mkv").exists()
    assert video.is_file()
    assert "file_links_removed" in [e.code for e in job.events]


def _links_left_by_a_failed_cleanup(db_session, tmp_path, env, monkeypatch):
    """Un upload rifiutato la cui pulizia non riesce: l'hardlink per i nomi
    resta, come dopo un riavvio a metà esecuzione."""
    video = write_video(env["root"] / "media" / "The Matrix (1999).mkv", 300 * KB)
    job = _approved(db_session, env, "media/" + video.name, {"a": _upload("Matrix A"), "b": {"action": "skip"}},
                    file_naming="generated")
    env["trackers"]["a"].error = _rejected()
    real_remove = upload_execute.hardlinks.remove_links
    monkeypatch.setattr(upload_execute.hardlinks, "remove_links",
                        lambda *a, **k: (_ for _ in ()).throw(OSError(5, "I/O error")))
    _run(db_session, tmp_path, job)
    monkeypatch.setattr(upload_execute.hardlinks, "remove_links", real_remove)
    link = env["root"] / "torrents" / "The.Matrix.1999.mkv"
    assert job.status == "failed" and os.path.samefile(link, video)
    assert "upload_cleanup_failed" in [e.code for e in job.events]
    return job, video, link


def test_a_retry_removes_the_links_an_earlier_run_left(db_session, tmp_path, env, monkeypatch):
    """Audit 2026-10-18 (finding 115): i link di un'esecuzione precedente
    erano solo in memoria. Il nuovo tentativo li riusa senza crearli, e se
    falliva di nuovo restavano nella cartella di seed per sempre."""
    job, video, link = _links_left_by_a_failed_cleanup(db_session, tmp_path, env, monkeypatch)

    upload_jobs.retry_failed(db_session, job)
    _run(db_session, tmp_path, job)

    assert job.status == "failed"
    assert not link.exists() and video.is_file()
    assert "file_links_removed" in [e.code for e in job.events]


def test_deleting_a_job_removes_the_links_it_made_that_seed_nothing(db_session, tmp_path, env, monkeypatch):
    job, video, link = _links_left_by_a_failed_cleanup(db_session, tmp_path, env, monkeypatch)
    data_dir = str(tmp_path / "data")

    upload_jobs.delete_job(db_session, job, data_dir)

    assert not link.exists() and video.is_file()
    assert db_session.query(upload_jobs.UploadJob).count() == 0
    assert not os.path.exists(upload_jobs.job_folder(data_dir, job.id))


def test_deleting_a_job_keeps_the_links_its_upload_seeds_from(db_session, tmp_path, env):
    video = write_video(env["root"] / "media" / "The Matrix (1999).mkv", 300 * KB)
    job = _approved(db_session, env, "media/" + video.name, {"a": _upload("Matrix A"), "b": {"action": "skip"}},
                    file_naming="generated")
    _run(db_session, tmp_path, job)
    link = env["root"] / "torrents" / "The.Matrix.1999.mkv"
    assert job.status == "done" and os.path.samefile(link, video)

    upload_jobs.delete_job(db_session, job, str(tmp_path / "data"))

    assert os.path.samefile(link, video)  # il torrent nel client seeda da lì


def test_a_link_this_job_never_made_is_never_removed(db_session, tmp_path, env):
    """Un file con lo stesso nome e lo stesso inode che c'era già (un altro
    job, l'utente): la pulizia lo riusa ma non è suo, e non lo toglie."""
    video = write_video(env["root"] / "media" / "The Matrix (1999).mkv", 300 * KB)
    link = env["root"] / "torrents" / "The.Matrix.1999.mkv"
    os.link(video, link)
    job = _approved(db_session, env, "media/" + video.name, {"a": _upload("Matrix A"), "b": {"action": "skip"}},
                    file_naming="generated")
    env["trackers"]["a"].error = _rejected()

    _run(db_session, tmp_path, job)
    upload_jobs.delete_job(db_session, job, str(tmp_path / "data"))

    assert os.path.samefile(link, video)


def _another_upload_of(db_session, env, video):
    """Un secondo upload degli stessi file con gli stessi nomi, verso l'altro
    tracker: riusa il link lasciato da un altro job (check_link) senza crearlo."""
    return _approved(db_session, env, "media/" + video.name, {"a": {"action": "skip"}, "b": _upload("Matrix B")},
                     file_naming="generated")


def test_a_link_left_behind_that_another_upload_seeds_from_stays(db_session, tmp_path, env, monkeypatch):
    """Revisione dell'audit 2026-10-18 (finding 115): un altro upload riusa
    il link lasciato da A e ci seeda. Riprovare A, ed eliminarlo, toglievano
    il file da cui seeda il torrent dell'altro."""
    job_a, video, link = _links_left_by_a_failed_cleanup(db_session, tmp_path, env, monkeypatch)
    job_b = _another_upload_of(db_session, env, video)
    _run(db_session, tmp_path, job_b)
    assert job_b.status == "done" and os.path.samefile(link, video)
    assert _adds(env["client"])[-1] == (job_b.targets[1].torrent_path, str(env["root"] / "torrents"))

    upload_jobs.retry_failed(db_session, job_a)
    _run(db_session, tmp_path, job_a)
    assert job_a.status == "failed"
    assert os.path.samefile(link, video)
    codes = [e.code for e in job_a.events]
    assert "file_links_kept" in codes and "file_links_removed" not in codes

    upload_jobs.delete_job(db_session, job_a, str(tmp_path / "data"))
    assert os.path.samefile(link, video)


def test_a_link_left_behind_that_a_running_upload_hashes_stays(db_session, tmp_path, env, monkeypatch):
    """Lo stesso mentre l'altro upload sta ancora creando il suo torrent: il
    suo seed non è ancora scritto, il file del suo contenuto sì."""
    job_a, video, link = _links_left_by_a_failed_cleanup(db_session, tmp_path, env, monkeypatch)
    job_b = _another_upload_of(db_session, env, video)
    data_dir, a_id = str(tmp_path / "data"), job_a.id
    hash_pieces = upload_execute.hash_pieces

    def delete_a_then_hash(*args, **kwargs):
        other = db_session.__class__(bind=db_session.get_bind())
        try:
            upload_jobs.delete_job(other, other.get(upload_jobs.UploadJob, a_id), data_dir)
        finally:
            other.close()
        return hash_pieces(*args, **kwargs)

    monkeypatch.setattr(upload_execute, "hash_pieces", delete_a_then_hash)
    _run(db_session, tmp_path, job_b)

    assert db_session.query(upload_jobs.UploadJob).filter_by(id=a_id).count() == 0
    assert job_b.status == "done" and os.path.samefile(link, video)


def test_a_cancel_during_the_screenshots_stops_them_and_sends_nothing(db_session, tmp_path, env, monkeypatch):
    """Audit 2026-10-18 (finding 22): annullare durante gli screenshot
    cambiava solo il DB, e le catture andavano avanti fino all'ultima."""
    video = write_video(env["root"] / "media" / "The Matrix (1999).mkv", 300 * KB)
    job = _approved(db_session, env, "media/" + video.name, {"a": _upload("Matrix A"), "b": {"action": "skip"}},
                    file_naming="generated")
    captured = []

    def cancelled_midway(video, out_dir, count=4, tonemap=False, should_stop=None):
        other = db_session.__class__(bind=db_session.get_bind())
        upload_jobs.cancel_job(other, other.get(upload_jobs.UploadJob, job.id))
        other.close()
        for i in range(count):
            if should_stop is not None and should_stop():
                raise upload_execute.screenshots.ScreenshotsStopped(video)
            captured.append(i)
        return []

    monkeypatch.setattr(upload_execute.screenshots, "generate_screenshots", cancelled_midway)
    _run(db_session, tmp_path, job)

    assert job.status == "cancelled"
    assert captured == []
    assert env["trackers"]["a"].uploads == []
    assert not (env["root"] / "torrents" / "The.Matrix.1999.mkv").exists()  # i link per i nomi tolti


@pytest.mark.parametrize("swap", ["another_file", "symlink"])
def test_the_cleanup_never_removes_what_took_the_place_of_a_link(db_session, tmp_path, env, monkeypatch, swap):
    """Audit 2026-10-18 (finding 75): la pulizia toglieva per percorso. Un
    altro file messo al posto dell'hardlink dopo la sua creazione (o un link
    simbolico) si cancellava al posto suo."""
    video = write_video(env["root"] / "media" / "The Matrix (1999).mkv", 300 * KB)
    job = _approved(db_session, env, "media/" + video.name, {"a": _upload("Matrix A"), "b": {"action": "skip"}},
                    file_naming="generated")
    job.overrides_json = json.dumps({**json.loads(job.overrides_json), "no_seed": True})
    db_session.commit()
    link = env["root"] / "torrents" / "The.Matrix.1999.mkv"
    check = upload_execute._used_elsewhere

    def swap_then_check(session, job, paths, since, data_dir):
        link.unlink()
        if swap == "another_file":
            link.write_bytes(b"not ours")
        else:
            link.symlink_to(video)
        return check(session, job, paths, since, data_dir)

    monkeypatch.setattr(upload_execute, "_used_elsewhere", swap_then_check)
    _run(db_session, tmp_path, job)

    assert job.status == "done"
    assert os.path.lexists(link)
    assert video.is_file()
    codes = [e.code for e in job.events]
    assert "file_links_changed" in codes and "file_links_removed" not in codes


def test_the_cleanup_never_follows_a_folder_turned_into_a_symlink(db_session, tmp_path, env):
    """Audit 2026-10-18 (finding 75): una cartella sopra l'hardlink diventata
    un link simbolico dopo la creazione portava l'unlink fuori dalla
    cartella di seed."""
    video = write_video(env["root"] / "media" / "The Matrix (1999).mkv", 300 * KB)
    job = _approved(db_session, env, "media/" + video.name, {"a": _upload("Matrix A"), "b": {"action": "skip"}})
    seed = env["root"] / "torrents"
    folder = seed / "The.Matrix.1999"
    [link] = upload_execute.link_files([(str(video), str(folder / "The.Matrix.1999.mkv"))], str(seed))
    ctx = {"created_links": [link], "created_ids": upload_execute._link_identities([link]),
           "seed_root": lambda: str(seed), "data_dir": str(tmp_path / "data")}
    elsewhere = env["root"] / "elsewhere"
    os.rename(folder, elsewhere)
    folder.symlink_to(elsewhere, target_is_directory=True)

    upload_execute._cleanup_unused_links(db_session, job, ctx, {})

    assert (elsewhere / "The.Matrix.1999.mkv").is_file()  # mai seguito il link simbolico
    assert "file_links_removed" not in [e.code for e in job.events]


def test_the_cleanup_never_removes_a_file_swapped_in_after_its_last_check(db_session, tmp_path, env, monkeypatch):
    """Audit 2026-10-18 (finding 75): fra _still_our_link e l'unlink resta
    una finestra. remove_links riceve l'identità di quando il link è nato
    (expected) e la ricontrolla nella cartella da cui toglie: un file messo
    lì in quel momento resta, anche per un percorso che non porta più la
    sua identità (una stringa, non un CreatedLink)."""
    video = write_video(env["root"] / "media" / "The Matrix (1999).mkv", 300 * KB)
    job = _approved(db_session, env, "media/" + video.name, {"a": _upload("Matrix A"), "b": {"action": "skip"}})
    seed = env["root"] / "torrents"
    link = seed / "The.Matrix.1999" / "The.Matrix.1999.mkv"
    [created] = upload_execute.link_files([(str(video), str(link))], str(seed))
    ctx = {"created_links": [str(created)], "created_ids": upload_execute._link_identities([created]),
           "seed_root": lambda: str(seed), "data_dir": str(tmp_path / "data")}
    checked = upload_execute._still_our_link

    def check_then_swap(path, recorded):
        ours = checked(path, recorded)
        os.unlink(path)
        with open(path, "wb") as f:
            f.write(b"someone else's file")
        return ours

    monkeypatch.setattr(upload_execute, "_still_our_link", check_then_swap)
    upload_execute._cleanup_unused_links(db_session, job, ctx, {})

    assert link.read_bytes() == b"someone else's file"
    assert "file_links_removed" not in [e.code for e in job.events]


def test_a_cleanup_error_never_changes_the_outcome(db_session, tmp_path, env, monkeypatch):
    video = _watched_job(db_session, env, "My.Movie.2024.mkv", {"a": _upload("Movie A"), "b": {"action": "skip"}})
    job = db_session.query(upload_jobs.UploadJob).one()
    monkeypatch.setattr(upload_execute, "_clear_watch_source",
                        lambda *a, **k: (_ for _ in ()).throw(PermissionError(13, "Permission denied")))

    _run(db_session, tmp_path, job)

    assert job.status == "done" and job.targets[0].torrent_id_remote == "100"
    assert video.is_file()
    assert "upload_cleanup_failed" in [e.code for e in job.events]


def test_a_leftover_that_cannot_be_removed_keeps_the_episode_folder_and_the_job_done(
        db_session, tmp_path, env, monkeypatch):
    from nazgarr.upload import execute

    env["disk"].watch_rel_path = "releases"
    db_session.commit()
    folder = env["root"] / "releases" / "Show.S01"
    episode = write_video(folder / "Show.S01E01.mkv", 300 * KB)
    (folder / "Show.nfo").write_text("nfo")
    write_video(folder / "Locked" / "notes.txt", 10)
    job = upload_jobs.create_job(db_session, env["disk"], "releases/Show.S01/Show.S01E01.mkv")
    job.split_from_id = job.id  # un episodio di una stagione divisa
    db_session.commit()
    os.chmod(folder / "Locked", 0o555)
    try:
        os.unlink(episode)  # l'episodio è già andato
        execute._remove_split_folder(db_session, job, str(env["root"] / "releases"))
    finally:
        os.chmod(folder / "Locked", 0o755)

    assert (folder / "Locked" / "notes.txt").is_file() and not (folder / "Show.nfo").exists()
    event = job.events[-1]
    assert event.code == "watch_folder_remove_failed" and event.level == "warning"
    assert json.loads(event.params_json)["files"] == ["Locked", "Locked/notes.txt"]


def test_a_published_upload_whose_record_cannot_be_written_is_never_marked_failed(
        db_session, tmp_path, env, monkeypatch):
    from sqlalchemy.exc import OperationalError

    video = write_video(env["root"] / "media" / "The.Matrix.1999.mkv", 300 * KB)
    job = _approved(db_session, env, "media/" + video.name, {"a": _upload("Matrix A"), "b": {"action": "skip"}})

    def locked(session, job, target, torrent_id_remote):
        raise OperationalError("COMMIT", None, Exception("database is locked"))

    monkeypatch.setattr(upload_execute, "_commit_published", locked)
    _run(db_session, tmp_path, job)

    a = job.targets[0]
    assert (a.status, a.error_message, a.torrent_id_remote) == ("done", "seed_failed", "100")
    assert len(env["trackers"]["a"].uploads) == 1 and _adds(env["client"]) == []
    journal = upload_jobs.published_journal(str(tmp_path / "data" / "uploads" / str(job.id)), a.id)
    with open(journal) as f:
        assert json.load(f) == {"torrent_id_remote": "100"}


def test_the_published_id_is_written_again_after_a_locked_commit(monkeypatch):
    from sqlalchemy.exc import OperationalError

    class Flaky:
        def __init__(self, fails):
            self.fails, self.added, self.rollbacks = fails, [], 0

        def add(self, obj):
            self.added.append(obj)

        def commit(self):
            if self.fails:
                self.fails -= 1
                raise OperationalError("COMMIT", None, Exception("database is locked"))

        def rollback(self):
            self.rollbacks += 1

    monkeypatch.setattr(upload_execute.time, "sleep", lambda s: None)
    job, target = types.SimpleNamespace(id=1), types.SimpleNamespace(id=2, torrent_id_remote=None)
    session = Flaky(2)
    upload_execute._commit_published(session, job, target, "100")
    assert target.torrent_id_remote == "100" and session.rollbacks == 2 and len(session.added) == 3

    with pytest.raises(OperationalError):
        upload_execute._commit_published(Flaky(3), job, target, "100")


def test_a_database_error_on_one_tracker_is_recorded_with_its_message(db_session, tmp_path, env, monkeypatch):
    """Un errore di SQLAlchemy ha anche lui .code e .params: prima finiva
    registrato come "e3q8", e params.items() falliva sulla tupla della query."""
    from sqlalchemy.exc import OperationalError

    job, _video = _reseed_job(db_session, tmp_path, env)
    monkeypatch.setattr(upload_execute, "run_reseed", lambda *a: (_ for _ in ()).throw(
        OperationalError("INSERT INTO x", (1,), Exception("disk I/O error"))))

    _run(db_session, tmp_path, job)

    target = job.targets[0]
    assert job.status == "failed" and target.status == "failed"
    assert "disk I/O error" in target.error_message and target.error_message != "e3q8"
    assert "reseed_failed" in [e.code for e in job.events]


# --- l'aggiunta al client e il nuovo tentativo del seed (audit 2026-10-18) ------


def test_the_client_waits_for_this_exact_torrent_and_one_already_there_is_kept(
        db_session, tmp_path, env, monkeypatch):
    """Senza l'info hash atteso qBittorrent prendeva per il nostro un torrent
    qualunque comparso nel frattempo; con, un duplicato è un errore chiaro:
    qui il primo tentativo era arrivato nel client prima di fallire."""
    video = write_video(env["root"] / "media" / "The.Matrix.1999.mkv", 300 * KB)
    job = _approved(db_session, env, "media/" + video.name, {"a": _upload("A"), "b": {"action": "skip"}})
    client = env["client"]
    real_add, expected = client.add_torrent, []

    def add(torrent_file, save_path, expected_info_hash=None, **kwargs):
        expected.append(expected_info_hash)
        if expected_info_hash in client.save_paths:
            raise TorrentAlreadyInClientError("already there")
        real_add(torrent_file, save_path, **kwargs)
        raise RuntimeError("timed out waiting for the torrent")  # arrivato, ma l'attesa è scaduta

    monkeypatch.setattr(client, "add_torrent", add)
    _run(db_session, tmp_path, job)
    target = job.targets[0]
    assert (target.status, target.error_message) == ("done", "seed_failed")
    with open(target.torrent_path, "rb") as f:
        assert expected == [compute_info_hash(f.read())]

    upload_execute.retry_seed(db_session, job, target)

    # Ricontrollato e fermo: parte solo a recheck riuscito (decisione dell'utente, 2026-10-19).
    assert target.error_message == "seed_recheck_pending" and client.rechecked == [expected[0]]
    assert not getattr(client, "started", [])
    assert "already_in_client_held" in [e.code for e in job.events]
    upload_execute.reconcile_held_seeds(db_session)
    assert target.error_message is None and client.started == [expected[0]]


def test_a_torrent_already_in_the_client_elsewhere_is_not_taken(db_session, tmp_path, env, monkeypatch):
    job, _video = _reseed_job(db_session, tmp_path, env)
    client = env["client"]

    def add(torrent_file, save_path, expected_info_hash=None, **kwargs):
        client.save_paths[expected_info_hash] = "/somewhere/else"
        raise TorrentAlreadyInClientError("already there")

    monkeypatch.setattr(client, "add_torrent", add)
    _run(db_session, tmp_path, job)

    target = job.targets[0]
    assert (target.status, target.error_message) == ("failed", "upload_already_in_client")
    assert client.rechecked == []


def test_a_retried_seed_relinks_into_the_seed_folder_never_the_library(db_session, tmp_path, env, monkeypatch):
    """Prima un nuovo tentativo, senza più l'hardlink nella cartella di seed,
    puntava il client direttamente alla cartella della libreria."""
    video = write_video(env["root"] / "media" / "The.Matrix.1999.mkv", 300 * KB)
    job = _approved(db_session, env, "media/" + video.name, {"a": _upload("A"), "b": {"action": "skip"}})
    client = env["client"]
    real_add = client.add_torrent
    monkeypatch.setattr(client, "add_torrent", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    _run(db_session, tmp_path, job)
    target = job.targets[0]
    os.unlink(env["root"] / "torrents" / video.name)  # tolto come hardlink orfano
    monkeypatch.setattr(client, "add_torrent", real_add)

    upload_execute.retry_seed(db_session, job, target)

    assert _adds(client) == [(target.torrent_path, str(env["root"] / "torrents"))]
    assert os.path.samefile(env["root"] / "torrents" / video.name, video)
    assert "seed_files_linked" in [e.code for e in job.events]


@pytest.mark.parametrize("tracker_can_give_it_back", [True, False])
def test_a_retried_seed_uses_the_trackers_own_torrent(db_session, tmp_path, env, tracker_can_give_it_back):
    """Il seed fallito prima di scaricare la copia del tracker: si riscarica
    (UNIT3D riscrive il campo source, l'info hash del nostro .torrent non è
    quello che conosce il tracker); solo se non si può, il nostro, e si dice."""
    video = write_video(env["root"] / "media" / "Movie.2024.mkv", 100 * KB)
    blocker = write_video(env["root"] / "torrents" / "Movie.2024.mkv", 50 * KB)  # un altro file con quel nome
    job = _approved(db_session, env, "media/" + video.name, {"a": _upload("A"), "b": {"action": "skip"}})
    _run(db_session, tmp_path, job)
    target = job.targets[0]
    assert target.error_message == "seed_failed"
    assert os.path.basename(target.torrent_path) == f"tracker-{target.tracker_id}.torrent"
    os.unlink(blocker)
    tracker = env["trackers"]["a"]
    if tracker_can_give_it_back:
        tracker.get_torrent_detail = lambda torrent_id: types.SimpleNamespace(
            download_link=f"https://tracker/torrent/download/{torrent_id}.key")

    upload_execute.retry_seed(db_session, job, target, str(tmp_path / "data"))

    added = torf.Torrent.read(env["client"].added[-1].source)
    codes = [e.code for e in job.events]
    if tracker_can_give_it_back:
        assert added.source == "Tracker100" and target.torrent_path.endswith("-seed.torrent")
        assert "seed_retry_own_torrent" not in codes
    else:
        assert added.source == "a" and "seed_retry_own_torrent" in codes


# --- quello che entra nel torrent (audit 2026-10-18) ---------------------------


def test_only_the_planned_files_enter_a_torrent_built_from_hardlinks(db_session, tmp_path, env, monkeypatch):
    """La cartella con il nome del torrent nella cartella di seed aveva già
    un file (un upload precedente, la promo di un altro tracker): finiva
    pubblicato e in seed senza che la decisione lo mostrasse."""
    from nazgarr.upload import file_names as upload_file_names

    folder = env["root"] / "media" / "The Matrix (1999)"
    write_video(folder / "The Matrix (1999).mkv", 300 * KB)
    (folder / "The Matrix (1999).nfo").write_text("nfo")
    job = _approved(db_session, env, "media/The Matrix (1999)", {"a": _upload("Matrix A"), "b": {"action": "skip"}},
                    file_naming="generated")
    name = upload_file_names.plan(db_session, job).content_name
    stale = env["root"] / "torrents" / name / "Other.Tracker.Promo.txt"
    stale.parent.mkdir(parents=True)
    stale.write_text("promo")
    threads = []
    real_generate = torf.Torrent.generate

    def generate(self, **kwargs):
        threads.append(kwargs.get("threads"))
        return real_generate(self, **kwargs)

    monkeypatch.setattr(torf.Torrent, "generate", generate)
    _run(db_session, tmp_path, job)

    assert job.status == "done", [e.code for e in job.events]
    files = sorted("/".join(f.parts) for f in torf.Torrent.read(job.targets[0].torrent_path).files)
    assert files and all(not f.endswith("Promo.txt") for f in files)
    event = next(e for e in job.events if e.code == "upload_extra_files_left_out")
    assert json.loads(event.params_json)["files"] == [f"{name}/Other.Tracker.Promo.txt"]
    assert stale.is_file()  # non è di questo job: resta dov'è
    assert threads and 1 <= threads[0] <= upload_execute.HASH_THREADS


def test_a_symlink_added_to_the_source_after_the_decision_stops_the_upload(db_session, tmp_path, env):
    """Il controllo dei link simbolici si faceva solo alla creazione del job;
    torf li segue, e il file a cui puntano finiva nel torrent pubblicato."""
    folder = env["root"] / "media" / "Movie (2024)"
    write_video(folder / "Movie.mkv", 300 * KB)
    (folder / "Movie.nfo").write_text("nfo")
    job = _approved(db_session, env, "media/Movie (2024)", {"a": _upload("Movie A"), "b": {"action": "skip"}})
    secret = tmp_path / "secret.key"
    secret.write_text("key")
    (folder / "extra.txt").symlink_to(secret)

    _run(db_session, tmp_path, job)

    target = job.targets[0]
    assert (target.status, target.error_message) == ("failed", "upload_source_has_symlinks")
    assert env["trackers"]["a"].uploads == []


# --- un nuovo tentativo dei tracker falliti (richiesta dell'utente, 2026-10-18) --


def _shots(monkeypatch):
    calls = []
    real = upload_execute.screenshots.generate_screenshots

    def counting(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(upload_execute.screenshots, "generate_screenshots", counting)
    return calls


def test_a_failed_tracker_is_retried_with_its_decision_and_no_new_match(db_session, tmp_path, env, monkeypatch):
    shots = _shots(monkeypatch)
    video = write_video(env["root"] / "media" / "The.Matrix.1999.1080p.WEB-DL.H.264-GRP.mkv", 300 * KB)
    env["trackers"]["a"].error = _unreachable("All connection attempts failed")
    job = _approved(db_session, env, "media/" + video.name, {"a": _upload("Matrix A"), "b": _upload("Matrix B")})
    _run(db_session, tmp_path, job)
    a, b = job.targets
    assert (job.status, a.status, b.status) == ("partial", "failed", "done") and len(shots) == 1

    env["trackers"]["a"].error = None  # il tracker è tornato
    retried = upload_jobs.retry_failed(db_session, job)

    assert [t.id for t in retried] == [a.id]
    assert (job.status, job.finished_at, a.status, a.error_message) == ("queued", None, "approved", None)
    assert job.queue_position is not None and job.events[-1].code == "job_retried"
    assert json.loads(job.events[-1].params_json)["trackers"] == "a"

    _run(db_session, tmp_path, job)

    assert (job.status, a.status, a.torrent_id_remote) == ("done", "done", "100"), [e.code for e in job.events]
    assert env["trackers"]["a"].uploads[0][0].name == "Matrix A"  # la decisione di allora
    assert len(env["trackers"]["b"].uploads) == 1  # "b" non si ricarica
    assert len(shots) == 1  # gli screenshot già sull'image host
    assert "https://img.example/0.png" in env["trackers"]["a"].uploads[0][0].description


def test_a_retry_checks_the_dupes_again_before_sending(db_session, tmp_path, env):
    from nazgarr.adapters.tracker.base import TorrentCandidate

    video = write_video(env["root"] / "media" / "The.Matrix.1999.1080p.WEB-DL.H.264-GRP.mkv", 300 * KB)
    env["trackers"]["a"].error = _unreachable("connect timeout")
    job = _approved(db_session, env, "media/" + video.name, {"a": _upload("Matrix A"), "b": {"action": "skip"}})
    _run(db_session, tmp_path, job)
    assert job.status == "failed"
    env["trackers"]["a"].error = None
    env["trackers"]["a"].catalog = [TorrentCandidate(
        torrent_id_remote="77", info_hash=None, name="The.Matrix.1999.1080p.WEB-DL.H.264-OTHER", size_bytes=300 * KB,
        file_list=None, mediainfo_unique_id=None)]

    upload_jobs.retry_failed(db_session, job)
    _run(db_session, tmp_path, job)

    a = job.targets[0]
    assert (job.status, a.status, a.error_message) == ("failed", "failed", "upload_dupe_appeared")
    assert env["trackers"]["a"].uploads == []


def test_an_interrupted_upload_is_retried_only_once_confirmed(db_session, tmp_path, env):
    """Non si sa se il tracker l'ha ricevuto: si riprova solo dopo una
    conferma, e il dupe check rifatto trova il nostro torrent se c'è."""
    from nazgarr.adapters.tracker.base import TorrentCandidate

    video = write_video(env["root"] / "media" / "The.Matrix.1999.1080p.WEB-DL.H.264-GRP.mkv", 300 * KB)
    job = _approved(db_session, env, "media/" + video.name, {"a": _upload("Matrix A"), "b": {"action": "skip"}})
    a = job.targets[0]
    a.status, a.error_message = "failed", "interrupted"
    upload_jobs.transition(db_session, job, "queued", "failed")

    with pytest.raises(UploadJobError) as exc:
        upload_jobs.retry_failed(db_session, job)
    assert (exc.value.code, exc.value.params) == ("upload_retry_needs_confirm", {"trackers": "a"})
    assert job.status == "failed" and a.status == "failed"

    # Era arrivato: il tracker lo elenca, e il nuovo tentativo si ferma lì.
    env["trackers"]["a"].catalog = [TorrentCandidate(
        torrent_id_remote="100", info_hash=None, name="Matrix A", size_bytes=300 * KB, file_list=None,
        mediainfo_unique_id=None)]
    upload_jobs.retry_failed(db_session, job, confirm_interrupted=True)
    assert json.loads(job.events[-1].params_json)["confirmed"] == "a"
    _run(db_session, tmp_path, job)

    assert (a.status, a.error_message) == ("failed", "upload_dupe_appeared")
    assert env["trackers"]["a"].uploads == []


def test_what_cannot_be_retried(db_session, tmp_path, env, monkeypatch):
    video = write_video(env["root"] / "media" / "Movie.2024.mkv", 100 * KB)
    job = _approved(db_session, env, "media/" + video.name, {"a": _upload("A"), "b": {"action": "skip"}})

    def refused(code):
        with pytest.raises(UploadJobError) as exc:
            upload_jobs.retry_failed(db_session, job)
        assert exc.value.code == code

    refused("upload_job_wrong_status")  # in coda, non finito
    monkeypatch.setattr(env["client"], "add_torrent", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    _run(db_session, tmp_path, job)
    assert (job.status, job.targets[0].error_message) == ("done", "seed_failed")
    refused("upload_job_wrong_status")  # pubblicato: si riprova il seed, non l'upload
    upload_jobs.transition(db_session, job, "done", "partial")
    refused("upload_retry_nothing")
    job.targets[0].status, job.targets[0].torrent_id_remote = "failed", None
    db_session.commit()
    os.unlink(video)
    refused("upload_source_missing")


def test_a_watched_release_that_failed_everywhere_stays_and_leaves_after_a_retry(db_session, tmp_path, env):
    video = _watched_job(db_session, env, "My.Movie.2024.mkv", {"a": _upload("Movie A"), "b": {"action": "skip"}})
    job = db_session.query(upload_jobs.UploadJob).one()
    env["trackers"]["a"].error = _unreachable("down")
    _run(db_session, tmp_path, job)
    assert job.status == "failed" and video.is_file()

    env["trackers"]["a"].error = None
    upload_jobs.retry_failed(db_session, job)
    _run(db_session, tmp_path, job)

    assert job.status == "done" and not video.exists()
    assert (env["root"] / "torrents" / "My.Movie.2024.mkv").is_file()


def test_a_watched_release_stays_until_every_tracker_is_done(db_session, tmp_path, env):
    """Decisione dell'utente, 2026-10-18: con un tracker fallito la release
    resta nella cartella osservata, così "Riprova" ha i suoi file (prima se
    ne andava dopo il primo tracker riuscito, e il nuovo tentativo era
    rifiutato con upload_source_missing); se ne va con l'ultimo tracker."""
    env["disk"].watch_rel_path = "releases"
    db_session.commit()
    folder = env["root"] / "releases" / "My Movie 2024"
    video = write_video(folder / "My.Movie.2024.1080p.WEB-DL-NZG.mkv", 300 * KB)
    write_video(folder / "Sample" / "sample.mkv", 10 * KB)
    job = _approved(db_session, env, "releases/My Movie 2024", {"a": _upload("Movie A"), "b": _upload("Movie B")})
    job.origin = "watch"
    db_session.commit()
    env["trackers"]["b"].error = _unreachable()

    _run(db_session, tmp_path, job)

    assert (job.status, [t.status for t in job.targets]) == ("partial", ["done", "failed"])
    seeding = env["root"] / "torrents" / "My Movie 2024" / video.name
    assert video.is_file() and os.path.samefile(video, seeding)  # "a" seeda dal suo hardlink
    kept = next(e for e in job.events if e.code == "watch_source_kept")
    assert (kept.level, json.loads(kept.params_json)) == ("info", {"trackers": "b"})
    assert "watch_source_moved" not in [e.code for e in job.events]

    env["trackers"]["b"].error = None
    assert [t.tracker.label for t in upload_jobs.retry_failed(db_session, job)] == ["b"]
    _run(db_session, tmp_path, job)

    assert (job.status, [t.status for t in job.targets]) == ("done", ["done", "done"]), [e.code for e in job.events]
    assert len(env["trackers"]["a"].uploads) == 1 and len(env["trackers"]["b"].uploads) == 1
    assert not video.exists() and seeding.is_file() and seeding.stat().st_nlink == 1
    assert "watch_source_moved" in [e.code for e in job.events]
    leftovers = next(e for e in job.events if e.code == "watch_source_leftovers")
    assert json.loads(leftovers.params_json)["files"] == ["My Movie 2024/Sample/sample.mkv"]


def test_a_retry_clears_the_files_only_the_first_runs_tracker_seeds(db_session, tmp_path, env):
    """Le copie in seed di un tracker andato al primo giro contano anche al
    nuovo tentativo (il file del job, _seeded_journal): "a" seeda film e nfo,
    il reseed di "b" solo il film, e senza le copie di "a" l'nfo restava
    nella cartella osservata."""
    env["disk"].watch_rel_path = "releases"
    db_session.commit()
    folder = env["root"] / "releases" / "My Movie 2024"
    video = write_video(folder / "My.Movie.2024.1080p.WEB-DL-NZG.mkv", 300 * KB)
    (folder / "My.Movie.2024.nfo").write_text("nfo")
    torrent_path, _ = torrent_create.create_torrent(str(video), "https://b/announce", str(tmp_path / "b.torrent"))
    with open(torrent_path, "rb") as f:
        good = f.read()
    other = write_video(tmp_path / "other" / "Other.mkv", 10 * KB)
    other_path, _ = torrent_create.create_torrent(str(other), "https://b/announce", str(tmp_path / "other.torrent"))
    with open(other_path, "rb") as f:
        env["trackers"]["b"].torrent_bytes = f.read()  # il tracker dà un altro torrent: il reseed fallisce
    job = upload_jobs.create_job(db_session, env["disk"], "releases/My Movie 2024")
    job.origin = "watch"
    job.overrides_json = json.dumps({"file_naming": "original"})
    b = next(t for t in job.targets if t.tracker.label == "b")
    b.dupes_json = json.dumps([{"torrent_id_remote": "9", "verdict": "identical", "download_link": "https://b/dl/9",
                                "verification": {"status": "passed", "info_hash": compute_info_hash(good)}}])
    db_session.commit()
    upload_jobs.transition(db_session, job, "identifying", "awaiting_decision", tmdb_id=603, title="My Movie",
                           year=2024, content_type="movie", kind="movie", mediainfo_text="MI")
    for target in job.targets:
        target.status = "awaiting_decision"
    db_session.commit()
    decisions = {"a": _upload("Movie A"), "b": {"action": "reseed", "reseed_torrent_id": "9"}}
    upload_decision.approve(db_session, job, [{"target_id": t.id, **decisions[t.tracker.label]} for t in job.targets])

    _run(db_session, tmp_path, job)

    a, b = job.targets
    assert (job.status, a.status, b.status, b.error_message) == (
        "partial", "done", "failed", "upload_reseed_torrent_changed")
    assert video.is_file() and (folder / "My.Movie.2024.nfo").is_file()
    assert os.path.isfile(upload_execute._seeded_journal(upload_jobs.job_folder(str(tmp_path / "data"), job.id),
                                                         a.id))

    env["trackers"]["b"].torrent_bytes = good
    upload_jobs.retry_failed(db_session, job)
    _run(db_session, tmp_path, job)

    assert (job.status, b.status) == ("done", "done"), [e.code for e in job.events]
    assert not folder.exists()  # anche l'nfo, che seeda solo per "a"
    assert (env["root"] / "torrents" / "My Movie 2024" / "My.Movie.2024.nfo").read_text() == "nfo"
    assert (env["root"] / "torrents" / video.name).is_file()  # il reseed di "b", con il nome del suo torrent
    assert "watch_source_leftovers" not in [e.code for e in job.events]


def test_a_journal_of_an_earlier_run_never_counts_for_a_tracker_that_runs_again(tmp_path):
    from nazgarr.upload import execute

    target = types.SimpleNamespace(id=7)
    ctx = {"dir": str(tmp_path)}
    copy = write_video(tmp_path / "copy.mkv", 10)
    execute._remember_seeded(ctx, target, [str(copy)])
    st = copy.stat()
    assert execute._journaled_seeded(str(tmp_path), 7) == {(st.st_dev, st.st_ino): os.path.realpath(copy)}

    execute._forget_seeded(ctx, target)  # il tracker riparte (es. dopo "Cambia match")
    assert execute._journaled_seeded(str(tmp_path), 7) == {}
    (tmp_path / "seeded-7.json").write_text("{not json")
    assert execute._journaled_seeded(str(tmp_path), 7) == {}  # illeggibile: niente si toglie


def test_the_episode_folder_stays_while_a_tracker_of_the_episode_failed(db_session, tmp_path, env):
    """La cartella di un episodio di una stagione divisa si toglie solo a job
    finito del tutto, come la sorgente (decisione dell'utente, 2026-10-18)."""
    env["disk"].watch_rel_path = "releases"
    db_session.commit()
    folder = env["root"] / "releases" / "Show.S01"
    episode = write_video(folder / "Show.S01E01.mkv", 300 * KB)
    (folder / "Show.nfo").write_text("nfo")
    job = _approved(db_session, env, "releases/Show.S01/Show.S01E01.mkv",
                    {"a": _upload("Show A"), "b": _upload("Show B")})
    job.origin, job.split_from_id = "watch", job.id
    db_session.commit()
    env["trackers"]["b"].error = _unreachable()

    _run(db_session, tmp_path, job)

    assert job.status == "partial"
    assert episode.is_file() and (folder / "Show.nfo").is_file()
    assert "watch_folder_removed" not in [e.code for e in job.events]

    env["trackers"]["b"].error = None
    upload_jobs.retry_failed(db_session, job)
    _run(db_session, tmp_path, job)

    assert job.status == "done", [e.code for e in job.events]
    assert not folder.exists()
    assert "watch_folder_removed" in [e.code for e in job.events]


def test_a_cancel_during_the_last_tracker_that_then_fails_can_still_be_retried(
        db_session, tmp_path, env, monkeypatch):
    """L'annullamento arriva mentre "b" invia, e "b" fallisce: l'esecuzione
    aveva già fatto tutto, la release resta nella cartella osservata per
    "Riprova", ma il job resta annullato. Prima né la ripresa (niente da
    fare) né "Riprova" (non finito) lo accettavano, e la release restava lì
    per sempre; la ripresa ora lo chiude 'partial', da cui "Riprova"."""
    video = _watched_job(db_session, env, "My.Movie.2024.mkv", {"a": _upload("Movie A"), "b": _upload("Movie B")})
    job = db_session.query(upload_jobs.UploadJob).one()
    tracker_b = env["trackers"]["b"]
    send = tracker_b.upload_torrent

    def cancel_then_fail(fields, torrent_path):
        monkeypatch.setattr(tracker_b, "upload_torrent", send)  # solo il primo invio
        other = db_session.__class__(bind=db_session.get_bind())
        upload_jobs.cancel_job(other, other.get(upload_jobs.UploadJob, job.id))
        other.close()
        raise _unreachable()

    monkeypatch.setattr(tracker_b, "upload_torrent", cancel_then_fail)
    _run(db_session, tmp_path, job)

    assert (job.status, [t.status for t in job.targets]) == ("cancelled", ["done", "failed"])
    assert video.is_file() and "watch_source_kept" in [e.code for e in job.events]
    with pytest.raises(UploadJobError, match="upload_job_wrong_status"):
        upload_jobs.retry_failed(db_session, job)

    assert upload_jobs.resume_job(db_session, job) == "partial"
    assert job.status == "partial" and job.finished_at is not None
    assert [t.status for t in job.targets] == ["done", "failed"] and len(tracker_b.uploads) == 0  # niente rifatto
    last = job.events[-1]
    assert (last.code, json.loads(last.params_json)) == ("job_partial", {"after_cancel": True})

    assert [t.tracker.label for t in upload_jobs.retry_failed(db_session, job)] == ["b"]
    _run(db_session, tmp_path, job)

    assert (job.status, [t.status for t in job.targets]) == ("done", ["done", "done"]), [e.code for e in job.events]
    assert len(env["trackers"]["a"].uploads) == 1 and len(tracker_b.uploads) == 1
    assert not video.exists() and (env["root"] / "torrents" / "My.Movie.2024.mkv").is_file()


def test_resuming_a_cancel_whose_only_send_may_have_arrived_asks_like_retry(db_session, tmp_path, env, monkeypatch):
    """Audit 2026-10-18 (R1): un tracker solo, annullato mentre inviava, e
    l'invio finito in un timeout. La ripresa lo rimandava all'analisi: il
    dupe check rifatto non vede un torrent in moderazione, l'errore incerto
    spariva, e una nuova approvazione lo pubblicava due volte senza la
    conferma di "Riprova". Ora il job finisce 'failed', e "Riprova" la chiede."""
    video = write_video(env["root"] / "media" / "The.Matrix.1999.1080p.WEB-DL.H.264-GRP.mkv", 300 * KB)
    job = _approved(db_session, env, "media/" + video.name, {"a": _upload("Matrix A"), "b": {"action": "skip"}})
    tracker_a = env["trackers"]["a"]

    def cancel_then_timeout(fields, torrent_path):
        other = db_session.__class__(bind=db_session.get_bind())
        upload_jobs.cancel_job(other, other.get(upload_jobs.UploadJob, job.id))
        other.close()
        error = UploadError("UNIT3D upload failed: timeout")
        error.__cause__ = httpx.ReadTimeout("timed out")
        raise error

    monkeypatch.setattr(tracker_a, "upload_torrent", cancel_then_timeout)
    _run(db_session, tmp_path, job)

    a = job.targets[0]
    assert (job.status, a.status, a.error_message) == ("cancelled", "failed", "upload_outcome_unknown")
    assert upload_jobs.resume_job(db_session, job) == "failed"
    assert (job.status, a.error_message) == ("failed", "upload_outcome_unknown")  # l'avviso resta
    last = job.events[-1]
    assert (last.code, json.loads(last.params_json)) == ("job_failed", {"after_cancel": True})
    with pytest.raises(UploadJobError) as exc:
        upload_jobs.retry_failed(db_session, job)
    assert exc.value.code == "upload_retry_needs_confirm"


def test_resuming_a_cancel_whose_only_send_was_refused_goes_back_to_the_decision(db_session, tmp_path, env,
                                                                                  monkeypatch):
    """Un invio rifiutato con certezza (un 4xx) non ha niente da confermare:
    la ripresa resta quella di prima, di nuovo alla decisione."""
    video = write_video(env["root"] / "media" / "The.Matrix.1999.1080p.WEB-DL.H.264-GRP.mkv", 300 * KB)
    job = _approved(db_session, env, "media/" + video.name, {"a": _upload("Matrix A"), "b": {"action": "skip"}})

    def cancel_then_refuse(fields, torrent_path):
        other = db_session.__class__(bind=db_session.get_bind())
        upload_jobs.cancel_job(other, other.get(upload_jobs.UploadJob, job.id))
        other.close()
        raise _rejected()

    monkeypatch.setattr(env["trackers"]["a"], "upload_torrent", cancel_then_refuse)
    _run(db_session, tmp_path, job)

    assert (job.status, job.targets[0].status) == ("cancelled", "failed")
    assert upload_jobs.resume_job(db_session, job) == "analyzing"


@pytest.mark.parametrize(("cause", "unsure"), [
    (httpx.ReadTimeout("timed out"), True),  # la risposta non è arrivata: il tracker può averlo salvato
    (None, True),  # "nessun id riconoscibile", con success: true
    (httpx.ConnectError("refused"), False),  # mai partita
    (httpx.HTTPStatusError("422", request=httpx.Request("POST", "https://t"),
                           response=httpx.Response(422, request=httpx.Request("POST", "https://t"))), False),
])
def test_an_upload_failed_after_sending_is_retried_only_once_confirmed(db_session, tmp_path, env, cause, unsure):
    """Audit 2026-10-18: un timeout, un 5xx o una risposta illeggibile dopo
    l'invio non dicono se il tracker l'ha salvato; "Riprova" lo rimandava con
    un clic, e la ricerca di UNIT3D non elenca i torrent in moderazione."""
    video = write_video(env["root"] / "media" / "The.Matrix.1999.1080p.WEB-DL.H.264-GRP.mkv", 300 * KB)
    error = UploadError(f"UNIT3D upload failed: {cause!r}")
    error.__cause__ = cause
    env["trackers"]["a"].error = error
    job = _approved(db_session, env, "media/" + video.name, {"a": _upload("Matrix A"), "b": {"action": "skip"}})

    _run(db_session, tmp_path, job)

    a = job.targets[0]
    assert job.status == "failed"
    if not unsure:
        assert a.error_message == str(error)
        upload_jobs.retry_failed(db_session, job)
        assert a.status == "approved"
        return
    assert a.error_message == "upload_outcome_unknown"
    event = next(e for e in job.events if e.code == "upload_outcome_unknown")
    assert json.loads(event.params_json)["error"] == str(error)
    with pytest.raises(UploadJobError) as exc:
        upload_jobs.retry_failed(db_session, job)
    assert (exc.value.code, exc.value.params) == ("upload_retry_needs_confirm", {"trackers": "a"})
    upload_jobs.retry_failed(db_session, job, confirm_interrupted=True)
    assert a.status == "approved"


def test_a_failure_before_sending_never_asks_for_a_confirmation(db_session, tmp_path, env):
    video = write_video(env["root"] / "media" / "Movie.2024.mkv", 100 * KB)
    job = _approved(db_session, env, "media/" + video.name, {"a": _upload("A"), "b": {"action": "skip"}})
    job.targets[0].tracker.announce_url = None
    db_session.commit()

    _run(db_session, tmp_path, job)

    assert job.targets[0].error_message == "tracker_missing_announce_url"
    upload_jobs.retry_failed(db_session, job)
    assert job.status == "queued"


# --- i file del torrent, come li conta torf (audit 2026-10-18) -----------------


def _hash_job(db_session, env):
    video = write_video(env["root"] / "media" / "Movie.2024.mkv", 100 * KB)
    return _approved(db_session, env, "media/" + video.name, {"a": _upload("A"), "b": {"action": "skip"}})


def test_a_folder_with_a_single_unrelated_file_never_becomes_the_torrent(db_session, env):
    """torf confronta i regex con "Nome/Nome/file" quando la cartella ha un
    file solo: "^Nome/file$" non lo toglieva, e il torrent era quel file."""
    job = _hash_job(db_session, env)
    root = env["root"] / "torrents" / "Movie.2024"
    (root).mkdir()
    (root / "Other.Release.mkv").write_bytes(b"y" * 4096)

    with pytest.raises(UploadJobError) as exc:
        upload_execute.hash_pieces(db_session, job, str(root), planned=[])
    assert exc.value.code == "no_video_files"
    with pytest.raises(UploadJobError) as exc:
        upload_execute.hash_pieces(db_session, job, str(root), planned=[str(root / "Movie.2024.mkv")])
    assert exc.value.code == "upload_planned_files_missing"
    assert "upload_extra_files_left_out" not in [e.code for e in job.events]


def test_extras_next_to_the_planned_files_in_one_subfolder_stay_out(db_session, env):
    job = _hash_job(db_session, env)
    root = env["root"] / "torrents" / "Show.S01"
    planned = write_video(root / "Season 1" / "Show.S01E01.mkv", 64 * KB)
    (root / "Season 1" / "Show.S01E01.en.srt").write_text("old subtitle")

    torrent = upload_execute.hash_pieces(db_session, job, str(root), planned=[str(planned)])

    assert ["/".join(f.parts) for f in torrent.files] == ["Show.S01/Season 1/Show.S01E01.mkv"]
    event = next(e for e in job.events if e.code == "upload_extra_files_left_out")
    assert json.loads(event.params_json)["files"] == ["Show.S01/Season 1/Show.S01E01.en.srt"]


def test_a_torrent_that_differs_from_the_plan_never_reaches_the_tracker(db_session, tmp_path, env, monkeypatch):
    """Se torf non toglie un file in più (qualunque sia il motivo), l'upload
    si ferma: mai un torrent con file che la decisione non mostrava."""
    from nazgarr.upload import file_names as upload_file_names

    folder = env["root"] / "media" / "The Matrix (1999)"
    write_video(folder / "The Matrix (1999).mkv", 300 * KB)
    (folder / "The Matrix (1999).nfo").write_text("nfo")
    job = _approved(db_session, env, "media/The Matrix (1999)", {"a": _upload("Matrix A"), "b": {"action": "skip"}},
                    file_naming="generated")
    name = upload_file_names.plan(db_session, job).content_name
    stale = env["root"] / "torrents" / name / "Other.Tracker.Promo.txt"
    stale.parent.mkdir(parents=True)
    stale.write_text("promo")
    monkeypatch.setattr(upload_execute, "_exclude_exact", lambda relative: "^never$")

    _run(db_session, tmp_path, job)

    target = job.targets[0]
    assert (target.status, target.error_message) == ("failed", "upload_torrent_files_mismatch")
    assert env["trackers"]["a"].uploads == []
    assert "upload_extra_files_left_out" not in [e.code for e in job.events]


# --- i link creati per i nomi che usa qualcun altro (audit 2026-10-18) ---------


@pytest.mark.parametrize(("user", "kept"), [("index", True), ("reseed", True), ("old_reseed", False)])
def test_links_another_torrent_uses_survive_a_failed_upload(db_session, tmp_path, env, user, kept):
    """Un reseed del motore approvato durante l'upload, con lo stesso nome,
    riusa l'hardlink appena creato (stesso inode): la pulizia dopo un upload
    fallito lo toglieva da sotto al suo torrent."""
    from nazgarr.core.models import (
        Candidate,
        CandidateFile,
        ClientTorrent,
        ClientTorrentFile,
        MediaItem,
        RunLog,
        SeedFile,
        SeedJob,
    )
    from nazgarr.upload import file_names as upload_file_names
    from tests.fakes import make_media_file

    video = write_video(env["root"] / "media" / "The Matrix (1999).mkv", 300 * KB)
    job = _approved(db_session, env, "media/" + video.name, {"a": _upload("Matrix A"), "b": {"action": "skip"}},
                    file_naming="generated")
    name = upload_file_names.plan(db_session, job).content_name
    link = env["root"] / "torrents" / name
    run = RunLog(run_type="manual", started_at=datetime.now(UTC))
    db_session.add(run)
    db_session.commit()
    if user == "index":
        seed = SeedFile(disk_id=env["disk"].id, relative_path=f"torrents/{name}", size_bytes=300 * KB, st_dev=1,
                        inode=1, last_scan_id=run.id, last_seen_at=datetime.now(UTC))
        torrent = ClientTorrent(torrent_client_id=env["client_row"].id, info_hash="h", name=name,
                                save_path=str(env["root"] / "torrents"), state="uploading",
                                last_polled_at=datetime.now(UTC))
        db_session.add_all([seed, torrent])
        db_session.commit()
        db_session.add(ClientTorrentFile(client_torrent_id=torrent.id, path_in_torrent=name, size_bytes=300 * KB,
                                         seed_file_id=seed.id, last_scan_id=run.id))
    else:
        media = make_media_file(db_session, env["disk"], "media/" + video.name, run, inode=video.stat().st_ino)
        item = MediaItem(content_type="movie", tmdb_id=603)
        db_session.add(item)
        db_session.commit()
        candidate = Candidate(media_item_id=item.id, tracker_id=job.targets[1].tracker_id, torrent_id_remote="7",
                              name=name, size_bytes=300 * KB, source="catalog_search", direction="media_to_torrent",
                              confidence=0.9)
        db_session.add(candidate)
        db_session.commit()
        db_session.add(CandidateFile(candidate_id=candidate.id, torrent_path=name, is_video=True,
                                     media_file_id=media.id))
        old = datetime(2026, 1, 1, tzinfo=UTC)
        db_session.add(SeedJob(candidate_id=candidate.id, final_status="in_progress" if user == "reseed" else "seeding",
                               hardlink_created_at=None if user == "reseed" else old))
    db_session.commit()
    env["trackers"]["a"].error = _rejected()

    _run(db_session, tmp_path, job)

    assert job.status == "failed"
    assert link.exists() is kept
    assert video.is_file()
    assert ("file_links_kept" in [e.code for e in job.events]) is kept


def test_a_published_upload_left_in_flight_ends_published_not_step_not_available(db_session, tmp_path, env):
    """Il worker non ha potuto registrare l'errore dopo un invio riuscito: il
    job resta 'running' con il target in 'uploading'. La coda, alla ripresa,
    lo rimette in coda con il target sistemato, e l'esecuzione lo chiude."""
    video = write_video(env["root"] / "media" / "Movie.2024.mkv", 100 * KB)
    job = _approved(db_session, env, "media/" + video.name, {"a": _upload("A"), "b": {"action": "skip"}})
    upload_jobs.transition(db_session, job, "queued", "running")
    a = job.targets[0]
    a.status = "uploading"
    db_session.commit()
    folder = tmp_path / "data" / "uploads" / str(job.id)
    folder.mkdir(parents=True)
    (folder / f"published-{a.id}.json").write_text('{"torrent_id_remote": "100"}')

    _run(db_session, tmp_path, job)

    assert (job.status, a.status, a.error_message, a.torrent_id_remote) == ("done", "done", "seed_failed", "100")
    assert "upload_step_not_available" not in [e.code for e in job.events]
    assert env["trackers"]["a"].uploads == []  # mai un secondo invio
