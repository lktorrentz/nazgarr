"""Adapter per un endpoint Torznab (issue #33): ricerca, matching, reseed e
cross-seed oltre i tracker UNIT3D, attraverso un URL Torznab e la sua API
key, qualunque software lo esponga (Prowlarr, Jackett, un indexer che parla
Torznab da sé).

Cosa fa:
- legge le capacità dell'endpoint con t=caps (modi di ricerca disponibili,
  parametri supportati da ciascuno, limite di risultati per pagina,
  categorie), tenute in cache per CAPS_TTL_SECONDS;
- cerca per gli identificativi che il modo supporta (tmdbid, poi imdbid) e
  ripiega sul titolo solo se non ce n'è nessuno o non hanno dato risultati;
- restituisce i risultati come TorrentCandidate con nome, dimensione, numero
  di file e info hash se l'endpoint li dà. Servono solo a scegliere quali
  candidati valutare: la struttura vera e la verifica dei piece vengono dal
  .torrent (download_torrent), come per UNIT3D (nazgarr/reseed/matching.py).

Cosa non fa: upload (Torznab non definisce un'API per pubblicare), storico
personale. Un risultato con solo un magnet non si usa: senza .torrent non
c'è niente da verificare contro i file locali.

Un endpoint aggregato (Prowlarr "all", Jackett "all") mescola tracker
diversi: meglio un tracker Nazgarr per indexer (Prowlarr: /<id>/api), con
il suo rate limit e, se serve, il suo announce_url per riconoscerne i
torrent nel client (nazgarr/torrents/tracker_scope.py).
"""

import hashlib
import logging
import re
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import ClassVar
from urllib.parse import quote, urljoin, urlsplit

import httpx

from nazgarr.adapters.tracker.base import (
    CatalogResults,
    MediaQuery,
    SearchCache,
    TorrentCandidate,
    TrackerAdapter,
    TrackerRateLimitedError,
    UploadError,
    _RateLimiter,
    _shared_limiter,
    _shared_search_cache,
    host_of_url,
)
from nazgarr.core import http_limits, net_guard

logger = logging.getLogger(__name__)

TORZNAB_NS = "http://torznab.com/schemas/2015/feed"

# Categorie standard Newznab/Torznab (le principali): le sottocategorie
# (2040 Movies/HD, 5070 TV/Anime...) stanno sotto queste.
CATEGORY_MOVIES = 2000
CATEGORY_TV = 5000

_INFO_HASH_RE = re.compile(r"^[0-9a-fA-F]{40}$")
_IMDB_RE = re.compile(r"^(?:tt)?0*(\d{1,10})$")


@dataclass(frozen=True)
class TorznabCaps:
    """Le capacità dichiarate da t=caps: per ogni modo disponibile ("search",
    "tv-search", "movie-search") i parametri che accetta."""

    modes: dict[str, frozenset[str]]
    limit_max: int | None = None
    limit_default: int | None = None
    categories: frozenset[int] = frozenset()

    def supports(self, mode: str, param: str) -> bool:
        return param in self.modes.get(mode, frozenset())


def parse_caps(xml_bytes: bytes) -> TorznabCaps:
    root = _parse_xml(xml_bytes)
    if root.tag != "caps":
        raise UploadError("The Torznab endpoint answered t=caps without a <caps> document")
    modes: dict[str, frozenset[str]] = {}
    searching = root.find("searching")
    for element in (searching if searching is not None else []):
        if (element.get("available") or "").lower() != "yes":
            continue
        params = element.get("supportedParams") or element.get("supportedparams") or "q"
        modes[element.tag] = frozenset(p.strip().lower() for p in params.split(",") if p.strip())
    limits = root.find("limits")
    categories = frozenset(
        int(c.get("id")) for c in root.iterfind("categories/category") if (c.get("id") or "").isdigit()
    )
    return TorznabCaps(
        modes=modes,
        limit_max=_positive_int(limits.get("max")) if limits is not None else None,
        limit_default=_positive_int(limits.get("default")) if limits is not None else None,
        categories=categories,
    )


