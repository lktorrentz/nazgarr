import json

import pytest

from nazgarr.core.models import TrackerUploadProfile
from nazgarr.upload import decision as upload_decision
from nazgarr.upload import jobs as upload_jobs
from nazgarr.upload import profiles as upload_profiles
from nazgarr.upload.jobs import UploadJobError
from nazgarr.upload.naming import build_name, season_token
from tests.upload_helpers import make_disk, make_tracker, write_video

ITT = "{title} ({year}) {resolution} {source} {video_codec} {audio_codec} {group}"


def test_build_name_adds_the_season_and_drops_empty_tokens():
    values = {"title": "Severance", "year": 2022, "season": "S02", "resolution": "1080p", "source": "WEB-DL",
              "video_codec": "H.264", "audio_codec": None, "group": "NTb"}
    rules = {"templates": {"default": ITT}}
    assert build_name(rules, values) == "Severance (2022) S02 1080p WEB-DL H.264-NTb"
    assert build_name(rules, {**values, "year": None, "group": None}) == "Severance S02 1080p WEB-DL H.264"
    assert build_name({"templates": {"default": "{title} {season} {resolution}"}}, values) == "Severance S02 1080p"
    dotted = {"templates": {"WEBDL": "{title} {season} {resolution} {group}"}, "separator": "."}
    assert build_name(dotted, {**values, "type": "WEBDL"}) == "Severance.S02.1080p-NTb"


@pytest.mark.parametrize(("kind", "seasons", "episode", "token"), [
    ("episode", [2], 3, "S02E03"), ("season_pack", [2], None, "S02"), ("complete_pack", [1, 2, 3], None, "S01-S03"),
    ("complete_pack", [4], None, "S04"), ("season_pack", [], None, None),
])
def test_season_token(kind, seasons, episode, token):
    assert season_token(kind, seasons, episode) == token


@pytest.fixture
def decision_job(db_session, tmp_path):
    """Un season pack a match confermato e analizzato, verso ITT e un tracker custom."""
    folder = tmp_path / "Severance.S02.1080p.ATVP.WEB-DL.DDP5.1.H.264-NTb"
    for e in (1, 2):
        write_video(folder / f"Severance.S02E0{e}.1080p.ATVP.WEB-DL.DDP5.1.H.264-NTb.mkv")
    disk = make_disk(db_session, tmp_path)
    itt = make_tracker(db_session, "itt", with_profile=False)
    upload_profiles.create_upload_profile(db_session, itt, "itt")
    make_tracker(db_session, "custom")
    job = upload_jobs.create_job(db_session, disk, folder.name)
    upload_jobs.transition(
        db_session, job, "identifying", "awaiting_decision", kind="season_pack", content_type="tv",
        title="Severance", year=2022, tmdb_id=95396, seasons_json="[2]",
    )
    for target in job.targets:
        target.status = "awaiting_decision"
    db_session.commit()
    upload_decision.propose(db_session, job)
    return job


def test_propose_names_ids_and_flags_per_tracker(decision_job):
    itt, custom = decision_job.targets
    # Regole ITT: pattern per le serie (senza anno), servizio, tipo WEB-DL.
    assert itt.proposed_name == "Severance S02 1080p FullHD ATVP WEB-DL DD+ 5.1 SDR H.264-NTb"
    assert (itt.category_id, itt.type_id, itt.resolution_id) == (2, 4, 3)
    assert json.loads(itt.flags_json) == {"anonymous": False, "personal_release": False, "internal": False,
                                          "stream": False, "freeleech": 0}
    # Profilo custom vuoto: il nome col formato di default, nessun id.
    assert custom.proposed_name == "Severance (2022) S02 1080p WEB-DL H.264 DD+ 5.1-NTb"
    assert (custom.category_id, custom.type_id, custom.resolution_id) == (None, None, None)
    assert json.loads(decision_job.analysis_json)["detected"]["service"] == "ATVP"


def test_the_profile_can_make_internal_the_default(db_session, decision_job):
    from nazgarr.core.models import TrackerUploadProfile
    itt = decision_job.targets[0]
    db_session.get(TrackerUploadProfile, itt.tracker_id).default_internal = True
    itt.flags_json = None
    db_session.commit()

    upload_decision.propose(db_session, decision_job)

    assert json.loads(itt.flags_json)["internal"] is True


