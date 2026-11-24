"""Adapter qBittorrent — prima implementazione concreta, via libreria
qbittorrent-api.

NON validato contro un'istanza qBittorrent reale (nessuna disponibile in
fase di sviluppo) — solo contro un client mockato nei test. Da verificare
prima di un uso reale, in particolare:
- la mappatura stato nativo -> RecheckStatus (CHECKING_STATES/ERROR_STATES)
- il campo `tracker` su torrents_info(): popolato solo se un tracker è
  correntemente raggiungibile/funzionante, può essere vuoto anche con
  tracker validi configurati — trattato quindi come best-effort, mai come
  fonte affidabile al 100% (qui usato solo per popolare
  client_torrent.tracker_url a scopo informativo, non per logica di match)
- il timing del polling in base.wait_for_hash

torrents_add() non garantisce di ritornare l'info_hash su ogni versione
dell'API qBittorrent: per essere version-agnostic, l'hash viene ricavato
confrontando l'elenco dei torrent prima/dopo l'aggiunta (diff), mai
fidandosi del solo valore di ritorno di torrents_add() — stesso approccio
di ratio-guardian.

"qui" (gestore multi-istanza per qBittorrent, docs/SPEC.md sezione 15) NON
usa questo adapter: espone una propria API di aggregazione (auth via
X-API-Key, percorsi sotto /api/instances/{id}/...), diversa dalla WebUI API
nativa di qBittorrent che qbittorrent-api si aspetta — confermato contro il
suo swagger/OpenAPI reale, non solo dedotto. Vedi nazgarr/adapters/torrent_client/qui.py.
"""

import copy
import functools
import hmac
import logging
import secrets
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed

from nazgarr.adapters.torrent_client.base import (
    CHECKING_STATES,
    CONTENT_LAYOUTS,
    ERROR_STATES,
    ClientTorrentFileInfo,
    ClientTorrentInfo,
    TorrentAlreadyInClientError,
    TorrentClientAdapter,
    TorrentStatus,
    expected_hash,
    largest_file_only,
    local_torrent_bytes,
    number,
    require_recheck,
    reusable_files,
    seeders,
    wait_for_hash,
)
from nazgarr.core import net_guard

logger = logging.getLogger(__name__)

# Richieste dei file in parallelo durante l'indicizzazione: qBittorrent non
# ha un endpoint per i file di più torrent (torrents/files vuole un hash),
# e una dopo l'altra costavano un giro di rete ciascuna. qBittorrent
# risponde alla WebUI da un solo thread, quindi in rete locale si guadagna
# poco; si guadagna con un client remoto o dietro un proxy (audit
# 2026-10-18). Pochi, per non caricare il client.
FILE_WORKERS = 4

# Una sessione di qBittorrent per client, non un login per operazione
# (segnalato 2026-11-24: il log di qBittorrent pieno di "WebAPI login
# success", uno per ogni operazione di Nazgarr, e ogni 30 secondi uno per
# ogni upload in attesa del recheck). Si tengono solo i cookie della sessione,
# mai il client: ogni operazione ha ancora il suo client e la sua sessione di
# requests, e i thread non si contendono niente (qbittorrent-api rifà la
# sessione da sé, a un nuovo login o dopo un errore). Un cookie scaduto, o
# qBittorrent riavviato, dà 403: qbittorrent-api rifà il login e il cookie
# nuovo torna qui alla chiusura. Solo in memoria, mai su disco né nei log:
# vale quanto la password, che l'adapter tiene già.
_SESSIONS_MAX = 32  # indirizzi e credenziali diversi; i più vecchi escono
_sessions_lock = threading.Lock()
_sessions: dict[str, list] = {}
# La chiave è un HMAC con un segreto del processo: nemmeno l'impronta della
# password resta ricavabile dalla memoria.
_SESSION_KEY_SECRET = secrets.token_bytes(32)


def _session_key(base_url: str, username: str | None, password: str | None) -> str:
    """Cambia con indirizzo e credenziali: dopo una modifica del client la
    prima operazione fa il login con quelle nuove."""
    parts = "\0".join((base_url, username or "", password or ""))
    return hmac.new(_SESSION_KEY_SECRET, parts.encode(), "sha256").hexdigest()


def _saved_cookies(key: str) -> list:
    with _sessions_lock:
        return [copy.copy(cookie) for cookie in _sessions.get(key, ())]


def _save_cookies(key: str, cookies: list | None) -> None:
    """I cookie della sessione per l'operazione dopo, o None per scordarla
    (un login fallito: il prossimo ci riprova senza il cookie vecchio)."""
    with _sessions_lock:
        _sessions.pop(key, None)
        if cookies:
            _sessions[key] = [copy.copy(cookie) for cookie in cookies]
            while len(_sessions) > _SESSIONS_MAX:
                _sessions.pop(next(iter(_sessions)))


