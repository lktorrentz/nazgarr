"""Nessuna istanza qBittorrent reale disponibile in fase di sviluppo (vedi
docstring di nazgarr/adapters/torrent_client/qbittorrent.py) — questi test
usano un client fittizio che imita la superficie di qbittorrent-api usata
dall'adapter, non una connessione reale."""

from dataclasses import dataclass

import pytest

from nazgarr.adapters.torrent_client.base import TorrentAddTimeoutError
from nazgarr.adapters.torrent_client.qbittorrent import QBittorrentAdapter
from nazgarr.torrents.metainfo import compute_info_hash


@dataclass
class FakeTorrent:
    hash: str
    name: str = "Movie.2024.mkv"
    save_path: str = "/torrents"
    state: str = "uploading"
    progress: float = 1.0
    category: str = ""
    tracker: str = ""
    total_size: int | None = None
    content_path: str | None = None


@dataclass
class FakeFile:
    name: str
    size: int


class FakeQbtClient:
    def __init__(self, torrents=None, files_by_hash=None):
        self._torrents = list(torrents or [])
        self._files_by_hash = files_by_hash or {}
        self.added_calls: list[dict] = []
        self.rechecked: list[str] = []
        self.started: list[str] = []

    def torrents_info(self, torrent_hashes=None):
        if torrent_hashes:
            return [t for t in self._torrents if t.hash == torrent_hashes]
        return list(self._torrents)

    def torrents_files(self, torrent_hash):
        return self._files_by_hash.get(torrent_hash, [])

    def torrents_add(self, save_path, is_skip_checking, use_auto_torrent_management, urls=None, torrent_files=None,
                     category=None, tags=None, content_layout=None, is_stopped=None, stop_condition=None):
        self.added_calls.append({"urls": urls, "torrent_files": torrent_files, "save_path": save_path,
                                 "is_skip_checking": is_skip_checking, "layout": content_layout,
                                 "stopped": is_stopped, "stop_condition": stop_condition,
                                 "category": category, "tags": tags, "auto": use_auto_torrent_management})
        # Come qBittorrent: l'hash di un .torrent mandato per contenuto è quello del suo info.
        self._torrents.append(FakeTorrent(hash=compute_info_hash(torrent_files) if torrent_files else "new-hash",
                                          save_path=save_path))

    def torrents_categories(self):
        return {"tv": {"savePath": ""}, "Movie": {"savePath": ""}, "anime": {"savePath": ""}}

    def torrents_recheck(self, torrent_hashes):
        self.rechecked.append(torrent_hashes)

    def torrents_start(self, torrent_hashes):
        self.started.append(torrent_hashes)


def _adapter(client):
    return QBittorrentAdapter(base_url="http://qbt", username="u", password="p", client=client, poll_interval=0.01)


def test_add_torrent_returns_new_hash_and_forces_recheck():
    client = FakeQbtClient(torrents=[FakeTorrent(hash="existing")])
    adapter = _adapter(client)

    info_hash = adapter.add_torrent("magnet:?xt=...", save_path="/torrents/movie")

    assert info_hash == "new-hash"
    assert client.added_calls[0]["is_skip_checking"] is False
    assert client.rechecked == ["new-hash"]


def test_a_local_torrent_file_is_sent_by_content_not_by_path(tmp_path):
    # Il .torrent di un upload sta nella cartella dati di Nazgarr, che il
    # client non vede: con urls= lo cercherebbe nel suo filesystem.
    torrent = tmp_path / "tracker-1-seed.torrent"
    torrent.write_bytes(b"d4:infod4:name1:xee")
    client = FakeQbtClient()

    _adapter(client).add_torrent(str(torrent), save_path="/downloads/movie")

    assert client.added_calls[0]["torrent_files"] == b"d4:infod4:name1:xee"
    assert client.added_calls[0]["urls"] is None


def test_add_torrent_rejects_force_recheck_false():
    adapter = _adapter(FakeQbtClient())
    with pytest.raises(ValueError, match="force_recheck=False"):
        adapter.add_torrent("magnet:?xt=...", save_path="/torrents/movie", force_recheck=False)


