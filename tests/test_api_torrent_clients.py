def test_create_list_update_delete_torrent_client(client):
    create = client.post(
        "/api/torrent-clients",
        json={"label": "qbt", "adapter_type": "qbittorrent", "base_url": "http://qbt:8080", "username": "admin"},
    )
    assert create.status_code == 201
    tc_id = create.json()["id"]
    assert create.json()["enabled"] is True

    listed = client.get("/api/torrent-clients").json()
    assert any(tc["id"] == tc_id for tc in listed)

    update = client.patch(f"/api/torrent-clients/{tc_id}", json={"enabled": False})
    assert update.status_code == 200
    assert update.json()["enabled"] is False

    delete = client.delete(f"/api/torrent-clients/{tc_id}")
    assert delete.status_code == 204
    assert client.get("/api/torrent-clients").json() == []


def test_rejects_unsupported_adapter_type(client):
    response = client.post(
        "/api/torrent-clients",
        json={"label": "utorrent", "adapter_type": "utorrent", "base_url": "http://utorrent:8080"},
    )
    assert response.status_code == 400


def test_deluge_transmission_and_rtorrent_are_built_in(client):
    from nazgarr.adapters.torrent_client.deluge import DelugeAdapter
    from nazgarr.adapters.torrent_client.rtorrent import RTorrentAdapter
    from nazgarr.adapters.torrent_client.transmission import TransmissionAdapter
    from nazgarr.core.models import TorrentClient
    from nazgarr.integrations import adapter_factory

    expected = {"deluge": DelugeAdapter, "transmission": TransmissionAdapter, "rutorrent": RTorrentAdapter}
    for adapter_type, cls in expected.items():
        created = client.post("/api/torrent-clients", json={
            "label": adapter_type, "adapter_type": adapter_type, "base_url": f"http://{adapter_type}:8080",
            "username": "u", "password": "pw-1234",
        })
        assert created.status_code == 201, created.text
        assert "pw-1234" not in created.text
        with client.app.state.session_factory() as session:
            adapter = adapter_factory.build_torrent_client_adapter(session.get(TorrentClient, created.json()["id"]))
        assert isinstance(adapter, cls)


def test_connection_test_reports_success(client, monkeypatch):
    tc_id = client.post(
        "/api/torrent-clients", json={"label": "qbt", "adapter_type": "qbittorrent", "base_url": "http://qbt"}
    ).json()["id"]

    class FakeAdapter:
        def list_torrents(self):
            return [object(), object(), object()]

    monkeypatch.setattr("nazgarr.integrations.adapter_factory.build_torrent_client_adapter", lambda tc: FakeAdapter())

    response = client.post(f"/api/torrent-clients/{tc_id}/test")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "torrents_found": 3, "error": None, "warnings": []}


def test_connection_test_only_counts_the_torrents(client, monkeypatch):
    """La prova chiedeva i file di ogni torrent solo per contarli (audit
    2026-10-18): con count_torrents una richiesta sola."""
    tc_id = client.post(
        "/api/torrent-clients", json={"label": "qbt", "adapter_type": "qbittorrent", "base_url": "http://qbt"}
    ).json()["id"]

    class FakeAdapter:
        def count_torrents(self):
            return 5000

        def list_torrents(self):
            raise AssertionError("l'elenco completo non serve per contare")

    monkeypatch.setattr("nazgarr.integrations.adapter_factory.build_torrent_client_adapter", lambda tc: FakeAdapter())

    assert client.post(f"/api/torrent-clients/{tc_id}/test").json()["torrents_found"] == 5000