@functools.cache
def pinned_client_class():
    """qbittorrentapi.Client, ma ogni sessione di requests che costruisce
    (una nuova anche dopo che ha scelto lo schema, o dopo un errore) si
    connette solo agli indirizzi che net_guard controlla nel momento della
    connessione (net_guard.requests_adapter), come gli altri adapter con
    net_guard.http_client (revisione dell'audit 2026-10-18: restava la
    finestra fra il controllo dell'host e la risoluzione di requests). I
    tentativi e il pool dell'adapter di qbittorrent-api restano i suoi.

    Sotto un lock, e la sessione segnata solo dopo aver montato l'adapter:
    qbittorrent-api pubblica la sessione nuova (_http_session) prima di
    montarci il suo HTTPAdapter, e i thread che chiedono i file durante
    l'indicizzazione (FILE_WORKERS), dopo un errore che la rifà, potevano
    prenderla in quel momento e connettersi con l'adapter senza controlli
    (revisione dell'audit 2026-10-18)."""
    import qbittorrentapi

    class PinnedClient(qbittorrentapi.Client):
        def __init__(self, *args, **kwargs):
            self._pin_lock = threading.Lock()
            # I cookie della sessione tenuta (_sessions), messi in ogni
            # sessione di requests nuova: qbittorrent-api la rifà già alla
            # prima richiesta, dopo aver scelto lo schema, e un cookie messo
            # solo nella prima andava perso.
            self.resume_cookies: list = []
            super().__init__(*args, **kwargs)

        @property
        def _session(self):
            with self._pin_lock:
                session = self._http_session
                if session is None or not getattr(session, "_nazgarr_pinned", False):
                    session = super()._session
                    default = session.get_adapter("http://")
                    adapter = net_guard.requests_adapter(
                        max_retries=default.max_retries,
                        pool_connections=getattr(default, "_pool_connections", 10),
                        pool_maxsize=getattr(default, "_pool_maxsize", 10),
                        pool_block=getattr(default, "_pool_block", False),
                    )
                    session.mount("http://", adapter)
                    session.mount("https://", adapter)
                    default.close()
                    for cookie in self.resume_cookies:
                        session.cookies.set_cookie(copy.copy(cookie))
                    session._nazgarr_pinned = True
                return session

        def auth_log_in(self, *args, **kwargs):
            """Il login parte senza i cookie della sessione tenuta (scaduta,
            se si arriva qui), e quelli nuovi valgono per le sessioni che
            qbittorrent-api rifà dopo, a un errore di connessione."""
            self.resume_cookies = []
            super().auth_log_in(*args, **kwargs)
            session = self._http_session
            self.resume_cookies = [copy.copy(cookie) for cookie in session.cookies] if session is not None else []

    return PinnedClient


