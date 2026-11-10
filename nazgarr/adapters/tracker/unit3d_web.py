"""Il sito di un tracker UNIT3D, non la sua API: quel poco che Nazgarr ci
fa con la sessione web dell'utente (il cookie del login, cifrato in
tracker.history_session_cookie).

Oggi solo il claim della resurrection (decisione dell'utente, 2026-11-10):
UNIT3D non lo espone nell'API, è il form della pagina del torrent
(`POST /users/{username}/resurrections` con `torrent_id`, protetto dal
token CSRF della sessione). Il form si legge dalla pagina invece di
scriverne l'indirizzo: le versioni prima del 2023 lo mandavano a
/graveyard/{id}, e la pagina lo mostra solo quando il tracker accetta il
claim (0 seeder, più di 30 giorni, nessun claim in attesa).

Solo funzioni pure sull'HTML e sul cookie: le richieste le fa l'adapter
(nazgarr/adapters/tracker/base.py), con il suo rate limit e net_guard.
"""

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urlsplit

# Il nome di un cookie (RFC 6265, token) e la lunghezza massima di quello
# salvato: una riga Cookie intera di un browser sta ben sotto.
_COOKIE_NAME_RE = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")
MAX_COOKIE_LENGTH = 8192


class InvalidCookie(ValueError):
    """Il testo incollato non è una riga Cookie (nome=valore; nome=valore)."""


def parse_cookie_header(raw: str) -> dict[str, str]:
    """I cookie di una riga Cookie copiata dal browser, con o senza
    "Cookie:" davanti. Rifiuta caratteri di controllo e spazi dentro i
    valori: finirebbero in un header HTTP."""
    text = (raw or "").strip()
    if text.lower().startswith("cookie:"):
        text = text[len("cookie:"):].strip()
    if not text or len(text) > MAX_COOKIE_LENGTH:
        raise InvalidCookie("empty or too long")
    cookies: dict[str, str] = {}
    for part in text.split(";"):
        part = part.strip()
        if not part:
            continue
        name, sep, value = part.partition("=")
        name, value = name.strip(), value.strip()
        if not sep or not _COOKIE_NAME_RE.fullmatch(name):
            raise InvalidCookie(f"not a name=value pair: {name[:40]!r}")
        if any(not 0x21 <= ord(c) <= 0x7E for c in value):
            raise InvalidCookie(f"the value of {name} has characters a cookie cannot have")
        cookies[name] = value
    if not cookies:
        raise InvalidCookie("no cookie")
    return cookies


def cookie_header(cookies: dict[str, str]) -> str:
    return "; ".join(f"{name}={value}" for name, value in cookies.items())


def normalize_cookie(raw: str) -> str:
    """Quello che si salva: la riga Cookie pulita. InvalidCookie se non lo è."""
    return cookie_header(parse_cookie_header(raw))


@dataclass
class Form:
    action: str
    method: str
    # Nome, valore e tipo di ogni <input> del form, nell'ordine della pagina.
    inputs: list[tuple[str, str, str]] = field(default_factory=list)

    def hidden(self) -> dict[str, str]:
        return {name: value for name, value, kind in self.inputs if name and kind == "hidden"}

    def has_password(self) -> bool:
        return any(kind == "password" for _name, _value, kind in self.inputs)


class _PageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.forms: list[Form] = []
        self.csrf_meta: str | None = None
        self._current: Form | None = None

    def handle_starttag(self, tag, attrs):
        values = {name.lower(): (value or "") for name, value in attrs}
        if tag == "form":
            self._current = Form(action=values.get("action", ""), method=(values.get("method") or "get").lower())
            self.forms.append(self._current)
        elif tag == "input" and self._current is not None:
            self._current.inputs.append((values.get("name", ""), values.get("value", ""),
                                         (values.get("type") or "text").lower()))
        elif tag == "meta" and values.get("name") == "csrf-token":
            self.csrf_meta = values.get("content") or None

    def handle_endtag(self, tag):
        if tag == "form":
            self._current = None


@dataclass
class Page:
    forms: list[Form]
    csrf_meta: str | None

    def is_login(self) -> bool:
        """La pagina chiede la password: la sessione non vale (un tema che
        mostra il login senza rimandare a /login)."""
        return any(form.has_password() for form in self.forms)


def parse_page(html: str) -> Page:
    parser = _PageParser()
    parser.feed(html)
    parser.close()
    return Page(forms=parser.forms, csrf_meta=parser.csrf_meta)


@dataclass(frozen=True)
class ResurrectionForm:
    action: str
    fields: dict[str, str]


def resurrection_form(page: Page, torrent_id: str) -> ResurrectionForm | None:
    """Il form del claim per questo torrent, se la pagina lo offre. Mai
    quello che annulla un claim (_method=DELETE, nella pagina dei propri
    claim). Il token CSRF dal form, o dal meta della pagina."""
    for form in page.forms:
        if form.method != "post":
            continue
        path = urlsplit(form.action).path.lower().rstrip("/")
        if "resurrection" not in path and "graveyard" not in path:
            continue
        fields = form.hidden()
        if fields.get("_method", "post").lower() != "post":
            continue
        if fields.get("torrent_id") != torrent_id and not path.endswith(f"/{torrent_id}"):
            continue
        if "_token" not in fields and page.csrf_meta:
            fields["_token"] = page.csrf_meta
        return ResurrectionForm(action=form.action, fields=fields)
    return None


def is_login_redirect(status: int, location: str | None) -> bool:
    return 300 <= status < 400 and "/login" in (location or "")