def test_add_torrent_times_out_if_no_new_hash_appears():
    class StuckClient(FakeQbtClient):
        def torrents_add(self, **kwargs):
            pass  # non aggiunge mai nulla: simula un client che non reagisce

    adapter = QBittorrentAdapter(
        base_url="http://qbt", username="u", password="p", client=StuckClient(), poll_interval=0.01, poll_timeout=0.05
    )
    with pytest.raises(TorrentAddTimeoutError):
        adapter.add_torrent("magnet:?xt=...", save_path="/torrents/movie")


@pytest.mark.parametrize(
    ("state", "progress", "expected"),
    [
        ("checkingResumeData", 0.5, "pending"),
        ("error", 0.0, "failed"),
        ("missingFiles", 0.0, "failed"),
        ("uploading", 1.0, "ok"),
        ("stalledDL", 0.4, "failed"),  # progress < 1.0 dopo un presunto recheck = dati incompleti
    ],
)
def test_get_torrent_status_maps_native_state(state, progress, expected):
    client = FakeQbtClient(torrents=[FakeTorrent(hash="h1", state=state, progress=progress)])
    adapter = _adapter(client)

    status = adapter.get_torrent_status("h1")

    assert status.recheck_status == expected


def test_get_torrent_status_raises_if_not_found():
    adapter = _adapter(FakeQbtClient())
    with pytest.raises(ValueError, match="missing"):
        adapter.get_torrent_status("missing")


def test_list_torrents_includes_files_and_tracker():
    client = FakeQbtClient(
        torrents=[FakeTorrent(hash="h1", name="Movie.2024.mkv", save_path="/torrents/movie", category="movies", tracker="https://tracker.example/announce")],
        files_by_hash={"h1": [FakeFile(name="Movie.2024.mkv", size=123), FakeFile(name="Movie.2024.nfo", size=10)]},
    )
    adapter = _adapter(client)

    torrents = adapter.list_torrents()

    assert len(torrents) == 1
    t = torrents[0]
    assert t.info_hash == "h1"
    assert t.category == "movies"
    assert t.tracker_url == "https://tracker.example/announce"
    assert [f.path_in_torrent for f in t.files] == ["Movie.2024.mkv", "Movie.2024.nfo"]


def test_list_torrents_treats_empty_category_and_tracker_as_none():
    client = FakeQbtClient(torrents=[FakeTorrent(hash="h1")], files_by_hash={"h1": []})
    adapter = _adapter(client)

    t = adapter.list_torrents()[0]

    assert t.category is None
    assert t.tracker_url is None


def test_a_torrent_verified_100_percent_by_nazgarr_skips_the_client_recheck_only_on_request():
    client = FakeQbtClient(torrents=[FakeTorrent(hash="existing")])

    _adapter(client).add_torrent("magnet:?xt=...", save_path="/torrents/movie", skip_check_verified=True)

    assert client.added_calls[0]["is_skip_checking"] is True
    assert client.rechecked == []


def test_category_and_tags_are_only_labels():
    client = FakeQbtClient()
    adapter = _adapter(client)

    adapter.add_torrent("magnet:?xt=...", save_path="/torrents/movie", category="movie", tags=["release", "nzg"])

    call = client.added_calls[0]
    # La gestione automatica resta spenta: la categoria non sposta i file.
    assert (call["category"], call["tags"], call["auto"]) == ("movie", "release,nzg", False)
    assert adapter.list_categories() == ["anime", "Movie", "tv"]


def test_the_content_layout_is_always_explicit():
    # "Original" anche se nessuno lo chiede: la preferenza globale del
    # client non deve spostare i file da dove li ha messi Nazgarr.
    plain, stripped = FakeQbtClient(), FakeQbtClient()
    _adapter(plain).add_torrent("magnet:?xt=...", save_path="/torrents/movie")
    _adapter(stripped).add_torrent("magnet:?xt=...", save_path="/torrents", content_layout="NoSubfolder")
    assert (plain.added_calls[0]["layout"], stripped.added_calls[0]["layout"]) == ("Original", "NoSubfolder")


@pytest.mark.parametrize(("prefs", "expected"), [
    ({"torrent_content_layout": "Subfolder"}, "Subfolder"),
    ({"torrent_content_layout": "NoSubfolder"}, "NoSubfolder"),
    ({"torrent_content_layout": "Original"}, "Original"),
    ({"create_subfolder_enabled": False}, "NoSubfolder"),  # prima della 4.3.2
    ({"create_subfolder_enabled": True}, "Original"),
    ({}, "Original"),
])
def test_content_layout_reads_the_client_preference(prefs, expected):
    client = FakeQbtClient()
    client.app_preferences = lambda: prefs
    assert _adapter(client).content_layout() == expected