def test_connection_test_logs_in_again_instead_of_reusing_the_session(client, monkeypatch):
    """qBittorrent tiene la sessione fra le operazioni (2026-11-24): la prova
    no, è delle credenziali salvate, e lo scorda prima di contare."""
    tc_id = client.post(
        "/api/torrent-clients", json={"label": "qbt", "adapter_type": "qbittorrent", "base_url": "http://qbt"}
    ).json()["id"]
    calls = []

    class FakeAdapter:
        def forget_session(self):
            calls.append("forget")

        def count_torrents(self):
            calls.append("count")
            return 1

    monkeypatch.setattr("nazgarr.integrations.adapter_factory.build_torrent_client_adapter", lambda tc: FakeAdapter())

    assert client.post(f"/api/torrent-clients/{tc_id}/test").json()["status"] == "ok"
    assert calls == ["forget", "count"]


def test_connection_test_reports_failure_against_unreachable_host(client):
    # Porta 1 su localhost: quasi certamente nessuno in ascolto, connection
    # refused immediato — nessuna chiamata di rete reale verso l'esterno.
    tc_id = client.post(
        "/api/torrent-clients",
        json={"label": "qbt", "adapter_type": "qbittorrent", "base_url": "http://127.0.0.1:1"},
    ).json()["id"]

    response = client.post(f"/api/torrent-clients/{tc_id}/test")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "error"
    assert body["error"]


def test_associate_and_dissociate_disk(client):
    (client.scan_root / "disk1").mkdir()
    disk_create = client.post("/api/disks", json={"label": "Disk 1", "root_path": str(client.scan_root / "disk1")})
    disk_id = disk_create.json()["id"]
    tc_id = client.post(
        "/api/torrent-clients", json={"label": "qbt", "adapter_type": "qbittorrent", "base_url": "http://qbt"}
    ).json()["id"]

    associate = client.post(f"/api/torrent-clients/{tc_id}/disks/{disk_id}")
    assert associate.status_code == 204
    disks = client.get("/api/torrent-clients").json()[0]["disks"]
    assert disks == [{"disk_id": disk_id, "torrent_client_root_path": None, "local_rel_path": None}]

    # idempotente: associare due volte non deve fallire né duplicare
    again = client.post(f"/api/torrent-clients/{tc_id}/disks/{disk_id}")
    assert again.status_code == 204
    disks = client.get("/api/torrent-clients").json()[0]["disks"]
    assert disks == [{"disk_id": disk_id, "torrent_client_root_path": None, "local_rel_path": None}]

    dissociate = client.delete(f"/api/torrent-clients/{tc_id}/disks/{disk_id}")
    assert dissociate.status_code == 204
    assert client.get("/api/torrent-clients").json()[0]["disks"] == []


def test_associate_disk_with_root_path_override(client):
    # Il motivo per cui l'override vive sulla coppia (disk, client) e non sul
    # disco: due client diversi sullo stesso disco possono vederlo montato
    # a path diversi nei rispettivi container.
    (client.scan_root / "disk1").mkdir()
    disk_id = client.post(
        "/api/disks", json={"label": "Disk 1", "root_path": str(client.scan_root / "disk1")}
    ).json()["id"]
    tc_a = client.post(
        "/api/torrent-clients", json={"label": "qbt-a", "adapter_type": "qbittorrent", "base_url": "http://a"}
    ).json()["id"]
    tc_b = client.post(
        "/api/torrent-clients", json={"label": "qbt-b", "adapter_type": "qbittorrent", "base_url": "http://b"}
    ).json()["id"]

    client.post(f"/api/torrent-clients/{tc_a}/disks/{disk_id}", json={"torrent_client_root_path": "/downloads-a"})
    client.post(f"/api/torrent-clients/{tc_b}/disks/{disk_id}", json={"torrent_client_root_path": "/downloads-b"})

    by_id = {tc["id"]: tc["disks"] for tc in client.get("/api/torrent-clients").json()}
    assert by_id[tc_a] == [{"disk_id": disk_id, "torrent_client_root_path": "/downloads-a", "local_rel_path": None}]
    assert by_id[tc_b] == [{"disk_id": disk_id, "torrent_client_root_path": "/downloads-b", "local_rel_path": None}]

    # re-associare aggiorna l'override invece di fallire
    client.post(f"/api/torrent-clients/{tc_a}/disks/{disk_id}", json={"torrent_client_root_path": "/downloads-a2"})
    updated = next(tc for tc in client.get("/api/torrent-clients").json() if tc["id"] == tc_a)
    assert updated["disks"] == [
        {"disk_id": disk_id, "torrent_client_root_path": "/downloads-a2", "local_rel_path": None}]


