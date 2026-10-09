"""Secondo punto di approvazione del flusso di upload v2 (docs/SPEC.md §9):
per ogni tracker l'azione (upload, reseed, salta), il nome della release,
i flag e gli id di categoria/tipo/risoluzione del profilo.

L'approvazione è la conferma umana obbligatoria di §9: da qui il worker
porta il job fino in fondo senza chiedere altro (decisione dell'utente,
2026-09-29). Per questo si approvano tutti i tracker insieme, ognuno con
la sua decisione esplicita, mai uno per default.
"""

import json
import logging

from sqlalchemy.orm import Session, object_session

from nazgarr.core.models import TrackerUploadProfile, UploadJob, UploadTarget
from nazgarr.integrations.adapter_factory import TmdbApiKeyMissingError
from nazgarr.torrents import client_labels
from nazgarr.upload import file_names as upload_file_names
from nazgarr.upload import jobs as upload_jobs
from nazgarr.upload import streaming_services
from nazgarr.upload.jobs import UploadJobError
from nazgarr.upload.naming import (
    DEFAULT_AUDIO_CODECS,
    DEFAULT_TYPE_LABELS,
    DETECTED_FIELDS,
    VARIABLES,
    audio_language_check,
    build_name,
    detect,
    release_values,
    rules_from_convention,
    with_tracker_language,
)
from nazgarr.upload.naming_examples import EXAMPLES
from nazgarr.upload.profiles import freeleech_options

logger = logging.getLogger(__name__)

FLAG_KEYS = ("anonymous", "personal_release", "internal", "stream")  # booleani; freeleech è a parte (percentuale)
# Override oltre ai valori rilevati: anno del nome, numero di screenshot,
# note in fondo alla descrizione, e non aggiungere il torrent al client.
EXTRA_OVERRIDES = {"year": int, "screenshot_count": int, "notes": str, "no_seed": bool, "file_naming": str,
                   "pack_mixed_confirmed": bool}
MAX_SCREENSHOTS = 12


def _profile(session: Session, target: UploadTarget) -> TrackerUploadProfile | None:
    return session.get(TrackerUploadProfile, target.tracker_id)


def _maps(profile: TrackerUploadProfile | None) -> tuple[dict, dict, dict]:
    if profile is None:
        return {}, {}, {}
    return (
        json.loads(profile.category_id_map_json or "{}"),
        json.loads(profile.type_id_map_json or "{}"),
        json.loads(profile.resolution_id_map_json or "{}"),
    )


# I valori più comuni per i "Detected details" (un menu nei campi, che restano
# liberi): type e resolution vengono dai profili dei tracker del job, cioè
# quelli che sanno mappare; gli altri dalle convenzioni dei nomi.
SOURCE_OPTIONS = ["BluRay", "3D BluRay", "HDDVD", "PAL DVD", "NTSC DVD", "DVD", "HDTV", "UHDTV", "UHDRip",
                  "WEB-DL", "WEBRip"]
VIDEO_CODEC_OPTIONS = ["x264", "x265", "H.264", "H.265", "AVC", "HEVC", "AV1", "VC-1", "MPEG-2", "VP9"]
HDR_OPTIONS = ["DV HDR", "DV", "HDR10+", "HDR", "HLG"]
EDITION_OPTIONS = ["Director's Cut", "Extended", "Theatrical", "Unrated", "Uncut", "Remastered", "IMAX",
                   "Special Edition", "Criterion"]
REPACK_OPTIONS = ["REPACK", "PROPER"]


def field_options(session: Session, job: UploadJob) -> dict[str, list[str]]:
    types: set[str] = set()
    resolutions: set[str] = set()
    audio = set(DEFAULT_AUDIO_CODECS.values())
    for target in job.targets:
        profile = _profile(session, target)
        _categories, type_map, resolution_map = _maps(profile)
        types |= set(type_map)
        resolutions |= set(resolution_map)
        rules = profile_rules(profile) or {}
        audio |= set((rules.get("audio_codecs") or {}).values())
    return {
        "type": sorted(types or DEFAULT_TYPE_LABELS),
        "resolution": sorted(resolutions, key=lambda r: (len(r), r)),
        "source": SOURCE_OPTIONS,
        "service": sorted(streaming_services.ABBREVIATIONS),
        "video_codec": VIDEO_CODEC_OPTIONS,
        "audio": sorted(a for a in audio if a),
        "hdr": HDR_OPTIONS,
        "edition": EDITION_OPTIONS,
        "repack": REPACK_OPTIONS,
        "hybrid": ["HYBRID"],
    }


def profile_rules(profile: TrackerUploadProfile | None) -> dict | None:
    if profile is None:
        return None
    if profile.naming_rules_json:
        return json.loads(profile.naming_rules_json)
    return rules_from_convention(profile.naming_convention)


