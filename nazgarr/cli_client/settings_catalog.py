"""Le impostazioni che la web UI espone, con un aiuto per il CLI (nazgarr
settings ls). Non ci sono quelle che sono solo stato dell'interfaccia
(SettingSpec.ui nel registro: il filtro per tracker, il tour, ...), che si
leggono e scrivono lo stesso con settings get/set. Una chiave che non è
un'impostazione il server la rifiuta (setting_unknown, audit 2026-10-18).

secret: chiavi segrete, che una API key non legge né scrive; safety: le
protezioni (verifica, esecuzione automatica, soglie), che una API key legge
ma non cambia (nazgarr/api/settings.py). Per entrambe il CLI chiede la password.

Ogni chiave di nazgarr/core/settings_registry.py (tranne quelle ui) è qui,
con un tipo compatibile: "text" del registro è text, lines o secret qui
(tests/test_consistency.py). Prima l'elenco era divergente
(rematch_interval_days intero qui e decimale sul server, due chiavi
mancanti) e il CLI rifiutava valori validi (audit 2026-10-18). I controlli
veri li fa il server: qui solo quanto serve a dire subito un errore di
battitura, senza riscrivere il valore ("7" resta "7", non "7.0").
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Setting:
    key: str
    help: str
    kind: str = "text"  # text | bool | int | float | choice | lines | secret
    safety: bool = False
    choices: tuple[str, ...] = ()


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
    Setting("rematch_interval_days", "Days before an unmatched file is searched again (e.g. 7, or 0.5).", "float"),
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
    Setting("upload_dupe_threshold",
            "Dupe check: a release in the same slot is a dupe from this score (0-100, default 70).", "int"),
    Setting("upload_identical_auto",
            "Identical torrent on the tracker: off (you decide), verify (hash check by itself), reseed (and "
            "reseed by itself at 100%).", "choice", safety=True, choices=("off", "verify", "reseed")),
    Setting("upload_auto_rename", "Rename files in new torrents automatically (true/false).", "bool"),
    Setting("upload_single_file", "A folder with one file becomes a single-file torrent (true/false).", "bool"),
    Setting("upload_single_file_folder", "With upload_single_file: keep or remove the folder.", "choice",
            choices=("keep", "remove")),
    Setting("upload_screenshot_count", "Screenshots per upload.", "int"),
    Setting("upload_tonemap_hdr", "Tone map HDR screenshots (true/false).", "bool"),
    Setting("upload_watch_split_seasons",
            "Split a folder with several seasons from the watched folder into one upload per season (on by default).",
            "bool"),
    Setting("upload_watch_split_incomplete",
            "Split an incomplete season from the watched folder into one upload per episode (on by default).", "bool"),
    Setting("upload_description_header", "BBCode added on top of every description.", "lines"),
    Setting("upload_description_signature", "BBCode added at the bottom of every description.", "lines"),
    Setting("image_host_priority", "Image hosts in order, comma separated (e.g. ptscreens,imgbb)."),
    # Radarr/Sonarr e aggiornamenti
    Setting("arr_webhook_search",
            "Search the trackers for a file Radarr/Sonarr just imported, before the next scan (off by default).",
            "bool"),
    Setting("update_check_auto", "Check GitHub for a new version every 12 hours (off by default).", "bool"),
    # Interfaccia
    Setting("size_units", "decimal (GB) or binary (GiB).", "choice", choices=("decimal", "binary")),
    Setting("ui_timezone", "Time zone of the dates in the UI (e.g. Europe/Rome)."),
    Setting("ui_date_format", "Date format of the UI."),
    Setting("ui_time_format", "Time format of the UI."),
]

BY_KEY = {s.key: s for s in CATALOG}

# Le chiavi che PUT /api/settings/{key} non scrive mai (MANAGED_KEYS in
# nazgarr/core/settings_registry.py, un test controlla che siano le stesse):
# dove si cambiano invece.
MANAGED = {
    "schedule_cron": "nazgarr schedule set",
    "schedule_last_slot": "nothing: Nazgarr keeps it itself",
    "upload_file_naming_rules": "Settings › Upload › Releases in the web UI",
    "instance_name": "Settings › System › Instances in the web UI",
    "plugins_disabled": "Settings › Extensions › Plugins in the web UI",
    "app_secret_key_canary": "nothing: Nazgarr keeps it itself",
    "release_notes_seen": "nothing: Nazgarr keeps it itself",
    "update_check_last": "nothing: Nazgarr keeps it itself",
    "identity_rules_version": "nothing: Nazgarr keeps it itself",
    "not_imported_status": "nothing: Nazgarr keeps it itself",
    "migration_backup_last": "nothing: Nazgarr keeps it itself (see `nazgarr db backups`)",
}
SECRET_WORDS = ("api_key", "token", "password", "secret", "cookie")


def needs_login(key: str, writing: bool) -> bool:
    """Come nazgarr/api/settings.py: i segreti sempre, le protezioni in scrittura."""
    entry = BY_KEY.get(key)
    if any(word in key for word in SECRET_WORDS):
        return True
    return writing and entry is not None and entry.safety


def normalize(key: str, value: str) -> str:
    """Il valore da mandare, come lo normalizza anche il server
    (nazgarr/core/settings_registry.py validate): ValueError con un
    messaggio da mostrare se non è del tipo giusto."""
    entry = BY_KEY.get(key)
    if entry is None:
        return value
    text = value.strip()
    if entry.kind == "bool":
        if text.lower() in ("true", "yes", "on", "1"):
            return "true"
        if text.lower() in ("false", "no", "off", "0"):
            return "false"
        raise ValueError(f"{key} is true or false")
    if entry.kind == "choice":
        if text.lower() in entry.choices:
            return text.lower()
        raise ValueError(f"{key} is one of: {', '.join(entry.choices)}")
    if entry.kind in ("int", "float"):
        if text == "":
            return text  # mai impostata: vale il default
        try:
            int(text) if entry.kind == "int" else float(text)
        except ValueError:
            raise ValueError(f"{key} is a {'whole ' if entry.kind == 'int' else ''}number, not {value!r}") from None
        return text
    return value