def test_overrides_recompute_names_and_ids(db_session, decision_job):
    upload_decision.update_overrides(db_session, decision_job, {"group": "ME", "type": "WEBRIP", "resolution": "",
                                                                "screenshot_count": "6"})

    itt = decision_job.targets[0]
    assert itt.proposed_name.endswith("-ME")
    assert itt.type_id == 5
    assert json.loads(decision_job.overrides_json) == {"group": "ME", "type": "WEBRIP", "screenshot_count": 6}

    with pytest.raises(UploadJobError) as exc:
        upload_decision.update_overrides(db_session, decision_job, {"bogus": 1})
    assert exc.value.code == "upload_unknown_override"
    with pytest.raises(UploadJobError):
        upload_decision.update_overrides(db_session, decision_job, {"screenshot_count": 99})


def _upload(target, **extra):
    return {"target_id": target.id, "action": "upload", "name": "Name", "category_id": 2, "type_id": 4,
            "resolution_id": 3, "flags": {"anonymous": True}, **extra}


def test_approve_queues_the_job_with_every_decision(db_session, decision_job):
    itt, custom = decision_job.targets

    upload_decision.approve(db_session, decision_job, [_upload(itt), {"target_id": custom.id, "action": "skip"}])

    assert decision_job.status == "queued" and decision_job.queue_position == 1
    assert (itt.status, itt.action, itt.approved_name) == ("approved", "upload", "Name")
    assert json.loads(itt.flags_json) == {"anonymous": True, "personal_release": False, "internal": False,
                                          "stream": False, "freeleech": 0}
    assert (custom.status, custom.action) == ("skipped", "skip")
    assert decision_job.events[-1].code == "job_queued"


def test_all_skipped_is_done(db_session, decision_job):
    skip_all = [{"target_id": t.id, "action": "skip"} for t in decision_job.targets]
    upload_decision.approve(db_session, decision_job, skip_all)
    assert decision_job.status == "done"


@pytest.mark.parametrize(("change", "code"), [
    ({"name": " "}, "upload_name_required"),
    ({"type_id": None}, "upload_ids_required"),
    ({"action": "maybe"}, "upload_invalid_action"),
    ({"action": "reseed"}, "upload_reseed_needs_identical"),
])
def test_approve_validates_every_tracker(db_session, decision_job, change, code):
    itt, custom = decision_job.targets
    with pytest.raises(UploadJobError) as exc:
        upload_decision.approve(db_session, decision_job, [_upload(itt, **change), _upload(custom)])
    assert exc.value.code == code
    assert decision_job.status == "awaiting_decision"


def test_approve_needs_a_decision_for_every_tracker_and_no_check_running(db_session, decision_job):
    itt, custom = decision_job.targets
    with pytest.raises(UploadJobError) as exc:
        upload_decision.approve(db_session, decision_job, [_upload(itt)])
    assert exc.value.code == "upload_decision_missing"

    custom.status = "verifying"
    db_session.commit()
    with pytest.raises(UploadJobError) as exc:
        upload_decision.approve(db_session, decision_job, [_upload(itt), _upload(custom)])
    assert exc.value.code == "upload_target_busy"


def test_reseed_of_an_identical_release_needs_a_passed_hash_check(db_session, decision_job):
    itt, custom = decision_job.targets
    itt.dupes_json = json.dumps([{"torrent_id_remote": "42", "verdict": "identical"}])
    db_session.commit()
    with pytest.raises(UploadJobError) as exc:
        upload_decision.approve(db_session, decision_job, [
            {"target_id": itt.id, "action": "reseed", "reseed_torrent_id": "42"}, _upload(custom),
        ])
    assert exc.value.code == "upload_reseed_needs_verification"

    itt.dupes_json = json.dumps([{"torrent_id_remote": "42", "verdict": "identical",
                                  "verification": {"status": "passed"}}])
    db_session.commit()
    upload_decision.approve(db_session, decision_job, [
        {"target_id": itt.id, "action": "reseed", "reseed_torrent_id": "42"}, _upload(custom),
    ])

    assert (itt.action, itt.reseed_torrent_id, itt.status) == ("reseed", "42", "approved")