def _local_title(session: Session, job: UploadJob, language: str, analysis: dict) -> str | None:
    """Titolo TMDB nella lingua del tracker, una chiamata per lingua e job."""
    titles = analysis.setdefault("titles", {})
    if language in titles:
        return titles[language]
    title = None
    if job.tmdb_id and job.content_type:
        from nazgarr.upload import identify as upload_identify

        try:
            title = upload_identify.tmdb_client(session).localized_title(job.content_type, job.tmdb_id, language)
        except TmdbApiKeyMissingError:
            title = None
        except Exception:
            logger.warning("Titolo in %s non disponibile per %s", language, job.tmdb_id, exc_info=True)
            return None  # non in cache: si riprova al prossimo cambio di override
    titles[language] = title
    return title


def clean_overrides(raw: dict | None) -> dict:
    cleaned: dict = {}
    for key, value in (raw or {}).items():
        if value is None or value == "":
            continue
        if key in DETECTED_FIELDS:
            cleaned[key] = str(value).strip()
        elif key in EXTRA_OVERRIDES:
            kind = EXTRA_OVERRIDES[key]
            try:
                cleaned[key] = kind(value) if kind is not bool else bool(value)
            except (TypeError, ValueError) as exc:
                raise UploadJobError("upload_invalid_override", field=key) from exc
        else:
            raise UploadJobError("upload_unknown_override", field=key)
    if "screenshot_count" in cleaned and not 0 <= cleaned["screenshot_count"] <= MAX_SCREENSHOTS:
        raise UploadJobError("upload_invalid_override", field="screenshot_count")
    if "file_naming" in cleaned and cleaned["file_naming"] not in upload_file_names.MODES:
        raise UploadJobError("upload_invalid_override", field="file_naming")
    return cleaned


def propose(session: Session, job: UploadJob) -> None:
    """Nome, id del profilo e flag proposti per ogni tracker, dai valori
    rilevati e dagli override. Rifatto a ogni cambio di override; non tocca
    mai quello che l'utente ha già approvato."""
    from_name = upload_file_names.name_detected(session, job)
    overrides = json.loads(job.overrides_json or "{}")
    analysis = json.loads(job.analysis_json or "{}")
    mediainfo = analysis.get("mediainfo")
    # I tag di "Detected details": i valori con le regole generiche, senza override.
    generic = release_values(job, from_name, mediainfo, {}, None)
    analysis["detected"] = {key: generic.get(key) for key in DETECTED_FIELDS}
    analysis["type_basis"] = generic.get("type_basis")
    for target in job.targets:
        profile = _profile(session, target)
        categories, types, resolutions = _maps(profile)
        rules = with_tracker_language(profile_rules(profile), target.tracker.language) or {}
        language = rules.get("title_language")
        local_title = _local_title(session, job, language, analysis) if language else None
        values = release_values(job, from_name, mediainfo, overrides, rules, local_title)
        target.proposed_name = build_name(rules, values)
        target.category_id = categories.get(job.content_type or "movie")
        target.type_id = types.get(values.get("type") or "")
        target.resolution_id = resolutions.get(values.get("resolution") or "")
        # Default del profilo sotto quello che c'è già (le scelte fatte alla
        # creazione), mai sopra.
        existing = json.loads(target.flags_json) if target.flags_json else {}
        target.flags_json = json.dumps({
            "anonymous": bool(profile and profile.default_anonymous),
            "personal_release": bool(profile and profile.default_personal_release),
            "internal": bool(profile and profile.default_internal),
            "stream": False,
            "freeleech": (profile.default_freeleech or 0) if profile else 0,
            **existing,
        })
    # La lingua del tracker nell'audio del file (un warning nella decisione).
    analysis["languages"] = {
        str(target.tracker_id): {"language": target.tracker.language, "status": status}
        for target in job.targets
        if (status := audio_language_check(target.tracker.language, mediainfo)) is not None
    }
    analysis["field_options"] = field_options(session, job)
    # Prima i titoli nelle lingue dei tracker appena letti: li usano anche i nomi dei file generati.
    job.analysis_json = json.dumps(analysis)
    analysis["file_names"] = file_names_preview(session, job)
    job.analysis_json = json.dumps(analysis)
    session.commit()


PREVIEW_FILES = 8


