"""Setup diretto via session_factory dell'app (esposta dal client fixture)
per creare candidate/match_review senza dover passare da uno scan+match
completo, che richiederebbe un tracker/client reali."""

from nazgarr.core.models import Candidate, MatchReview, MediaItem, Tracker


def _session(client):
    return client.app.state.session_factory()


def test_list_reviews_empty(client):
    response = client.get("/api/reviews")
    assert response.status_code == 200
    assert response.json() == []


def test_approve_and_reject_review(client):
    session = _session(client)
    try:
        tracker = Tracker(label="t", adapter_type="unit3d", base_url="https://t.example", api_token="x")
        session.add(tracker)
        session.commit()
        item = MediaItem(content_type="movie", tmdb_id=1)
        session.add(item)
        session.commit()
        candidate = Candidate(
            media_item_id=item.id, tracker_id=tracker.id, torrent_id_remote="1", name="x", size_bytes=1,
            source="catalog_search", direction="media_to_torrent", confidence=0.5,
        )
        session.add(candidate)
        session.commit()
        match_review = MatchReview(candidate_id=candidate.id, status="pending")
        session.add(match_review)
        session.commit()
        review_id = match_review.id
    finally:
        session.close()

    listed = client.get("/api/reviews").json()
    assert len(listed) == 1
    assert listed[0]["id"] == review_id
    assert listed[0]["direction"] == "media_to_torrent"

    reject = client.post(f"/api/reviews/{review_id}/reject")
    assert reject.status_code == 200
    assert reject.json()["status"] == "rejected"

    assert client.get("/api/reviews").json() == []


def test_approve_nonexistent_review_404(client):
    response = client.post("/api/reviews/999/approve")
    assert response.status_code == 404


def test_client_labels_of_a_reseed_are_chosen_on_its_review(client):
    from nazgarr.core.models import TorrentClient

    session = _session(client)
    try:
        qb = TorrentClient(label="qb", adapter_type="qbittorrent", base_url="http://qb.test", category_tv="tv",
                           tags_reseed="reseed")
        tracker = Tracker(label="t", adapter_type="unit3d", base_url="https://t.example", api_token="x")
        item = MediaItem(content_type="tv", tmdb_id=2)
        session.add_all([qb, tracker, item])
        session.commit()
        candidate = Candidate(
            media_item_id=item.id, tracker_id=tracker.id, torrent_id_remote="1", name="x", size_bytes=1,
            source="catalog_search", direction="media_to_torrent", confidence=0.5,
        )
        session.add(candidate)
        session.commit()
        match_review = MatchReview(candidate_id=candidate.id, status="pending")
        session.add(match_review)
        session.commit()
        review_id, client_id = match_review.id, qb.id
    finally:
        session.close()

    [listed] = client.get("/api/reviews").json()
    # I default del client, come li vedrebbe il reseed senza scelte.
    assert (listed["torrent_client_id"], listed["default_client_category"], listed["default_client_tags"]) == (
        client_id, "tv", "reseed")
    assert (listed["client_category"], listed["client_tags"]) == (None, None)

    chosen = client.put(f"/api/reviews/{review_id}/client-labels",
                        json={"client_category": "anime", "client_tags": "a, b"}).json()
    assert (chosen["client_category"], chosen["client_tags"]) == ("anime", "a,b")
    back = client.put(f"/api/reviews/{review_id}/client-labels", json={}).json()
    assert (back["client_category"], back["client_tags"]) == (None, None)

    client.post(f"/api/reviews/{review_id}/reject")
    assert client.put(f"/api/reviews/{review_id}/client-labels", json={}).status_code == 409


_next_tmdb = iter(range(1000, 2000))


def _review(client, status="pending", decided_by=None, seed_job=None, verify_status=None):
    from nazgarr.core.models import SeedJob

    session = _session(client)
    try:
        tracker = Tracker(label="t", adapter_type="unit3d", base_url="https://t.example", api_token="x")
        item = MediaItem(content_type="movie", tmdb_id=next(_next_tmdb))
        session.add_all([tracker, item])
        session.commit()
        candidate = Candidate(
            media_item_id=item.id, tracker_id=tracker.id, torrent_id_remote="1", name="x", size_bytes=1,
            source="catalog_search", direction="media_to_torrent", confidence=0.5,
        )
        session.add(candidate)
        session.commit()
        row = MatchReview(candidate_id=candidate.id, status=status, decided_by=decided_by, verify_status=verify_status)
        session.add(row)
        if seed_job:
            session.add(SeedJob(candidate_id=candidate.id, final_status=seed_job))
        session.commit()
        return row.id
    finally:
        session.close()


def _no_verification(client):
    from nazgarr.core import settings_repo

    session = _session(client)
    try:
        settings_repo.set_setting(session, "verify_before_execute", "false")
    finally:
        session.close()


def test_a_decided_review_is_never_approved_or_executed_again(client, monkeypatch):
    # Con la verifica spenta una review rifiutata (dal sistema o dall'utente)
    # o già eseguita ripartiva: hardlink e client per un file escluso, o un
    # secondo seed job (audit 2026-10-18, finding 65).
    from nazgarr.reseed import review

    executed = []
    monkeypatch.setattr(review, "_try_execute", lambda session, row, **kw: executed.append(row.id))
    _no_verification(client)
    cases = [_review(client, "rejected", "system"), _review(client, "rejected", "user"),
             _review(client, "approved", "user", seed_job="seeding")]

    for review_id in cases:
        response = client.post(f"/api/reviews/{review_id}/approve")
        assert response.status_code == 409
        assert response.json()["detail"] == {"code": "review_already_decided", "params": {"id": review_id}}
        assert client.post(f"/api/reviews/{review_id}/reject").status_code == 409
    assert executed == []