def test_a_torrent_is_added_running_whatever_the_client_preferences():
    # "Non avviare automaticamente" o "fermati dopo il controllo dei file"
    # nelle preferenze lasciavano fermi i reseed dopo il recheck.
    client = FakeQbtClient()
    _adapter(client).add_torrent("magnet:?xt=...", save_path="/torrents/movie")
    assert (client.added_calls[0]["stopped"], client.added_calls[0]["stop_condition"]) == (False, "None")
    assert client.started == ["new-hash"]


@pytest.mark.parametrize("delete_files", [False, True])
def test_remove_torrent_deletes_files_only_on_request(delete_files):
    class DeletingClient(FakeQbtClient):
        def torrents_delete(self, delete_files, torrent_hashes):
            self.deleted = (torrent_hashes, delete_files)

    client = DeletingClient(torrents=[FakeTorrent(hash="h1")])

    _adapter(client).remove_torrent("h1", delete_files=delete_files)

    assert client.deleted == ("h1", delete_files)


class FakeQbtServer:
    """qBittorrent sotto requests: qbittorrent-api vero sopra, con la scelta
    dello schema, i 403 e i nuovi login. Conta i login, come il log di
    qBittorrent ("WebAPI login success")."""

    def __init__(self, password="p", cookie_name="SID"):
        self.password = password
        self.cookie_name = cookie_name
        self.sids: set[str] = set()
        self.logins = 0
        self.login_cookies: list[str] = []  # i cookie che ogni login si porta dietro

    def restart(self):
        self.sids.clear()

    def handle(self, request):
        import email.message
        from types import SimpleNamespace
        from urllib.parse import parse_qs, urlsplit

        import requests

        path = urlsplit(request.url).path
        cookie = request.headers.get("Cookie", "")
        sent = dict(part.split("=", 1) for part in cookie.split("; ") if "=" in part)
        status, body, set_cookie = 200, "", None
        if path.endswith("/auth/login"):
            form = parse_qs(request.body or "")
            self.login_cookies.append(cookie)
            if form.get("password") == [self.password]:
                self.logins += 1
                sid = f"sid{len(self.sids) + self.logins}"
                self.sids.add(sid)
                body, set_cookie = "Ok.", f"{self.cookie_name}={sid}; HttpOnly; path=/; SameSite=Strict"
            else:
                body = "Fails."
        elif request.method != "HEAD" and sent.get(self.cookie_name) not in self.sids:
            status, body = 403, "Forbidden"
        elif path.endswith("/torrents/info"):
            body = '[{"hash": "h1", "name": "Movie.mkv"}]'

        response = requests.Response()
        response.status_code, response._content, response.url, response.request = (
            status, body.encode(), request.url, request)
        response.headers["Content-Type"] = "application/json" if body.startswith("[") else "text/plain"
        headers = email.message.Message()
        if set_cookie:
            headers["Set-Cookie"] = set_cookie
        response.raw = SimpleNamespace(_original_response=SimpleNamespace(msg=headers))
        return response


@pytest.fixture
def qbt_server(monkeypatch):
    import requests

    from nazgarr.adapters.torrent_client import qbittorrent
    from nazgarr.core import net_guard

    server = FakeQbtServer()

    class Transport(requests.adapters.BaseAdapter):
        def send(self, request, **kwargs):
            return server.handle(request)

        def close(self):
            pass

    monkeypatch.setattr(net_guard, "check_url", lambda url: None)
    monkeypatch.setattr(net_guard, "requests_adapter", lambda **kwargs: Transport())
    monkeypatch.setattr(qbittorrent, "_sessions", {})
    return server


def _count(password="p"):
    with QBittorrentAdapter("http://nas.example:8080", "u", password) as adapter:
        return adapter.count_torrents()


def test_one_login_per_client_not_per_operation(qbt_server):
    """Segnalato 2026-11-24: il log di qBittorrent pieno di "WebAPI login
    success", uno per ogni operazione di Nazgarr. La sessione si tiene: le
    operazioni dopo la prima non rifanno il login."""
    assert [_count() for _ in range(3)] == [1, 1, 1]
    assert qbt_server.logins == 1


