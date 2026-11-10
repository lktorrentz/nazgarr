"""API di configurazione per i tracker (docs/SPEC.md sezione 6) e per il
loro profilo di upload opzionale 1:1 (docs/SPEC.md sezione 9, Fase 6)."""

import json
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, object_session

from nazgarr.adapters.tracker import unit3d_web
from nazgarr.adapters.tracker.base import NotSupportedError
from nazgarr.api.types import (
    HttpUrlStr,
    check_destinations,
    host_changed,
    insecure_public_url,
    require_secrets_for_new_host,
)
from nazgarr.core.errors import coded_detail, from_coded_error
from nazgarr.core.logs import safe_error
from nazgarr.core.models import (
    Candidate,
    MatchAttempt,
    MatchReview,
    SeedJob,
    TorrentClient,
    Tracker,
    TrackerUploadProfile,
    UploadJob,
    UploadTarget,
)
from nazgarr.integrations import adapter_factory
from nazgarr.plugins import REGISTRY
from nazgarr.plugins import config as plugin_config
from nazgarr.torrents import tracker_icons
from nazgarr.upload import decision as upload_decision
from nazgarr.upload import description as upload_description
from nazgarr.upload import jobs as upload_jobs
from nazgarr.upload import naming_rules
from nazgarr.upload import profiles as upload_profiles
from nazgarr.upload.naming import LANG3
from nazgarr.web.deps import get_or_404, get_session

LANGUAGE_CODES = frozenset(LANG3)

router = APIRouter(prefix="/api/trackers", tags=["trackers"])

# Richieste al minuto verso l'API del tracker: 0 vale il default (30). Un
# valore negativo faceva sollevare IndexError al limitatore a ogni chiamata,
# e ogni ricerca e download di quel tracker falliva finché non si correggeva
# (audit 2026-10-18).
RATE_LIMIT_MAX = 600


class TrackerCreateRequest(BaseModel):
    label: str
    adapter_type: str
    base_url: HttpUrlStr
    api_token: str
    announce_url: HttpUrlStr | None = None  # necessario solo per creare un nuovo .torrent da caricare (Fase 6, §9)
    rate_limit_per_min: int | None = Field(default=None, ge=0, le=RATE_LIMIT_MAX)
    rss_key: str | None = None  # facoltativa: appresa in automatico dall'API
    # La riga Cookie del login sul sito (con "Ricordami"): facoltativa, per il
    # claim automatico della resurrection (nazgarr/adapters/tracker/unit3d_web.py).
    session_cookie: str | None = None
    torrent_client_id: int | None = None  # client per i reseed di questo tracker, None = il primo abilitato
    language: str | None = None  # ISO 639-1, es. "it": per i nomi degli upload (nazgarr/upload/naming.py)
    # Requisito di seed (hit and run), facoltativo: nazgarr/torrents/seed_requirements.py.
    min_seed_time_seconds: int | None = Field(default=None, ge=0)
    min_ratio: float | None = Field(default=None, ge=0)
    seed_rule: Literal["any", "all"] | None = None
    config: dict | None = None  # campi di un adapter di un plugin (GET /api/plugins)
    # Profilo di upload da creare insieme: "auto" = quello bundlato con lo
    # stesso host (es. ITT), la chiave di un profilo bundlato, o "none".
    upload_profile: str = "auto"


class TrackerUpdateRequest(BaseModel):
    label: str | None = None
    base_url: HttpUrlStr | None = None
    api_token: str | None = None
    announce_url: HttpUrlStr | None = None
    rate_limit_per_min: int | None = Field(default=None, ge=0, le=RATE_LIMIT_MAX)
    enabled: bool | None = None
    rss_key: str | None = None  # "" la cancella (torna al solo recupero automatico)
    session_cookie: str | None = None  # "" la cancella (niente più claim automatico)
    torrent_client_id: int | None = None  # esplicitamente null = torna al primo client abilitato
    language: str | None = None  # esplicitamente null o "" = nessuna lingua
    min_seed_time_seconds: int | None = Field(default=None, ge=0)  # esplicitamente null = nessun minimo
    min_ratio: float | None = Field(default=None, ge=0)
    seed_rule: Literal["any", "all"] | None = None
    config: dict | None = None  # campi di un adapter di un plugin; un segreto null resta com'era


