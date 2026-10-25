from xml.sax.saxutils import escape

import httpx
import pytest

from nazgarr.adapters.tracker.base import MediaQuery, TrackerRateLimitedError, UploadError
from nazgarr.adapters.tracker.torznab import TorznabTrackerAdapter, parse_caps
from nazgarr.core.models import Tracker
from nazgarr.integrations import adapter_factory
from nazgarr.reseed import download_links

ENDPOINT = "http://prowlarr.example:9696/1/api"

CAPS = b"""<?xml version="1.0" encoding="UTF-8"?>
<caps>
  <server title="Prowlarr"/>
  <limits default="2" max="2"/>
  <searching>
    <search available="yes" supportedParams="q"/>
    <tv-search available="yes" supportedParams="q,season,ep,imdbid,tvdbid,tmdbid"/>
    <movie-search available="yes" supportedParams="q,imdbid,tmdbid"/>
    <audio-search available="no" supportedParams="q"/>
  </searching>
  <categories>
    <category id="2000" name="Movies"><subcat id="2040" name="Movies/HD"/></category>
    <category id="5000" name="TV"/>
  </categories>
</caps>"""

CAPS_TITLE_ONLY = b"""<caps><limits max="100"/><searching>
  <search available="yes" supportedParams="q"/>
  <movie-search available="yes" supportedParams="q,year"/>
</searching></caps>"""


def _item(guid, title, size, link=None, **attrs) -> str:
    link = link if link is not None else f"{ENDPOINT.rsplit('/api', 1)[0]}/download?apikey=key123&link={guid}"
    extra = "".join(f'<torznab:attr name="{k}" value="{v}"/>' for k, v in attrs.items())
    return (f"<item><title>{title}</title><guid>{escape(guid)}</guid><link>{escape(link)}</link><size>{size}</size>"
            f'<enclosure url="{escape(link)}" length="{size}" type="application/x-bittorrent"/>{extra}</item>')


def _feed(*items: str) -> bytes:
    return ('<?xml version="1.0"?><rss version="2.0" xmlns:torznab="http://torznab.com/schemas/2015/feed">'
            f"<channel><title>x</title>{''.join(items)}</channel></rss>").encode()


def _adapter(handler, **kwargs):
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return TorznabTrackerAdapter(base_url=ENDPOINT, api_key="key123", http_client=client, **kwargs)


def _router(searches, caps=CAPS, requests=None):
    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        if requests is not None:
            requests.append(params)
        assert params.pop("apikey") == "key123"
        if params["t"] == "caps":
            return httpx.Response(200, content=caps)
        return httpx.Response(200, content=searches(params))
    return handler


def test_caps_are_parsed():
    caps = parse_caps(CAPS)
    assert set(caps.modes) == {"search", "tv-search", "movie-search"}  # audio non disponibile
    assert caps.supports("movie-search", "tmdbid") and not caps.supports("search", "tmdbid")
    assert caps.limit_max == 2 and caps.categories == frozenset({2000, 5000})


def test_a_movie_is_searched_by_tmdbid_and_results_become_candidates():
    requests = []

    def searches(params):
        assert params["t"] == "movie" and params["tmdbid"] == "157336" and params["cat"] == "2000"
        return _feed(_item("g1", "Interstellar 2014 1080p", 1000, files=3,
                           infohash="ABCDEF0123456789ABCDEF0123456789ABCDEF01"))

    adapter = _adapter(_router(searches, requests=requests))
    (c,) = adapter.search_media(MediaQuery(tmdb_id=157336, content_type="movie", title="Interstellar"))

    assert (c.torrent_id_remote, c.name, c.size_bytes, c.file_count) == ("g1", "Interstellar 2014 1080p", 1000, 3)
    assert c.info_hash == "abcdef0123456789abcdef0123456789abcdef01"
    assert c.file_list is None and c.download_link.endswith("link=g1")
    assert [r["t"] for r in requests] == ["caps", "movie"]  # nessun ripiego: l'id ha trovato


def test_without_results_by_id_it_falls_back_to_imdb_then_title():
    seen = []

    def searches(params):
        seen.append({k: v for k, v in params.items() if k in ("tmdbid", "imdbid", "q", "season")})
        return _feed(_item("g", "Show S02 1080p", 5000)) if "q" in params else _feed()

    adapter = _adapter(_router(searches))
    results = adapter.search_media(MediaQuery(tmdb_id=1399, content_type="tv", imdb_id="tt0944947",
                                              title="Show", season=2))

    assert [c.torrent_id_remote for c in results] == ["g"]
    assert seen == [{"tmdbid": "1399", "season": "2"}, {"imdbid": "0944947", "season": "2"},
                    {"q": "Show", "season": "2"}]


def test_title_only_endpoint_uses_q_and_year():
    def searches(params):
        assert "tmdbid" not in params and params["q"] == "Interstellar" and params["year"] == "2014"
        return _feed()

    adapter = _adapter(_router(searches, caps=CAPS_TITLE_ONLY))
    assert adapter.search_media(MediaQuery(tmdb_id=1, content_type="movie", title="Interstellar", year=2014)) == []


def test_nothing_to_search_with_makes_no_search_request():
    requests = []
    adapter = _adapter(_router(lambda p: pytest.fail("cercato"), caps=CAPS_TITLE_ONLY, requests=requests))
    assert adapter.search_by_tmdb(1) == []
    assert [r["t"] for r in requests] == ["caps"]


