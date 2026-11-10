"""Contratto per gli adapter tracker (docs/SPEC.md sezione 6, ereditato da
ratio-guardian §7). Limite noto: non esiste API pubblica documentata per
lo storico personale su UNIT3D — get_own_history() è opzionale e può non
essere supportato, il chiamante deve degradare pulito al motore generale.

Unit3dTrackerAdapter è porting diretto da ratio-guardian (shape della
risposta già verificata contro un'istanza reale, ITT) — nessun dettaglio
API riscoperto qui."""

import hashlib
import json
import logging
import re
import threading
import time
from abc import ABC, abstractmethod
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Literal
from urllib.parse import parse_qs, urljoin, urlsplit

import httpx

from nazgarr.adapters.tracker import unit3d_web
from nazgarr.core import http_limits, net_guard
from nazgarr.library import mediainfo_text

logger = logging.getLogger(__name__)


def _count(value) -> int | None:
    """Un intero non negativo dalla risposta del tracker, o None."""
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def host_of_url(url: str | None) -> str | None:
    host = urlsplit(url).netloc.lower() if url else ""
    return host.removeprefix("www.") or None


class NotSupportedError(Exception):
    """Sollevata quando l'adapter non supporta una funzionalità (es. storico
    personale non disponibile via API per questo tracker/istanza)."""


class UploadError(Exception):
    """Upload fallito lato tracker (API ha risposto ma con esito negativo,
    o risposta in un formato inatteso) — mai silenziato: la conferma umana
    obbligatoria (docs/SPEC.md §9) resta l'ultimo passo prima di questa
    chiamata, ma se il tracker stesso rifiuta va segnalato esplicitamente."""


class TrackerRateLimitedError(UploadError):
    """Il tracker continua a rispondere 429 anche dopo i retry con attesa.
    Il matching smette di interrogare QUEL tracker per il resto della run
    (continuare servirebbe solo a peggiorare il blocco), mai un errore del
    singolo file. Sottoclasse di UploadError così il flusso di upload, che
    già intercetta UploadError come un 502 pulito, resta invariato."""


class _BodyTooLarge(UploadError):
    """Una risposta oltre il tetto (o la scadenza) di Unit3dTrackerAdapter._get:
    i chiamanti la riscrivono col loro messaggio."""


@dataclass
class UploadFields:
    """Campi risolti per una singola richiesta di upload (docs/SPEC.md §9
    punto 9). Shape verificata contro il codice tracker reale e funzionante
    di Upload-Assistant (src/trackers/UNIT3D.py get_data(), riferimento di
    dominio, nessun codice riusato) — sottoinsieme rilevante per questo
    progetto: i campi UNIT3D non gestiti qui (bdinfo, igdb, region_id,
    distributor_id, featured/doubleup/sticky) sono sempre
    inviati con il default "non impostato"/"0" dell'API, mai omessi (un
    campo mancante non è equivalente al suo default esplicito per ogni
    installazione UNIT3D)."""

    name: str
    description: str
    mediainfo: str
    category_id: int
    type_id: int
    resolution_id: int
    tmdb_id: int
    imdb_id: str = "0"
    tvdb_id: int = 0
    mal_id: int = 0
    season_number: int | None = None
    episode_number: int | None = None
    anonymous: bool = False
    personal_release: bool = False
    internal: bool = False  # solo per chi ha i permessi sul tracker: altrimenti lo rifiuta
    free: int = 0  # freeleech in percentuale (0 = nessuno), se il tracker lo concede a chi carica
    stream: bool = False
    sd: bool = False
    keywords: str = ""


@dataclass
class TorrentCandidate:
    torrent_id_remote: str
    info_hash: str | None
    name: str
    size_bytes: int
    file_list: list[str] | None  # None se il tracker non espone la struttura file
    mediainfo_unique_id: str | None
    folder: str | None = None  # sottocartella del pack (UNIT3D "folder"), None per file singolo
    download_link: str | None = None  # URL autenticato al .torrent, necessario per aggiungerlo al client
    file_sizes: dict[str, int] | None = None  # nome file -> dimensione in byte, se il tracker lo espone
    mediainfo_unique_ids_by_filename: dict[str, str] | None = None  # nome file (basename) -> Unique ID,
    # utile per un futuro motore di matching season-pack-aware (fuori scope in questa fase,
    # vedi nazgarr/reseed/matching.py) — il blob media_info per un pack è la concatenazione dei report
    # per-file, mediainfo_unique_id da solo cattura solo il primo.
    # Per il dupe check di un upload (docs/SPEC.md §9 "Dupe check"); None se il tracker non lo dice.
    # category_id: film e serie hanno numerazioni TMDB diverse, la ricerca per tmdbId le mescola.
    category_id: int | None = None
    type_id: int | None = None
    resolution: str | None = None  # come la scrive il tracker, es. "1080p"
    seeders: int | None = None
    # Il MediaInfo del primo file, letto dal testo del tracker (nazgarr/library/mediainfo_text.py).
    media_info_summary: dict | None = None


@dataclass
class TorrentRecord:
    """Voce dello storico personale (download o upload)."""
    torrent_id_remote: str
    info_hash: str | None
    name: str
    size_bytes: int
    tmdb_id: int | None
    source: Literal["uploaded", "downloaded"]
    file_list: list[str] | None