class TrackerUploadProfileSummary(BaseModel):
    source_profile_key: str | None
    naming_version: int | None
    naming_customized: bool
    naming_update_available: int | None
    freeleech_options: list[int]


class TrackerResponse(BaseModel):
    id: int
    label: str
    adapter_type: str
    base_url: str
    # http:// verso un host pubblico: il token viaggia in chiaro. Salvato lo
    # stesso, con un avviso (decisione dell'utente, 2026-10-18).
    insecure_public_url: bool = False
    # Contiene la passkey: come la chiave RSS, l'API dice solo se c'è.
    has_announce_url: bool = False
    rate_limit_per_min: int | None
    enabled: bool
    has_rss_key: bool = False  # mai la chiave stessa, solo se ce n'è una (manuale o appresa)
    # Il cookie della sessione web apre tutto l'account: l'API dice solo se c'è.
    has_session_cookie: bool = False
    torrent_client_id: int | None = None
    language: str | None = None
    min_seed_time_seconds: int | None = None
    min_ratio: float | None = None
    seed_rule: str = "any"
    config: dict = {}  # {"values": {...}, "secrets_set": [...]}, mai i segreti
    upload_profile: TrackerUploadProfileSummary | None = None  # per la scheda: senza profilo niente upload

    @classmethod
    def from_model(cls, t: Tracker) -> "TrackerResponse":
        profile = object_session(t).get(TrackerUploadProfile, t.id)
        summary = None
        if profile is not None:
            summary = TrackerUploadProfileSummary(
                source_profile_key=profile.source_profile_key, naming_version=profile.naming_version,
                naming_customized=bool(profile.naming_customized),
                naming_update_available=profile.naming_update_available,
                freeleech_options=upload_profiles.freeleech_options(profile),
            )
        return cls(
            id=t.id, label=t.label, adapter_type=t.adapter_type, base_url=t.base_url,
            insecure_public_url=insecure_public_url(t.base_url), has_announce_url=bool(t.announce_url),
            rate_limit_per_min=t.rate_limit_per_min, enabled=t.enabled,
            has_rss_key=bool(t.rss_key), has_session_cookie=bool(t.history_session_cookie),
            torrent_client_id=t.torrent_client_id, language=t.language,
            min_seed_time_seconds=t.min_seed_time_seconds, min_ratio=t.min_ratio, seed_rule=t.seed_rule or "any",
            config=plugin_config.public_for_row(t, "tracker"),
            upload_profile=summary,
        )


def _apply_config(tracker: Tracker, config: dict | None, *, creating: bool) -> None:
    try:
        plugin_config.apply_to_row(tracker, "tracker", config, creating=creating)
    except plugin_config.AdapterConfigError as exc:
        raise HTTPException(status_code=400, detail=from_coded_error(exc)) from exc


def _language(raw: str | None) -> str | None:
    """Un codice ISO 639-1 ("it"), o None; qualunque altra cosa è un errore."""
    value = (raw or "").strip().lower()
    if not value:
        return None
    if value not in LANGUAGE_CODES:
        raise HTTPException(status_code=400, detail=coded_detail("tracker_language_invalid", language=raw))
    return value


def _session_cookie(raw: str | None) -> str | None:
    """La riga Cookie pulita da salvare, None per "" (la cancella)."""
    if raw is None or not raw.strip():
        return None
    try:
        return unit3d_web.normalize_cookie(raw)
    except unit3d_web.InvalidCookie as exc:
        raise HTTPException(status_code=400, detail=coded_detail("tracker_session_cookie_invalid")) from exc


def _get_tracker_or_404(session: Session, tracker_id: int) -> Tracker:
    return get_or_404(session, Tracker, tracker_id, "tracker_not_found")