def _positive_int(value) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _parse_xml(xml_bytes: bytes) -> ET.Element:
    # Niente DTD: un documento che ne dichiara una non è una risposta Torznab
    # (e le entità definite lì sono il modo di gonfiare il parsing).
    if b"<!DOCTYPE" in xml_bytes[:4096].upper():
        raise UploadError("The Torznab endpoint sent an XML document with a DTD: not used")
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise UploadError("The Torznab endpoint sent an answer that is not valid XML") from exc
    if root.tag == "error":
        _raise_torznab_error(root)
    return root


def _error_element(body: bytes) -> ET.Element | None:
    """L'<error> della specifica in una risposta HTTP di errore, se c'è."""
    if not body.lstrip().startswith(b"<") or b"<!DOCTYPE" in body[:4096].upper():
        return None
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return None
    return root if root.tag == "error" else None


def _raise_torznab_error(element: ET.Element) -> None:
    """<error code="..." description="..."/> della specifica: 500 (limite di
    richieste) e 501 (limite di download) fermano il tracker per la run come
    un 429, il resto è un errore della singola richiesta."""
    code = element.get("code") or "?"
    description = element.get("description") or ""
    message = f"Torznab error {code}: {description}".rstrip(": ")
    if code in ("500", "501"):
        raise TrackerRateLimitedError(message)
    raise UploadError(message)


