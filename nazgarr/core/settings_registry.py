"""Le impostazioni note (app_settings), con tipo, default e se sono una
protezione: un posto solo invece di default ripetuti nei moduli e di
booleani letti in due modi ((x or "true") != "false" da una parte,
x == "true" dall'altra).

- get_bool/get_int/get_float/get_choice leggono una chiave col suo tipo e
  il suo default; un valore illeggibile vale il default.
- validate controlla e normalizza un valore prima di salvarlo
  (PUT /api/settings/{key}): una soglia fuori da 0-1 o un "yes" al posto di
  true non arrivano più al DB. Le chiavi che non sono qui (dinamiche: host
  di immagini, plugin; o solo dell'interfaccia) passano com'erano.
- SAFETY_KEYS: le protezioni, che una API key legge ma non cambia
  (nazgarr/api/settings.py) e che il CLI chiede con la password.
"""

from dataclasses import dataclass

from sqlalchemy.orm import Session

from nazgarr.core import settings_repo

Kind = str  # bool | int | float | choice


@dataclass(frozen=True)
class SettingSpec:
    key: str
    kind: Kind
    default: object
    safety: bool = False
    minimum: float | None = None
    maximum: float | None = None
    choices: tuple[str, ...] = ()


_SPECS = [
    # Le protezioni dell'esecuzione (docs/SPEC.md §3): mai cambiate da una API key.
    SettingSpec("verify_before_execute", "bool", True, safety=True),
    SettingSpec("skip_client_recheck_when_verified", "bool", False, safety=True),
    SettingSpec("auto_execute_above_threshold", "bool", False, safety=True),
    SettingSpec("confidence_threshold_auto_media_to_torrent", "float", 0.95, safety=True, minimum=0, maximum=1),
    SettingSpec("confidence_threshold_auto_torrent_to_client", "float", 0.98, safety=True, minimum=0, maximum=1),
    # Reseeding
    SettingSpec("cross_seed_search", "bool", True),
    # Pack e singoli dello stesso tracker (nazgarr/library/seeding.py), spento di default.
    SettingSpec("reseed_pack_and_singles", "bool", False),
    SettingSpec("rematch_interval_days", "float", 7.0, minimum=0),
    # Upload
    SettingSpec("upload_auto_match_threshold", "float", 0.9, minimum=0, maximum=1),
    SettingSpec("upload_auto_rename", "bool", True),
    SettingSpec("upload_single_file", "bool", False),
    SettingSpec("upload_single_file_folder", "choice", "keep", choices=("keep", "remove")),
    SettingSpec("upload_tonemap_hdr", "bool", False),
    SettingSpec("upload_screenshot_count", "int", 4, minimum=0, maximum=12),
    # Una stagione incompleta dalla cartella osservata: un upload per episodio (nazgarr/upload/split.py).
    SettingSpec("upload_watch_split_incomplete", "bool", True),
    # Aggiornamenti: controllo automatico ogni 12 ore (nazgarr/core/updates.py),
    # spento di default: contatta GitHub solo se l'utente lo accende.
    SettingSpec("update_check_auto", "bool", False),
    # Webhook di Radarr/Sonarr: cercare sui tracker un file appena importato
    # (nazgarr/integrations/arr_webhooks.py). Spento: la scansione lo fa comunque.
    SettingSpec("arr_webhook_search", "bool", False),
]

REGISTRY: dict[str, SettingSpec] = {spec.key: spec for spec in _SPECS}
SAFETY_KEYS = frozenset(spec.key for spec in _SPECS if spec.safety)

_TRUE = ("true", "yes", "on", "1")
_FALSE = ("false", "no", "off", "0")


class SettingValueError(ValueError):
    """Un valore non valido per una chiave nota; code per il messaggio tradotto."""

    def __init__(self, key: str, spec: SettingSpec):
        super().__init__(key)
        self.key, self.spec = key, spec


def validate(key: str, value: str) -> str:
    """Il valore da salvare, normalizzato (bool -> "true"/"false"). Una chiave
    non nel registro torna com'è; una nota con un valore non valido solleva
    SettingValueError."""
    spec = REGISTRY.get(key)
    if spec is None:
        return value
    text = value.strip()
    if text == "":
        return ""  # mai impostata: vale il default
    if spec.kind == "bool":
        if text.lower() in _TRUE:
            return "true"
        if text.lower() in _FALSE:
            return "false"
        raise SettingValueError(key, spec)
    if spec.kind == "choice":
        if text.lower() in spec.choices:
            return text.lower()
        raise SettingValueError(key, spec)
    try:
        number = int(text) if spec.kind == "int" else float(text)
    except ValueError as exc:
        raise SettingValueError(key, spec) from exc
    if (spec.minimum is not None and number < spec.minimum) or (spec.maximum is not None and number > spec.maximum):
        raise SettingValueError(key, spec)
    return text  # come l'ha scritto l'utente ("7", non "7.0")


def _raw(session: Session, key: str) -> str | None:
    raw = settings_repo.get_setting(session, key)
    return None if raw is None or raw.strip() == "" else raw


def get_bool(session: Session, key: str) -> bool:
    raw = _raw(session, key)
    if raw is not None:
        if raw.strip().lower() in _TRUE:
            return True
        if raw.strip().lower() in _FALSE:
            return False
    return bool(REGISTRY[key].default)


def get_float(session: Session, key: str) -> float:
    try:
        return float(_raw(session, key))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return float(REGISTRY[key].default)  # type: ignore[arg-type]


def get_int(session: Session, key: str) -> int:
    try:
        return int(_raw(session, key))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return int(REGISTRY[key].default)  # type: ignore[arg-type]


def get_choice(session: Session, key: str) -> str:
    spec = REGISTRY[key]
    raw = (_raw(session, key) or "").strip().lower()
    return raw if raw in spec.choices else str(spec.default)