@router.get("", response_model=list[TrackerResponse])
def list_trackers(session: Session = Depends(get_session)):
    return [TrackerResponse.from_model(t) for t in session.query(Tracker).all()]


@router.post("", response_model=TrackerResponse, status_code=201)
def create_tracker(body: TrackerCreateRequest, session: Session = Depends(get_session)):
    check_destinations(body.base_url, body.announce_url)
    supported = REGISTRY.types("tracker")  # integrati e plugin (nazgarr/plugins)
    if body.adapter_type not in supported:
        raise HTTPException(
            status_code=400,
            detail=coded_detail(
                "tracker_adapter_type_unsupported",
                adapter_type=body.adapter_type, supported=sorted(supported),
            ),
        )
    key = upload_profiles.bundled_key_for_url(body.base_url) if body.upload_profile == "auto" else (
        None if body.upload_profile == "none" else body.upload_profile)
    if key and key not in {p["key"] for p in upload_profiles.list_bundled_profiles()}:
        raise HTTPException(status_code=400, detail=coded_detail("bundled_profile_not_found", key=key))
    tracker = Tracker(
        label=body.label, adapter_type=body.adapter_type, base_url=body.base_url,
        api_token=body.api_token, announce_url=body.announce_url,
        rate_limit_per_min=body.rate_limit_per_min or 30,
        rss_key=body.rss_key.strip() if body.rss_key and body.rss_key.strip() else None,
        history_session_cookie=_session_cookie(body.session_cookie),
        torrent_client_id=body.torrent_client_id, language=_language(body.language),
        min_seed_time_seconds=body.min_seed_time_seconds, min_ratio=body.min_ratio, seed_rule=body.seed_rule,
    )
    _apply_config(tracker, body.config, creating=True)
    session.add(tracker)
    session.commit()
    if key:
        upload_profiles.create_upload_profile(session, tracker, key)
        session.refresh(tracker)
    return TrackerResponse.from_model(tracker)


@router.patch("/{tracker_id}", response_model=TrackerResponse)
def update_tracker(
    tracker_id: int, body: TrackerUpdateRequest, request: Request, session: Session = Depends(get_session)
):
    check_destinations(body.base_url, body.announce_url)
    tracker = _get_tracker_or_404(session, tracker_id)
    missing = [name for name, stored, sent in (
        ("api_token", tracker.api_token, body.api_token),
    ) if stored and sent is None] + plugin_config.secrets_not_resent(tracker, "tracker", body.config)
    require_secrets_for_new_host(tracker.base_url, body.base_url, missing)
    if body.base_url is not None and host_changed(tracker.base_url, body.base_url):
        if body.rss_key is None:
            tracker.rss_key = None  # si riimpara dal nuovo host, non gli si manda quella del vecchio
        if body.session_cookie is None:
            tracker.history_session_cookie = None  # la sessione di un altro sito: mai mandata al nuovo
    if body.label is not None:
        tracker.label = body.label
    if body.base_url is not None:
        if body.base_url != tracker.base_url:
            tracker_icons.forget_icon(request.app.state.settings.data_dir, tracker.id)
        tracker.base_url = body.base_url
    if body.api_token is not None:
        tracker.api_token = body.api_token
    if body.announce_url is not None:
        tracker.announce_url = body.announce_url
    if body.rate_limit_per_min is not None:
        # 0 vale il default anche qui, come alla creazione: un plugin legge
        # la riga così com'è, e un limitatore a 0 non funziona.
        tracker.rate_limit_per_min = body.rate_limit_per_min or 30
    if body.enabled is not None:
        tracker.enabled = body.enabled
    if body.rss_key is not None:
        tracker.rss_key = body.rss_key.strip() or None
    if body.session_cookie is not None:
        tracker.history_session_cookie = _session_cookie(body.session_cookie)
    if "torrent_client_id" in body.model_fields_set:
        tracker.torrent_client_id = body.torrent_client_id
    if "language" in body.model_fields_set:
        tracker.language = _language(body.language)
    for field in ("min_seed_time_seconds", "min_ratio", "seed_rule"):
        if field in body.model_fields_set:
            setattr(tracker, field, getattr(body, field))
    if "config" in body.model_fields_set:
        _apply_config(tracker, body.config, creating=False)
    session.commit()
    return TrackerResponse.from_model(tracker)