def test_freeleech_from_creation_survives_the_proposal_and_is_validated(db_session, tmp_path):
    folder = tmp_path / "Movie.2024.1080p.WEB-DL-GRP.mkv"
    write_video(folder)
    disk = make_disk(db_session, tmp_path)
    tracker = make_tracker(db_session, "fl")
    profile = db_session.get(TrackerUploadProfile, tracker.id)
    profile.freeleech_options_json = "[25, 50]"
    db_session.commit()
    job = upload_jobs.create_job(db_session, disk, folder.name, tracker_choices={tracker.id: {"freeleech": 50}})
    upload_jobs.transition(db_session, job, "identifying", "awaiting_decision", kind="movie", content_type="movie",
                           title="Movie", year=2024, tmdb_id=1)
    job.targets[0].status = "awaiting_decision"
    db_session.commit()

    upload_decision.propose(db_session, job)
    target = job.targets[0]
    assert json.loads(target.flags_json)["freeleech"] == 50

    with pytest.raises(UploadJobError) as exc:
        upload_decision.approve(db_session, job, [_upload(target, flags={"freeleech": 75})])
    assert exc.value.code == "upload_freeleech_not_allowed"
    upload_decision.approve(db_session, job, [_upload(target, flags={"freeleech": 25})])
    assert json.loads(target.flags_json)["freeleech"] == 25


def test_the_decision_offers_the_file_names_of_each_mode(db_session, decision_job):
    upload_decision.propose(db_session, decision_job)
    names = json.loads(decision_job.analysis_json)["file_names"]

    assert set(names["available"]) <= {"hardlink", "generated", "original"} and "original" in names["available"]
    assert names["default"] in names["available"] or names["default"] == "original"
    assert all(preview["count"] >= 1 for preview in names["previews"].values())
    with pytest.raises(UploadJobError):
        upload_decision.update_overrides(db_session, decision_job, {"file_naming": "whatever"})
    upload_decision.update_overrides(db_session, decision_job, {"file_naming": "original"})
    assert json.loads(decision_job.overrides_json)["file_naming"] == "original"


def test_the_release_details_come_from_the_file_then_the_folder(db_session):
    from types import SimpleNamespace

    from nazgarr.upload.file_names import name_detected

    job = SimpleNamespace(
        analysis_json=json.dumps({"name_source": {
            "name": "Movie.Name.2024.WEB-DL.H.264-GRP", "fallback": "Movie Name 2024 1080p WEB-DL", "origin": "source",
        }}),
        source_path="/data/watch/Movie Name 2024 1080p WEB-DL",
    )

    detected = name_detected(db_session, job)

    assert (detected["group"], detected["source"]) == ("GRP", "WEB-DL")  # dal file
    assert detected["resolution"] == "1080p"  # il file non la dice: dalla cartella


def test_without_a_group_in_the_name_the_releaser_name_is_used(db_session):
    from types import SimpleNamespace

    from nazgarr.core import settings_repo
    from nazgarr.upload.file_names import name_detected

    def job(name):
        return SimpleNamespace(analysis_json=json.dumps({"name_source": {"name": name}}), source_path=f"/data/{name}")

    assert name_detected(db_session, job("Movie Name (2024).mkv"))["group"] is None
    settings_repo.set_setting(db_session, "upload_releaser_name", " NZG ")
    assert name_detected(db_session, job("Movie Name (2024).mkv"))["group"] == "NZG"
    assert name_detected(db_session, job("Movie.Name.2024.1080p.WEB-DL.H.264-GRP.mkv"))["group"] == "GRP"


def test_the_detected_details_suggest_what_the_trackers_accept(db_session, tmp_path):
    from nazgarr.core.models import TrackerUploadProfile
    from nazgarr.upload import jobs as upload_jobs
    from nazgarr.upload.decision import field_options
    from tests.upload_helpers import make_disk, make_tracker, write_video

    tracker = make_tracker(db_session)
    profile = db_session.get(TrackerUploadProfile, tracker.id)
    profile.type_id_map_json = json.dumps({"REMUX": 1, "WEBDL": 4})
    profile.resolution_id_map_json = json.dumps({"2160p": 1, "1080p": 2, "720p": 3})
    db_session.commit()
    write_video(tmp_path / "Movie.2024.mkv")
    job = upload_jobs.create_job(db_session, make_disk(db_session, tmp_path), "Movie.2024.mkv")

    options = field_options(db_session, job)

    assert options["type"] == ["REMUX", "WEBDL"]
    assert options["resolution"] == ["720p", "1080p", "2160p"]
    assert "BluRay" in options["source"] and "NF" in options["service"] and "TrueHD" in options["audio"]