class TorznabTrackerAdapter(TrackerAdapter):
    """Un endpoint Torznab come tracker: base_url è l'URL dell'endpoint così
    come lo dà il software che lo espone (es. http://prowlarr:9696/1/api,
    http://jackett:9117/api/v2.0/indexers/<id>/results/torznab/), api_token
    la sua API key, mandata come parametro apikey come vuole la specifica."""

    CAPS_TTL_SECONDS = 3600
    # Pagine lette per ricerca (offset/limit): oltre, risultati troncati.
    MAX_SEARCH_PAGES = 5
    FALLBACK_PAGE_SIZE = 100
    MAX_XML_BYTES = 16 * 1024 * 1024
    MAX_TORRENT_BYTES = 20 * 1024 * 1024
    RESPONSE_DEADLINE_SECONDS = 120.0
    MAX_REDIRECTS = 5

    _MAX_429_RETRIES = 3
    _MAX_RETRY_WAIT_SECONDS = 120.0
    _BASE_BACKOFF_SECONDS = 5.0

    def __init__(
        self,
        base_url: str,
        api_key: str,
        rate_limit_per_min: int = 30,
        http_client: httpx.Client | None = None,
        cache_ttl_seconds: int = 600,
        sleep: Callable[[float], None] = time.sleep,
        shared: bool = False,
    ):
        self.base_url = base_url.strip()
        self.api_key = api_key
        self._client = http_client or net_guard.http_client(timeout=15.0)
        self._rate_limiter = (
            _shared_limiter(self.base_url, rate_limit_per_min) if shared else _RateLimiter(rate_limit_per_min))
        self._cache_ttl_seconds = cache_ttl_seconds
        self._search_cache: SearchCache = (
            _shared_search_cache(self.base_url, api_key) if shared else SearchCache())
        self._sleep = sleep
        self.on_rate_limit_wait: Callable[[float | None], None] | None = None

    # -- capacità ----------------------------------------------------------

    def caps(self) -> TorznabCaps:
        # Nella stessa cache delle ricerche (condivisa per endpoint e chiave),
        # con la sua scadenza.
        key = ("caps",)
        cached = self._search_cache.lookup(key, self.CAPS_TTL_SECONDS)
        if cached is not None:
            return cached
        caps = parse_caps(self._get(self.base_url, {"t": "caps"}, limit=self.MAX_XML_BYTES))
        self._search_cache.store(key, self.CAPS_TTL_SECONDS, caps)
        return caps

    # -- ricerca -----------------------------------------------------------

    def search_by_tmdb(self, tmdb_id: int) -> list[TorrentCandidate]:
        return self.search_media(MediaQuery(tmdb_id=tmdb_id))

    def search_media(self, query: MediaQuery) -> list[TorrentCandidate]:
        key = ("search", query)
        cached = self._search_cache.lookup(key, self._cache_ttl_seconds)
        if cached is not None:
            return cached
        caps = self.caps()
        results = CatalogResults()
        for params in self._search_plan(caps, query):
            results = self._run_search(caps, params, query)
            if results:
                break
        self._search_cache.store(key, self._cache_ttl_seconds, results)
        return results

    _FUNCTIONS: ClassVar[dict[str, str]] = {"movie-search": "movie", "tv-search": "tvsearch", "search": "search"}

    def _search_plan(self, caps: TorznabCaps, query: MediaQuery) -> list[dict]:
        """Le ricerche da provare in ordine, ognuna solo se la precedente non
        ha trovato niente: per tmdbid, per imdbid, per titolo. Solo con i
        parametri che l'endpoint dichiara per quel modo. Senza il tipo del
        contenuto (search_by_tmdb) gli id si provano nei modi film e serie."""
        wanted = {"movie": ["movie-search"], "tv": ["tv-search"]}.get(
            query.content_type or "", ["movie-search", "tv-search"])
        modes = [mode for mode in wanted if mode in caps.modes] or ["search"]
        category = {"movie": CATEGORY_MOVIES, "tv": CATEGORY_TV}.get(query.content_type or "")

        def base(mode: str) -> dict:
            params: dict = {"t": self._FUNCTIONS[mode]}
            if category is not None and category in caps.categories:
                params["cat"] = str(category)
            if query.season is not None and caps.supports(mode, "season"):
                params["season"] = str(query.season)
            return params

        plan: list[dict] = []
        imdb = _imdb_digits(query.imdb_id)
        for param, value in (("tmdbid", str(query.tmdb_id)), ("imdbid", imdb)):
            plan += [{**base(mode), param: value} for mode in modes if value and caps.supports(mode, param)]
        # Il titolo nel modo specifico se accetta q, se no nella ricerca generica.
        title_mode = next((mode for mode in [*modes, "search"] if caps.supports(mode, "q")), None)
        if query.title and title_mode is not None:
            params = base(title_mode)
            text = query.title
            if query.content_type == "movie" and query.year:
                if caps.supports(title_mode, "year"):
                    params["year"] = str(query.year)
                else:
                    text = f"{text} {query.year}"
            if query.season is not None and "season" not in params:
                text = f"{text} S{query.season:02d}"
            params["q"] = text
            plan.append(params)
        if not plan:
            logger.info("Torznab %s: nessun parametro utilizzabile per tmdb %s (caps senza tmdbid/imdbid, "
                        "titolo sconosciuto)", self.base_url, query.tmdb_id)
        return plan

    def _run_search(self, caps: TorznabCaps, params: dict, query: MediaQuery) -> CatalogResults:
        page_size = caps.limit_max or caps.limit_default or self.FALLBACK_PAGE_SIZE
        results = CatalogResults()
        seen: set[str] = set()
        offset = 0
        for _ in range(self.MAX_SEARCH_PAGES):
            body = self._get(self.base_url, {**params, "limit": str(page_size), "offset": str(offset)},
                             limit=self.MAX_XML_BYTES)
            items = self._parse_items(body)
            new = [(c, ids) for c, ids in items if c.torrent_id_remote not in seen]
            seen.update(c.torrent_id_remote for c, _ids in new)
            results.extend(c for c, ids in new if _belongs(ids, query))
            # Fine dei risultati: una pagina corta, o nessun risultato nuovo
            # (un endpoint che ignora offset ripete la prima).
            if len(items) < page_size or not new:
                return results
            offset += len(items)
        results.truncated = True
        logger.warning("Torznab %s: più di %d pagine di risultati per %s, letti solo i primi %d",
                       self.base_url, self.MAX_SEARCH_PAGES, params, len(seen))
        return results

    def _parse_items(self, xml_bytes: bytes) -> list[tuple[TorrentCandidate, dict[str, str]]]:
        """I risultati, ognuno con gli identificativi che dichiara (tmdbid, imdbid)."""
        root = _parse_xml(xml_bytes)
        channel = root.find("channel")
        if root.tag != "rss" or channel is None:
            raise UploadError("Unexpected answer from the Torznab search (no RSS channel)")
        return [c for c in (self._to_candidate(item) for item in channel.iterfind("item")) if c is not None]

    def _to_candidate(self, item: ET.Element) -> tuple[TorrentCandidate, dict[str, str]] | None:
        attrs: dict[str, str] = {}
        for attr in item.iterfind(f"{{{TORZNAB_NS}}}attr"):
            name, value = (attr.get("name") or "").lower(), attr.get("value")
            if name and value is not None:
                attrs.setdefault(name, value)
        enclosure = item.find("enclosure")
        title = (item.findtext("title") or "").strip()
        link = _http_link(enclosure.get("url") if enclosure is not None else None) or _http_link(
            item.findtext("link"))
        size = (_positive_int(attrs.get("size")) or _positive_int(item.findtext("size"))
                or _positive_int(enclosure.get("length") if enclosure is not None else None))
        if not title or not link or not size:
            # Senza .torrent (solo magnet) o senza dimensione non c'è niente
            # da scegliere né da verificare.
            return None
        info_hash = attrs.get("infohash")
        candidate = TorrentCandidate(
            torrent_id_remote=self._remote_id(item.findtext("guid") or link),
            info_hash=info_hash.lower() if info_hash and _INFO_HASH_RE.match(info_hash) else None,
            name=title,
            size_bytes=size,
            file_list=None,
            mediainfo_unique_id=None,
            download_link=link,
            file_count=_positive_int(attrs.get("files")),
        )
        ids = {k: attrs[k] for k in ("tmdbid", "imdbid") if attrs.get(k) not in (None, "", "0")}
        return candidate, ids

    def _remote_id(self, guid: str) -> str:
        """Il guid del risultato (stabile fra le ricerche). Se contiene la
        API key (Jackett usa il link di download quando l'indexer non ha una
        pagina di dettaglio) o è troppo lungo, un suo hash: l'id finisce nel
        DB e nella UI in chiaro."""
        guid = guid.strip()
        if (self.api_key and self.api_key in guid) or len(guid) > 300:
            return "sha256:" + hashlib.sha256(guid.encode()).hexdigest()[:40]
        return guid

    # -- download ----------------------------------------------------------

    def download_torrent(self, url: str) -> bytes:
        """Il .torrent di un risultato. Il link deve essere dell'endpoint
        stesso (Prowlarr e Jackett fanno da proxy ai download): un link verso
        un altro host non si segue, arriva da chiunque abbia caricato
        qualcosa sugli indexer. Un redirect dell'endpoint (Prowlarr con il
        redirect acceso rimanda al tracker) sì, controllato a ogni passo; un
        redirect a un magnet è un errore, non c'è un .torrent."""
        full_url = urljoin(self.base_url, url)
        if host_of_url(full_url) != host_of_url(self.base_url):
            raise UploadError(f"Download link outside the Torznab endpoint ({host_of_url(full_url)}): not followed")
        for _ in range(self.MAX_REDIRECTS + 1):
            scheme = urlsplit(full_url).scheme.lower()
            if scheme == "magnet":
                raise UploadError("The Torznab endpoint redirected to a magnet link: no .torrent to verify")
            if scheme not in ("http", "https"):
                raise UploadError(f"Download link with scheme {scheme or 'none'!r}: not followed")
            content = self._get(full_url, None, limit=self.MAX_TORRENT_BYTES, allow_redirect=True)
            if isinstance(content, _Redirect):
                full_url = urljoin(full_url, content.location)
                continue
            if not content.startswith(b"d"):
                raise UploadError("The Torznab endpoint sent something that is not a .torrent file")
            return content
        raise UploadError(f"Too many redirects downloading the .torrent (more than {self.MAX_REDIRECTS})")

    # Al posto della API key nei link salvati sui candidati
    # (nazgarr/reseed/download_links.py): rimessa solo per scaricare.
    KEY_PLACEHOLDER = "nazgarr-apikey"

    def strip_download_link(self, link: str | None) -> str | None:
        """Il link di download senza la API key dell'endpoint (Prowlarr la
        mette in apikey=, Jackett in jackett_apikey=): nel DB non resta."""
        if not link or not self.api_key:
            return link
        parts = urlsplit(link)
        for key in {self.api_key, quote(self.api_key, safe="")}:
            if key in parts.query:
                return parts._replace(query=parts.query.replace(key, self.KEY_PLACEHOLDER)).geturl()
        return link

    def restore_download_link(self, link: str | None) -> str | None:
        """Il link salvato con la API key attuale (anche se cambiata nel frattempo)."""
        if not link:
            return link
        parts = urlsplit(link)
        if self.KEY_PLACEHOLDER not in parts.query:
            return link
        return parts._replace(
            query=parts.query.replace(self.KEY_PLACEHOLDER, quote(self.api_key, safe=""))).geturl()

    # -- HTTP --------------------------------------------------------------

    def _deadline(self) -> float:
        return time.monotonic() + self.RESPONSE_DEADLINE_SECONDS

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

    def _get(self, url: str, params: dict | None, limit: int, allow_redirect: bool = False):
        """GET con il rate limit dell'endpoint, i retry su 429 e il corpo
        letto con un tetto (nazgarr/core/http_limits.py). params, se dati,
        sono una chiamata all'API (con apikey); senza, un URL già completo
        (un link di download, che porta la sua chiave). Con allow_redirect, un 3xx
        torna come _Redirect invece di essere un errore."""
        try:
            net_guard.check_url(url)
        except net_guard.ForbiddenDestination as exc:
            raise UploadError(f"Request to the Torznab endpoint refused: {exc}") from exc
        if params is not None:
            params = {**params, "apikey": self.api_key}
        headers = {"Accept-Encoding": http_limits.ACCEPT_ENCODING}
        for attempt in range(self._MAX_429_RETRIES + 1):
            self._rate_limiter.wait(self._sleep)
            try:
                with self._client.stream("GET", url, params=params, headers=headers) as response:
                    if response.status_code != 429:
                        return self._read(response, limit, allow_redirect)
                    wait = self._retry_wait_seconds(response, attempt)
            except (httpx.HTTPError, httpx.InvalidURL) as exc:
                raise UploadError(f"Request to the Torznab endpoint failed: {exc}") from exc
            if attempt == self._MAX_429_RETRIES:
                raise TrackerRateLimitedError(
                    f"Torznab endpoint still rate limiting (429) after {self._MAX_429_RETRIES} retries")
            logger.warning("Torznab %s ha risposto 429, nuovo tentativo tra %.0fs", self.base_url, wait)
            if self.on_rate_limit_wait is not None:
                self.on_rate_limit_wait(wait)
            self._sleep(wait)
            if self.on_rate_limit_wait is not None:
                self.on_rate_limit_wait(None)
        raise AssertionError("unreachable")  # pragma: no cover

    def _read(self, response: httpx.Response, limit: int, allow_redirect: bool):
        if response.is_redirect:
            location = response.headers.get("location")
            if not allow_redirect or not location:
                raise UploadError(f"The Torznab endpoint answered with an unexpected redirect ({response.status_code})")
            return _Redirect(location)
        try:
            body = http_limits.read_limited(response, limit, self._deadline())
        except http_limits.ResponseLimitExceeded as exc:
            raise UploadError(f"The Torznab endpoint sent an answer {exc}: not used") from exc
        if response.is_error:
            # Prowlarr e Jackett spiegano l'errore con l'<error> della specifica.
            error = _error_element(body)
            if error is not None:
                _raise_torznab_error(error)
            raise UploadError(f"Request to the Torznab endpoint failed: HTTP {response.status_code}")
        return body


@dataclass(frozen=True)
class _Redirect:
    location: str


def _belongs(ids: dict[str, str], query: MediaQuery) -> bool:
    """Un risultato che dichiara un altro tmdbid/imdbid non è questo
    contenuto (capita con le ricerche per titolo)."""
    if ids.get("tmdbid") and ids["tmdbid"] != str(query.tmdb_id):
        return False
    imdb = _imdb_digits(query.imdb_id)
    return not (imdb and ids.get("imdbid") and _imdb_digits(ids["imdbid"]) != imdb)


def _http_link(value: str | None) -> str | None:
    value = (value or "").strip()
    return value if urlsplit(value).scheme.lower() in ("http", "https") else None


def _imdb_digits(value: str | None) -> str | None:
    """L'id IMDb come lo vuole Torznab (solo cifre, zeri a sinistra fino a 7)."""
    match = _IMDB_RE.match((value or "").strip())
    return match.group(1).zfill(7) if match else None
