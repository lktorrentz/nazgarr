"""Ordinamenti degli episodi di una serie (decisioni dell'utente, 2026-10-04):
le stagioni di default di TMDB, i suoi gruppi di episodi (digitale, DVD,
assoluto...), l'ordine "aired" di TVDB letto da Sonarr e, come ultima
risorsa, gli ordini di TVDB dalla sua API. Lo stesso episodio può avere
numeri diversi in ognuno; i file seguono quello che ha scelto chi li ha
pubblicati.

Ogni ordinamento ha la stessa forma: stagioni di episodi, e ogni episodio
rimanda a uno o più episodi di riferimento, quelli delle stagioni di default
di TMDB. Molti a molti: un episodio "doppio" (due episodi trasmessi insieme,
con due titoli) rimanda a due episodi di riferimento, e il contrario. Così
si traduce da un ordinamento all'altro passando dai riferimenti.

Usato al primo punto di approvazione dell'upload: Nazgarr sceglie di default
l'ordinamento che combacia meglio con i file (a parità, quello dell'ultima
volta per la serie, poi TVDB aired, poi TMDB), sempre modificabile, e avvisa
solo se i file non seguono TVDB aired, l'ordine di Sonarr. Più gli episodi
trovati, tradotti in ognuno."""

import json
import logging
import re
import threading
import time
from collections import OrderedDict, defaultdict
from copy import deepcopy
from dataclasses import asdict, dataclass, field

import httpx
from sqlalchemy.orm import Session

from nazgarr.core import net_guard, settings_repo

logger = logging.getLogger(__name__)

Ref = tuple[int, int]  # (stagione, episodio) nelle stagioni di default di TMDB

TMDB_DEFAULT = "tmdb:default"
TVDB_AIRED_KEYS = ("sonarr:aired", "tvdb:official", "tvdb:default")
# I tipi di gruppo di episodi di TMDB (https://developer.themoviedb.org).
TMDB_GROUP_TYPES = {
    1: "Original air date", 2: "Absolute", 3: "DVD", 4: "Digital", 5: "Story arc", 6: "Production", 7: "TV",
}
CACHE_SECONDS = 3600
# Sonarr è locale e cambia più spesso (una serie aggiunta): una scadenza più corta.
SONARR_CACHE_SECONDS = 600
# Combacia "meglio" solo con un margine: piccole differenze non fanno avvisi.
WARNING_MARGIN = 0.05


@dataclass
class OrderEpisode:
    number: int
    titles: list[str] = field(default_factory=list)
    air_date: str | None = None
    refs: list[Ref] = field(default_factory=list)


@dataclass
class EpisodeOrder:
    key: str
    label: str
    source: str  # tmdb | sonarr | tvdb
    seasons: dict[int, list[OrderEpisode]] = field(default_factory=dict)

    def episodes(self) -> dict[Ref, OrderEpisode]:
        return {(season, ep.number): ep for season, eps in self.seasons.items() for ep in eps}

    def to_dict(self) -> dict:
        return {
            "key": self.key, "label": self.label, "source": self.source,
            "seasons": [
                {"season_number": s, "episodes": [{**asdict(e), "refs": [list(r) for r in e.refs]} for e in eps]}
                for s, eps in sorted(self.seasons.items())
            ],
        }

    @staticmethod
    def from_dict(data: dict) -> "EpisodeOrder":
        return EpisodeOrder(
            key=data["key"], label=data["label"], source=data["source"],
            seasons={
                s["season_number"]: [
                    OrderEpisode(number=e["number"], titles=list(e.get("titles") or []), air_date=e.get("air_date"),
                                 refs=[tuple(r) for r in e.get("refs") or []])
                    for e in s["episodes"]
                ]
                for s in data["seasons"]
            },
        )


# --- Traduzione ----------------------------------------------------------------