def file_names_preview(session: Session, job: UploadJob) -> dict:
    """I nomi dei file nel torrent per ogni modalità disponibile
    (nazgarr/upload/file_names.py), per sceglierla nella decisione."""
    try:
        inputs = upload_file_names.NameInputs(session, job)  # sorgente e hardlink letti una volta
        available = upload_file_names.available_modes(session, job, inputs)
        previews = {}
        for mode in available:
            plan = upload_file_names.plan(session, job, mode, inputs)
            if plan.mode != mode:
                continue  # es. "generated" senza titolo ripiega sui nomi originali
            targets = [target for _source, target in plan.files]
            previews[mode] = {"name": plan.content_name, "files": targets[:PREVIEW_FILES], "count": len(targets),
                              "single_file": plan.single_file, "folder": plan.folder}
        return {
            "available": [m for m in available if m in previews],
            "default": upload_file_names.default_mode(session, job, inputs),
            "previews": previews,
        }
    except OSError:
        logger.warning("Anteprima dei nomi dei file non disponibile per il job %s", job.id, exc_info=True)
        return {"available": [], "default": None, "previews": {}}


def update_overrides(session: Session, job: UploadJob, overrides: dict | None) -> None:
    if job.status != "awaiting_decision":
        raise UploadJobError("upload_job_wrong_status", status=job.status)
    job.overrides_json = json.dumps(clean_overrides(overrides))
    propose(session, job)


def _validate(target: UploadTarget, decision: dict) -> dict:
    action = decision.get("action")
    if action not in ("upload", "reseed", "skip"):
        raise UploadJobError("upload_invalid_action", tracker=target.tracker.label)
    out = {"action": action}
    for key in ("client_category", "client_tags"):
        if key in decision:
            out[key] = decision[key]
    if action == "upload":
        name = (decision.get("name") or "").strip()
        if not name:
            raise UploadJobError("upload_name_required", tracker=target.tracker.label)
        ids = {key: decision.get(key) for key in ("category_id", "type_id", "resolution_id")}
        if any(not isinstance(v, int) for v in ids.values()):
            raise UploadJobError("upload_ids_required", tracker=target.tracker.label)
        flags = decision.get("flags") or {}
        freeleech = int(flags.get("freeleech") or 0)
        if freeleech and freeleech not in freeleech_options(_profile(object_session(target), target)):
            raise UploadJobError("upload_freeleech_not_allowed", tracker=target.tracker.label, value=freeleech)
        out.update(ids, name=name, flags={**{key: bool(flags.get(key)) for key in FLAG_KEYS}, "freeleech": freeleech})
    if action == "reseed":
        dupes = json.loads(target.dupes_json or "[]")
        torrent_id = decision.get("reseed_torrent_id") or target.reseed_torrent_id
        identical = {d["torrent_id_remote"]: d for d in dupes if d["verdict"] == "identical"}
        if torrent_id not in identical:
            raise UploadJobError("upload_reseed_needs_identical", tracker=target.tracker.label)
        # Solo dopo un full hash check passato (decisione dell'utente,
        # 2026-10-01): con byte diversi il client riscaricherebbe i piece
        # sbagliati dentro l'hardlink, sovrascrivendo il file in libreria. E un
        # torrent con lo stesso nome e la stessa dimensione lo può caricare
        # chiunque sul tracker.
        if (identical[torrent_id].get("verification") or {}).get("status") != "passed":
            raise UploadJobError("upload_reseed_needs_verification", tracker=target.tracker.label)
        out["reseed_torrent_id"] = torrent_id
    return out


def client_label_defaults(job: UploadJob, target: UploadTarget) -> dict:
    """Categoria e tag proposti nel client del tracker: per tipo di
    contenuto (anime compreso) e per azione."""
    client = target.torrent_client
    return {
        "category": client_labels.default_category(client, job.content_type, bool(job.anime)),
        "tags_upload": client_labels.default_tags(client, "upload"),
        "tags_reseed": client_labels.default_tags(client, "reseed"),
    }


def _client_labels(job: UploadJob, target: UploadTarget, decision: dict) -> tuple[str | None, str | None]:
    """Quelli scelti nella decisione (vuoto = nessuno), se no i default."""
    defaults = client_label_defaults(job, target)
    category = decision["client_category"] if "client_category" in decision else defaults["category"]
    tags = decision["client_tags"] if "client_tags" in decision else defaults[f"tags_{decision['action']}"]
    tags = ", ".join(client_labels.split_tags(tags)) or None
    return (category or "").strip() or None, tags