class TrackerSessionCheckResponse(BaseModel):
    # unsupported: l'adapter del tracker non usa il sito (un plugin).
    status: Literal["ok", "no_session", "session_expired", "failed", "unsupported"]
    reason: str | None = None  # perché non è andata, in inglese (il testo dell'adapter)


@router.post("/{tracker_id}/session-check", response_model=TrackerSessionCheckResponse)
def check_session(tracker_id: int, session: Session = Depends(get_session)):
    """Se il cookie della sessione web salvato vale ancora: una pagina del
    sito in sola lettura, niente viene inviato."""
    tracker = _get_tracker_or_404(session, tracker_id)
    try:
        with adapter_factory.tracker(tracker) as adapter:
            result = adapter.check_web_session()
    except NotSupportedError:
        return TrackerSessionCheckResponse(status="unsupported")
    except Exception as exc:
        return TrackerSessionCheckResponse(status="failed", reason=safe_error(exc))
    status = result.status if result.status in ("ok", "no_session", "session_expired") else "failed"
    return TrackerSessionCheckResponse(status=status, reason=result.detail)


@router.get("/{tracker_id}/icon")
def tracker_icon(tracker_id: int, request: Request, session: Session = Depends(get_session)):
    """La favicon del tracker, dalla cache locale (nazgarr/torrents/tracker_icons.py)."""
    tracker = _get_tracker_or_404(session, tracker_id)
    path = tracker_icons.fetch_icon(request.app.state.settings.data_dir, tracker.id, tracker.base_url)
    if path is None:
        raise HTTPException(status_code=404, detail=coded_detail("tracker_icon_not_available"))
    return FileResponse(path, headers={"Cache-Control": "private, max-age=86400"})


def _tracker_busy(session: Session, tracker_id: int) -> bool:
    """Un'esecuzione di un suo candidato in corso, o un upload verso di lui
    che il worker sta portando avanti (o un suo tracker in invio): la
    cancellazione a cascata toglierebbe la riga su cui l'esecutore o il
    worker scriveranno, lasciando un hardlink o un torrent nel client senza
    traccia (docs/SPEC.md, "Deleting a tracker, a disk or a client").

    Anche un target col torrent fermo nel client in attesa del suo recheck
    (un reseed del flusso degli upload, o un Retry seed): il reconcile segue
    solo quella riga per avviarlo, e tolta a cascata restava fermo per
    sempre, come per l'eliminazione del job (upload_jobs._refuse_if_seed_pending).
    Come lì, non con il client spento: il reconcile non lo segue comunque, e
    un client spento per sempre bloccherebbe la cancellazione (audit 2026-10-18)."""
    executing = (
        session.query(SeedJob.id).join(Candidate, SeedJob.candidate_id == Candidate.id)
        .filter(Candidate.tracker_id == tracker_id, SeedJob.final_status == "in_progress")
        .first()
    )
    if executing is not None:
        return True
    uploading = (
        session.query(UploadTarget.id).join(UploadJob, UploadTarget.job_id == UploadJob.id)
        .filter(UploadTarget.tracker_id == tracker_id,
                or_(UploadJob.status.in_(upload_jobs.WORKER_STATES),
                    UploadTarget.status.in_(upload_jobs.BUSY_TARGET_STATES)))
        .first()
    )
    if uploading is not None:
        return True
    held = (
        session.query(UploadTarget.id)
        .join(TorrentClient, UploadTarget.torrent_client_id == TorrentClient.id)
        .filter(UploadTarget.tracker_id == tracker_id, UploadTarget.status == upload_jobs.TargetStatus.DONE,
                UploadTarget.error_message == upload_jobs.SEED_RECHECK_PENDING,
                TorrentClient.enabled.is_(True))
        .first()
    )
    return held is not None