@dataclass(frozen=True)
class TrackerWebResult:
    """Esito di un'azione sul sito del tracker (non sulla sua API) con la
    sessione web dell'utente: check_web_session e claim_resurrection.

    status:
    - "ok": la sessione vale (check_web_session);
    - "claimed": il tracker ha registrato il claim (claim_resurrection);
    - "not_offered": la pagina del torrent non offre il claim (ha seeder,
      è troppo recente, un claim è già in attesa, anche il tuo);
    - "refused": il tracker ha rifiutato il claim inviato (un tuo upload);
    - "no_session": nessun cookie di sessione salvato;
    - "session_expired": il tracker rimanda al login;
    - "failed": tutto il resto (rete, 419, una protezione del sito), con detail.
    """

    status: Literal["ok", "claimed", "not_offered", "refused", "no_session", "session_expired", "failed"]
    detail: str | None = None


class TrackerAdapter(ABC):
    """Un'istanza per ogni tracker configurato (vedi tabella `tracker`)."""

    def close(self) -> None:
        """Chiude le connessioni dell'adapter. Chi lo costruisce lo chiude
        dopo l'uso (`with adapter_factory.build_...(...) as adapter:`): ogni
        operazione ha il suo adapter, e senza le connessioni restavano aperte
        fino al garbage collector."""
        close = getattr(getattr(self, "_client", None), "close", None)
        if callable(close):
            close()

    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    @abstractmethod
    def search_by_tmdb(self, tmdb_id: int) -> list[TorrentCandidate]:
        """Ricerca sul catalogo pubblico del tracker. Sempre richiesto:
        è il fallback generale usato anche per il cross-seed di file mai
        scaricati con questo account. Tutti i risultati, non la prima
        pagina; se il tracker ne ha più di quanti l'adapter ne legge, una
        CatalogResults con truncated=True (vedi catalog_truncated)."""
        raise NotImplementedError

    def get_own_history(self) -> list[TorrentRecord]:
        """Storico personale (upload + download), se il tracker/adapter lo
        supporta. Solleva NotSupportedError se non disponibile: il chiamante
        deve gestire questo caso degradando al motore di matching generale,
        mai trattarlo come errore fatale."""
        raise NotSupportedError(f"{self.__class__.__name__} non supporta get_own_history()")

    def download_torrent(self, url: str) -> bytes:
        """Scarica un .torrent (download_link di un candidato) passando dallo
        stesso rate limiting delle altre chiamate a questo tracker — mai con
        un client HTTP a parte, altrimenti la verifica piece-hash di molti
        candidati ambigui sfora il limite del tracker senza che il limiter
        lo sappia. NotSupportedError se l'adapter non lo implementa."""
        raise NotSupportedError(f"{self.__class__.__name__} non supporta download_torrent()")

    def upload_torrent(self, fields: UploadFields, torrent_path: str) -> "UploadedTorrent":
        """Pubblica un nuovo upload (docs/SPEC.md §9): l'id sul tracker e il
        link per riscaricarne il .torrent, che è quello da seedare (il
        tracker può averlo riscritto: vedi UploadedTorrent). NotSupportedError
        se l'adapter non lo implementa, UploadError se il tracker rifiuta la
        richiesta."""
        raise NotSupportedError(f"{self.__class__.__name__} non supporta upload_torrent()")

    def check_web_session(self) -> TrackerWebResult:
        """Se la sessione web salvata vale ancora (una pagina del sito, in
        sola lettura). NotSupportedError se l'adapter non usa il sito."""
        raise NotSupportedError(f"{self.__class__.__name__} non supporta check_web_session()")

    def claim_resurrection(self, torrent_id_remote: str) -> TrackerWebResult:
        """Il claim della resurrection di un torrent morto, con la sessione
        web dell'utente, prima che il client lo annunci (decisione
        dell'utente, 2026-11-10: nazgarr/upload/execute.py::run_reseed).
        NotSupportedError se il tracker non ha il claim o l'adapter non lo sa
        fare."""
        raise NotSupportedError(f"{self.__class__.__name__} non supporta claim_resurrection()")


class CatalogResults(list):
    """I risultati di search_by_tmdb, con `truncated` se il tracker ne
    aveva altri oltre il limite di pagine dell'adapter: il controllo dei
    dupe di un upload deve poterlo dire invece di sembrare completo. Una
    lista normale per chi non lo guarda."""

    truncated: bool = False


def catalog_truncated(results) -> bool:
    """Se una ricerca del catalogo è incompleta (un plugin che restituisce
    una lista normale non lo dice mai)."""
    return bool(getattr(results, "truncated", False))


@dataclass(frozen=True)
class UploadedTorrent:
    """Esito di upload_torrent. Un tracker UNIT3D riscrive il dizionario info
    del .torrent che riceve (campo source col suo valore, private): l'info
    hash sul tracker non è quello del file inviato, e un client che seeda il
    file inviato riceve "InfoHash not found". Si seeda quindi il .torrent
    riscaricato da download_link."""

    torrent_id_remote: str
    download_link: str | None


class _RateLimiter:
    """Sliding window: al massimo `max_per_min` richieste in ogni finestra di
    60s, bloccante (time.sleep) oltre soglia. Thread-safe: ogni chiamata si
    prenota il suo istante sotto lock e aspetta fuori, così più thread (run
    pianificata, upload, controllo completo) non superano insieme il limite."""

    def __init__(self, max_per_min: int):
        self.max_per_min = max_per_min
        self._timestamps: deque[float] = deque()
        self._lock = threading.Lock()

    @property
    def max_per_min(self) -> int:
        return self._max_per_min

    @max_per_min.setter
    def max_per_min(self, value: int) -> None:
        # Almeno 1 (audit 2026-10-18): con un valore negativo salvato sul
        # tracker, wait() leggeva _timestamps[-max_per_min] su una coda vuota
        # (IndexError a ogni chiamata, senza mai riprendersi) e ogni ricerca
        # e download di quel tracker falliva. Vale anche per le righe già nel
        # DB, che _shared_limiter riassegna a ogni adapter costruito.
        self._max_per_min = max(1, int(value))

    def wait(self, sleep: Callable[[float], None] = time.sleep) -> None:
        with self._lock:
            now = time.monotonic()
            while self._timestamps and self._timestamps[0] < now - 60:
                self._timestamps.popleft()
            slot = now
            if len(self._timestamps) >= self.max_per_min:
                # Quando la max_per_min-esima richiesta più recente esce dalla finestra.
                slot = max(now, self._timestamps[-self.max_per_min] + 60)
            self._timestamps.append(slot)
        if slot > now:
            sleep(slot - now)


