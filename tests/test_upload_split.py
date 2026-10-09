"""Una stagione incompleta divisa in episodi (nazgarr/upload/split.py): i primi
2 episodi di 8 usciti insieme nella cartella osservata."""

import json
import os

import pytest

from nazgarr.core import settings_repo
from nazgarr.core.models import UploadJob
from nazgarr.library import episode_orders
from nazgarr.upload import execute as upload_execute
from nazgarr.upload import identify as upload_identify
from nazgarr.upload import jobs as upload_jobs
from nazgarr.upload import split as upload_split
from tests.upload_helpers import FakeTMDB, make_disk, make_tracker, tmdb_result, write_video

FOLDER = "Show.S01E01E02.1080p.WEB-DL-GRP"
EPISODES = ("Show.S01E01.1080p.WEB-DL-GRP.mkv", "Show.S01E02.1080p.WEB-DL-GRP.mkv")


def _release(tmp_path):
    """La release nella cartella osservata, con un nfo e un sample."""
    folder = tmp_path / "releases" / FOLDER
    for name in EPISODES:
        write_video(folder / name)
    (folder / "Show.S01E01E02.nfo").write_text("notes")
    write_video(folder / "Sample" / "show.sample.mkv", size=100)
    return folder


def _tmdb(monkeypatch, episodes_in_season):
    monkeypatch.setattr(upload_identify.adapter_factory, "build_media_resolver",
                        lambda session, arr_index=None: type("R", (), {"SOURCE": "x", "resolve": lambda s, p: None})())
    show = tmdb_result(1399, "Show", 2024, "tv")
    details = {**show, "seasons": [{"season_number": 1, "episode_count": episodes_in_season}]}
    fake = FakeTMDB(search={("tv", "Show", None): [show]}, details={("tv", 1399): details})
    monkeypatch.setattr(upload_identify, "tmdb_client", lambda session: fake)
    # Niente ordinamenti alternativi (TVDB, gruppi TMDB): basta il conteggio di TMDB.
    monkeypatch.setattr(episode_orders, "for_job", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("offline")))


def _identified(db_session, tmp_path, monkeypatch, *, episodes_in_season=8, origin="watch"):
    tracker = make_tracker(db_session)
    disk = make_disk(db_session, tmp_path)
    _release(tmp_path)
    _tmdb(monkeypatch, episodes_in_season)
    # Il nome della release non ha l'anno: il match vale 0.85.
    settings_repo.set_setting(db_session, upload_identify.AUTO_MATCH_SETTING, "0.8")
    finished = []
    monkeypatch.setattr(upload_jobs, "_emit_finished", lambda session, job: finished.append(job.id))
    job = upload_jobs.create_job(db_session, disk, f"releases/{FOLDER}",
                                 tracker_choices={tracker.id: {"freeleech": 25}}, origin=origin)
    kicked = []
    worker = type("W", (), {"kick": lambda self, job_id, status: kicked.append((job_id, status))})()
    upload_identify.handle(db_session, job, worker)
    return job, kicked, finished


def test_two_episodes_of_eight_become_two_episode_uploads(db_session, tmp_path, monkeypatch):
    job, kicked, finished = _identified(db_session, tmp_path, monkeypatch)

    assert job.status == "cancelled"
    split = next(e for e in job.events if e.code == "job_split")
    children = [db_session.get(UploadJob, i) for i in json.loads(split.params_json)["jobs"]]
    assert [(c.kind, json.loads(c.seasons_json), c.episode, c.tmdb_id, c.status) for c in children] == [
        ("episode", [1], 1, 1399, "analyzing"), ("episode", [1], 2, 1399, "analyzing")]
    assert [os.path.basename(c.relative_path) for c in children] == list(EPISODES)
    assert {(c.origin, c.split_from_id) for c in children} == {("watch", job.id)}
    # Stessi tracker, con il freeleech scelto, e gli episodi vanno avanti da soli.
    assert [json.loads(c.targets[0].flags_json) for c in children] == [{"freeleech": 25}] * 2
    assert kicked == [(c.id, "analyzing") for c in children]
    assert finished == []  # niente notifica "upload annullato"
    with pytest.raises(upload_jobs.UploadJobError, match="upload_job_split"):
        upload_jobs.resume_job(db_session, job)


