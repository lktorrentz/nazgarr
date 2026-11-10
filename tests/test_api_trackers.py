def test_create_list_update_delete_tracker(client):
    create = client.post(
        "/api/trackers",
        json={"label": "ITT", "adapter_type": "unit3d", "base_url": "https://itt.example", "api_token": "secret"},
    )
    assert create.status_code == 201
    tracker_id = create.json()["id"]
    assert "api_token" not in create.json()  # mai esposto in risposta

    listed = client.get("/api/trackers").json()
    assert any(t["id"] == tracker_id for t in listed)

    update = client.patch(f"/api/trackers/{tracker_id}", json={"enabled": False})
    assert update.status_code == 200
    assert update.json()["enabled"] is False

    delete = client.delete(f"/api/trackers/{tracker_id}")
    assert delete.status_code == 204
    assert client.get("/api/trackers").json() == []


def test_rejects_unsupported_adapter_type(client):
    response = client.post(
        "/api/trackers",
        json={"label": "x", "adapter_type": "gazelle", "base_url": "https://x.example", "api_token": "t"},
    )
    assert response.status_code == 400


def test_tracker_client_can_be_set_and_cleared(client):
    created = client.post("/api/trackers", json={
        "label": "ITT", "adapter_type": "unit3d", "base_url": "https://t.example", "api_token": "x",
        "torrent_client_id": 3,
    }).json()
    assert created["torrent_client_id"] == 3

    kept = client.patch(f"/api/trackers/{created['id']}", json={"label": "ITT 2"}).json()
    assert kept["torrent_client_id"] == 3
    cleared = client.patch(f"/api/trackers/{created['id']}", json={"torrent_client_id": None}).json()
    assert cleared["torrent_client_id"] is None


def test_tracker_icon_and_upload_profile_summary(client, monkeypatch, tmp_path):
    from nazgarr.api import trackers as trackers_api

    tracker_id = client.post("/api/trackers", json={
        "label": "ITT", "adapter_type": "unit3d", "base_url": "https://itt.example", "api_token": "x",
    }).json()["id"]
    assert client.get("/api/trackers").json()[0]["upload_profile"] is None

    client.post(f"/api/trackers/{tracker_id}/upload-profile", json={"profile_key": "itt"})
    summary = client.get("/api/trackers").json()[0]["upload_profile"]
    assert summary["source_profile_key"] == "itt" and summary["naming_version"] >= 1

    monkeypatch.setattr(trackers_api.tracker_icons, "fetch_icon", lambda data_dir, tid, base_url: None)
    assert client.get(f"/api/trackers/{tracker_id}/icon").status_code == 404
    icon = tmp_path / "icon.png"
    icon.write_bytes(b"\\x89PNG")
    monkeypatch.setattr(trackers_api.tracker_icons, "fetch_icon", lambda data_dir, tid, base_url: str(icon))
    assert client.get(f"/api/trackers/{tracker_id}/icon").status_code == 200


def test_the_seeding_requirement_is_optional_and_editable(client):
    created = client.post("/api/trackers", json={
        "label": "ITT", "adapter_type": "unit3d", "base_url": "https://t.example", "api_token": "x",
    }).json()
    assert (created["min_seed_time_seconds"], created["min_ratio"], created["seed_rule"]) == (None, None, "any")

    url = f"/api/trackers/{created['id']}"
    set_ = client.patch(url, json={"min_seed_time_seconds": 259200, "min_ratio": 1.0, "seed_rule": "all"}).json()
    assert (set_["min_seed_time_seconds"], set_["min_ratio"], set_["seed_rule"]) == (259200, 1.0, "all")
    kept = client.patch(url, json={"label": "ITT 2"}).json()
    assert kept["min_ratio"] == 1.0
    cleared = client.patch(url, json={"min_ratio": None}).json()
    assert (cleared["min_ratio"], cleared["min_seed_time_seconds"]) == (None, 259200)
    assert client.patch(url, json={"min_ratio": -1}).status_code == 422
    assert client.patch(url, json={"seed_rule": "maybe"}).status_code == 422


def test_the_rate_limit_must_be_a_usable_number(client):
    """Un valore negativo faceva sollevare IndexError al limitatore a ogni
    chiamata al tracker (audit 2026-10-18)."""
    body = {"label": "t", "adapter_type": "unit3d", "base_url": "https://t.example", "api_token": "x"}
    assert client.post("/api/trackers", json={**body, "rate_limit_per_min": -5}).status_code == 422
    assert client.post("/api/trackers", json={**body, "rate_limit_per_min": 100_000}).status_code == 422
    created = client.post("/api/trackers", json={**body, "rate_limit_per_min": 0}).json()
    assert created["rate_limit_per_min"] == 30  # 0 vale il default
    assert client.patch(f"/api/trackers/{created['id']}", json={"rate_limit_per_min": -1}).status_code == 422
    assert client.patch(f"/api/trackers/{created['id']}", json={"rate_limit_per_min": 60}).json()[
        "rate_limit_per_min"] == 60
    assert client.patch(f"/api/trackers/{created['id']}", json={"rate_limit_per_min": 0}).json()[
        "rate_limit_per_min"] == 30


# --- la sessione web, per il claim della resurrection (decisione dell'utente, 2026-11-10)


def _unit3d(client, **extra):
    return client.post("/api/trackers", json={"label": "ITT", "adapter_type": "unit3d",
                                              "base_url": "https://itt.example", "api_token": "tok", **extra})


def test_the_session_cookie_is_saved_clean_and_never_returned(client):
    created = _unit3d(client, session_cookie="Cookie: remember_web_x=abc;  unit3d_session=s").json()
    assert created["has_session_cookie"] is True
    assert "abc" not in str(client.get("/api/trackers").json())

    from nazgarr.core.models import Tracker

    session = client.app.state.session_factory()
    try:
        assert session.get(Tracker, created["id"]).history_session_cookie == "remember_web_x=abc; unit3d_session=s"
    finally:
        session.close()

    kept = client.patch(f"/api/trackers/{created['id']}", json={"label": "ITT 2"}).json()
    assert kept["has_session_cookie"] is True
    cleared = client.patch(f"/api/trackers/{created['id']}", json={"session_cookie": ""}).json()
    assert cleared["has_session_cookie"] is False


def test_a_cookie_line_that_could_break_a_header_is_refused(client):
    response = _unit3d(client, session_cookie="a=1\r\nX-Evil: 1")
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "tracker_session_cookie_invalid"


def test_a_new_host_drops_the_session_of_the_old_one(client):
    created = _unit3d(client, session_cookie="a=1").json()
    moved = client.patch(f"/api/trackers/{created['id']}", json={"base_url": "https://other.example",
                                                                 "api_token": "tok"}).json()
    assert moved["has_session_cookie"] is False


def test_the_session_check_reports_what_the_site_says(client, monkeypatch):
    from nazgarr.adapters.tracker.base import NotSupportedError, TrackerWebResult
    from nazgarr.integrations import adapter_factory

    created = _unit3d(client, session_cookie="a=1").json()

    class _Adapter:
        result = TrackerWebResult("session_expired")

        def check_web_session(self):
            if self.result is None:
                raise NotSupportedError("no site")
            return self.result

        def close(self):
            pass

    monkeypatch.setattr(adapter_factory, "build_tracker_adapter", lambda row: _Adapter())
    checked = client.post(f"/api/trackers/{created['id']}/session-check")
    assert checked.status_code == 200 and checked.json() == {"status": "session_expired", "reason": None}

    _Adapter.result = None
    assert client.post(f"/api/trackers/{created['id']}/session-check").json()["status"] == "unsupported"