def test_results_declaring_another_id_are_dropped():
    def searches(params):
        return _feed(_item("ok", "Movie", 10, tmdbid=1), _item("other", "Movie", 10, tmdbid=2),
                     _item("imdb", "Movie", 10, imdbid="tt9999999"), _item("plain", "Movie", 10))

    adapter = _adapter(_router(searches))
    results = adapter.search_media(MediaQuery(tmdb_id=1, content_type="movie", imdb_id="tt0000001"))
    assert [c.torrent_id_remote for c in results] == ["ok", "plain"]


def test_magnet_only_and_sizeless_results_are_skipped():
    def searches(params):
        return _feed(_item("magnet", "M", 10, link="magnet:?xt=urn:btih:abc"), _item("nosize", "M", 0),
                     _item("good", "M", 10))

    adapter = _adapter(_router(searches))
    assert [c.torrent_id_remote for c in adapter.search_by_tmdb(1)] == ["good"]


def test_results_are_paged_with_offset_up_to_the_limit():
    offsets = []

    def searches(params):
        offset = int(params["offset"])
        offsets.append(offset)
        assert params["limit"] == "2"
        return _feed(*[_item(f"g{i}", "M", 10) for i in range(offset, min(offset + 2, 5))])

    adapter = _adapter(_router(searches))
    results = adapter.search_by_tmdb(1)
    assert [c.torrent_id_remote for c in results] == ["g0", "g1", "g2", "g3", "g4"]
    assert offsets == [0, 2, 4] and not results.truncated


def test_an_endpoint_ignoring_offset_does_not_loop():
    adapter = _adapter(_router(lambda params: _feed(_item("a", "M", 10), _item("b", "M", 10))))
    assert [c.torrent_id_remote for c in adapter.search_by_tmdb(1)] == ["a", "b"]


def test_searches_and_caps_are_cached():
    requests = []
    adapter = _adapter(_router(lambda params: _feed(), requests=requests))
    adapter.search_by_tmdb(1)
    adapter.search_by_tmdb(1)
    adapter.search_by_tmdb(2)
    # Senza tipo: tmdbid nei modi film e serie, una sola volta per tmdb_id.
    assert [r["t"] for r in requests] == ["caps", "movie", "tvsearch", "movie", "tvsearch"]


def test_torznab_errors_are_reported():
    def handler(request):
        return httpx.Response(200, content=b'<error code="100" description="Invalid API Key"/>')

    with pytest.raises(UploadError, match="Invalid API Key"):
        _adapter(handler).search_by_tmdb(1)


def test_request_limit_error_stops_the_tracker():
    def handler(request):
        return httpx.Response(429, content=b'<error code="500" description="Request limit reached"/>',
                              headers={"Retry-After": "1"})

    with pytest.raises(TrackerRateLimitedError):
        _adapter(handler, sleep=lambda s: None).search_by_tmdb(1)


def test_http_error_with_torznab_error_body():
    def handler(request):
        return httpx.Response(401, content=b'<error code="100" description="Unauthorized"/>')

    with pytest.raises(UploadError, match="Unauthorized"):
        _adapter(handler).search_by_tmdb(1)


def test_a_document_with_a_dtd_is_refused():
    def handler(request):
        return httpx.Response(200, content=b'<!DOCTYPE caps [<!ENTITY a "aaaa">]><caps>&a;</caps>')

    with pytest.raises(UploadError, match="DTD"):
        _adapter(handler).caps()


def test_download_follows_a_redirect_from_the_endpoint():
    def handler(request):
        if request.url.host == "prowlarr.example":
            return httpx.Response(301, headers={"location": "https://tracker.example/dl/1.torrent"})
        return httpx.Response(200, content=b"d4:infod4:name1:xee")

    assert _adapter(handler).download_torrent(f"{ENDPOINT}/../download?apikey=key123") == b"d4:infod4:name1:xee"


def test_download_refuses_links_outside_the_endpoint_magnets_and_non_torrents():
    def handler(request):
        if request.url.path.endswith("magnet"):
            return httpx.Response(302, headers={"location": "magnet:?xt=urn:btih:abc"})
        return httpx.Response(200, content=b"<html>login</html>")

    adapter = _adapter(handler)
    with pytest.raises(UploadError, match="outside"):
        adapter.download_torrent("https://elsewhere.example/x.torrent")
    with pytest.raises(UploadError, match="magnet"):
        adapter.download_torrent("http://prowlarr.example:9696/1/magnet")
    with pytest.raises(UploadError, match=r"not a \.torrent"):
        adapter.download_torrent("http://prowlarr.example:9696/1/download")


def test_the_api_key_never_stays_in_the_stored_link():
    adapter = _adapter(lambda request: httpx.Response(200))
    link = "http://prowlarr.example:9696/1/download?apikey=key123&link=abc&file=Movie"

    stored = download_links.stored_link(adapter, link)
    assert "key123" not in stored

    class _Candidate:
        download_link = stored

    adapter.api_key = "newkey"  # cambiata nel frattempo: vale quella attuale
    assert download_links.usable_link(adapter, _Candidate()) == link.replace("key123", "newkey")


def test_a_guid_containing_the_api_key_is_hashed():
    def searches(params):
        return _feed(_item("http://jackett.example/dl/?jackett_apikey=key123&path=x", "M", 10))

    (c,) = _adapter(_router(searches)).search_by_tmdb(1)
    assert "key123" not in c.torrent_id_remote and c.torrent_id_remote.startswith("sha256:")


def test_the_factory_builds_a_torznab_adapter():
    row = Tracker(label="p", adapter_type="torznab", base_url=ENDPOINT, api_token="key123", rate_limit_per_min=10)
    with adapter_factory.tracker(row) as adapter:
        assert isinstance(adapter, TorznabTrackerAdapter)
        assert adapter.api_key == "key123" and adapter._rate_limiter.max_per_min == 10
