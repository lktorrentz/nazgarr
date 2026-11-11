"""guessit sui nomi dei file locali, dopo aver tolto quello che non è parte
del nome: il suffisso dei titoli di MakeMKV ("Movie_t00.mkv", "title_t01",
uno per titolo del disco), che guessit legge come parte del titolo ("Movie
t00") o come titolo dell'episodio, e che fa sbagliare il match su TMDB
(segnalato dall'utente, 2026-10-03).

Un nome che comincia col numero dell'episodio ("08 - Titolo.mkv", senza
stagione): guessit legge come altri episodi, e a volte come stagione, anche
i numeri del titolo. "08 - Sig. A in roco 426" diventava S04E08E26, "08 -
Area 51" E08E51 (segnalato dall'utente, 2026-11-11): lì l'episodio è il
numero davanti, e la stagione viene solo dalle cartelle.
"""

import os
import re

import guessit

# "_t00" … "_t99" (e "_t100"+, dischi con molti titoli) in fondo al nome, prima
# dell'estensione. Con "_" o uno spazio davanti, come li scrive MakeMKV.
_MAKEMKV_TITLE = re.compile(r"[_ ]t\d{2,3}$")
# "08 - Titolo", "8_-_Titolo"; non "08 - 09 - Titolo" (due episodi).
_LEADING_EPISODE = re.compile(r"(\d{1,3})[\s_]*[-–][\s_]+(?!\d{1,3}[\s_]*[-–])")
# Stagione ed episodio scritti nel nome: lì guessit ha già la risposta giusta.
_SEASON_EPISODE = re.compile(r"(?i)(?<![a-z0-9])(?:s\d{1,2}[\s._-]*e\d{1,3}|\d{1,2}x\d{2,3})(?![a-z0-9])")


def clean_name(name: str) -> str:
    """Il nome (o percorso) senza il suffisso di MakeMKV, estensione e
    cartelle intatte."""
    folder, filename = os.path.split(name)
    stem, ext = os.path.splitext(filename)
    if ext and not re.fullmatch(r"\.[A-Za-z0-9]{2,4}", ext):
        stem, ext = filename, ""  # "Movie.2024_t00" senza estensione: il punto è parte del nome
    cleaned = _MAKEMKV_TITLE.sub("", stem)
    if not cleaned:
        return name
    return os.path.join(folder, cleaned + ext) if folder else cleaned + ext


def as_list(value) -> list:
    """guessit dà un valore o una lista (es. più episodi): sempre una lista."""
    if value is None:
        return []
    return list(value) if isinstance(value, list) else [value]


def _leading_episode(filename: str, result) -> int | None:
    """Il numero davanti al nome di un episodio, se guessit lo legge come
    episodio con accanto altro (episodi o una stagione dal titolo), o se con
    le cartelle lo perde ("Stagione 2/08 - Volo 815": nessun episodio)."""
    match = _LEADING_EPISODE.match(filename)
    if match is None or _SEASON_EPISODE.search(filename) or result.get("type") != "episode":
        return None
    number = int(match.group(1))
    episodes = as_list(result.get("episode"))
    return number if number in episodes or not episodes else None


def guess(name: str, options: dict | None = None) -> dict:
    cleaned = clean_name(name)
    result = guessit.guessit(cleaned, options) if options else guessit.guessit(cleaned)
    folder, filename = os.path.split(cleaned)
    number = _leading_episode(filename, result)
    if number is None:
        return result
    result["episode"] = number
    season = guessit.guessit(folder).get("season") if folder else None
    if season is None:
        result.pop("season", None)
    else:
        result["season"] = season
    return result