def test_approving_twice_executes_once(client, monkeypatch):
    from nazgarr.reseed import review

    executed = []
    monkeypatch.setattr(review, "_try_execute", lambda session, row, **kw: executed.append(row.id))
    _no_verification(client)
    review_id = _review(client)

    assert client.post(f"/api/reviews/{review_id}/approve").status_code == 200
    assert client.post(f"/api/reviews/{review_id}/approve").status_code == 409
    assert executed == [review_id]


def test_with_verification_a_decided_review_starts_no_check(client, monkeypatch):
    from nazgarr.reseed import full_check

    started = []
    monkeypatch.setattr(full_check, "start_check", lambda *a, **k: started.append(a))
    review_id = _review(client, "rejected", "system")

    assert client.post(f"/api/reviews/{review_id}/approve").status_code == 409
    assert started == []


def _statuses(client, ids):
    session = _session(client)
    try:
        return [(row.status, row.verify_status) for row in (session.get(MatchReview, i) for i in ids)]
    finally:
        session.close()


def _approve_all_queue(client):
    # Una consigliata, una sotto soglia, una consigliata col controllo già
    # fallito, una consigliata in verifica e una già eseguita.
    return {
        "recommended": _review(client, "auto_approved"),
        "below": _review(client, "pending"),
        "failed": _review(client, "auto_approved", verify_status="failed"),
        "verifying": _review(client, "auto_approved", verify_status="verifying"),
        "executed": _review(client, "auto_approved", seed_job="seeding"),
    }


def test_approve_all_takes_only_the_recommended_by_default(client, monkeypatch):
    from nazgarr.reseed import approve_all, review

    monkeypatch.setattr(approve_all, "_current", None)
    executed = []
    monkeypatch.setattr(review, "_try_execute", lambda session, row, **kw: executed.append(row.id))
    _no_verification(client)
    queue = _approve_all_queue(client)

    status = client.get("/api/reviews/approve-all").json()
    assert (status["recommended"], status["below_threshold"], status["verify_failed"]) == (1, 1, 1)
    assert status["verify_before_execute"] is False
    assert status["batch"] is None

    response = client.post("/api/reviews/approve-all", json={})
    assert response.status_code == 202
    assert (response.json()["total"], response.json()["verify"]) == (1, False)

    batch = client.get("/api/reviews/approve-all").json()["batch"]
    assert batch["finished_at"] is not None
    assert (batch["done"], batch["approved"], batch["skipped"], batch["failed"]) == (1, 1, 0, 0)
    assert executed == [queue["recommended"]]
    assert _statuses(client, [queue["below"], queue["failed"], queue["verifying"]]) == [
        ("pending", None), ("auto_approved", "failed"), ("auto_approved", "verifying")]


def test_approve_all_can_include_the_ones_below_the_threshold(client, monkeypatch):
    from nazgarr.reseed import approve_all, review

    monkeypatch.setattr(approve_all, "_current", None)
    executed = []
    monkeypatch.setattr(review, "_try_execute", lambda session, row, **kw: executed.append(row.id))
    _no_verification(client)
    queue = _approve_all_queue(client)

    assert client.post("/api/reviews/approve-all", json={"include_below_threshold": True}).json()["total"] == 2
    assert sorted(executed) == sorted([queue["recommended"], queue["below"]])
    assert client.get("/api/reviews/approve-all").json()["recommended"] == 0


def test_with_verification_approve_all_only_queues_the_checks(client, monkeypatch):
    from types import SimpleNamespace

    from nazgarr.reseed import approve_all, full_check, review

    monkeypatch.setattr(approve_all, "_current", None)
    executed, started = [], []
    monkeypatch.setattr(review, "_try_execute", lambda session, row, **kw: executed.append(row.id))

    def start_check(*_args, review_id=None, **_kwargs):
        started.append(review_id)
        return SimpleNamespace(id=f"c{review_id}")

    monkeypatch.setattr(full_check, "start_check", start_check)
    queue = _approve_all_queue(client)

    assert client.post("/api/reviews/approve-all", json={}).json()["verify"] is True
    assert started == [queue["recommended"]]
    assert executed == []
    assert _statuses(client, [queue["recommended"]]) == [("auto_approved", "verifying")]
    # In verifica non si conta più: un secondo clic non la riprende.
    assert client.get("/api/reviews/approve-all").json()["recommended"] == 0


def test_a_review_decided_meanwhile_is_skipped(client, monkeypatch):
    from nazgarr.reseed import approve_all, review

    executed = []
    monkeypatch.setattr(review, "_try_execute", lambda session, row, **kw: executed.append(row.id))
    _no_verification(client)
    rejected = _review(client, "rejected", "user")
    batch = approve_all.Batch(id="b", total=2, verify=False)

    approve_all.run(client.app.state.session_factory, batch, [rejected, 999_999])

    assert (batch.done, batch.approved, batch.skipped, batch.failed) == (2, 0, 2, 0)
    assert batch.finished_at is not None
    assert executed == []


def test_only_one_approve_all_at_a_time(client, monkeypatch):
    from nazgarr.reseed import approve_all

    monkeypatch.setattr(approve_all, "_current", approve_all.Batch(id="running", total=5, verify=True))
    _review(client, "auto_approved")

    response = client.post("/api/reviews/approve-all", json={})
    assert response.status_code == 409
    assert response.json()["detail"] == {"code": "approve_all_in_progress", "params": {}}