class TrackerDeletePreview(BaseModel):
    """Cosa se ne va dal DB eliminando il tracker (decisione dell'utente,
    2026-10-18: la storia va con lui, ON DELETE CASCADE), da mostrare nella
    conferma. Sul tracker, sui dischi e nei client non cambia niente: un
    reseed in seed resta nel client, ne sparisce solo il record."""
    candidates: int  # torrent trovati su di lui, coi loro file
    reviews: int  # proposte di reseed, in attesa o già decise
    executions: int  # reseed eseguiti o tentati (seed_job)
    executions_seeding: int  # di cui ancora in seed nel client: lì restano
    match_attempts: int  # le ricerche fatte per ogni file
    upload_targets: int  # upload verso di lui (il job resta per gli altri tracker)
    upload_profile: bool
    busy: bool  # un'esecuzione, un upload o un recheck di un suo reseed in corso: il DELETE risponde 409 tracker_busy


@router.get("/{tracker_id}/delete-preview", response_model=TrackerDeletePreview)
def tracker_delete_preview(tracker_id: int, session: Session = Depends(get_session)):
    """In sola lettura: i conteggi di quello che DELETE /api/trackers/{id}
    toglierebbe, per la conferma dell'interfaccia e del CLI."""
    _get_tracker_or_404(session, tracker_id)
    candidate_ids = session.query(Candidate.id).filter(Candidate.tracker_id == tracker_id)

    def count(query) -> int:
        return query.order_by(None).count()

    jobs = session.query(SeedJob.id).filter(SeedJob.candidate_id.in_(candidate_ids))
    return TrackerDeletePreview(
        candidates=count(candidate_ids),
        reviews=count(session.query(MatchReview.id).filter(MatchReview.candidate_id.in_(candidate_ids))),
        executions=count(jobs),
        executions_seeding=count(jobs.filter(SeedJob.final_status == "seeding")),
        match_attempts=count(session.query(MatchAttempt.id).filter(MatchAttempt.tracker_id == tracker_id)),
        upload_targets=count(session.query(UploadTarget.id).filter(UploadTarget.tracker_id == tracker_id)),
        upload_profile=session.get(TrackerUploadProfile, tracker_id) is not None,
        busy=_tracker_busy(session, tracker_id),
    )


@router.delete("/{tracker_id}", status_code=204)
def delete_tracker(tracker_id: int, session: Session = Depends(get_session)):
    """Con il tracker se ne va quello che dipende da lui (candidati, review,
    esecuzioni, tentativi, target e profilo di upload: docs/schema.sql), ma
    non mentre un'esecuzione o un upload suo è in corso (audit 2026-10-18)."""
    tracker = _get_tracker_or_404(session, tracker_id)
    if _tracker_busy(session, tracker_id):
        raise HTTPException(status_code=409, detail=coded_detail("tracker_busy", label=tracker.label))
    session.delete(tracker)
    try:
        session.commit()
    except IntegrityError as exc:
        # Una riga che lo riferisce senza un'azione per la cancellazione (un
        # DB di prima della migrazione 10, o una riga nuova): un 409 col suo
        # codice, non un 500 "FOREIGN KEY constraint failed".
        session.rollback()
        raise HTTPException(status_code=409, detail=coded_detail("tracker_in_use", label=tracker.label)) from exc


def _check_naming_rules(rules: dict) -> None:
    """Regole di naming con la forma che naming.py si aspetta, o un 400 che
    dice quale chiave: salvate storte facevano fallire ogni upload verso il
    tracker, e l'anteprima rispondeva 500 (audit 2026-10-18)."""
    try:
        naming_rules.validate(rules)
    except naming_rules.NamingRulesError as exc:
        raise HTTPException(status_code=400, detail=from_coded_error(exc)) from exc


class UploadProfileCreateRequest(BaseModel):
    profile_key: str | None = None  # None = profilo custom vuoto (docs/SPEC.md §9)