# Le ricerche tenute per tracker (e token): oltre, la meno usata di recente
# esce. Una voce è qualche decina di KB (i candidati di un titolo), e prima
# nessuna usciva mai: per tutta la vita del processo restavano tutti i
# titoli cercati, per ogni tracker (audit 2026-10-18, finding 130).
MAX_CACHED_SEARCHES = 2000


class SearchCache(OrderedDict):
    """Le ricerche di un tracker per tmdb_id, con la loro scadenza (il
    cache_ttl_seconds dell'adapter che le ha scritte) e al più max_entries:
    a ogni scrittura escono quelle scadute, e oltre il tetto quella usata
    meno di recente. Prima la scadenza si guardava solo in lettura, e niente
    usciva mai. È un dict (tmdb_id -> (scritta, ttl, candidati)): chi deve
    dimenticare una ricerca la toglie con pop (nazgarr/upload/execute.py).
    Condivisa fra i thread (le run, "Cerca ora", gli upload): un lock."""

    def __init__(self, max_entries: int = MAX_CACHED_SEARCHES):
        super().__init__()
        self.max_entries = max_entries
        self._lock = threading.Lock()

    def lookup(self, tmdb_id: int, ttl: float) -> list | None:
        with self._lock:
            entry = super().get(tmdb_id)
            if entry is None:
                return None
            stored, _ttl, candidates = entry
            if time.monotonic() - stored >= ttl:
                super().pop(tmdb_id, None)
                return None
            self.move_to_end(tmdb_id)
            return candidates

    def store(self, tmdb_id: int, ttl: float, candidates: list) -> None:
        with self._lock:
            now = time.monotonic()
            self._drop_expired(now)
            self[tmdb_id] = (now, ttl, candidates)
            self.move_to_end(tmdb_id)
            while len(self) > self.max_entries:
                self.popitem(last=False)

    def pop(self, key, *default):
        with self._lock:
            return super().pop(key, *default)

    def sweep(self) -> int:
        """Toglie le ricerche scadute; quante ne restano."""
        with self._lock:
            self._drop_expired(time.monotonic())
            return len(self)

    def _drop_expired(self, now: float) -> None:
        for key in [key for key, (stored, ttl, _c) in self.items() if now - stored >= ttl]:
            super().pop(key, None)


# Rate limit e cache delle ricerche condivisi per tracker in tutto il
# processo: ogni operazione (run, "Cerca ora", upload, controllo completo)
# costruisce il suo adapter, e con un limitatore per adapter ognuna aveva il
# suo budget, così insieme superavano il limite del tracker.
_shared_lock = threading.Lock()
_shared_limiters: dict[str, _RateLimiter] = {}
_shared_search_caches: dict[tuple[str, str], SearchCache] = {}


def _shared_limiter(base_url: str, max_per_min: int) -> _RateLimiter:
    with _shared_lock:
        limiter = _shared_limiters.get(base_url)
        if limiter is None:
            limiter = _shared_limiters[base_url] = _RateLimiter(max_per_min)
        limiter.max_per_min = max_per_min  # cambiato dalla configurazione del tracker
        return limiter


def _shared_search_cache(base_url: str, api_token: str) -> SearchCache:
    # Per token: i risultati possono dipendere dall'utente (freeleech, accesso).
    key = (base_url, hashlib.sha256(api_token.encode()).hexdigest())
    with _shared_lock:
        # Le cache degli altri token (uno cambiato, un tracker tolto) senza
        # più niente di valido escono: restavano per sempre (audit 2026-10-18).
        for other in [k for k, cache in _shared_search_caches.items() if k != key and cache.sweep() == 0]:
            del _shared_search_caches[other]
        cache = _shared_search_caches.get(key)
        if cache is None:
            cache = _shared_search_caches[key] = SearchCache()
        return cache


def reset_shared_state() -> None:
    """Per i test."""
    with _shared_lock:
        _shared_limiters.clear()
        _shared_search_caches.clear()