def test_an_expired_session_logs_in_again_by_itself_without_the_old_cookie(qbt_server):
    _count()
    qbt_server.restart()  # qBittorrent riavviato, o la sessione scaduta
    assert _count() == 1
    assert _count() == 1
    assert qbt_server.logins == 2
    assert qbt_server.login_cookies == ["", ""]  # il login parte pulito, mai col cookie scaduto


def test_new_credentials_log_in_with_them(qbt_server):
    _count()
    qbt_server.password = "new"
    qbt_server.restart()
    assert _count("new") == 1
    assert _count("new") == 1
    assert qbt_server.logins == 2


def test_a_failed_login_forgets_the_session(qbt_server):
    import qbittorrentapi

    from nazgarr.adapters.torrent_client import qbittorrent

    _count()
    qbt_server.password = "changed in qBittorrent"
    qbt_server.restart()
    with pytest.raises(qbittorrentapi.LoginFailed):
        _count()
    assert qbittorrent._sessions == {}


def test_the_client_test_logs_in_for_real(qbt_server):
    """La prova del client è delle credenziali salvate: una password cambiata
    solo in qBittorrent deve risultare subito, anche con la sessione viva."""
    import qbittorrentapi

    _count()
    with QBittorrentAdapter("http://nas.example:8080", "u", "p") as adapter:
        adapter.forget_session()
        assert adapter.count_torrents() == 1
    assert qbt_server.logins == 2
    assert _count() == 1 and qbt_server.logins == 2  # la sessione nuova resta per le altre

    qbt_server.password = "changed in qBittorrent"
    with QBittorrentAdapter("http://nas.example:8080", "u", "p") as adapter:
        adapter.forget_session()
        with pytest.raises(qbittorrentapi.LoginFailed):
            adapter.count_torrents()


def test_the_cookie_name_of_newer_qbittorrent_is_kept_too(qbt_server):
    qbt_server.cookie_name = "QBT_SID_8080"  # dalla 5.2
    assert [_count() for _ in range(2)] == [1, 1]
    assert qbt_server.logins == 1


def test_an_adapter_never_used_keeps_the_session(qbt_server):
    _count()
    QBittorrentAdapter("http://nas.example:8080", "u", "p").close()
    assert _count() == 1 and qbt_server.logins == 1


def test_the_factory_closes_the_adapter_even_when_the_operation_fails(monkeypatch):
    import pytest

    from nazgarr.integrations import adapter_factory

    closed = []

    class Fake:
        def close(self):
            closed.append(True)

    monkeypatch.setattr(adapter_factory, "build_torrent_client_adapter", lambda row: Fake())
    with pytest.raises(RuntimeError), adapter_factory.torrent_client(object()):
        raise RuntimeError("client irraggiungibile")
    with adapter_factory.torrent_client(object()):
        pass
    assert closed == [True, True]


def test_stop_stops_the_torrent():
    class StoppingClient(FakeQbtClient):
        def __init__(self):
            super().__init__()
            self.stopped = []

        def torrents_stop(self, torrent_hashes):
            self.stopped.append(torrent_hashes)

    client = StoppingClient()
    _adapter(client).stop("h1")
    assert client.stopped == ["h1"]


def test_a_torrent_added_meanwhile_by_another_tool_is_never_taken_for_ours(tmp_path):
    """Senza un hash atteso l'adapter prendeva "il primo torrent nuovo": uno
    aggiunto nello stesso momento da Sonarr o cross-seed passava per il
    nostro, e recheck e avvio finivano su quello (audit 2026-10-18). Con un
    .torrent locale l'hash si ricava dai byte mandati."""
    torrent = tmp_path / "upload.torrent"
    torrent.write_bytes(b"d4:infod4:name1:xee")
    ours = compute_info_hash(torrent.read_bytes())

    class BusyClient(FakeQbtClient):
        """Al primo controllo dopo l'aggiunta il solo torrent nuovo è quello
        dell'altro strumento; il nostro compare dal secondo. Così il "primo
        torrent nuovo" di prima sbaglia sempre, non a seconda dell'ordine
        di un set."""

        def __init__(self):
            super().__init__()
            self.pending = None
            self.polls = 0

        def torrents_add(self, **kwargs):
            self._torrents.append(FakeTorrent(hash="other-tool"))
            super().torrents_add(**kwargs)
            self.pending = self._torrents.pop()

        def torrents_info(self, torrent_hashes=None):
            if self.pending is not None:
                self.polls += 1
                if self.polls == 2:
                    self._torrents.append(self.pending)
                    self.pending = None
            return super().torrents_info(torrent_hashes)

    client = BusyClient()
    info_hash = _adapter(client).add_torrent(str(torrent), save_path="/downloads/movie")

    assert info_hash == ours and client.polls == 2  # aspettato il nostro, non preso l'altro
    assert client.rechecked == [ours] and client.started == [ours]