def test_a_complete_season_stays_a_pack(db_session, tmp_path, monkeypatch):
    job, _kicked, _finished = _identified(db_session, tmp_path, monkeypatch, episodes_in_season=2)

    assert (job.status, job.kind) == ("analyzing", "season_pack")
    assert db_session.query(UploadJob).count() == 1


def test_the_option_off_or_a_manual_upload_keep_the_pack(db_session, tmp_path, monkeypatch):
    settings_repo.set_setting(db_session, upload_split.SPLIT_SETTING, "false")
    job, _kicked, _finished = _identified(db_session, tmp_path, monkeypatch)
    assert (job.status, job.kind) == ("analyzing", "season_pack")

    settings_repo.set_setting(db_session, upload_split.SPLIT_SETTING, "true")
    manual = upload_jobs.create_job(db_session, job.disk, f"releases/{FOLDER}")
    upload_identify.handle(db_session, manual, None)
    assert (manual.status, manual.kind) == ("analyzing", "season_pack")  # a mano: il tasto al match


def test_the_watched_folder_goes_once_no_episode_is_left(db_session, tmp_path, monkeypatch):
    job, _kicked, _finished = _identified(db_session, tmp_path, monkeypatch)
    first, second = db_session.query(UploadJob).filter(UploadJob.split_from_id == job.id).order_by(UploadJob.id)
    job.disk.watch_rel_path = "releases"
    db_session.commit()
    folder = tmp_path / "releases" / FOLDER
    watch = str(tmp_path / "releases")

    os.remove(folder / EPISODES[0])  # spostato nella cartella delle release dal primo upload
    upload_execute._remove_split_folder(db_session, first, watch)
    assert folder.is_dir()  # c'è ancora il secondo episodio

    os.remove(folder / EPISODES[1])
    upload_execute._remove_split_folder(db_session, second, watch)
    assert not folder.exists()
    removed = next(e for e in second.events if e.code == "watch_folder_removed")
    assert sorted(json.loads(removed.params_json)["files"]) == ["Sample/show.sample.mkv", "Show.S01E01E02.nfo"]


def test_split_from_the_match_by_hand(client, tmp_path, monkeypatch):
    from nazgarr.upload.worker import UploadWorker
    from tests.upload_helpers import InlineExecutor

    session = client.app.state.session_factory()
    disk = make_disk(session, tmp_path)
    make_tracker(session)
    settings_repo.set_setting(session, upload_identify.AUTO_MATCH_SETTING, "0")  # si ferma al match
    _release(tmp_path)
    _tmdb(monkeypatch, 8)
    job = upload_jobs.create_job(session, disk, f"releases/{FOLDER}")
    upload_identify.handle(session, job, None)
    assert job.status == "awaiting_match"
    job_id = job.id
    session.close()
    kicked = []
    client.app.state.upload_worker = UploadWorker(client.app.state.session_factory, str(tmp_path / "data"),
                                                  light_executor=InlineExecutor(), heavy_executor=InlineExecutor())
    monkeypatch.setattr(client.app.state.upload_worker, "kick", lambda job_id, status: kicked.append(job_id))
    body = {"content_type": "tv", "tmdb_id": 1399, "kind": "season_pack", "seasons": [1]}

    assert client.post(f"/api/uploads/{job_id}/split", json={**body, "kind": "episode"}).status_code == 400
    response = client.post(f"/api/uploads/{job_id}/split", json=body)

    assert response.status_code == 200, response.text
    children = response.json()["job_ids"]
    assert len(children) == 2 and kicked == children
    assert client.get(f"/api/uploads/{job_id}").json()["status"] == "cancelled"