class Unit3dTrackerAdapter(TrackerAdapter):
    """Prima implementazione concreta. Shape della risposta verificata contro
    un'istanza reale (ITT):

    - GET /api/torrents/filter?tmdbId=...
      -> {"data": [{"type": "torrent", "id": "...", "attributes": {...}}, ...]}
    - GET /api/torrents/:id
      -> {"type": "torrent", "id": "...", "attributes": {...}}   (NIENTE wrapper "data")
    - GET /api/user -> SOLO statistiche aggregate (upload/download totali,
      ratio, hit&run). NON restituisce la lista dei torrent.
    - Nessun endpoint pubblico documentato per la lista storico personale:
      quella pagina esiste solo come HTML autenticato (profilo -> history/uploads).
      get_own_history(), se implementato, dovrebbe passare da uno scraper HTML
      esplicitamente marcato come fragile (si rompe a ogni cambio tema/versione)
      e con caching/sync incrementale locale — non implementato qui.
    - Autenticazione: Bearer token (verificato funzionante). L'API supporta anche
      api_token come query string o form param, ma l'header evita che il token
      finisca in URL/log.
    - `info_hash` NON è esposto da questi endpoint (solo `download_link`,
      un URL autenticato al file .torrent) — TorrentCandidate.info_hash resta
      sempre None da questo adapter, il contratto lo prevede già come opzionale.
    - `attributes.media_info` è l'output testuale grezzo di mediainfo: lo
      Unique ID va estratto con una regex, non è un campo strutturato.
      Compare nella sezione General (a livello di intero container), non
      solo nello stream video — trattarlo comunque come candidato forte,
      mai come certezza assoluta (vedi motore di matching).
    - Rispetta rate_limit_per_min configurato per il tracker; risultati di
      search_by_tmdb cachati in memoria per cache_ttl_seconds. Ogni pagina
      della ricerca (perPage=100, cursore meta.next_cursor) è una richiesta
      col suo turno nel rate limit, fino a MAX_SEARCH_PAGES.
    - Su 429 attende quanto indica Retry-After (secondi o data HTTP; senza,
      un backoff esponenziale), al massimo _MAX_RETRY_WAIT_SECONDS per
      tentativo e _MAX_429_RETRIES tentativi, poi TrackerRateLimitedError.
    """

    _MAX_429_RETRIES = 3
    _MAX_RETRY_WAIT_SECONDS = 120.0
    _BASE_BACKOFF_SECONDS = 5.0

    # /api/torrents/filter è paginato a cursore (25 per pagina di default,
    # 100 al massimo, come chiede anche Upload-Assistant). Prima si leggeva
    # solo la prima pagina: per una serie con un upload per episodio, o un
    # film in molte versioni, i torrent più vecchi non diventavano mai
    # candidati e il controllo dei dupe poteva non vedere un doppione.
    SEARCH_PER_PAGE = 100
    MAX_SEARCH_PAGES = 10

    _UNIQUE_ID_RE = re.compile(r"Unique ID\s*:\s*(\S+)")
    _GENERAL_SECTION_RE = re.compile(r"(?m)^\s*General\s*$")
    _COMPLETE_NAME_RE = re.compile(r"Complete name\s*:\s*(.+)")

    def __init__(
        self,
        base_url: str,
        api_token: str,
        rate_limit_per_min: int = 30,
        http_client: httpx.Client | None = None,
        cache_ttl_seconds: int = 600,
        sleep: Callable[[float], None] = time.sleep,
        rss_key: str | None = None,
        shared: bool = False,
        session_cookie: str | None = None,
    ):
        """shared: rate limit e cache delle ricerche comuni a tutti gli adapter
        dello stesso tracker nel processo (quelli costruiti dall'app, vedi
        nazgarr/plugins/builtin.py); senza, propri di questo adapter (test).
        session_cookie: la riga Cookie della sessione web dell'utente, per il
        claim della resurrection (nazgarr/adapters/tracker/unit3d_web.py)."""
        self.base_url = base_url.rstrip("/")
        self.api_token = api_token
        self.session_cookie = session_cookie
        # Connesso solo a indirizzi controllati nel momento della connessione
        # (nazgarr/core/net_guard.py): l'URL e i link di download vengono dal tracker.
        self._client = http_client or net_guard.http_client(base_url=self.base_url, timeout=15.0)
        self._rate_limiter = (
            _shared_limiter(self.base_url, rate_limit_per_min) if shared else _RateLimiter(rate_limit_per_min))
        self._cache_ttl_seconds = cache_ttl_seconds
        self._search_cache: SearchCache = _shared_search_cache(self.base_url, api_token) if shared else SearchCache()
        self._sleep = sleep
        # Chiave attuale dei link di download: quella salvata sul tracker,
        # poi sempre aggiornata dal download_link di ogni risposta dell'API
        # (che la conosce per definizione), vedi rewrite_download_link().
        self.rss_key = rss_key
        # Chiamato con i secondi di attesa prima di un retry su 429, e con
        # None quando si riprende: la run lo mostra nel popup di stato invece
        # di sembrare bloccata (nazgarr/reseed/pipeline.py).
        self.on_rate_limit_wait: Callable[[float | None], None] | None = None

    def search_by_tmdb(self, tmdb_id: int) -> list[TorrentCandidate]:
        cached = self._search_cache.lookup(tmdb_id, self._cache_ttl_seconds)
        if cached is not None:
            return cached

        candidates = CatalogResults()
        params: dict = {"tmdbId": tmdb_id, "perPage": self.SEARCH_PER_PAGE}
        seen: set = set()
        for _ in range(self.MAX_SEARCH_PAGES):
            body = self._get_json("/api/torrents/filter", params=params)
            data = body.get("data", []) if isinstance(body, dict) else None
            if not isinstance(data, list):
                raise UploadError("Unexpected answer from the UNIT3D tracker search")
            candidates.extend(self._to_candidate(item) for item in data)
            following = self._next_page(body)
            if following is None:  # ultima pagina
                break
            if following in seen:
                # Un tracker che ripete un cursore già visto: non si gira in
                # tondo, ma le pagine dopo non si sono lette, quindi il
                # catalogo non è completo.
                candidates.truncated = True
                logger.warning(
                    "Tracker %s: cursore ripetuto nei risultati per tmdb %s, letti solo i primi %d torrent",
                    self.base_url, tmdb_id, len(candidates),
                )
                break
            seen.add(following)
            params = {"tmdbId": tmdb_id, "perPage": self.SEARCH_PER_PAGE, **dict(following)}
        else:
            candidates.truncated = True
            logger.warning(
                "Tracker %s: più di %d pagine di risultati per tmdb %s, letti solo i primi %d torrent",
                self.base_url, self.MAX_SEARCH_PAGES, tmdb_id, len(candidates),
            )
        self._search_cache.store(tmdb_id, self._cache_ttl_seconds, candidates)
        return candidates

    @staticmethod
    def _next_page(body: dict) -> tuple | None:
        """I parametri della pagina dopo: il cursore di UNIT3D
        (meta.next_cursor), o cursor/page presi dal link links.next. Il link
        non si segue mai così com'è: arriva dal tracker e potrebbe puntare
        altrove, se ne leggono solo i parametri."""
        meta = body.get("meta")
        cursor = meta.get("next_cursor") if isinstance(meta, dict) else None
        if isinstance(cursor, str) and cursor:
            return (("cursor", cursor),)
        links = body.get("links")
        following = links.get("next") if isinstance(links, dict) else None
        if isinstance(following, str) and following:
            query = parse_qs(urlsplit(following).query)
            for key in ("cursor", "page"):
                if query.get(key):
                    return ((key, query[key][0]),)
        return None

    def get_torrent_detail(self, torrent_id_remote: str) -> TorrentCandidate:
        """Dettaglio singolo torrent — utile per rileggere lo stato
        aggiornato di un candidate specifico senza rifare una ricerca completa."""
        body = self._get_json(f"/api/torrents/{torrent_id_remote}")
        if not isinstance(body, dict):
            raise UploadError("Unexpected answer from the UNIT3D tracker")
        return self._to_candidate(body)

    def get_own_history(self) -> list[TorrentRecord]:
        raise NotSupportedError(
            "Storico personale non esposto via API pubblica su UNIT3D; "
            "richiede scraper HTML dedicato, non implementato."
        )

    _TORRENT_ID_RE = re.compile(r"/(\d+)\.")

    def upload_torrent(self, fields: UploadFields, torrent_path: str) -> UploadedTorrent:
        """POST /api/torrents/upload, multipart (file "torrent" + campi
        form). Risposta {"success": bool, "message": str, "data": <URL di
        download tipo "https://tracker/torrents/download/12345.<token>">}
        — l'id numerico si estrae dall'URL stesso, non è un campo a parte
        (shape verificata contro Upload-Assistant, vedi UploadFields)."""
        data = {
            "name": fields.name,
            "description": fields.description,
            "mediainfo": fields.mediainfo,
            "bdinfo": "",
            "category_id": str(fields.category_id),
            "type_id": str(fields.type_id),
            "resolution_id": str(fields.resolution_id),
            "tmdb": str(fields.tmdb_id),
            "imdb": str(fields.imdb_id),
            "tvdb": str(fields.tvdb_id),
            "mal": str(fields.mal_id),
            "igdb": "0",
            "anonymous": "1" if fields.anonymous else "0",
            "stream": "1" if fields.stream else "0",
            "sd": "1" if fields.sd else "0",
            "keywords": fields.keywords,
            "personal_release": "1" if fields.personal_release else "0",
            "internal": "1" if fields.internal else "0",
            "featured": "0",
            "free": str(fields.free),
            "doubleup": "0",
            "sticky": "0",
        }
        if fields.season_number is not None:
            data["season_number"] = str(fields.season_number)
        if fields.episode_number is not None:
            data["episode_number"] = str(fields.episode_number)

        try:
            with open(torrent_path, "rb") as f:
                torrent_bytes = f.read()
        except OSError as exc:
            raise UploadError(f"Could not read the .torrent to upload: {exc}") from exc
        files = {"torrent": ("torrent.torrent", torrent_bytes, "application/x-bittorrent")}

        self._check_destination("/api/torrents/upload")
        self._rate_limiter.wait(self._sleep)
        try:
            with self._client.stream(
                "POST",
                "/api/torrents/upload",
                data=data,
                files=files,
                headers={"Authorization": f"Bearer {self.api_token}", "Accept": "application/json",
                         "Accept-Encoding": http_limits.ACCEPT_ENCODING},
            ) as response:
                response.raise_for_status()
                response_data = json.loads(http_limits.read_limited(response, self.MAX_JSON_BYTES, self._deadline()))
        except http_limits.ResponseLimitExceeded as exc:
            raise UploadError(f"UNIT3D upload failed: the tracker sent an answer {exc}") from exc
        except (httpx.HTTPError, httpx.InvalidURL, ValueError) as exc:
            raise UploadError(f"UNIT3D upload failed: {exc}") from exc

        if not isinstance(response_data, dict):
            raise UploadError(f"UNIT3D upload response is not an object: {response_data!r}")
        if not response_data.get("success"):
            raise UploadError(f"UNIT3D upload rejected by the tracker: {response_data.get('message', response_data)}")

        link = response_data.get("data") or ""
        match = self._TORRENT_ID_RE.search(link)
        if not match:
            raise UploadError(f"UNIT3D upload response has no recognizable torrent id: {response_data!r}")
        return UploadedTorrent(torrent_id_remote=match.group(1), download_link=urljoin(self.base_url + "/", link))

    _DOWNLOAD_KEY_RE = re.compile(r"(/torrent/download/\d+\.)([A-Za-z0-9]+)$")

    def _learn_rss_key(self, download_link: str | None) -> None:
        match = self._DOWNLOAD_KEY_RE.search(urlsplit(download_link).path) if download_link else None
        if match and host_of_url(download_link) == host_of_url(self.base_url):
            self.rss_key = match.group(2)

    def rewrite_download_link(self, url: str) -> str:
        """Lo stesso link di download con la chiave attuale: un link salvato
        altrove prima di un cambio di chiave (history di Sonarr/Radarr)
        torna valido senza chiedere niente al tracker. Invariato se la
        chiave non è nota, se non è un link di questo tracker o se non ha la
        forma /torrent/download/<id>.<chiave>."""
        if not self.rss_key or host_of_url(url) != host_of_url(self.base_url):
            return url
        parts = urlsplit(url)
        if not self._DOWNLOAD_KEY_RE.search(parts.path):
            return url
        return parts._replace(path=self._DOWNLOAD_KEY_RE.sub(rf"\g<1>{self.rss_key}", parts.path)).geturl()

    MAX_TORRENT_BYTES = 20 * 1024 * 1024
    # Una pagina di risultati JSON (con il media_info di ogni torrent) e la
    # risposta di un upload: ben sotto, anche per un pack con molti episodi.
    MAX_JSON_BYTES = 32 * 1024 * 1024
    # Tempo massimo per leggere una risposta intera: il timeout di httpx vale
    # per ogni lettura, e un byte ogni 14 secondi lo rispettava per sempre.
    RESPONSE_DEADLINE_SECONDS = 120.0

    def download_torrent(self, url: str) -> bytes:
        # Il download_link di UNIT3D è già autenticato (passkey nell'URL):
        # nessun header Bearer, il token API non deve seguire un URL che
        # potrebbe anche puntare fuori da base_url. E un link che punta a un
        # altro host non si segue affatto: arriva dal tracker (o da chi ci ha
        # caricato un torrent), e farebbe chiamare al server indirizzi interni.
        # Lo schema conta quanto l'host: un link http:// su un tracker https://
        # manderebbe la passkey in chiaro, e un altro schema non è un .torrent.
        outside = self._outside_tracker(urljoin(self.base_url + "/", url))
        if outside:
            raise UploadError(f"Download link {outside}: not followed")
        try:
            return self._get(url, authenticated=False, limit=self.MAX_TORRENT_BYTES)
        except _BodyTooLarge as exc:
            raise UploadError(f"The tracker sent a .torrent {exc}: not used") from exc

    def _outside_tracker(self, full_url: str) -> str | None:
        """Perché un URL che viene dal tracker (un link di download, l'action
        di un form) non si segue, o None: un altro host, o uno schema
        diverso (http:// su un tracker https:// manderebbe in chiaro la
        passkey o il cookie di sessione)."""
        tracker_host = host_of_url(self.base_url)
        link_host = host_of_url(full_url)
        if not link_host or not tracker_host or not (
            link_host == tracker_host or link_host.endswith("." + tracker_host)
        ):
            return f"outside the tracker ({link_host})"
        scheme, tracker_scheme = urlsplit(full_url).scheme.lower(), urlsplit(self.base_url).scheme.lower()
        if scheme not in ("http", "https") or (tracker_scheme == "https" and scheme != "https"):
            return f"with scheme {scheme or 'none'!r} on a {tracker_scheme} tracker"
        return None

    # --- il sito, con la sessione web dell'utente (unit3d_web.py) -------------

    # Una pagina HTML del sito: quella di un torrent con un MediaInfo lungo e i commenti sta ben sotto.
    WEB_PAGE_MAX_BYTES = 8 * 1024 * 1024

    def check_web_session(self) -> TrackerWebResult:
        cookies = self._web_cookies()
        if isinstance(cookies, TrackerWebResult):
            return cookies
        try:
            page = self._web_page("/", cookies)
        except UploadError as exc:
            return TrackerWebResult("failed", str(exc))
        return page if isinstance(page, TrackerWebResult) else TrackerWebResult("ok")

    def claim_resurrection(self, torrent_id_remote: str) -> TrackerWebResult:
        """Il form della pagina del torrent, inviato come lo invierebbe il
        browser; l'esito si legge dalla pagina dopo: senza più il form, il
        claim è in attesa. Tre richieste, ognuna col suo turno nel rate
        limit. Un claim inviato di cui non si legge l'esito è "failed": al
        nuovo tentativo la pagina dice com'è andata (non lo offre più)."""
        torrent_id = str(torrent_id_remote)
        if not torrent_id.isdigit():
            return TrackerWebResult("failed", f"{torrent_id!r} is not a UNIT3D torrent id")
        cookies = self._web_cookies()
        if isinstance(cookies, TrackerWebResult):
            return cookies
        page_path = f"/torrents/{torrent_id}"
        try:
            page = self._web_page(page_path, cookies)
            if isinstance(page, TrackerWebResult):
                return page
            form = unit3d_web.resurrection_form(page, torrent_id)
            if form is None:
                return TrackerWebResult("not_offered")
            if not form.fields.get("_token"):
                return TrackerWebResult("failed", "The claim form on the torrent page has no CSRF token")
            action = urljoin(self.base_url + page_path, form.action or page_path)
            outside = self._outside_tracker(action)
            if outside:
                return TrackerWebResult("failed", f"The claim form points {outside}")
            status, location, _body = self._web("POST", action, cookies, data=form.fields)
            if unit3d_web.is_login_redirect(status, location):
                return TrackerWebResult("session_expired")
            if status == 419:
                return TrackerWebResult("failed", "The tracker rejected the form token (HTTP 419)")
            if status == 403:
                return TrackerWebResult("refused", "HTTP 403")
            if not 200 <= status < 400:
                return TrackerWebResult("failed", self._web_status_detail(status, location))
            after = self._web_page(page_path, cookies)
        except UploadError as exc:
            return TrackerWebResult("failed", str(exc))
        if isinstance(after, TrackerWebResult):
            return TrackerWebResult("failed", f"Claim sent, but the torrent page could not be read after it "
                                              f"({after.detail or after.status})")
        if unit3d_web.resurrection_form(after, torrent_id) is None:
            return TrackerWebResult("claimed")
        return TrackerWebResult("refused", "The torrent page still offers the claim after sending it "
                                           "(UNIT3D refuses it on your own uploads)")

    def _web_cookies(self) -> dict[str, str] | TrackerWebResult:
        if not self.session_cookie:
            return TrackerWebResult("no_session")
        try:
            return unit3d_web.parse_cookie_header(self.session_cookie)
        except unit3d_web.InvalidCookie:
            return TrackerWebResult("failed", "The saved session cookie is not a valid Cookie line")

    def _web_page(self, path: str, cookies: dict[str, str]) -> "unit3d_web.Page | TrackerWebResult":
        status, location, body = self._web("GET", path, cookies)
        if unit3d_web.is_login_redirect(status, location):
            return TrackerWebResult("session_expired")
        if status != 200:
            return TrackerWebResult("failed", self._web_status_detail(status, location))
        page = unit3d_web.parse_page(body)
        if page.is_login():
            return TrackerWebResult("session_expired")
        return page

    @staticmethod
    def _web_status_detail(status: int, location: str | None) -> str:
        if 300 <= status < 400:
            return f"HTTP {status}, redirect to {location or '?'}"
        if status in (403, 503):
            return f"HTTP {status} (a protection of the site, like Cloudflare, may block requests from the server)"
        return f"HTTP {status}"

    def _web(self, method: str, url: str, cookies: dict[str, str],
             data: dict[str, str] | None = None) -> tuple[int, str | None, str]:
        """Una richiesta al sito, mai seguendo i redirect: stato, Location e
        corpo (vuoto per un redirect o un errore). I cookie che il tracker
        rimanda (la sessione nuova creata dal cookie "Ricordami", il token
        XSRF) aggiornano `cookies` per la richiesta dopo; nel DB resta
        quello incollato dall'utente. Mai il token dell'API."""
        self._check_destination(url)
        headers = {
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Encoding": http_limits.ACCEPT_ENCODING,
            "Cookie": unit3d_web.cookie_header(cookies),
            "Referer": self.base_url + "/",
        }
        if data is not None:
            headers["Origin"] = self.base_url
        self._rate_limiter.wait(self._sleep)
        try:
            with self._client.stream(method, url, headers=headers, data=data) as response:
                for name, value in response.cookies.items():
                    cookies[name] = value
                location = response.headers.get("location")
                if response.is_redirect or response.status_code >= 400:
                    return response.status_code, location, ""
                body = http_limits.read_limited(response, self.WEB_PAGE_MAX_BYTES, self._deadline())
                return response.status_code, location, body.decode(response.charset_encoding or "utf-8",
                                                                   errors="replace")
        except http_limits.ResponseLimitExceeded as exc:
            raise UploadError(f"The tracker sent a page {exc}: not used") from exc
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            raise UploadError(f"Request to the tracker site failed: {exc}") from exc

    def _retry_wait_seconds(self, response: httpx.Response, attempt: int) -> float:
        header = response.headers.get("Retry-After")
        wait: float | None = None
        if header:
            try:
                wait = float(header)
            except ValueError:
                try:
                    wait = parsedate_to_datetime(header).timestamp() - time.time()
                except (TypeError, ValueError):
                    wait = None
        if wait is None:
            wait = self._BASE_BACKOFF_SECONDS * (2**attempt)
        return min(max(wait, 1.0), self._MAX_RETRY_WAIT_SECONDS)

    def _check_destination(self, path: str) -> None:
        """net_guard prima di ogni richiesta, non solo quando base_url si
        salva: un nome del tracker che dopo si risolve nei metadati del cloud
        (o un link di download su un suo sottodominio) non si chiama."""
        try:
            net_guard.check_url(urljoin(self.base_url + "/", path))
        except net_guard.ForbiddenDestination as exc:
            raise UploadError(f"Request to the UNIT3D tracker refused: {exc}") from exc

    def _deadline(self) -> float:
        return time.monotonic() + self.RESPONSE_DEADLINE_SECONDS

    def _get_json(self, path: str, params: dict | None = None):
        try:
            return json.loads(self._get(path, params=params))
        except _BodyTooLarge as exc:
            raise UploadError(f"The UNIT3D tracker sent an answer {exc}: not used") from exc
        except ValueError as exc:
            raise UploadError("The UNIT3D tracker sent an answer that is not valid JSON") from exc

    def _get(self, path: str, params: dict | None = None, authenticated: bool = True,
             limit: int | None = None) -> bytes:
        """Il corpo della risposta, letto in streaming fino a `limit` byte
        decompressi (default MAX_JSON_BYTES) entro RESPONSE_DEADLINE_SECONDS:
        nazgarr/core/http_limits.py. Oltre il tetto _BodyTooLarge, che il
        chiamante traduce in un UploadError col suo messaggio."""
        # Solo le codifiche che read_limited decomprime con un tetto.
        headers = {"Accept-Encoding": http_limits.ACCEPT_ENCODING}
        if authenticated:
            headers["Authorization"] = f"Bearer {self.api_token}"
        limit = self.MAX_JSON_BYTES if limit is None else limit
        self._check_destination(path)
        for attempt in range(self._MAX_429_RETRIES + 1):
            self._rate_limiter.wait(self._sleep)
            try:
                with self._client.stream("GET", path, params=params, headers=headers) as response:
                    if response.status_code != 429:
                        return self._read_body(response, limit)
                    wait = self._retry_wait_seconds(response, attempt)
            except (httpx.HTTPError, httpx.InvalidURL) as exc:  # InvalidURL non è un HTTPError
                raise UploadError(f"Request to the UNIT3D tracker failed: {exc}") from exc
            if attempt == self._MAX_429_RETRIES:
                raise TrackerRateLimitedError(
                    f"UNIT3D tracker still rate limiting (429) after {self._MAX_429_RETRIES} retries"
                )
            logger.warning("Tracker %s ha risposto 429, nuovo tentativo tra %.0fs", self.base_url, wait)
            if self.on_rate_limit_wait is not None:
                self.on_rate_limit_wait(wait)
            self._sleep(wait)
            if self.on_rate_limit_wait is not None:
                self.on_rate_limit_wait(None)
        raise AssertionError("unreachable")  # pragma: no cover — l'ultimo tentativo ritorna o solleva

    def _read_body(self, response: httpx.Response, limit: int) -> bytes:
        if response.is_redirect and "/login" in response.headers.get("location", ""):
            # UNIT3D rimanda al login un link di download con una chiave non
            # più valida (rsskey rigenerata, o link salvato altrove, es. nella
            # history di Sonarr/Radarr) invece di rispondere 401/403.
            raise UploadError("The tracker rejected the link (redirect to login): its key is no longer valid")
        try:
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise UploadError(f"Request to the UNIT3D tracker failed: {exc}") from exc
        try:
            return http_limits.read_limited(response, limit, self._deadline())
        except http_limits.ResponseLimitExceeded as exc:
            raise _BodyTooLarge(str(exc)) from exc

    def _to_candidate(self, item: dict) -> TorrentCandidate:
        attrs = item["attributes"]
        self._learn_rss_key(attrs.get("download_link"))
        files = attrs.get("files") or []
        folder, file_list, file_sizes = self._normalize_pack_structure(attrs.get("folder"), files)
        return TorrentCandidate(
            torrent_id_remote=str(item["id"]),
            info_hash=None,
            name=attrs["name"],
            size_bytes=attrs["size"],
            file_list=file_list,
            mediainfo_unique_id=self._extract_unique_id(attrs.get("media_info")),
            folder=folder,
            download_link=attrs.get("download_link"),
            file_sizes=file_sizes,
            mediainfo_unique_ids_by_filename=self._extract_unique_ids_by_filename(attrs.get("media_info")),
            category_id=_count(attrs.get("category_id")),
            type_id=_count(attrs.get("type_id")),
            resolution=attrs["resolution"][:20] if isinstance(attrs.get("resolution"), str) else None,
            seeders=_count(attrs.get("seeders")),
            media_info_summary=mediainfo_text.summarize(
                attrs["media_info"] if isinstance(attrs.get("media_info"), str) else None),
        )

    @staticmethod
    def _normalize_pack_structure(
        raw_folder: str | None, files: list[dict]
    ) -> tuple[str | None, list[str], dict[str, int]]:
        """Determina la cartella del pack (se ricavabile qui) e normalizza
        file_list/file_sizes a nomi nudi — il resto del motore assume
        SEMPRE folder e nome file separati, mai un path già annidato
        dentro file_list.

        Alcune istanze UNIT3D riportano il path relativo già dentro
        files[].name invece che nella sola attributes.folder: se tutti i
        file condividono lo stesso primo componente di path, quella è la
        cartella vera — va tolta dal nome e MAI anche aggiunta separatamente.

        Se manca sia attributes.folder sia un path annidato, NON si
        indovina più una cartella da attributes.name: è un titolo
        "leggibile" per la UI, non necessariamente il vero nome di release
        (vedi nazgarr/torrents/metainfo.py per la motivazione completa e il fallback
        che legge il .torrent stesso)."""
        names = [f["name"] for f in files]

        embedded_folder = None
        if names and all("/" in name for name in names):
            first_components = {name.split("/", 1)[0] for name in names}
            if len(first_components) == 1:
                embedded_folder = next(iter(first_components))

        if embedded_folder:
            prefix = embedded_folder + "/"
            stripped_names = [name[len(prefix):] if name.startswith(prefix) else name for name in names]
            file_sizes = {
                (f["name"][len(prefix):] if f["name"].startswith(prefix) else f["name"]): f["size"]
                for f in files
                if "size" in f
            }
            return embedded_folder, stripped_names, file_sizes

        file_sizes = {f["name"]: f["size"] for f in files if "size" in f}
        return raw_folder, names, file_sizes

    @classmethod
    def _extract_unique_id(cls, media_info: str | None) -> str | None:
        if not media_info:
            return None
        match = cls._UNIQUE_ID_RE.search(media_info)
        return match.group(1) if match else None

    @classmethod
    def _extract_unique_ids_by_filename(cls, media_info: str | None) -> dict[str, str]:
        """Un blob media_info di un season pack è la concatenazione dei
        report mediainfo di ciascun file, ciascuno con la propria sezione
        General (Complete name + Unique ID). Ritorna {basename: unique_id}
        per ogni sezione dove entrambi i campi sono presenti."""
        if not media_info:
            return {}
        result: dict[str, str] = {}
        for section in cls._GENERAL_SECTION_RE.split(media_info)[1:]:
            name_match = cls._COMPLETE_NAME_RE.search(section)
            id_match = cls._UNIQUE_ID_RE.search(section)
            if name_match and id_match:
                filename = re.split(r"[\\/]", name_match.group(1).strip())[-1]
                result[filename] = id_match.group(1)
        return result