def test_a_local_torrent_already_in_the_client_is_a_duplicate_not_a_timeout(tmp_path):
    from nazgarr.adapters.torrent_client.base import TorrentAlreadyInClientError

    torrent = tmp_path / "upload.torrent"
    torrent.write_bytes(b"d4:infod4:name1:xee")
    client = FakeQbtClient(torrents=[FakeTorrent(hash=compute_info_hash(torrent.read_bytes()).upper())])

    with pytest.raises(TorrentAlreadyInClientError):
        _adapter(client).add_torrent(str(torrent), save_path="/downloads/movie")
    assert client.added_calls == []


def test_the_index_keeps_the_client_order_and_reports_progress():
    torrents = [FakeTorrent(hash=f"h{i}", name=f"M{i}.mkv") for i in range(10)]
    client = FakeQbtClient(torrents=torrents,
                           files_by_hash={t.hash: [FakeFile(name=t.name, size=i)] for i, t in enumerate(torrents)})
    progress = []

    listed = _adapter(client).list_torrents(on_progress=lambda done, total: progress.append((done, total)))

    assert [t.info_hash for t in listed] == [t.hash for t in torrents]
    assert [t.files[0].size_bytes for t in listed] == list(range(10))
    assert progress == [(i, 10) for i in range(1, 11)]


def test_the_credentials_test_counts_torrents_without_their_files():
    class NoFiles(FakeQbtClient):
        def torrents_files(self, torrent_hash):
            raise AssertionError("file non richiesti per contare")

    assert _adapter(NoFiles(torrents=[FakeTorrent(hash="a"), FakeTorrent(hash="b")])).count_torrents() == 2


def test_a_reseed_is_added_stopped_and_rechecked_never_started():
    """Decisione dell'utente, 2026-10-18: un reseed entra fermo e parte solo
    a recheck riuscito (nazgarr/reseed/executor.py::reconcile_seed_job); il
    recheck c'è lo stesso, qBittorrent lo fa anche su un torrent fermo."""
    client = FakeQbtClient()
    adapter = _adapter(client)

    info_hash = adapter.add_torrent("magnet:?xt=...", save_path="/torrents/movie", stopped=True)

    assert QBittorrentAdapter.can_add_stopped is True
    assert client.added_calls[0]["stopped"] is True and client.rechecked == [info_hash]
    assert client.started == []
    adapter.start(info_hash)
    assert client.started == [info_hash]


def test_the_live_path_check_asks_the_files_of_only_the_torrents_it_looks_at():
    """Audit 2026-10-18: la verifica dei percorsi chiedeva i file di ogni
    torrent (list_torrents) e ne guardava i primi 5000."""
    asked = []

    class Counting(FakeQbtClient):
        def torrents_files(self, torrent_hash):
            asked.append(torrent_hash)
            return super().torrents_files(torrent_hash)

    client = Counting(torrents=[FakeTorrent(hash=f"h{i}") for i in range(5)],
                      files_by_hash={"h0": [FakeFile("Movie/a.nfo", 10), FakeFile("Movie/a.mkv", 900)]})

    largest = _adapter(client).list_largest_files(limit=2)

    assert sorted(asked) == ["h0", "h1"]
    assert [(t.info_hash, [(f.path_in_torrent, f.size_bytes) for f in t.files]) for t in largest] == [
        ("h0", [("Movie/a.mkv", 900)]), ("h1", [])]


class CountingQbtClient(FakeQbtClient):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.files_asked: list[str] = []

    def torrents_files(self, torrent_hash):
        self.files_asked.append(torrent_hash)
        return super().torrents_files(torrent_hash)