def approve(session: Session, job: UploadJob, decisions: list[dict]) -> None:
    """Tutti i tracker insieme, ognuno con la sua decisione. Porta il job in
    coda ('queued'), o direttamente a 'done' se ogni tracker è saltato."""
    if job.status != "awaiting_decision":
        raise UploadJobError("upload_job_wrong_status", status=job.status)
    busy = [t for t in job.targets if t.status != "awaiting_decision"]
    if busy:
        raise UploadJobError("upload_target_busy", tracker=busy[0].tracker.label, status=busy[0].status)
    by_target = {d.get("target_id"): d for d in decisions}
    missing = [t for t in job.targets if t.id not in by_target]
    if missing:
        raise UploadJobError("upload_decision_missing", tracker=missing[0].tracker.label)
    validated = {t.id: _validate(t, by_target[t.id]) for t in job.targets}
    # Un pack con episodi di release diverse (nazgarr/upload/pack.py): niente
    # upload finché l'utente non dice di saperlo. Un reseed sì: il torrent è
    # già quello del tracker.
    mixed = json.loads(job.analysis_json or "{}").get("pack_mixed")
    overrides = json.loads(job.overrides_json or "{}")
    if mixed and not overrides.get("pack_mixed_confirmed") and any(
            d["action"] == "upload" for d in validated.values()):
        raise UploadJobError("upload_pack_mixed_unconfirmed", fields=", ".join(sorted(mixed)))

    for target in job.targets:
        decision = validated[target.id]
        target.action = decision["action"]
        if decision["action"] == "upload":
            target.approved_name = decision["name"]
            target.flags_json = json.dumps(decision["flags"])
            target.category_id = decision["category_id"]
            target.type_id = decision["type_id"]
            target.resolution_id = decision["resolution_id"]
        if decision["action"] == "reseed":
            target.reseed_torrent_id = decision["reseed_torrent_id"]
        if decision["action"] in ("upload", "reseed"):
            target.client_category, target.client_tags = _client_labels(job, target, decision)
        status = upload_jobs.TargetStatus
        upload_jobs.set_target_status(target, status.SKIPPED if decision["action"] == "skip" else status.APPROVED)
        upload_jobs.log_event(
            session, job, "target_approved", target=target, action=decision["action"],
            name=decision.get("name"), torrent=decision.get("reseed_torrent_id"),
        )
    session.commit()

    if all(t.action == "skip" for t in job.targets):
        upload_jobs.transition(session, job, "awaiting_decision", "done")
        upload_jobs.log_event(session, job, "all_skipped")
    else:
        upload_jobs.transition(
            session, job, "awaiting_decision", "queued", queue_position=upload_jobs.next_queue_position(session)
        )
        upload_jobs.log_event(session, job, "job_queued", position=job.queue_position)
    session.commit()


def _example_values(example, rules: dict) -> dict:
    local_title = example.local_titles.get(rules.get("title_language") or "")
    return release_values(example.job(), detect(example.release_name), example.mediainfo, {}, rules, local_title)


def preview_names(session: Session, rules: dict, tracker_language: str | None = None) -> dict:
    """Anteprima delle regole di naming nell'editor del profilo: il nome
    finale di ogni esempio fisso (nazgarr/upload/naming_examples.py) e, se c'è,
    dell'ultimo upload già analizzato (valori veri); sotto ogni template, il
    nome che darebbe (l'upload vero, se no il primo esempio; l'esempio da
    serie per il pattern delle serie)."""
    job = (
        session.query(UploadJob)
        .filter(UploadJob.tmdb_id.isnot(None), UploadJob.analysis_json.isnot(None))
        .order_by(UploadJob.id.desc())
        .first()
    )
    rules = with_tracker_language(rules, tracker_language) or {}
    examples = [(example, _example_values(example, rules)) for example in EXAMPLES]
    results = [
        {"kind": "example", "key": example.key, "label": example.label, "name": build_name(rules, values)}
        for example, values in examples
    ]
    series = next(values for example, values in examples if example.content_type == "tv")
    if job is not None:
        analysis = json.loads(job.analysis_json or "{}")
        language = rules.get("title_language")
        local_title = (analysis.get("titles") or {}).get(language) if language else None
        values = release_values(
            job, upload_file_names.name_detected(session, job), analysis.get("mediainfo"),
            json.loads(job.overrides_json or "{}"), rules, local_title,
        )
        sample = {"kind": "job", "label": job.title or job.relative_path}
        results.insert(0, {"kind": "job", "key": f"job-{job.id}", "label": sample["label"],
                           "name": build_name(rules, values)})
        if job.content_type == "tv":
            series = values
    else:
        values = examples[0][1]
        sample = {"kind": "example", "label": EXAMPLES[0].label}
    templates = rules.get("templates") or {}
    names = {}
    for key in templates:
        if key == "tv":
            names[key] = build_name(rules, series)
        else:
            movie = {**values, "content_type": "movie", "season": None, "episode": None}
            names[key] = build_name(rules, {**movie, "type": key if key != "default" else values.get("type")})
    variables = {key: values.get(key) for key in VARIABLES}
    return {"sample": sample, "variables": variables, "names": names, "examples": results}