def test_files_are_renamed_automatically_unless_turned_off(db_session, tmp_path):
    from nazgarr.core import settings_repo
    from nazgarr.upload import file_names as upload_file_names
    from nazgarr.upload import jobs as upload_jobs
    from tests.upload_helpers import make_disk, make_tracker, write_video

    make_tracker(db_session)
    disk = make_disk(db_session, tmp_path)
    write_video(tmp_path / "releases" / "My.Movie.2024.mkv")
    watched = upload_jobs.create_job(db_session, disk, "releases/My.Movie.2024.mkv", origin="watch")

    # Una release della cartella osservata prende il nome generato...
    assert upload_file_names.default_mode(db_session, watched) == "generated"
    # ...a meno che il rename automatico sia spento: allora i nomi originali.
    settings_repo.set_setting(db_session, upload_file_names.AUTO_RENAME_SETTING, "false")
    assert upload_file_names.default_mode(db_session, watched) == "original"


def test_the_file_names_preview_reads_the_source_once(db_session, decision_job, monkeypatch):
    """Ogni modifica di un override ricalcola l'anteprima: disponibili,
    predefinita e il piano di ogni modalità leggono la sorgente una volta."""
    from nazgarr.upload import decision as upload_decision
    from nazgarr.upload import file_names as upload_file_names

    walks = []
    real = upload_file_names._source_files
    monkeypatch.setattr(upload_file_names, "_source_files", lambda job: walks.append(1) or real(job))

    preview = upload_decision.file_names_preview(db_session, decision_job)

    assert preview["previews"] and len(walks) == 1


def _itt_only_job(db_session, tmp_path, monkeypatch, *, also_custom=False):
    from dataclasses import asdict
    from types import SimpleNamespace

    from nazgarr.upload import identify as upload_identify
    from nazgarr.upload.source import scan_source

    folder = tmp_path / "Severance.S02.1080p.ATVP.WEB-DL.DDP5.1.H.264-NTb"
    for e in (1, 2):
        write_video(folder / f"Severance.S02E0{e}.1080p.ATVP.WEB-DL.DDP5.1.H.264-NTb.mkv")
    disk = make_disk(db_session, tmp_path)
    itt = make_tracker(db_session, "itt", with_profile=False)
    upload_profiles.create_upload_profile(db_session, itt, "itt")
    itt.language = "it"
    if also_custom:
        make_tracker(db_session, "custom")
    db_session.commit()
    monkeypatch.setattr(upload_identify, "tmdb_client", lambda session: SimpleNamespace(
        localized_title=lambda content_type, tmdb_id, language: "Scissione" if language == "it" else None))
    job = upload_jobs.create_job(db_session, disk, folder.name)
    upload_jobs.transition(
        db_session, job, "identifying", "awaiting_decision", kind="season_pack", content_type="tv",
        title="Severance", year=2022, tmdb_id=95396, seasons_json="[2]",
        layout_json=json.dumps(asdict(scan_source(str(folder)))),
    )
    for target in job.targets:
        target.status = "awaiting_decision"
    db_session.commit()
    upload_decision.propose(db_session, job)
    return json.loads(job.analysis_json)["file_names"]["previews"]["generated"]


def test_generated_file_names_use_the_title_in_the_trackers_language(db_session, tmp_path, monkeypatch):
    # Segnalato (2026-10-09): i nomi generati prendevano il titolo originale (inglese) anche per ITT.
    generated = _itt_only_job(db_session, tmp_path, monkeypatch)

    assert generated["name"].startswith("Scissione.S02")
    assert all(name.split("/")[-1].startswith("Scissione.S02E0") for name in generated["files"])


def test_trackers_that_disagree_keep_the_original_title(db_session, tmp_path, monkeypatch):
    generated = _itt_only_job(db_session, tmp_path, monkeypatch, also_custom=True)

    assert generated["name"].startswith("Severance.S02")