def test_the_index_asks_the_files_only_of_new_or_changed_torrents():
    """Audit 2026-10-18, finding 35 (residuo): una richiesta dei file per
    ogni torrent a ogni run, anche per quelli che non cambiano mai."""
    from nazgarr.adapters.torrent_client.base import ClientTorrentFileInfo, ClientTorrentInfo

    def known_of(info_hash, name="M.mkv", save_path="/torrents", state="stalledUP", files=(("M.mkv", 10),)):
        return ClientTorrentInfo(info_hash=info_hash, name=name, save_path=save_path, state=state,
                                 files=[ClientTorrentFileInfo(path, size) for path, size in files])

    torrents = [
        FakeTorrent(hash="same", name="M.mkv", state="uploading", total_size=10, content_path="/torrents/M.mkv"),
        FakeTorrent(hash="new", name="M.mkv", state="uploading", total_size=10),
        FakeTorrent(hash="renamed", name="M2.mkv", state="uploading", total_size=10),
        FakeTorrent(hash="moved", name="M.mkv", save_path="/other", state="uploading", total_size=10),
        FakeTorrent(hash="downloading", name="M.mkv", state="downloading", progress=0.5, total_size=10),
        FakeTorrent(hash="was_incomplete", name="M.mkv", state="uploading", total_size=10),
        FakeTorrent(hash="resized", name="M.mkv", state="uploading", total_size=11),
        FakeTorrent(hash="file_renamed", name="M.mkv", state="uploading", total_size=10,
                    content_path="/torrents/Other.mkv"),
        FakeTorrent(hash="pack", name="Pack", state="pausedUP", total_size=30, content_path="/torrents/Pack"),
    ]
    known = {
        "same": known_of("same"), "renamed": known_of("renamed"), "moved": known_of("moved"),
        "downloading": known_of("downloading", state="downloading"),
        "was_incomplete": known_of("was_incomplete", state="stoppedDL", files=(("M.mkv.!qB", 10),)),
        "resized": known_of("resized"), "file_renamed": known_of("file_renamed"),
        "pack": known_of("pack", name="Pack", state="stoppedUP", files=(("Pack/E01.mkv", 10), ("Pack/E02.mkv", 20))),
    }
    client = CountingQbtClient(torrents=torrents,
                               files_by_hash={t.hash: [FakeFile(name="fresh.mkv", size=1)] for t in torrents})
    progress = []

    listed = _adapter(client).list_torrents(on_progress=lambda done, total: progress.append((done, total)),
                                            known=known)

    assert sorted(client.files_asked) == sorted(
        ["new", "renamed", "moved", "downloading", "was_incomplete", "resized", "file_renamed"])
    by_hash = {t.info_hash: t for t in listed}
    assert [f.path_in_torrent for f in by_hash["same"].files] == ["M.mkv"]
    assert [f.path_in_torrent for f in by_hash["pack"].files] == ["Pack/E01.mkv", "Pack/E02.mkv"]
    assert [f.path_in_torrent for f in by_hash["new"].files] == ["fresh.mkv"]
    assert [t.info_hash for t in listed] == [t.hash for t in torrents]  # l'ordine del client
    assert progress[0] == (2, 9) and progress[-1] == (9, 9)


def test_a_plugin_with_its_own_state_names_can_reuse_the_file_lists():
    """reusable_files è nell'SDK (1.7), ma "completo" voleva gli stati di
    qBittorrent: un plugin che riporta "seeding" non riusava mai niente
    (revisione dell'audit 2026-10-18)."""
    from nazgarr import sdk

    known = sdk.ClientTorrentInfo(info_hash="h", name="M.mkv", save_path="/t", state="seeding",
                                  files=[sdk.ClientTorrentFileInfo("M.mkv", 10)])
    now = {"name": "M.mkv", "save_path": "/t", "state": "seeding", "total_size": 10}

    assert sdk.reusable_files(known, **now) is None  # di default, gli stati di qBittorrent
    assert sdk.reusable_files(known, **now, complete=lambda state: state == "seeding") == known.files
    assert sdk.reusable_files(known, **{**now, "state": "downloading"},
                              complete=lambda state: state == "seeding") is None


def test_without_known_files_every_torrent_is_asked():
    torrents = [FakeTorrent(hash="h1", total_size=10)]
    client = CountingQbtClient(torrents=torrents, files_by_hash={"h1": [FakeFile(name="M.mkv", size=10)]})

    _adapter(client).list_torrents()

    assert client.files_asked == ["h1"]