class UploadProfileUpdateRequest(BaseModel):
    category_id_map: dict[str, int] | None = None
    type_id_map: dict[str, int] | None = None
    resolution_id_map: dict[str, int] | None = None
    naming_convention: str | None = None
    naming_rules: dict | None = None  # regole di naming: salvarle le segna come modificate dall'utente
    description_template: str | None = None
    default_anonymous: bool | None = None
    default_personal_release: bool | None = None
    default_internal: bool | None = None
    freeleech_options: list[int] | None = None
    default_freeleech: int | None = None


class UploadProfileResponse(BaseModel):
    tracker_id: int
    category_id_map: dict[str, int]
    type_id_map: dict[str, int]
    resolution_id_map: dict[str, int]
    naming_convention: str | None
    naming_rules: dict | None
    naming_version: int | None
    naming_customized: bool
    naming_update_available: int | None
    description_template: str | None
    default_anonymous: bool
    default_personal_release: bool
    default_internal: bool = False
    freeleech_options: list[int]
    default_freeleech: int | None
    source_profile_key: str | None

    @classmethod
    def from_model(cls, p: TrackerUploadProfile) -> "UploadProfileResponse":
        return cls(
            tracker_id=p.tracker_id,
            category_id_map=json.loads(p.category_id_map_json) if p.category_id_map_json else {},
            type_id_map=json.loads(p.type_id_map_json) if p.type_id_map_json else {},
            resolution_id_map=json.loads(p.resolution_id_map_json) if p.resolution_id_map_json else {},
            naming_convention=p.naming_convention,
            naming_rules=json.loads(p.naming_rules_json) if p.naming_rules_json else None,
            naming_version=p.naming_version,
            naming_customized=bool(p.naming_customized),
            naming_update_available=p.naming_update_available,
            description_template=p.description_template,
            default_anonymous=p.default_anonymous,
            default_personal_release=p.default_personal_release,
            default_internal=bool(p.default_internal),
            freeleech_options=upload_profiles.freeleech_options(p),
            default_freeleech=p.default_freeleech,
            source_profile_key=p.source_profile_key,
        )


class BundledUploadProfileResponse(BaseModel):
    key: str
    label: str
    adapter_type: str
    base_url: str | None  # host dell'API del tracker (mai l'announce URL, personale) — prefill comodo,
                          # mai vincolante: resta modificabile in fase di creazione del Tracker
    announce_url_page: str | None = None  # la pagina del tracker dove si copia il proprio announce URL


@router.get("/upload-profiles/bundled", response_model=list[BundledUploadProfileResponse])
def list_bundled_upload_profiles():
    return upload_profiles.list_bundled_profiles()


def _get_upload_profile_or_404(session: Session, tracker_id: int) -> TrackerUploadProfile:
    return get_or_404(session, TrackerUploadProfile, tracker_id, "tracker_no_upload_profile", tracker=tracker_id)


@router.post("/{tracker_id}/upload-profile", response_model=UploadProfileResponse, status_code=201)
def create_upload_profile(
    tracker_id: int, body: UploadProfileCreateRequest, session: Session = Depends(get_session)
):
    tracker = _get_tracker_or_404(session, tracker_id)
    if session.get(TrackerUploadProfile, tracker_id) is not None:
        raise HTTPException(status_code=409, detail=coded_detail("tracker_upload_profile_conflict", id=tracker_id))
    try:
        profile = upload_profiles.create_upload_profile(session, tracker, body.profile_key)
    except upload_profiles.ProfileNotFoundError as exc:
        raise HTTPException(status_code=400, detail=from_coded_error(exc)) from exc
    return UploadProfileResponse.from_model(profile)


@router.get("/{tracker_id}/upload-profile", response_model=UploadProfileResponse)
def get_upload_profile(tracker_id: int, session: Session = Depends(get_session)):
    return UploadProfileResponse.from_model(_get_upload_profile_or_404(session, tracker_id))


