"""Il sito UNIT3D con la sessione web dell'utente: il claim della
resurrection (decisione dell'utente, 2026-11-10) e il controllo della
sessione. Le pagine imitano il markup di UNIT3D
(resources/views/torrent/partials/buttons.blade.php)."""

import httpx
import pytest

from nazgarr.adapters.tracker import unit3d_web
from nazgarr.adapters.tracker.base import Unit3dTrackerAdapter

BASE = "https://tracker.example"
COOKIE = "remember_web_59ba=abc%3D; unit3d_session=old"


def _claim_page(torrent_id="7", action=f"{BASE}/users/me/resurrections", token="tok-1"):
    return f"""<html><head><meta name="csrf-token" content="{token}"></head><body>
    <form class="dialog__form" method="POST" action="{BASE}/users/me/resurrections/12">
        <input type="hidden" name="_token" value="{token}" autocomplete="off">
        <input type="hidden" name="_method" value="DELETE">
    </form>
    <li x-data="dialog"><button x-bind="showDialog">Resurrect</button>
      <dialog><form class="dialog__form" method="POST" action="{action}" x-bind="dialogForm">
        <input type="hidden" name="_token" value="{token}" autocomplete="off">
        <input type="hidden" name="torrent_id" value="{torrent_id}" />
        <button class="form__button form__button--filled">Resurrect</button>
      </form></dialog></li></body></html>"""


PENDING_PAGE = """<html><head><meta name="csrf-token" content="tok-2"></head><body>
<button class="form__button" disabled>pending</button></body></html>"""
LOGIN_PAGE = """<html><body><form method="POST" action="/login">
<input name="username"><input type="password" name="password"></form></body></html>"""


def _adapter(handler, cookie=COOKIE):
    client = httpx.Client(transport=httpx.MockTransport(handler), base_url=BASE)
    return Unit3dTrackerAdapter(base_url=BASE, api_token="api-tok", http_client=client, session_cookie=cookie)


class _Site:
    """Il sito finto: la pagina del torrent con o senza il form, il POST del claim."""

    def __init__(self, before, after=PENDING_PAGE, post_status=302, post_location=f"{BASE}/torrents/7"):
        self.before, self.after = before, after
        self.post_status, self.post_location = post_status, post_location
        self.requests: list[httpx.Request] = []
        self.posted = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.method == "POST":
            self.posted = True
            return httpx.Response(self.post_status,
                                  headers={"location": self.post_location} if self.post_location else {})
        page = self.after if self.posted else self.before
        if isinstance(page, httpx.Response):
            return page
        return httpx.Response(200, text=page, headers={
            "content-type": "text/html; charset=utf-8", "set-cookie": "unit3d_session=fresh; Path=/; Secure"})


def test_a_claim_sends_the_form_of_the_torrent_page_and_reads_the_outcome_after():
    site = _Site(_claim_page())

    result = _adapter(site).claim_resurrection("7")

    assert result.status == "claimed"
    get, post, again = site.requests
    assert (get.method, get.url.path, post.method, post.url.path, again.url.path) == (
        "GET", "/torrents/7", "POST", "/users/me/resurrections", "/torrents/7")
    assert dict(httpx.QueryParams(post.content.decode())) == {"_token": "tok-1", "torrent_id": "7"}
    # Il cookie incollato, con la sessione nuova che il sito ha appena rimandato.
    assert post.headers["cookie"] == "remember_web_59ba=abc%3D; unit3d_session=fresh"
    assert all("authorization" not in r.headers for r in site.requests)  # mai il token dell'API sul sito


def test_a_page_without_the_form_does_not_offer_the_claim():
    site = _Site(PENDING_PAGE)

    assert _adapter(site).claim_resurrection("7").status == "not_offered"
    assert not site.posted


def test_a_redirect_to_the_login_means_the_session_expired():
    site = _Site(httpx.Response(302, headers={"location": f"{BASE}/login"}))

    assert _adapter(site).claim_resurrection("7").status == "session_expired"
    assert _adapter(_Site(LOGIN_PAGE)).claim_resurrection("7").status == "session_expired"


def test_a_form_still_offered_after_the_post_was_refused():
    """UNIT3D rifiuta il claim di un proprio upload rimandando alla pagina."""
    site = _Site(_claim_page(), after=_claim_page(token="tok-3"))

    result = _adapter(site).claim_resurrection("7")

    assert result.status == "refused" and "own uploads" in result.detail


def test_a_rejected_form_token_is_a_failure():
    site = _Site(_claim_page(), post_status=419, post_location=None)

    result = _adapter(site).claim_resurrection("7")

    assert result.status == "failed" and "419" in result.detail


def test_a_form_that_points_elsewhere_is_never_sent():
    site = _Site(_claim_page(action="https://evil.example/users/me/resurrections"))

    result = _adapter(site).claim_resurrection("7")

    assert result.status == "failed" and "outside the tracker" in result.detail
    assert not site.posted


def test_an_older_unit3d_graveyard_form_is_found_too():
    page = f"""<form method="POST" action="{BASE}/graveyard/7">
        <input type="hidden" name="_token" value="t"><input type="hidden" name="seedtime" value="2592000"></form>"""

    form = unit3d_web.resurrection_form(unit3d_web.parse_page(page), "7")

    assert form is not None and form.fields == {"_token": "t", "seedtime": "2592000"}


def test_the_token_comes_from_the_page_meta_when_the_form_has_none():
    page = f"""<meta name="csrf-token" content="meta-tok"><form method="post" action="{BASE}/users/me/resurrections">
        <input type="hidden" name="torrent_id" value="7"></form>"""

    form = unit3d_web.resurrection_form(unit3d_web.parse_page(page), "7")

    assert form.fields == {"torrent_id": "7", "_token": "meta-tok"}


def test_the_form_of_another_torrent_is_not_this_ones():
    assert unit3d_web.resurrection_form(unit3d_web.parse_page(_claim_page(torrent_id="8")), "7") is None


def test_without_a_cookie_nothing_is_asked_to_the_site():
    site = _Site(_claim_page())
    adapter = _adapter(site, cookie=None)

    assert adapter.claim_resurrection("7").status == "no_session"
    assert adapter.check_web_session().status == "no_session"
    assert site.requests == []


@pytest.mark.parametrize(("page", "status"), [
    ("<html><body>home</body></html>", "ok"),
    (LOGIN_PAGE, "session_expired"),
    (httpx.Response(302, headers={"location": f"{BASE}/login"}), "session_expired"),
    (httpx.Response(503, text="Just a moment..."), "failed"),
])
def test_the_session_check_reads_the_home_page(page, status):
    site = _Site(page)

    result = _adapter(site).check_web_session()

    assert result.status == status
    assert [r.method for r in site.requests] == ["GET"]


@pytest.mark.parametrize("raw", [
    "Cookie: a=1; b=2", "a=1; b=2", "  a=1;b=2;  ",
])
def test_a_cookie_line_from_the_browser_is_cleaned(raw):
    assert unit3d_web.normalize_cookie(raw) == "a=1; b=2"


@pytest.mark.parametrize("raw", ["", "just-a-value", "a=1\r\nX-Injected: 1", "a=has space", "a b=1"])
def test_a_cookie_line_that_could_break_the_header_is_refused(raw):
    with pytest.raises(unit3d_web.InvalidCookie):
        unit3d_web.normalize_cookie(raw)
