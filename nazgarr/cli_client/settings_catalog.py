"""Le impostazioni che la web UI espone, con un aiuto per il CLI (nazgarr
settings ls). Una chiave non in elenco si legge e scrive lo stesso con
settings get/set: l'elenco serve a trovarle.

secret: chiavi segrete, che una API key non legge né scrive; safety: le
protezioni (verifica, esecuzione automatica, soglie), che una API key legge
ma non cambia (nazgarr/api/settings.py). Per entrambe il CLI chiede la password.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Setting:
    key: str
    help: str
    kind: str = "text"  # text | bool | int | float | lines | secret
    safety: bool = False


CATALOG = [
    # Metadati
    Setting("tmdb_api_key", "TMDB API key (v3): identifies every file. Free at themoviedb.org.", "secret"),
    Setting("tvdb_api_key", "TVDB API key: optional, a last resort for episode orders.", "secret"),
    # Esclusioni
    Setting("exclusion_patterns", "Your exclusion patterns, one per line (e.g. */Extras/*).", "lines"),
    Setting("exclusion_presets", "Enabled exclusion presets, comma separated (see the web UI for the list)."),
    # Reseeding
    Setting("cross_seed_search", "Search files seeding on one tracker on the others too (true/false).", "bool"),
    Setting("reseed_pack_and_singles",
            "Seed an episode both in its season pack and alone on the same tracker (off by default).", "bool"),
    Setting("rematch_interval_days", "Days before an unmatched file is searched again.", "int"),
    Setting("confidence_threshold_auto_media_to_torrent",
            "Confidence (0-1) above which a library→torrent match is recommended.", "float", safety=True),
    Setting("confidence_threshold_auto_torrent_to_client",
            "Confidence (0-1) above which a torrent→client match is recommended.", "float", safety=True),
    Setting("auto_execute_above_threshold", "Execute recommended matches without asking (off by default).",
            "bool", safety=True),
    Setting("verify_before_execute", "Read every piece before linking or adding a torrent (on by default).",
            "bool", safety=True),
    Setting("skip_client_recheck_when_verified",
            "Skip the client recheck when Nazgarr verified 100% (off by default).", "bool", safety=True),
    # Upload
    Setting("upload_releaser_name", "Your group name: watched-folder uploads, and uploads whose name has no group."),
    Setting("upload_auto_match_threshold", "TMDB match confirmed on its own at or above this (0-1, 0 = off).",
            "float"),
    Setting("upload_auto_rename", "Rename files in new torrents automatically (true/false).", "bool"),
    Setting("upload_single_file", "A folder with one file becomes a single-file torrent (true/false).", "bool"),
    Setting("upload_single_file_folder", "With upload_single_file: keep or remove the folder."),
    Setting("upload_screenshot_count", "Screenshots per upload.", "int"),
    Setting("upload_tonemap_hdr", "Tone map HDR screenshots (true/false).", "bool"),
    Setting("upload_watch_split_incomplete",
            "Split an incomplete season from the watched folder into one upload per episode (on by default).", "bool"),
    Setting("upload_description_header", "BBCode added on top of every description.", "lines"),
    Setting("upload_description_signature", "BBCode added at the bottom of every description.", "lines"),
    Setting("image_host_priority", "Image hosts in order, comma separated (e.g. ptscreens,imgbb)."),
    # Interfaccia
    Setting("size_units", "decimal (GB) or binary (GiB)."),
    Setting("ui_timezone", "Time zone of the dates in the UI (e.g. Europe/Rome)."),
    Setting("ui_date_format", "Date format of the UI."),
    Setting("ui_time_format", "Time format of the UI."),
]

BY_KEY = {s.key: s for s in CATALOG}
SECRET_WORDS = ("api_key", "token", "password", "secret", "cookie")


def needs_login(key: str, writing: bool) -> bool:
    """Come nazgarr/api/settings.py: i segreti sempre, le protezioni in scrittura."""
    entry = BY_KEY.get(key)
    if any(word in key for word in SECRET_WORDS):
        return True
    return writing and entry is not None and entry.safety


def normalize(key: str, value: str) -> str:
    entry = BY_KEY.get(key)
    if entry is None:
        return value
    if entry.kind == "bool":
        lowered = value.strip().lower()
        if lowered in ("true", "yes", "on", "1"):
            return "true"
        if lowered in ("false", "no", "off", "0"):
            return "false"
        raise ValueError(f"{key} is true or false")
    if entry.kind == "int":
        return str(int(value))
    if entry.kind == "float":
        return str(float(value))
    return value