def test_a_client_seeing_only_a_subfolder_of_the_disk(client):
    root = client.scan_root / "data"
    (root / "qbittorrent").mkdir(parents=True)
    disk_id = client.post("/api/disks", json={"label": "main", "root_path": str(root)}).json()["id"]
    tc = client.post("/api/torrent-clients", json={"label": "qbit", "adapter_type": "qbittorrent",
                                                   "base_url": "http://qbit"}).json()["id"]

    response = client.post(f"/api/torrent-clients/{tc}/disks/{disk_id}",
                           json={"torrent_client_root_path": "/download", "local_rel_path": "/qbittorrent/"})

    assert response.status_code == 204
    assert client.get("/api/torrent-clients").json()[0]["disks"] == [
        {"disk_id": disk_id, "torrent_client_root_path": "/download", "local_rel_path": "qbittorrent"}]
    for body, code in (({"torrent_client_root_path": "/download", "local_rel_path": "missing"}, "folder_not_found"),
                       ({"torrent_client_root_path": "/download", "local_rel_path": "../x"}, "path_outside_scope"),
                       ({"local_rel_path": "qbittorrent"}, "client_mapping_needs_client_root")):
        refused = client.post(f"/api/torrent-clients/{tc}/disks/{disk_id}", json=body)
        assert refused.status_code == 400 and refused.json()["detail"]["code"] == code, body


def _transmission(client, monkeypatch, rename_partial_files):
    """Un client Transmission con la sua RPC finta (tests/test_torrent_client_transmission.py)."""
    import httpx

    from nazgarr.adapters.torrent_client.transmission import TransmissionAdapter
    from tests.test_torrent_client_transmission import _TransmissionMock

    tc_id = client.post("/api/torrent-clients", json={
        "label": "tr", "adapter_type": "transmission", "base_url": "http://tr:9091"}).json()["id"]
    mock = _TransmissionMock(rename_partial_files=rename_partial_files)
    monkeypatch.setattr(
        "nazgarr.integrations.adapter_factory.build_torrent_client_adapter",
        lambda tc: TransmissionAdapter(tc.base_url, None, None,
                                       http_client=httpx.Client(transport=httpx.MockTransport(mock.handler))))
    return tc_id


def test_transmission_renaming_partial_files_is_a_warning_on_its_card(client, monkeypatch):
    """Decisione dell'utente, 2026-10-19: con rename-partial-files l'hardlink
    di un reseed col recheck fallito restava "<nome>.part"."""
    tc_id = _transmission(client, monkeypatch, rename_partial_files=True)

    assert client.get(f"/api/torrent-clients/{tc_id}/warnings").json() == {
        "status": "ok", "warnings": ["rename_partial_files"], "error": None}
    assert client.post(f"/api/torrent-clients/{tc_id}/test").json()["warnings"] == ["rename_partial_files"]


def test_no_warning_when_transmission_keeps_the_names(client, monkeypatch):
    tc_id = _transmission(client, monkeypatch, rename_partial_files=False)

    assert client.get(f"/api/torrent-clients/{tc_id}/warnings").json()["warnings"] == []


def test_the_warnings_of_a_client_that_does_not_answer_are_an_error_not_a_warning(client):
    tc_id = client.post("/api/torrent-clients", json={
        "label": "tr", "adapter_type": "transmission", "base_url": "http://127.0.0.1:1"}).json()["id"]

    body = client.get(f"/api/torrent-clients/{tc_id}/warnings").json()

    assert body["status"] == "error" and body["warnings"] == [] and body["error"]