@router.patch("/{tracker_id}/upload-profile", response_model=UploadProfileResponse)
def update_upload_profile(
    tracker_id: int, body: UploadProfileUpdateRequest, session: Session = Depends(get_session)
):
    profile = _get_upload_profile_or_404(session, tracker_id)
    if body.category_id_map is not None:
        profile.category_id_map_json = json.dumps(body.category_id_map)
    if body.type_id_map is not None:
        profile.type_id_map_json = json.dumps(body.type_id_map)
    if body.resolution_id_map is not None:
        profile.resolution_id_map_json = json.dumps(body.resolution_id_map)
    if body.naming_convention is not None:
        profile.naming_convention = body.naming_convention
    if body.naming_rules is not None:
        _check_naming_rules(body.naming_rules)
    if body.naming_rules is not None and json.dumps(body.naming_rules) != profile.naming_rules_json:
        profile.naming_rules_json = json.dumps(body.naming_rules)
        profile.naming_customized = True
    if body.description_template is not None:
        # Si compila prima di salvarlo (audit 2026-10-18): un template rotto o
        # enorme falliva solo al primo upload, durante la preparazione.
        try:
            upload_description.check_template(body.description_template)
        except upload_description.DescriptionTemplateError as exc:
            raise HTTPException(status_code=400, detail=from_coded_error(exc)) from exc
        profile.description_template = body.description_template
    if body.default_anonymous is not None:
        profile.default_anonymous = body.default_anonymous
    if body.default_personal_release is not None:
        profile.default_personal_release = body.default_personal_release
    if body.default_internal is not None:
        profile.default_internal = body.default_internal
    if body.freeleech_options is not None:
        profile.freeleech_options_json = json.dumps(sorted({v for v in body.freeleech_options if 0 < v <= 100}))
    if body.default_freeleech is not None:
        profile.default_freeleech = body.default_freeleech or None
    session.commit()
    return UploadProfileResponse.from_model(profile)


@router.post("/{tracker_id}/upload-profile/naming/update", response_model=UploadProfileResponse)
def update_naming_rules(tracker_id: int, session: Session = Depends(get_session)):
    """Le regole di naming del profilo bundlato al posto di quelle modificate
    dall'utente (l'aggiornamento offerto da naming_update_available)."""
    profile = _get_upload_profile_or_404(session, tracker_id)
    try:
        upload_profiles.update_naming_from_bundled(session, profile)
    except upload_profiles.ProfileNotFoundError as exc:
        raise HTTPException(status_code=400, detail=from_coded_error(exc)) from exc
    return UploadProfileResponse.from_model(profile)


@router.post("/{tracker_id}/upload-profile/restore", response_model=UploadProfileResponse)
def restore_upload_profile(
    tracker_id: int, body: UploadProfileCreateRequest, session: Session = Depends(get_session)
):
    """Il profilo bundlato (quello d'origine se profile_key manca) al posto
    di quello che c'è, modifiche dell'utente comprese."""
    profile = _get_upload_profile_or_404(session, tracker_id)
    try:
        upload_profiles.restore_upload_profile(session, profile, body.profile_key)
    except upload_profiles.ProfileNotFoundError as exc:
        raise HTTPException(status_code=400, detail=from_coded_error(exc)) from exc
    return UploadProfileResponse.from_model(profile)


class NamingPreviewRequest(BaseModel):
    naming_rules: dict


@router.post("/{tracker_id}/upload-profile/naming/preview")
def preview_naming_rules(tracker_id: int, body: NamingPreviewRequest, session: Session = Depends(get_session)) -> dict:
    """Le regole in modifica (non ancora salvate) applicate a un upload
    reale o a un esempio: un nome per template e il valore di ogni variabile."""
    _get_upload_profile_or_404(session, tracker_id)
    tracker = _get_tracker_or_404(session, tracker_id)
    _check_naming_rules(body.naming_rules)
    return upload_decision.preview_names(session, body.naming_rules, tracker.language)


@router.delete("/{tracker_id}/upload-profile", status_code=204)
def delete_upload_profile(tracker_id: int, session: Session = Depends(get_session)):
    profile = _get_upload_profile_or_404(session, tracker_id)
    session.delete(profile)
    session.commit()