class QBittorrentAdapter(TorrentClientAdapter):
    can_add_stopped = True

    def __init__(
        self,
        base_url: str,
        username: str | None,
        password: str | None,
        client=None,
        poll_interval: float = 0.5,
        poll_timeout: float = 30.0,
    ):
        self.base_url = base_url
        self.username = username
        self.password = password
        self.poll_interval = poll_interval
        self.poll_timeout = poll_timeout
        # None con un client passato da fuori (i test): niente sessioni tenute.
        self._session_key: str | None = None
        if client is not None:
            self._client = client
        else:
            # L'indirizzo si ricontrolla qui, a ogni adapter costruito (uno per
            # operazione), non solo quando si salva: un DNS cambiato dopo il
            # salvataggio verso i metadati del cloud dà subito l'errore chiaro.
            # Un host senza schema (qbittorrent-api lo accetta) vale come http.
            net_guard.check_url(base_url if "://" in base_url else f"http://{base_url}")
            self._client = pinned_client_class()(host=base_url, username=username, password=password)
            self._session_key = _session_key(base_url, username, password)
            self._client.resume_cookies = _saved_cookies(self._session_key)

    def forget_session(self) -> None:
        """Per la prova del client: un login vero con le credenziali salvate,
        non la sessione di prima (una password cambiata solo in qBittorrent
        non risulterebbe finché quella vale)."""
        if self._session_key is not None:
            self._client.resume_cookies = []
            if self._client._http_session is not None:
                self._client._http_session.cookies.clear()

    def close(self) -> None:
        """Non esce dalla sessione: la tiene per l'operazione dopo (vedi
        _sessions). Prima ogni operazione faceva login e logout."""
        if self._session_key is None:
            return
        # Quelli ripresi, o quelli di un login riuscito; nessuno dopo un login
        # fallito, e allora si scorda anche la sessione. Tutti i cookie, non
        # solo SID: dalla 5.2 si chiama QBT_SID_<porta>, e il nome si può
        # cambiare.
        _save_cookies(self._session_key, self._client.resume_cookies)

    def add_torrent(
        self, torrent_file_or_url: str, save_path: str, force_recheck: bool = True,
        expected_info_hash: str | None = None, skip_check_verified: bool = False,
        category: str | None = None, tags: list[str] | None = None, content_layout: str = "Original",
        stopped: bool = False,
    ) -> str:
        require_recheck(force_recheck)

        before_hashes = self._hashes()
        # Un .torrent locale (un upload, o quello che l'esecutore ha appena
        # scaricato e controllato) si manda col suo contenuto: il client non
        # vede quel percorso, e con "urls" lo cercherebbe nel suo filesystem
        # (No such file or directory). Un URL resta un URL.
        content = local_torrent_bytes(torrent_file_or_url)
        source = {"torrent_files": content} if content is not None else {"urls": torrent_file_or_url}
        # torrents_add non dice l'hash su ogni versione dell'API, e ignora in
        # silenzio un duplicato: lo si aspetta per hash (quello del chiamante
        # o dei byte mandati), e il diff prima/dopo resta solo per un URL.
        expected = expected_hash(expected_info_hash, content)
        if expected and expected in before_hashes:
            raise TorrentAlreadyInClientError(
                f"Torrent {expected} is already in the client (possibly with another save path): nothing added"
            )
        self._client.torrents_add(
            **source,
            save_path=save_path,
            is_skip_checking=skip_check_verified,
            use_auto_torrent_management=False,
            content_layout=content_layout,
            # Fermo solo se lo chiede il chiamante (un reseed, che parte dopo
            # il recheck riuscito): il recheck di un torrent fermo qBittorrent
            # lo fa lo stesso, avviandolo per il controllo e fermandolo dopo.
            # Altrimenti avviato e senza condizione di arresto, qualunque
            # cosa dicano le preferenze ("non avviare automaticamente",
            # "fermati dopo il controllo dei file"): un upload deve seedare.
            # qbittorrent-api manda sia "stopped" (WebUI API 2.11+) sia
            # "paused" (qBittorrent 4).
            is_stopped=stopped,
            stop_condition="None",
            **({"category": category} if category else {}),
            **({"tags": ",".join(tags)} if tags else {}),
        )
        info_hash = wait_for_hash(self._hashes, before_hashes, expected, self.poll_interval,
                                  self.poll_timeout, "qBittorrent")
        if not skip_check_verified:
            self._client.torrents_recheck(torrent_hashes=info_hash)
        if not stopped:
            self.start(info_hash)  # una versione che ignora "stopped" all'aggiunta
        return info_hash

    def start(self, info_hash: str) -> None:
        self._client.torrents_start(torrent_hashes=info_hash)

    def stop(self, info_hash: str) -> None:
        # qbittorrent-api usa "pause" sulle WebUI API prima della 2.11 (qBittorrent 4).
        self._client.torrents_stop(torrent_hashes=info_hash)

    def content_layout(self) -> str:
        prefs = self._client.app_preferences()
        layout = prefs.get("torrent_content_layout")
        if layout in CONTENT_LAYOUTS:
            return layout
        # Prima della 4.3.2 c'era solo "crea sottocartella" sì/no.
        subfolder = prefs.get("create_subfolder_enabled")
        if subfolder is None:
            return "Original"
        return "Original" if subfolder else "NoSubfolder"

    def _hashes(self) -> set[str]:
        return {t.hash.lower() for t in self._client.torrents_info()}

    def list_categories(self) -> list[str]:
        return sorted(self._client.torrents_categories().keys(), key=str.lower)

    def recheck(self, info_hash: str) -> None:
        self._client.torrents_recheck(torrent_hashes=info_hash)

    def remove_torrent(self, info_hash: str, delete_files: bool) -> None:
        self._client.torrents_delete(delete_files=delete_files, torrent_hashes=info_hash)

    def get_torrent_status(self, info_hash: str) -> TorrentStatus:
        results = self._client.torrents_info(torrent_hashes=info_hash)
        if not results:
            raise ValueError(f"Torrent {info_hash} not found in the client")
        torrent = results[0]
        state = torrent.state

        if state in CHECKING_STATES:
            recheck_status = "pending"
        elif state in ERROR_STATES:
            recheck_status = "failed"
        elif torrent.progress >= 1.0:
            recheck_status = "ok"
        else:
            # Dopo un recheck, dati incompleti significa che il file
            # hardlinkato non corrisponde a quanto atteso dal torrent.
            recheck_status = "failed"

        return TorrentStatus(
            info_hash=torrent.hash, state=state, recheck_status=recheck_status, progress=torrent.progress,
            incomplete=state not in ERROR_STATES and state not in CHECKING_STATES and torrent.progress < 1.0,
            amount_left=getattr(torrent, "amount_left", None),
        )

    def get_torrent_info(self, info_hash: str) -> ClientTorrentInfo | None:
        results = self._client.torrents_info(torrent_hashes=info_hash)
        if not results:
            return None
        return self._info(results[0], self._files(results[0].hash))

    def count_torrents(self) -> int:
        return len(self._client.torrents_info())

    def list_torrents(self, on_progress: Callable[[int, int], None] | None = None,
                      known: dict[str, ClientTorrentInfo] | None = None) -> list[ClientTorrentInfo]:
        # L'elenco nel thread chiamante (anche il login, se serve), poi i
        # file FILE_WORKERS alla volta; ordine e avanzamento restano qui.
        # Solo per i torrent nuovi o cambiati: degli altri valgono i file
        # dell'ultima indicizzazione (known, reusable_files; audit
        # 2026-10-18, finding 35: una richiesta per ogni torrent a ogni run).
        torrents = list(self._client.torrents_info())
        total = len(torrents)
        files = [self._reusable(torrent, known) for torrent in torrents]
        todo = [i for i, torrent_files in enumerate(files) if torrent_files is None]
        reused = total - len(todo)
        if known is not None:
            logger.debug("qBittorrent: file di %d torrent dall'ultima indicizzazione, %d da chiedere", reused,
                         len(todo))

        def progress(done: int, _todo: int) -> None:
            if on_progress is not None:
                on_progress(reused + done, total)

        if on_progress is not None and reused:
            on_progress(reused, total)
        fetched = self._files_of([torrents[i] for i in todo], progress if on_progress is not None else None)
        for i, torrent_files in zip(todo, fetched, strict=True):
            files[i] = torrent_files
        return [self._info(torrent, torrent_files or [])
                for torrent, torrent_files in zip(torrents, files, strict=True)]

    @staticmethod
    def _reusable(torrent, known: dict[str, ClientTorrentInfo] | None) -> list[ClientTorrentFileInfo] | None:
        if not known:
            return None
        return reusable_files(
            known.get(torrent.hash), name=torrent.name, save_path=torrent.save_path, state=torrent.state,
            total_size=number(getattr(torrent, "total_size", None), int),
            content_path=getattr(torrent, "content_path", None) or None,
        )

    def list_largest_files(self, limit: int | None = None) -> list[ClientTorrentInfo]:
        # Nessun endpoint per i file di più torrent: le richieste restano una
        # per torrent, ma solo per quelli che la verifica guarda.
        torrents = list(self._client.torrents_info())[:limit]
        return [largest_file_only(self._info(torrent, torrent_files))
                for torrent, torrent_files in zip(torrents, self._files_of(torrents), strict=True)]

    def _files_of(self, torrents, on_progress: Callable[[int, int], None] | None = None,
                  ) -> list[list[ClientTorrentFileInfo]]:
        """I file di ogni torrent, FILE_WORKERS richieste alla volta, nell'ordine dei torrent."""
        files: list[list[ClientTorrentFileInfo] | None] = [None] * len(torrents)
        pool = ThreadPoolExecutor(max_workers=FILE_WORKERS)
        try:
            futures = {pool.submit(self._files, t.hash): i for i, t in enumerate(torrents)}
            for done, future in enumerate(as_completed(futures), start=1):
                files[futures[future]] = future.result()
                if on_progress is not None:
                    on_progress(done, len(torrents))
        finally:
            pool.shutdown(wait=True, cancel_futures=True)  # stop o errore: niente richieste rimaste in coda
        return [f or [] for f in files]

    def _files(self, info_hash: str) -> list[ClientTorrentFileInfo]:
        return [
            ClientTorrentFileInfo(path_in_torrent=f.name, size_bytes=f.size)
            for f in self._client.torrents_files(torrent_hash=info_hash)
        ]

    @staticmethod
    def _info(torrent, files: list[ClientTorrentFileInfo]) -> ClientTorrentInfo:
        return ClientTorrentInfo(
            info_hash=torrent.hash,
            name=torrent.name,
            save_path=torrent.save_path,
            state=torrent.state,
            category=getattr(torrent, "category", "") or None,
            tracker_url=getattr(torrent, "tracker", "") or None,
            files=files,
            ratio=number(getattr(torrent, "ratio", None), float),
            seeding_time_seconds=number(getattr(torrent, "seeding_time", None), int),
            added_on=number(getattr(torrent, "added_on", None), int),
            swarm_seeders=seeders(getattr(torrent, "num_complete", None)),
        )