def translate(source: EpisodeOrder, target: EpisodeOrder, season: int, episode: int) -> list[Ref]:
    """(stagione, episodio) in `source` -> gli episodi di `target` con gli
    stessi riferimenti, in ordine. Vuoto se non si sa."""
    if source.key == target.key:
        return [(season, episode)]
    ep = source.episodes().get((season, episode))
    if ep is None or not ep.refs:
        return []
    wanted = set(ep.refs)
    return [ref for ref, other in sorted(target.episodes().items()) if wanted & set(other.refs)]


def align(episodes: list[tuple[int, OrderEpisode]], reference: list[tuple[int, OrderEpisode]]) -> None:
    """Riferimenti per un ordinamento che non ha gli id TMDB degli episodi
    (Sonarr, TVDB): stesso numero di episodi -> uno a uno; uno il doppio
    dell'altro -> a coppie (gli episodi accorpati); altrimenti per data di
    uscita. Le liste sono in ordine di uscita, gli speciali a parte."""
    for _season, ep in episodes:
        ep.refs = []
    if not episodes or not reference:
        return
    refs = [(season, ep.number) for season, ep in reference]
    n, m = len(episodes), len(refs)
    if n == m:
        for (_s, ep), ref in zip(episodes, refs, strict=True):
            ep.refs = [ref]
        return
    if m == 2 * n:
        for i, (_s, ep) in enumerate(episodes):
            ep.refs = refs[2 * i:2 * i + 2]
        return
    if n == 2 * m:
        for i, (_s, ep) in enumerate(episodes):
            ep.refs = [refs[i // 2]]
        return
    by_date: dict[str, list[Ref]] = defaultdict(list)
    for (season, ep), ref in zip(reference, refs, strict=True):
        if ep.air_date:
            by_date[ep.air_date].append(ref)
    mine: dict[str, list[OrderEpisode]] = defaultdict(list)
    for _s, ep in episodes:
        if ep.air_date:
            mine[ep.air_date].append(ep)
    for date, eps in mine.items():
        theirs = by_date.get(date) or []
        if not theirs:
            continue
        for i, ep in enumerate(eps):
            # Ripartiti in proporzione: due episodi lo stesso giorno contro
            # uno solo dall'altra parte vanno tutti e due su quello.
            start = i * len(theirs) // len(eps)
            end = max(start + 1, (i + 1) * len(theirs) // len(eps))
            ep.refs = theirs[start:end]


def _in_airing_order(order: EpisodeOrder, specials: bool) -> list[tuple[int, OrderEpisode]]:
    return [
        (season, ep)
        for season, eps in sorted(order.seasons.items()) if (season == 0) == specials
        for ep in sorted(eps, key=lambda e: e.number)
    ]


def align_to(order: EpisodeOrder, reference: EpisodeOrder) -> None:
    align(_in_airing_order(order, False), _in_airing_order(reference, False))
    align(_in_airing_order(order, True), _in_airing_order(reference, True))


# --- Fonti -------------------------------------------------------------------------

# Risposte di TMDB, TVDB e Sonarr per CACHE_SECONDS: la scheda di una serie,
# il match e il reseeding chiedono gli stessi ordinamenti più volte. Con un
# lock (thread diversi: API, worker, run) e un limite di voci.
_CACHE_MAX = 256
_cache: OrderedDict[tuple, tuple[float, object]] = OrderedDict()
_cache_lock = threading.Lock()


def _cached(key: tuple, fetch, copy: bool = False, ttl: float = CACHE_SECONDS):
    """copy: una copia a ogni lettura, per i valori che chi li riceve
    modifica (gli ordinamenti, che align_to riallinea): la voce in cache
    resta com'era."""
    with _cache_lock:
        hit = _cache.get(key)
        if hit and time.monotonic() - hit[0] < ttl:
            _cache.move_to_end(key)
            return deepcopy(hit[1]) if copy else hit[1]
    value = fetch()
    with _cache_lock:
        _cache[key] = (time.monotonic(), value)
        _cache.move_to_end(key)
        while len(_cache) > _CACHE_MAX:
            _cache.popitem(last=False)
    return deepcopy(value) if copy else value


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


def tmdb_orders(client, tmdb_id: int) -> list[EpisodeOrder]:
    """Le stagioni di default (con gli episodi: una chiamata ogni 20
    stagioni) e i gruppi di episodi, che portano i numeri di default."""
    def fetch() -> list[EpisodeOrder]:
        show = client.get(f"/tv/{tmdb_id}")
        numbers = [s["season_number"] for s in show.get("seasons") or []]
        default = EpisodeOrder(TMDB_DEFAULT, "TMDB", "tmdb")
        for start in range(0, len(numbers), 20):
            chunk = numbers[start:start + 20]
            body = client.get(f"/tv/{tmdb_id}", append_to_response=",".join(f"season/{n}" for n in chunk))
            for n in chunk:
                season = body.get(f"season/{n}") or {}
                default.seasons[n] = [
                    OrderEpisode(number=e["episode_number"], titles=[e.get("name") or ""],
                                 air_date=e.get("air_date"), refs=[(n, e["episode_number"])])
                    for e in season.get("episodes") or []
                ]
        orders = [default]
        for group in client.get(f"/tv/{tmdb_id}/episode_groups").get("results") or []:
            if not group.get("episode_count"):
                continue
            detail = client.get(f"/tv/episode_group/{group['id']}")
            order = EpisodeOrder(
                f"tmdb:group:{group['id']}",
                f"TMDB · {group.get('name') or TMDB_GROUP_TYPES.get(group.get('type'), 'Group')}", "tmdb",
            )
            parts = sorted(detail.get("groups") or [], key=lambda g: g.get("order", 0))
            for index, part in enumerate(parts):
                season = _group_season(part, index, parts)
                if season in order.seasons:
                    continue  # due gruppi con lo stesso numero: tiene il primo
                order.seasons[season] = [
                    OrderEpisode(number=pos + 1, titles=[e.get("name") or ""], air_date=e.get("air_date"),
                                 refs=[(e["season_number"], e["episode_number"])])
                    for pos, e in enumerate(sorted(part.get("episodes") or [], key=lambda e: e.get("order", 0)))
                ]
            if order.seasons:
                orders.append(order)
        return orders

    return _cached(("tmdb", tmdb_id), fetch, copy=True)


_SPECIALS = re.compile(r"\b(specials?|speciali|extras?)\b", re.IGNORECASE)
_NUMBER = re.compile(r"(\d+)")


def _group_season(part: dict, index: int, parts: list[dict]) -> int:
    """Il numero di stagione di un gruppo di un episode group TMDB: dal nome
    ("Season 1", "Stagione 2", "Part 3"; gli speciali sono la 0), altrimenti
    dalla posizione. Le posizioni partono spesso da 0 anche senza un gruppo
    di speciali: allora la prima è la stagione 1."""
    name = part.get("name") or ""
    if _SPECIALS.search(name):
        return 0
    number = _NUMBER.search(name)
    if number:
        return int(number.group(1))
    order = part.get("order", index)
    starts_at_zero = min((p.get("order", i) for i, p in enumerate(parts)), default=0) == 0
    has_specials = any(_SPECIALS.search(p.get("name") or "") for p in parts)
    return order + 1 if starts_at_zero and not has_specials else order


class TmdbApi:
    """GET verso TMDB con la chiave delle impostazioni."""

    def __init__(self, api_key: str, client: httpx.Client | None = None):
        self.api_key = api_key
        self._client = client or httpx.Client(base_url="https://api.themoviedb.org/3", timeout=15.0)

    def get(self, path: str, **params) -> dict:
        response = self._client.get(path, params={"api_key": self.api_key, **params})
        response.raise_for_status()
        return response.json()


def sonarr_order(session: Session, tmdb_id: int, tvdb_id: int | None, api_factory=None) -> EpisodeOrder | None:
    """L'ordine "aired" di TVDB come lo usa Sonarr, se la serie è lì."""
    from nazgarr.core.models import SonarrInstance
    from nazgarr.integrations.arr import ArrApi, close_api

    factory = api_factory or ArrApi
    for instance in session.query(SonarrInstance).filter_by(enabled=True).order_by(SonarrInstance.priority):
        api = None
        try:
            api = factory(instance)
            # Tutte le serie dell'istanza in una risposta: in cache per istanza
            # (prima si riscaricava a ogni scheda, match e serie del reseeding).
            where = (instance.id, instance.base_url)
            all_series = _cached(("sonarr_series", *where), lambda: api.get("/api/v3/series") or [],
                                 ttl=SONARR_CACHE_SECONDS)
            series = next(
                (s for s in all_series
                 if s.get("tmdbId") == tmdb_id or (tvdb_id and s.get("tvdbId") == tvdb_id)),
                None,
            )
            if series is None:
                continue
            order = EpisodeOrder("sonarr:aired", f"TVDB · Aired ({instance.label})", "sonarr")
            episodes = _cached(("sonarr_episodes", *where, series["id"]),
                               lambda: api.get("/api/v3/episode", seriesId=series["id"]) or [],
                               ttl=SONARR_CACHE_SECONDS)
            for e in episodes:
                if e.get("seasonNumber") is None or e.get("episodeNumber") is None:
                    continue
                order.seasons.setdefault(e["seasonNumber"], []).append(
                    OrderEpisode(number=e["episodeNumber"], titles=[e.get("title") or ""], air_date=e.get("airDate")))
            for eps in order.seasons.values():
                eps.sort(key=lambda ep: ep.number)
            return order
        except Exception:
            logger.warning("Episodi da %s non disponibili", instance.label, exc_info=True)
        finally:
            close_api(api)
    return None


TVDB_API = "https://api4.thetvdb.com/v4"
TVDB_TYPE_LABELS = {
    "official": "Aired", "default": "Aired", "dvd": "DVD", "absolute": "Absolute", "alternate": "Alternate",
    "regional": "Regional", "altdvd": "Alternate DVD", "alttwo": "Alternate 2",
}


class TvdbApi:
    """API v4 di TVDB con la chiave delle impostazioni (tvdb_api_key):
    l'ultima risorsa, quando Sonarr non ha la serie o nessun altro
    ordinamento combacia con i file."""

    def __init__(self, api_key: str, client: httpx.Client | None = None):
        self._client = client or httpx.Client(base_url=TVDB_API, timeout=20.0)
        self._api_key = api_key
        self._token: str | None = None

    def _login(self) -> None:
        net_guard.check_url(TVDB_API)
        response = self._client.post("/login", json={"apikey": self._api_key})
        response.raise_for_status()
        self._token = (response.json().get("data") or {}).get("token")

    def get(self, path: str, **params) -> dict:
        if self._token is None:
            self._login()
        response = self._client.get(path, params=params or None, headers={"Authorization": f"Bearer {self._token}"})
        response.raise_for_status()
        return response.json().get("data") or {}


def tvdb_orders(api: TvdbApi, tvdb_id: int) -> list[EpisodeOrder]:
    """Ogni tipo di stagione della serie su TVDB (aired, DVD, assoluto,
    alternativi), con i suoi episodi."""
    def fetch() -> list[EpisodeOrder]:
        extended = api.get(f"/series/{tvdb_id}/extended", short="true")
        types = {t.get("type"): t.get("name") for t in extended.get("seasonTypes") or [] if t.get("type")}
        orders = []
        for kind, name in types.items():
            order = EpisodeOrder(f"tvdb:{kind}", f"TVDB · {name or TVDB_TYPE_LABELS.get(kind, kind)}", "tvdb")
            page = 0
            while page is not None and page < 50:
                body = api.get(f"/series/{tvdb_id}/episodes/{kind}", page=page)
                for e in body.get("episodes") or []:
                    if e.get("seasonNumber") is None or e.get("number") is None:
                        continue
                    order.seasons.setdefault(e["seasonNumber"], []).append(
                        OrderEpisode(number=e["number"], titles=[e.get("name") or ""], air_date=e.get("aired")))
                page = page + 1 if (body.get("episodes") and len(body["episodes"]) >= 500) else None
            for eps in order.seasons.values():
                eps.sort(key=lambda ep: ep.number)
            if order.seasons:
                orders.append(order)
        return orders

    return _cached(("tvdb", tvdb_id), fetch, copy=True)


# --- Combacia con i file ------------------------------------------------------------


def fit(order: EpisodeOrder, found: dict[int, list[int]], pack: bool) -> dict:
    """Quanto i file (stagione -> episodi) combaciano con un ordinamento.
    Stagione per stagione e per numero, mai per posizione (uno speciale
    davanti non sposta niente).

    Per un pack o una libreria conta prima di tutto il numero di episodi di
    ogni stagione (decisione dell'utente, 2026-10-04): 13 file nella stagione
    1 combaciano con un ordinamento che ne ha 13, anche se i loro numeri
    vengono da un'altra numerazione (es. Sonarr in TVDB aired, con tre
    segmenti per file). Poi, meno, che i numeri dei file esistano.
    - coverage: quanti numeri dei file esistono nell'ordinamento;
    - score: 0.75 numero di episodi per stagione + 0.25 coverage (un pack o
      una libreria), solo coverage per un episodio singolo."""
    files = {(s, e) for s, eps in found.items() for e in eps}
    known = order.episodes()
    matched = len(files & set(known))
    coverage = matched / len(files) if files else 0.0
    score = coverage
    complete = 0
    if pack and files:
        counts = 0.0
        for season, eps in found.items():
            expected = len(order.seasons.get(season) or [])
            ratio = min(len(eps), expected) / max(len(eps), expected) if expected else 0.0
            counts += ratio * len(eps)
            complete += ratio >= 1.0
        score = 0.75 * (counts / len(files)) + 0.25 * coverage
    return {"score": round(score, 3), "coverage": round(coverage, 3), "matched": matched, "files": len(files),
            "complete_seasons": complete}


def collect(session: Session, tmdb_id: int, tvdb_id: int | None, found: dict[int, list[int]], pack: bool,
            tmdb_api=None, tvdb_api=None, sonarr_factory=None, status: dict | None = None) -> list[EpisodeOrder]:
    """Gli ordinamenti di una serie, già allineati alle stagioni di TMDB:
    TMDB (default e gruppi), Sonarr e, come ultima risorsa, TVDB. status, se
    dato, dice com'è andata ogni fonte (per capire perché un ordinamento manca)."""
    status = status if status is not None else {}
    orders: list[EpisodeOrder] = []
    tmdb_key = settings_repo.get_setting(session, "tmdb_api_key")
    if tmdb_api is None and tmdb_key:
        tmdb_api = TmdbApi(tmdb_key)
    if tmdb_api is None:
        status["tmdb"] = "no_key"
    else:
        try:
            orders += tmdb_orders(tmdb_api, tmdb_id)
            status["tmdb"] = "ok"
        except Exception as exc:
            logger.warning("Ordinamenti TMDB non disponibili per %s", tmdb_id, exc_info=True)
            status["tmdb"] = f"error: {type(exc).__name__}: {exc}"[:200]
    default = next((o for o in orders if o.key == TMDB_DEFAULT), None)
    sonarr = sonarr_order(session, tmdb_id, tvdb_id, sonarr_factory)
    status["sonarr"] = "ok" if sonarr is not None else "not_found"
    if sonarr is not None:
        orders.append(sonarr)

    # TVDB, l'ultima risorsa: se Sonarr non ha la serie, o se i file non
    # seguono il suo ordine (un altro ordine TVDB, es. "Joined", li coprirebbe).
    if not tvdb_id:
        status["tvdb"] = "no_tvdb_id"
    elif not (sonarr is None or (found and fit(sonarr, found, pack)["score"] < 1.0)):
        status["tvdb"] = "not_needed"
    else:
        key = settings_repo.get_setting(session, "tvdb_api_key")
        if tvdb_api is None and key:
            tvdb_api = TvdbApi(key)
        if tvdb_api is None:
            status["tvdb"] = "no_key"
        else:
            try:
                fetched = tvdb_orders(tvdb_api, tvdb_id)
                orders += [o for o in fetched
                           if not (sonarr is not None and o.key in ("tvdb:official", "tvdb:default"))]
                status["tvdb"] = "ok" if fetched else "empty"
            except Exception as exc:
                logger.warning("Ordinamenti TVDB non disponibili per %s", tvdb_id, exc_info=True)
                status["tvdb"] = f"error: {type(exc).__name__}: {exc}"[:200]
    if default is not None:
        for order in orders:
            if order.source in ("sonarr", "tvdb"):
                align_to(order, default)
    return orders


def best_fit(orders: list[EpisodeOrder], found: dict[int, list[int]], pack: bool,
             preferred: str | None = None, by: str = "score") -> EpisodeOrder | None:
    """Quello che combacia meglio; a parità la scelta della serie, poi TVDB
    aired, poi TMDB. Senza episodi, la stessa priorità. by="coverage": quello
    in cui esistono i numeri dei file (la loro numerazione, per tradurli)."""
    keys = [o.key for o in orders]
    tvdb_aired = next((k for k in TVDB_AIRED_KEYS if k in keys), None)
    # Priorità a TVDB (decisione dell'utente, 2026-10-04): aired, poi gli altri ordini TVDB, poi TMDB.
    other_tvdb = [o.key for o in orders if o.source == "tvdb" and o.key != tvdb_aired]
    rank = list(dict.fromkeys(k for k in (preferred, tvdb_aired, *other_tvdb, TMDB_DEFAULT, *keys) if k and k in keys))
    if not orders:
        return None
    if not found:
        return next(o for o in orders if o.key == rank[0])
    fits = {o.key: fit(o, found, pack) for o in orders}
    # Per la numerazione, a pari numeri esistenti decide la forma delle stagioni.
    return max(orders, key=lambda o: (fits[o.key][by], fits[o.key]["score"], -rank.index(o.key)))


def build(session: Session, tmdb_id: int, tvdb_id: int | None, found: dict[int, list[int]], pack: bool,
          tmdb_api=None, tvdb_api=None, sonarr_factory=None) -> dict:
    """Gli ordinamenti di una serie per il primo punto di approvazione."""
    from nazgarr.core.models import EpisodeOrderPreference

    status: dict = {}
    orders = collect(session, tmdb_id, tvdb_id, found, pack, tmdb_api, tvdb_api, sonarr_factory, status)
    fits = {o.key: fit(o, found, pack) for o in orders}
    keys = [o.key for o in orders]
    preference = session.get(EpisodeOrderPreference, tmdb_id)
    preferred = preference.order_key if preference is not None and preference.order_key in keys else None
    tvdb_aired = next((k for k in TVDB_AIRED_KEYS if k in keys), None)
    # Di default quello che combacia meglio con i file (decisione dell'utente, 2026-10-04).
    chosen = best_fit(orders, found, pack, preferred)
    recommended = chosen.key if chosen is not None else None
    # L'avviso solo se i file non seguono TVDB aired, l'ordine di Sonarr, e
    # solo se TVDB aired si conosce (Sonarr o la chiave TVDB).
    warning = None
    if found and recommended and tvdb_aired and recommended != tvdb_aired \
            and fits[recommended]["score"] > fits[tvdb_aired]["score"] + WARNING_MARGIN:
        warning = {"code": "files_not_tvdb_aired", "order": recommended, "tvdb": tvdb_aired}
    # La numerazione dei numeri dei file (per tradurli): quella in cui
    # esistono, che può non essere quella proposta (es. Sonarr in TVDB aired
    # con tre segmenti per file, e i file che sono gli episodi di un'altra).
    numbering = best_fit(orders, found, pack, preferred, by="coverage") if found else chosen
    files_order = numbering.key if numbering is not None else recommended
    by_key = {o.key: o for o in orders}
    return {
        "sources": status,
        "orders": [o.to_dict() for o in orders],
        "recommended": recommended,
        "files_order": files_order,
        "fits": fits,
        "warning": warning,
        # Gli episodi dei file, tradotti in ogni ordinamento: per i conteggi.
        "found": {
            key: translate_found(by_key[files_order], by_key[key], found) if files_order else {}
            for key in keys
        },
    }


def translate_found(source: EpisodeOrder, target: EpisodeOrder, found: dict[int, list[int]]) -> dict[int, list[int]]:
    out: dict[int, set[int]] = defaultdict(set)
    for season, eps in found.items():
        for e in eps:
            for s2, e2 in translate(source, target, season, e):
                out[s2].add(e2)
    return {s: sorted(eps) for s, eps in sorted(out.items())}


# --- Upload ------------------------------------------------------------------------


def found_in_layout(layout_json: str | None) -> dict[int, list[int]]:
    """Gli episodi trovati nei file (nazgarr/upload/source.py), stagione -> episodi."""
    layout = json.loads(layout_json or "{}")
    return {int(season): sorted(eps) for season, eps in (layout.get("episodes_by_season") or {}).items()}


def tvdb_id_for(session: Session, tmdb_id: int, tmdb_api=None) -> int | None:
    """L'id TVDB di una serie TMDB, dai suoi id esterni."""
    key = settings_repo.get_setting(session, "tmdb_api_key")
    api = tmdb_api or (TmdbApi(key) if key else None)
    if api is None:
        return None
    try:
        return _cached(("tvdb_id", tmdb_id), lambda: api.get(f"/tv/{tmdb_id}/external_ids").get("tvdb_id"))
    except httpx.HTTPError:
        logger.warning("Id esterni TMDB non disponibili per %s", tmdb_id, exc_info=True)
        return None


def for_job(session: Session, job, tmdb_id: int, tvdb_id: int | None = None, **sources) -> dict:
    """Gli ordinamenti per il match di un job: con gli episodi dei suoi file."""
    forced = json.loads(job.forced_ids_json or "{}").get("tvdb")
    tvdb_id = tvdb_id or (int(forced) if forced else None) or tvdb_id_for(session, tmdb_id, sources.get("tmdb_api"))
    pack = (job.kind or "") in ("season_pack", "complete_pack")
    return build(session, tmdb_id, tvdb_id, found_in_layout(job.layout_json), pack, **sources)


class EpisodeOrderError(ValueError):
    pass


def snapshot(result: dict, order_key: str) -> dict:
    """L'ordinamento scelto e quello dei file, salvati sul job: servono a
    tradurre i numeri degli episodi dei file (nei nomi generati)."""
    by_key = {o["key"]: o for o in result["orders"]}
    if order_key not in by_key:
        raise EpisodeOrderError(order_key)
    files = result.get("files_order") or order_key
    return {"chosen": by_key[order_key], "files": by_key.get(files, by_key[order_key])}


def numbers_in(result: dict, order_key: str, kind: str | None, seasons: list[int],
               episode: int | None) -> tuple[list[int], int | None]:
    """Stagioni ed episodio di un job nella numerazione di un ordinamento:
    gli episodi dei file tradotti (result["found"], da build). Come propone
    il match a mano (frontend MatchStep). Senza episodi trovati restano
    quelli dei file."""
    found = (result.get("found") or {}).get(order_key) or {}
    if not found:
        return seasons, episode
    translated = sorted(int(s) for s in found)
    if kind == "episode":
        first = found.get(translated[0]) or found.get(str(translated[0])) or []
        return translated, (first[0] if first else episode)
    return translated, episode


def remember(session: Session, tmdb_id: int, order_key: str) -> None:
    """La scelta diventa la proposta per la stessa serie la prossima volta."""
    from nazgarr.core.db_utils import bulk_upsert
    from nazgarr.core.models import EpisodeOrderPreference

    bulk_upsert(session, EpisodeOrderPreference.__table__, [{"tmdb_id": tmdb_id, "order_key": order_key}],
                conflict_cols=["tmdb_id"], update_cols=["order_key"])


def translate_files(job, season: int, episode: int) -> list[Ref]:
    """Un episodio come lo numerano i file -> come lo numera l'ordinamento
    scelto al match. Senza ordinamento scelto (o senza traduzione) resta com'è."""
    data = json.loads(job.episode_order_json or "{}")
    if not data.get("chosen") or not data.get("files"):
        return [(season, episode)]
    chosen, files = EpisodeOrder.from_dict(data["chosen"]), EpisodeOrder.from_dict(data["files"])
    return translate(files, chosen, season, episode) or [(season, episode)]


def group(episodes) -> dict[int, list[int]]:
    """(stagione, episodio) -> stagione: [episodi]."""
    out: dict[int, set[int]] = defaultdict(set)
    for season, episode in episodes:
        out[season].add(episode)
    return {s: sorted(e) for s, e in out.items()}


class Translator:
    """Per il motore di matching (reseeding): i numeri degli episodi di un
    torrent nella numerazione della libreria, quando i due seguono
    ordinamenti diversi. Gli ordinamenti di ogni serie si raccolgono una volta
    per run (e restano in cache per un'ora)."""

    def __init__(self, session: Session, **sources):
        self.session = session
        self.sources = sources
        self._orders: dict[int, list[EpisodeOrder]] = {}

    def orders(self, tmdb_id: int, library: set[Ref] | None = None) -> list[EpisodeOrder]:
        if tmdb_id not in self._orders:
            try:
                tvdb_id = tvdb_id_for(self.session, tmdb_id, self.sources.get("tmdb_api"))
                # Con gli episodi della libreria: se niente li copre del tutto, anche TVDB.
                self._orders[tmdb_id] = collect(self.session, tmdb_id, tvdb_id, group(library or set()), True,
                                                **self.sources)
            except Exception:
                logger.warning("Ordinamenti degli episodi non disponibili per %s", tmdb_id, exc_info=True)
                self._orders[tmdb_id] = []
        return self._orders[tmdb_id]

    def mapping(self, tmdb_id: int, torrent: set[Ref], library: set[Ref]) -> dict[Ref, Ref]:
        """Episodio del torrent -> episodio della libreria. Solo traduzioni uno
        a uno che finiscono su un episodio presente: un file con due episodi
        accorpati non diventa mai un file solo della libreria."""
        orders = self.orders(tmdb_id, library)
        if not orders or not torrent or not library:
            return {}
        source = best_fit(orders, group(torrent), pack=True)
        # La libreria: i numeri registrati (spesso di Sonarr), non la forma delle stagioni.
        target = best_fit(orders, group(library), pack=True, by="coverage")
        if source is None or target is None or source.key == target.key:
            return {}
        out = {}
        for season, episode in torrent:
            translated = translate(source, target, season, episode)
            if len(translated) == 1 and translated[0] in library:
                out[(season, episode)] = translated[0]
        return out

