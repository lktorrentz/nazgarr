"""API del flusso di upload v2 (docs/SPEC.md §9 "Upload flow v2"). La
sorgente si sceglie con lo stesso file browser scoped per disco già usato
altrove (nazgarr/core/fs_scope.py, mai un path assoluto passato dal client); il
lavoro vero lo fa il worker (nazgarr/upload/worker.py), l'API crea i job, li
mostra e registra le decisioni dell'utente ai due punti di approvazione."""

import json
import logging
from datetime import datetime
from typing import Any, Literal, NotRequired, TypedDict

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, with_config
from sqlalchemy import func
from sqlalchemy.orm import Session, defer, joinedload, object_session, selectinload

from nazgarr.core import settings_repo
from nazgarr.core.db_utils import as_utc
from nazgarr.core.errors import coded_detail, from_coded_error
from nazgarr.core.fs_scope import ScopeViolation
from nazgarr.core.logs import safe_error
from nazgarr.core.models import Disk, TorrentClient, TrackerUploadProfile, UploadEvent, UploadJob, UploadTarget
from nazgarr.integrations import adapter_factory
from nazgarr.integrations.adapter_factory import TmdbApiKeyMissingError
from nazgarr.library import episode_orders
from nazgarr.upload import decision as upload_decision
from nazgarr.upload import execute as upload_execute
from nazgarr.upload import file_names as upload_file_names
from nazgarr.upload import identify as upload_identify
from nazgarr.upload import jobs as upload_jobs
from nazgarr.upload import match_score as upload_match_score
from nazgarr.upload import naming_rules
from nazgarr.upload import pack as upload_pack
from nazgarr.upload import profiles as upload_profiles
from nazgarr.upload import split as upload_split
from nazgarr.upload import verify as upload_verify
from nazgarr.upload.jobs import UploadJobError
from nazgarr.web.deps import get_or_404, get_session

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/uploads", tags=["uploads"])


class ForcedIds(BaseModel):
    tmdb: str | None = None  # "123", "movie/123", "tv/123" o un URL TMDB
    imdb: str | None = None
    tvdb: int | None = None
    mal: int | None = None


class TrackerChoice(BaseModel):
    freeleech: int | None = None  # percentuale scelta alla creazione, tra quelle del profilo


class UploadCreateRequest(BaseModel):
    disk_id: int
    relative_path: str | None = None
    # Un pack di video scelti a mano (nazgarr/upload/pack.py), al posto di relative_path.
    files: list[str] | None = Field(default=None, max_length=upload_pack.MAX_FILES)
    tracker_ids: list[int] | None = None  # None = tutti i tracker con un profilo di upload
    forced_ids: ForcedIds | None = None
    overrides: dict | None = None
    tracker_choices: dict[int, TrackerChoice] | None = None


class UploadMatchRequest(BaseModel):
    content_type: str  # movie | tv
    tmdb_id: int
    kind: str  # movie | episode | season_pack | complete_pack
    seasons: list[int] = []  # nella numerazione dell'ordinamento scelto
    episode: int | None = None
    episode_order: str | None = None  # l'ordinamento degli episodi (nazgarr/library/episode_orders.py)


class OrderEpisodeResponse(BaseModel):
    number: int
    titles: list[str]
    air_date: str | None = None
    refs: list[list[int]]


class OrderSeasonResponse(BaseModel):
    season_number: int
    episodes: list[OrderEpisodeResponse]


class EpisodeOrderResponse(BaseModel):
    key: str
    label: str
    source: str
    seasons: list[OrderSeasonResponse]


class OrderFit(BaseModel):
    score: float  # quanto combacia (numero di episodi per stagione, poi i numeri)
    coverage: float = 0  # quanti numeri dei file esistono
    matched: int
    files: int
    complete_seasons: int


class OrderWarning(BaseModel):
    code: str  # files_not_tvdb_aired
    order: str  # quello scelto, che combacia con i file
    tvdb: str  # TVDB aired, che invece non combacia


class EpisodeOrdersResponse(BaseModel):
    # Com'è andata ogni fonte: tmdb, sonarr, tvdb -> ok | no_key | not_found |
    # not_needed | no_tvdb_id | empty | error: ... (perché un ordinamento manca).
    sources: dict[str, str] = {}
    orders: list[EpisodeOrderResponse]
    recommended: str | None
    files_order: str | None  # la numerazione dei numeri dei file: da qui si traducono
    fits: dict[str, OrderFit]
    warning: OrderWarning | None
    found: dict[str, dict[int, list[int]]]  # gli episodi dei file, tradotti in ogni ordinamento


class UploadOverridesRequest(BaseModel):
    overrides: dict


class TargetDecision(BaseModel):
    target_id: int
    action: str  # upload | reseed | skip
    name: str | None = None
    flags: dict[str, bool | int] | None = None  # freeleech è una percentuale
    category_id: int | None = None
    type_id: int | None = None
    resolution_id: int | None = None
    reseed_torrent_id: str | None = None
    # Nel client: assenti = i default del client, vuoti = nessuno.
    client_category: str | None = None
    client_tags: str | None = None


class UploadApproveRequest(BaseModel):
    targets: list[TargetDecision]
    # Parte a quest'ora invece che appena tocca a lui (None o un'ora passata: subito).
    scheduled_at: datetime | None = None


class UploadScheduleRequest(BaseModel):
    scheduled_at: datetime | None = None  # None: parte appena tocca a lui


class UploadVerifyRequest(BaseModel):
    torrent_id_remote: str


class UploadReidentifyRequest(BaseModel):
    forced_ids: ForcedIds | None = None


class UploadRetryRequest(BaseModel):
    # Anche i tracker il cui invio si è fermato a metà (non si sa se il
    # tracker l'ha ricevuto): solo dopo averlo controllato sul tracker.
    confirm_interrupted: bool = False


# I campi a forma fissa del flusso di upload, tipizzati (audit 2026-10-18):
# erano dict liberi, e il frontend ne riscriveva a mano la forma in più
# posti; un nome cambiato qui non lo vedeva nessun controllo. Tolleranti con
# le righe salvate da versioni precedenti (default per le chiavi aggiunte
# dopo): una risposta che non si valida sarebbe un 500 per tutta la pagina.


class UploadDupeVerification(BaseModel):
    """Il full hash check di un dupe (nazgarr/upload/verify.py run_check)."""

    status: Literal["passed", "failed", "error"]
    reason: str | None = None
    pieces: int | None = None
    ok: int | None = None
    mismatched: int | None = None
    unreadable: int | None = None


class UploadDupeResponse(BaseModel):
    """Un torrent del tracker trovato dal dupe check, col suo verdetto
    (nazgarr/upload/dupes.py classify). Mai il link di download."""

    model_config = ConfigDict(coerce_numbers_to_str=True)

    torrent_id_remote: str
    name: str = ""
    size_bytes: int | None = None
    verdict: Literal["identical", "same_slot", "trump", "possible", "different"]
    # 0-100: quanto dello slot è noto e uguale (same_slot, trump, possible); None per gli altri.
    score: int | None = None
    reasons: list[str] = []
    unknown: list[str] = []  # i tratti che non si sanno (coverage, resolution, type, codec, hdr, languages)
    seeders: int | None = None  # 0: un torrent morto; None se il tracker non lo dice
    size_delta: float | None = None  # (sorgente - torrent) / torrent
    verification: UploadDupeVerification | None = None


class UploadLayoutVideo(BaseModel):
    relative_path: str
    size_bytes: int = 0
    season: int | None = None
    episodes: list[int] = []


class UploadLayoutResponse(BaseModel):
    """La sorgente come l'ha letta l'identificazione (nazgarr/upload/source.py
    SourceLayout, o quella di un pack in nazgarr/upload/pack.py)."""

    kind: Literal["movie", "episode", "season_pack", "complete_pack"]
    content_type: Literal["movie", "tv"]
    videos: list[UploadLayoutVideo] = []
    other_files: int = 0
    total_size_bytes: int = 0
    main_video: str | None = None
    title: str | None = None
    year: int | None = None
    seasons: list[int] = []
    episodes_by_season: dict[int, list[int]] = {}  # stagione -> episodi trovati nei nomi
    release_group: str | None = None


class UploadClientMatch(BaseModel):
    """Un torrent dei client con gli stessi video (nazgarr/upload/analysis.py _client_matches)."""

    model_config = ConfigDict(coerce_numbers_to_str=True)

    client_id: int | None = None
    client: str | None = None
    name: str
    info_hash: str
    tracker_host: str | None = None
    category: str | None = None
    state: str | None = None
    match: Literal["hardlink", "same_size"]
    videos_matched: int = 0
    videos_hardlinked: int | None = None
    videos_total: int = 0


class UploadArrGrab(BaseModel):
    """Un grab di Radarr/Sonarr dei file della sorgente (analysis.py _arr_grabs)."""

    model_config = ConfigDict(coerce_numbers_to_str=True)

    tracker_host: str | None = None
    torrent_id_remote: str | None = None
    info_hash: str | None = None
    indexer: str | None = None


class UploadNameSource(BaseModel):
    """Da dove si leggono i valori del nome (analysis.py name_source)."""

    name: str
    origin: str
    fallback: str | None = None


class UploadSeedingHere(BaseModel):
    name: str
    client: str | None = None


class UploadLanguageCheck(BaseModel):
    """La lingua del tracker nell'audio del file (naming.py audio_language_check)."""

    language: str
    status: Literal["present", "missing", "unknown"]


class UploadTypeBasis(BaseModel):
    """Perché tipo e sorgente sono quelli rilevati (naming.py release_values)."""

    type: str
    source: str | None = None
    evidence: list[str] = []
    encoder: str | None = None


class UploadOtherJob(BaseModel):
    """Un altro upload non finito con gli stessi video (analysis.py other_jobs)."""

    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    id: int
    status: str
    kind: str | None = None
    title: str | None = None
    seasons: list[int] = []
    episode: int | None = None
    videos: int = 0  # quanti video in comune


class UploadAnalysisResponse(BaseModel):
    """Quello che l'analisi ha trovato (nazgarr/upload/analysis.py) e la
    decisione vi ha aggiunto (nazgarr/upload/decision.py propose). Le chiavi
    lette dal frontend sono tipizzate; le altre passano com'erano."""

    model_config = ConfigDict(extra="allow")

    total_size_bytes: int | None = None
    file_count: int | None = None
    client_matches: list[UploadClientMatch] = []
    arr_grabs: list[UploadArrGrab] = []
    # nazgarr/library/mediainfo.py extract_summary; la sua forma la descrive
    # frontend/src/lib/mediainfo.ts (MediaInfoSummary).
    mediainfo: Any = None
    name_source: UploadNameSource | None = None
    seeding_here: dict[str, UploadSeedingHere] = {}  # id del target -> il torrent che già seeda quei byte
    dupes_truncated: dict[str, int] = {}  # id del target -> risultati letti da un catalogo tagliato
    languages: dict[str, UploadLanguageCheck] = {}  # id del tracker -> la sua lingua nell'audio
    detected: dict[str, Any] = {}  # i valori rilevati per il nome, senza override
    type_basis: UploadTypeBasis | None = None
    field_options: dict[str, list[str]] = {}  # i valori che i tracker del job accettano
    other_jobs: list[UploadOtherJob] = []  # altri upload non finiti sugli stessi file


# I candidati TMDB come TypedDict, non BaseModel: una chiave che un job di
# prima non ha (confidence, source) resta assente invece di diventare null,
# come la legge la schermata di match (MetadataCandidate).
@with_config(ConfigDict(extra="allow"))
class UploadCandidateParts(TypedDict):
    """Da cosa viene la confidence di un candidato (nazgarr/upload/match_score.py parts)."""

    basis: Literal["forced", "arr", "name"]
    title: NotRequired[float]
    title_guess: NotRequired[str | None]
    title_matched: NotRequired[str | None]
    year: NotRequired[float]
    year_guess: NotRequired[int | None]
    year_candidate: NotRequired[int | None]
    year_waived: NotRequired[bool]
    type: NotRequired[float]
    type_guess: NotRequired[str]
    type_candidate: NotRequired[str]
    ambiguous: NotRequired[float]


class UploadCandidateResponse(TypedDict):
    """Un candidato TMDB dell'identificazione (nazgarr/upload/identify.py
    find_candidates), con la sua confidence. Le altre chiavi salvate (i
    titoli in altre lingue, i dettagli di un id forzato) restano nel job."""

    tmdb_id: int
    content_type: Literal["movie", "tv"]
    title: str | None
    original_title: NotRequired[str | None]
    year: int | None
    poster_path: str | None
    overview: NotRequired[str | None]
    source: NotRequired[str]
    confidence: NotRequired[float]
    ambiguous: NotRequired[bool]
    confidence_parts: NotRequired[UploadCandidateParts]


class UploadTargetResponse(BaseModel):
    id: int
    tracker_id: int
    tracker_label: str
    tracker_base_url: str | None  # per i link ai torrent del dupe check (UNIT3D: /torrents/<id>)
    torrent_client_id: int | None
    torrent_client_label: str | None
    status: str
    suggested_action: str | None
    action: str | None
    dupes: list[UploadDupeResponse]
    reseed_torrent_id: str | None
    proposed_name: str | None
    approved_name: str | None
    flags: dict
    freeleech_options: list[int]
    category_id: int | None
    type_id: int | None
    resolution_id: int | None
    info_hash: str | None
    torrent_id_remote: str | None
    remote_url: str | None  # pagina del torrent sul tracker (UNIT3D: /torrents/<id>)
    error_message: str | None
    finished_at: datetime | None
    # Le mappe del profilo, per scegliere categoria/tipo/risoluzione a mano.
    category_id_map: dict[str, int]
    type_id_map: dict[str, int]
    resolution_id_map: dict[str, int]
    client_category: str | None = None  # scelti all'approvazione
    client_tags: str | None = None
    client_defaults: dict = {}  # {"category", "tags_upload", "tags_reseed"} dal client del tracker
    # Il tracker ha la sessione web: il claim della resurrection lo fa Nazgarr
    # prima del reseed (nazgarr/upload/execute.py::_claim_resurrection).
    auto_resurrection: bool = False

    @classmethod
    def from_model(
        cls, t: UploadTarget, profiles: dict[int, TrackerUploadProfile] | None = None,
    ) -> "UploadTargetResponse":
        # profiles: già letti per tutta la lista, invece di una query per target.
        profile = (
            profiles.get(t.tracker_id) if profiles is not None
            else object_session(t).get(TrackerUploadProfile, t.tracker_id)
        )
        return cls(
            id=t.id, tracker_id=t.tracker_id, tracker_label=t.tracker.label,
            tracker_base_url=t.tracker.base_url.rstrip("/") if t.tracker.base_url else None,
            auto_resurrection=bool(t.tracker.history_session_cookie),
            torrent_client_id=t.torrent_client_id,
            torrent_client_label=t.torrent_client.label if t.torrent_client else None,
            status=t.status, suggested_action=t.suggested_action, action=t.action,
            # Mai il link di download (UploadDupeResponse non l'ha): nessuno lo
            # usa da qui, e quelli salvati prima avevano la chiave personale
            # del tracker (audit 2026-10-18).
            dupes=[UploadDupeResponse.model_validate(d) for d in _loads(t.dupes_json, [])],
            reseed_torrent_id=t.reseed_torrent_id,
            proposed_name=t.proposed_name, approved_name=t.approved_name,
            flags=_loads(t.flags_json, {}), freeleech_options=upload_profiles.freeleech_options(profile),
            category_id=t.category_id, type_id=t.type_id,
            resolution_id=t.resolution_id, info_hash=t.info_hash, torrent_id_remote=t.torrent_id_remote,
            remote_url=_remote_url(t),
            error_message=t.error_message, finished_at=t.finished_at,
            category_id_map=_loads(profile.category_id_map_json if profile else None, {}),
            type_id_map=_loads(profile.type_id_map_json if profile else None, {}),
            resolution_id_map=_loads(profile.resolution_id_map_json if profile else None, {}),
            client_category=t.client_category, client_tags=t.client_tags,
            client_defaults=upload_decision.client_label_defaults(t.job, t),
        )


class UploadEventResponse(BaseModel):
    id: int
    target_id: int | None
    created_at: datetime
    level: str
    code: str
    params: dict

    @classmethod
    def from_model(cls, e: UploadEvent) -> "UploadEventResponse":
        return cls(
            id=e.id, target_id=e.target_id, created_at=e.created_at, level=e.level, code=e.code,
            params=_loads(e.params_json, {}),
        )


class UploadJobSummary(BaseModel):
    id: int
    disk_id: int | None
    relative_path: str
    is_dir: bool
    kind: str | None
    status: str
    stage: str | None
    progress_done: int | None
    progress_total: int | None
    queue_position: int | None
    content_type: str | None
    tmdb_id: int | None
    title: str | None
    year: int | None
    poster_path: str | None
    seasons: list[int]
    episode: int | None
    error_message: str | None
    created_at: datetime | None
    finished_at: datetime | None
    targets: list[UploadTargetResponse]
    origin: str | None = None  # "watch": dalla cartella osservata (nazgarr/upload/watch.py); "pack"
    pack_name: str | None = None  # un pack di file scelti a mano (nazgarr/upload/pack.py)
    scheduled_at: datetime | None = None  # in coda per partire a quest'ora (UTC)

    @classmethod
    def fields_from(cls, j: UploadJob, profiles: dict[int, TrackerUploadProfile] | None = None) -> dict:
        return dict(
            id=j.id, disk_id=j.disk_id, relative_path=j.relative_path, is_dir=j.is_dir, kind=j.kind,
            status=j.status, stage=j.stage, progress_done=j.progress_done, progress_total=j.progress_total,
            queue_position=j.queue_position, content_type=j.content_type, tmdb_id=j.tmdb_id, title=j.title,
            year=j.year, poster_path=j.poster_path, seasons=_loads(j.seasons_json, []), episode=j.episode,
            error_message=j.error_message, created_at=j.created_at, finished_at=j.finished_at,
            targets=[UploadTargetResponse.from_model(t, profiles) for t in j.targets], origin=j.origin,
            pack_name=upload_pack.name(j) if upload_pack.is_pack(j) else None,
            scheduled_at=as_utc(j.scheduled_at),
        )

    @classmethod
    def from_model(cls, j: UploadJob, profiles: dict[int, TrackerUploadProfile] | None = None) -> "UploadJobSummary":
        return cls(**cls.fields_from(j, profiles))


class UploadJobDetail(UploadJobSummary):
    source_path: str
    pack_files: list[str] = []  # relativi al disco
    imdb_id: str | None
    tvdb_id: int | None
    mal_id: int | None
    forced_ids: dict
    overrides: dict
    layout: UploadLayoutResponse | None
    candidates: list[UploadCandidateResponse]
    analysis: UploadAnalysisResponse | None
    mediainfo_text: str | None
    screenshot_urls: list[str]
    descriptions: dict[int, str]  # target_id -> descrizione inviata, solo nel dettaglio (è lunga)
    events: list[UploadEventResponse]
    episode_order: str | None = None  # l'ordinamento degli episodi scelto al match
    episode_order_label: str | None = None

    @classmethod
    def from_model(cls, j: UploadJob) -> "UploadJobDetail":
        return cls(
            **cls.fields_from(j),
            source_path=j.source_path, pack_files=_loads(j.pack_json, {}).get("files", []),
            imdb_id=j.imdb_id, tvdb_id=j.tvdb_id, mal_id=j.mal_id,
            forced_ids=_loads(j.forced_ids_json, {}), overrides=_loads(j.overrides_json, {}),
            layout=_loads(j.layout_json, None),
            candidates=upload_match_score.explained(_loads(j.candidates_json, []), _loads(j.layout_json, None)),
            analysis=_loads(j.analysis_json, None), mediainfo_text=j.mediainfo_text,
            screenshot_urls=_loads(j.screenshot_urls_json, []),
            descriptions={t.id: t.description_rendered for t in j.targets if t.description_rendered},
            events=[UploadEventResponse.from_model(e) for e in j.events],
            episode_order=j.episode_order,
            episode_order_label=(_loads(j.episode_order_json, {}).get("chosen") or {}).get("label"),
        )


def _remote_url(t: UploadTarget) -> str | None:
    torrent_id = t.torrent_id_remote or (t.reseed_torrent_id if t.action == "reseed" else None)
    if not torrent_id or not t.tracker.base_url:
        return None
    return f"{t.tracker.base_url.rstrip('/')}/torrents/{torrent_id}"


def _loads(raw: str | None, default):
    return json.loads(raw) if raw else default


def _get_job_or_404(session: Session, upload_id: int) -> UploadJob:
    return get_or_404(session, UploadJob, upload_id, "upload_job_not_found")


def _worker(request: Request):
    return request.app.state.upload_worker


# Rifiuti per lo stato del momento, non per una richiesta sbagliata: 409.
_CONFLICT_CODES = {"upload_job_busy", "upload_retry_nothing", "upload_retry_needs_confirm"}


def _refused(exc: UploadJobError) -> HTTPException:
    return HTTPException(status_code=409 if exc.code in _CONFLICT_CODES else 400, detail=from_coded_error(exc))


def _summaries(session: Session, query) -> list[UploadJobSummary]:
    """La lista senza una query per job e per target (audit 2026-10-18: 300
    job con 3 tracker erano 1205 query, a ogni poll di 2 s): target, tracker
    e client in due query, i profili in una, e senza le colonne lunghe che
    il riepilogo non mostra (mediainfo, analisi, layout, candidati)."""
    jobs = query.options(
        selectinload(UploadJob.targets).options(
            joinedload(UploadTarget.tracker), joinedload(UploadTarget.torrent_client),
            defer(UploadTarget.description_rendered),
        ),
        defer(UploadJob.mediainfo_text), defer(UploadJob.analysis_json), defer(UploadJob.layout_json),
        defer(UploadJob.candidates_json), defer(UploadJob.episode_order_json),
    ).all()
    profiles = {p.tracker_id: p for p in session.query(TrackerUploadProfile).all()}
    return [UploadJobSummary.from_model(j, profiles) for j in jobs]


def _active_jobs(session: Session):
    return session.query(UploadJob).filter(UploadJob.status.notin_(upload_jobs.FINAL_STATES))


@router.get("", response_model=list[UploadJobSummary])
def list_uploads(scope: Literal["all", "active"] = "all", session: Session = Depends(get_session)):
    """Dal più recente. scope=active: solo quelli non finiti, pochi, per la
    coda che si interroga ogni 2 s; lo storico è paginato a parte
    (/api/uploads/history). Senza scope tutti, come prima (CLI, istanze
    remote di versioni precedenti)."""
    query = _active_jobs(session) if scope == "active" else session.query(UploadJob)
    return _summaries(session, query.order_by(UploadJob.id.desc()))


class UploadHistoryResponse(BaseModel):
    jobs: list[UploadJobSummary]  # finiti, dal più recente
    total: int  # tutti quelli finiti, non solo questa pagina
    next_before_id: int | None  # per la pagina dopo; None = finiti


@router.get("/history", response_model=UploadHistoryResponse)
def upload_history(
    limit: int = Query(50, ge=1, le=500), before_id: int | None = None, session: Session = Depends(get_session),
):
    """Lo storico (upload finiti) a pagine: before_id = l'id più piccolo della
    pagina prima. Non si interroga in continuazione, e cresce senza fine."""
    final = session.query(UploadJob).filter(UploadJob.status.in_(upload_jobs.FINAL_STATES))
    total = final.count()
    page = final.filter(UploadJob.id < before_id) if before_id is not None else final
    jobs = _summaries(session, page.order_by(UploadJob.id.desc()).limit(limit + 1))
    more = len(jobs) > limit
    jobs = jobs[:limit]
    return UploadHistoryResponse(jobs=jobs, total=total, next_before_id=jobs[-1].id if more else None)


class QueueOrderRequest(BaseModel):
    job_ids: list[int]


class UploadTrackerResponse(BaseModel):
    id: int
    label: str
    torrent_client_id: int | None  # dove andrà in seed il torrent generato
    torrent_client_label: str | None
    freeleech_options: list[int]
    default_freeleech: int | None


@router.get("/trackers", response_model=list[UploadTrackerResponse])
def list_upload_trackers(session: Session = Depends(get_session)):
    """I tracker selezionabili per un upload (abilitati, con un profilo) e il
    client che userebbero: quello del tracker o, senza, il primo abilitato."""
    out = []
    for tracker in upload_jobs.upload_trackers(session):
        client_id = upload_jobs.default_client_id(session, tracker)
        client = session.get(TorrentClient, client_id) if client_id is not None else None
        profile = session.get(TrackerUploadProfile, tracker.id)
        out.append(UploadTrackerResponse(
            id=tracker.id, label=tracker.label, torrent_client_id=client_id,
            torrent_client_label=client.label if client is not None else None,
            freeleech_options=upload_profiles.freeleech_options(profile),
            default_freeleech=profile.default_freeleech if profile else None,
        ))
    return out


class ImageHostStatusResponse(BaseModel):
    with_api_key: list[str]
    usable: list[str]
    removed: list[str] = []  # tolti da un aggiornamento, finché l'avviso non si chiude
    order: list[str] = []  # tutti gli host registrati, nell'ordine in cui si provano


class UploadNotice(BaseModel):
    id: int  # l'id dell'evento del job: il frontend ricorda l'ultimo visto
    upload_id: int
    kind: str  # detected | ready
    title: str | None
    year: int | None
    path: str


class UploadNoticesResponse(BaseModel):
    notices: list[UploadNotice]
    latest_id: int  # da qui parte un browser che non ha mai chiesto (niente arretrati)


_NOTICE_CODES = {"job_created": "detected", "analysis_done": "ready"}


@router.get("/notices", response_model=UploadNoticesResponse)
def upload_notices(after: int | None = None, session: Session = Depends(get_session)):
    """Gli avvisi nell'app per le release della cartella osservata: rilevata,
    e pronta per la tua decisione. Solo dopo l'evento `after`; senza, nessun
    arretrato, solo da dove partire."""
    latest = session.query(func.max(UploadEvent.id)).scalar() or 0
    if after is None:
        return UploadNoticesResponse(notices=[], latest_id=latest)
    rows = (
        session.query(UploadEvent, UploadJob)
        .join(UploadJob, UploadJob.id == UploadEvent.job_id)
        .filter(UploadEvent.id > after, UploadEvent.code.in_(_NOTICE_CODES), UploadJob.origin == "watch")
        .order_by(UploadEvent.id)
        .limit(50)
        .all()
    )
    return UploadNoticesResponse(
        notices=[
            UploadNotice(id=event.id, upload_id=job.id, kind=_NOTICE_CODES[event.code], title=job.title,
                         year=job.year, path=job.relative_path)
            for event, job in rows
        ],
        latest_id=latest,
    )


@router.get("/image-hosts", response_model=ImageHostStatusResponse)
def image_host_status(session: Session = Depends(get_session)):
    """Gli host di immagini configurati, per avvisare prima di un upload."""
    return ImageHostStatusResponse(**adapter_factory.image_host_status(session))


class FileNamingRequest(BaseModel):
    rules: dict


class FileNamingResponse(BaseModel):
    rules: dict[str, Any]  # le regole salvate (nazgarr/upload/naming_rules.py)
    default: dict[str, Any]  # quelle di default, per tornarci


class FileNamingPreviewSample(BaseModel):
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    kind: Literal["job", "example"]
    label: str | None = None


class FileNamingPreviewExample(BaseModel):
    kind: Literal["job", "example"]
    key: str
    label: str
    name: str


class FileNamingPreviewResponse(BaseModel):
    """L'anteprima delle regole (decision.py preview_names), tipizzata (audit
    2026-10-18). I valori come testo: l'anno è un numero nei valori del nome."""

    model_config = ConfigDict(coerce_numbers_to_str=True)

    sample: FileNamingPreviewSample
    variables: dict[str, str | None]
    names: dict[str, str]
    examples: list[FileNamingPreviewExample]


@router.get("/file-naming", response_model=FileNamingResponse)
def get_file_naming(session: Session = Depends(get_session)):
    """Il pattern dei nomi dei file nel torrent (nazgarr/upload/file_names.py):
    quello salvato, e quello di default per tornarci."""
    return {"rules": upload_file_names.rules(session), "default": upload_file_names.DEFAULT_RULES}


def _check_naming_rules(rules: dict) -> None:
    """Regole con la forma che naming.py si aspetta, o un 400 che dice quale
    chiave (audit 2026-10-18): salvate storte facevano fallire l'analisi di
    ogni upload, e l'anteprima rispondeva 500. Come per i profili
    (nazgarr/api/trackers.py)."""
    try:
        naming_rules.validate(rules)
    except naming_rules.NamingRulesError as exc:
        raise HTTPException(status_code=400, detail=from_coded_error(exc)) from exc


@router.put("/file-naming", response_model=FileNamingResponse)
def put_file_naming(body: FileNamingRequest, session: Session = Depends(get_session)):
    _check_naming_rules(body.rules)
    settings_repo.set_setting(session, upload_file_names.SETTING, json.dumps(body.rules))
    return get_file_naming(session)


@router.post("/file-naming/preview", response_model=FileNamingPreviewResponse)
def preview_file_naming(body: FileNamingRequest, session: Session = Depends(get_session)):
    """Le regole in modifica sugli esempi (e sull'ultimo upload), con i nomi
    puliti come diventano i file: niente ":" né accenti, un punto alla volta."""
    _check_naming_rules(body.rules)
    preview = upload_decision.preview_names(session, body.rules)
    preview["names"] = {key: upload_file_names.sanitize(name) + ".mkv" for key, name in preview["names"].items()}
    for example in preview.get("examples", []):
        example["name"] = upload_file_names.sanitize(example["name"]) + ".mkv"
    return preview


@router.put("/queue", response_model=list[UploadJobSummary])
def reorder_queue(body: QueueOrderRequest, session: Session = Depends(get_session)):
    """Ordine della coda: il primo parte per primo quando il worker è libero.
    Risponde con i job non finiti, come GET /api/uploads?scope=active."""
    upload_jobs.reorder_queue(session, body.job_ids)
    return _summaries(session, _active_jobs(session).order_by(UploadJob.id.desc()))


@router.get("/{upload_id}", response_model=UploadJobDetail)
def get_upload(upload_id: int, session: Session = Depends(get_session)):
    return UploadJobDetail.from_model(_get_job_or_404(session, upload_id))


@router.post("", response_model=UploadJobDetail, status_code=201)
def create_upload(body: UploadCreateRequest, request: Request, session: Session = Depends(get_session)):
    disk = session.get(Disk, body.disk_id)
    if disk is None:
        raise HTTPException(status_code=404, detail=coded_detail("disk_not_found", id=body.disk_id))
    try:
        forced = body.forced_ids.model_dump() if body.forced_ids else None
        choices = {tid: c.model_dump() for tid, c in (body.tracker_choices or {}).items()}
        # Le stesse regole della decisione (audit 2026-10-18): prima gli
        # override dati alla creazione non passavano da nessun controllo
        # (un content_name ".", screenshot senza limite, chiavi sconosciute).
        overrides = upload_decision.clean_overrides(body.overrides)
        if body.files is not None:
            job = upload_pack.create_job(session, disk, body.files, body.tracker_ids, forced, overrides, choices)
        elif body.relative_path:
            job = upload_jobs.create_job(session, disk, body.relative_path, body.tracker_ids, forced, overrides,
                                         choices)
        else:
            raise UploadJobError("upload_source_not_found", path="")
    except (ScopeViolation, UploadJobError) as exc:
        raise HTTPException(status_code=400, detail=from_coded_error(exc)) from exc
    _worker(request).kick(job.id, job.status)
    return UploadJobDetail.from_model(job)


@router.post("/{upload_id}/cancel", response_model=UploadJobDetail)
def cancel_upload(upload_id: int, session: Session = Depends(get_session)):
    job = _get_job_or_404(session, upload_id)
    try:
        upload_jobs.cancel_job(session, job)
    except UploadJobError as exc:
        raise HTTPException(status_code=400, detail=from_coded_error(exc)) from exc
    return UploadJobDetail.from_model(job)


@router.post("/{upload_id}/resume", response_model=UploadJobDetail)
def resume_upload(upload_id: int, request: Request, session: Session = Depends(get_session)):
    """Un upload annullato riparte da dove si era fermato (upload_jobs.resume_job)."""
    job = _get_job_or_404(session, upload_id)
    try:
        upload_jobs.resume_job(session, job, active=_worker(request).is_active(job.id))
    except UploadJobError as exc:
        raise _refused(exc) from exc
    if job.status in upload_jobs.WORKER_STATES:
        _worker(request).kick(job.id, job.status)
    return UploadJobDetail.from_model(job)


@router.post("/{upload_id}/retry", response_model=UploadJobDetail)
def retry_upload(
    upload_id: int, request: Request, body: UploadRetryRequest | None = None, session: Session = Depends(get_session)
):
    """Un nuovo tentativo dei tracker falliti, con le scelte già fatte
    (upload_jobs.retry_failed): di nuovo in coda, senza ripassare dal match."""
    job = _get_job_or_404(session, upload_id)
    try:
        upload_jobs.retry_failed(session, job, confirm_interrupted=bool(body and body.confirm_interrupted),
                                 active=_worker(request).is_active(job.id))
    except UploadJobError as exc:
        raise _refused(exc) from exc
    _worker(request).kick(job.id, job.status)
    return UploadJobDetail.from_model(job)


@router.delete("/{upload_id}", status_code=204)
def delete_upload(upload_id: int, request: Request, session: Session = Depends(get_session)):
    job = _get_job_or_404(session, upload_id)
    try:
        upload_jobs.delete_job(session, job, request.app.state.settings.data_dir,
                               active=_worker(request).is_active(job.id))
    except UploadJobError as exc:
        raise _refused(exc) from exc


def _confirm(session: Session, job: UploadJob, body: UploadMatchRequest, split: bool = False) -> dict | None:
    """La conferma del match (upload_identify.confirm), con gli errori come
    risposte HTTP. split: con la divisione in episodi chiesta, scritta nello
    stesso UPDATE (SPLIT_REQUESTED). Restituisce i dettagli TMDB (None senza
    chiave)."""
    if body.content_type not in ("movie", "tv"):
        raise HTTPException(status_code=400, detail=coded_detail("invalid_content_type", value=body.content_type))
    try:
        details = upload_identify.tmdb_client(session).full_details(body.content_type, body.tmdb_id)
    except TmdbApiKeyMissingError:
        details = None  # solo Radarr/Sonarr: niente dettagli TMDB, bastano gli id
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=coded_detail("tmdb_error", error=safe_error(exc))) from exc
    try:
        # La numerazione scelta vale per tutto il resto: nome, file, campi del tracker.
        upload_identify.confirm(
            session, job, content_type=body.content_type, tmdb_id=body.tmdb_id, kind=body.kind,
            seasons=body.seasons, episode=body.episode, details=details,
            order_key=body.episode_order, remember=True, split_requested=split,
        )
    except episode_orders.EpisodeOrderError as exc:
        raise HTTPException(status_code=400, detail=coded_detail(
            "upload_episode_order_unknown", order=body.episode_order)) from exc
    except UploadJobError as exc:
        raise HTTPException(status_code=400, detail=from_coded_error(exc)) from exc
    session.commit()
    return details


@router.post("/{upload_id}/match", response_model=UploadJobDetail)
def confirm_match(
    upload_id: int, body: UploadMatchRequest, request: Request, session: Session = Depends(get_session)
):
    """Primo punto di approvazione: il contenuto giusto, e per le serie
    stagione ed episodio. Da qui il worker analizza da solo."""
    job = _get_job_or_404(session, upload_id)
    _confirm(session, job, body)
    _worker(request).kick(job.id, job.status)
    return UploadJobDetail.from_model(job)


class UploadSplitResponse(BaseModel):
    job_ids: list[int]


@router.post("/{upload_id}/split", response_model=UploadSplitResponse)
def split_upload(
    upload_id: int, body: UploadMatchRequest, request: Request, session: Session = Depends(get_session)
):
    """Al match, "Dividi in episodi": conferma il match del season pack e lo
    divide in un upload per episodio (nazgarr/upload/split.py). L'upload
    originale si chiude; gli episodi vanno avanti da soli fino alla decisione.
    La divisione chiesta si scrive con il match: un riavvio a metà non la
    perde, la rifà l'analisi (upload_identify.resume_split; audit 2026-10-18,
    prima il pack ripartiva intero)."""
    job = _get_job_or_404(session, upload_id)
    if body.kind != "season_pack" or not job.is_dir or upload_pack.is_pack(job):
        raise HTTPException(status_code=400, detail=coded_detail("upload_split_not_a_pack"))
    if _worker(request).is_active(job.id):
        # Un passo del worker sta ancora lavorando il job: l'identificazione
        # scrive awaiting_match e poi continua il suo auto-match. Divisa
        # adesso, alla sua fine partiva l'analisi, che con SPLIT_REQUESTED
        # toglieva gli episodi che questa richiesta sta creando e rifaceva
        # la divisione (resume_split). Come per ripresa ed eliminazione.
        raise _refused(UploadJobError("upload_job_busy"))
    details = _confirm(session, job, body, split=True)
    try:
        children = upload_split.split(session, job, details)
    except Exception as exc:
        # Il match è confermato: se non si divide, va avanti come pack. Un
        # errore qualunque (audit 2026-10-18: un OSError leggendo un episodio
        # dava un 500, e il pack restava fermo fino al riavvio), e nessun
        # episodio creato fin lì resta vivo accanto al pack: li toglie la
        # divisione (split._discard), e se non ci è riuscita qui.
        session.rollback()
        session.refresh(job)
        try:
            upload_identify.drop_split_children(session, job)
            # Non riuscita: il pack va avanti intero, l'analisi non la rifà.
            upload_identify.split_decided(session, job)
        except Exception:
            session.rollback()
            logger.warning("Upload %s: upload della divisione non tolti", job.id, exc_info=True)
        _worker(request).kick(job.id, job.status)
        raise _split_refused(job, exc) from exc
    for child in children:
        if child.status in upload_jobs.WORKER_STATES:
            _worker(request).kick(child.id, child.status)
    return UploadSplitResponse(job_ids=[child.id for child in children])


def _split_refused(job: UploadJob, exc: Exception) -> HTTPException:
    """La risposta a una divisione fallita, sempre con un codice."""
    if isinstance(exc, UploadJobError):
        return HTTPException(status_code=400, detail=from_coded_error(exc))
    if isinstance(exc, ScopeViolation):
        return HTTPException(status_code=400, detail=coded_detail("upload_split_not_a_pack"))
    if isinstance(exc, OSError):
        return HTTPException(status_code=400, detail=coded_detail("upload_source_unreadable", error=safe_error(exc)))
    logger.error("Upload %s: divisione non riuscita", job.id, exc_info=exc)
    return HTTPException(status_code=500, detail=coded_detail("upload_prepare_failed", error=safe_error(exc)))


@router.get("/{upload_id}/episode-orders", response_model=EpisodeOrdersResponse)
def get_episode_orders(upload_id: int, tmdb_id: int, session: Session = Depends(get_session)):
    """Gli ordinamenti degli episodi di una serie candidata, con quanto ci
    combaciano i file del job, quello proposto e l'eventuale avviso."""
    job = _get_job_or_404(session, upload_id)
    return episode_orders.for_job(session, job, tmdb_id)


@router.post("/{upload_id}/rematch", response_model=UploadJobDetail)
def rematch_upload(upload_id: int, session: Session = Depends(get_session)):
    """Dalla decisione torna al match (nazgarr/upload/jobs.py back_to_match): per
    un match, automatico o no, che si è rivelato sbagliato."""
    job = _get_job_or_404(session, upload_id)
    try:
        upload_jobs.back_to_match(session, job)
    except UploadJobError as exc:
        raise HTTPException(status_code=400, detail=from_coded_error(exc)) from exc
    return UploadJobDetail.from_model(job)


@router.post("/{upload_id}/reidentify", response_model=UploadJobDetail)
def reidentify_upload(
    upload_id: int, body: UploadReidentifyRequest, request: Request, session: Session = Depends(get_session)
):
    job = _get_job_or_404(session, upload_id)
    try:
        upload_jobs.reidentify(session, job, body.forced_ids.model_dump() if body.forced_ids else None)
    except UploadJobError as exc:
        raise HTTPException(status_code=400, detail=from_coded_error(exc)) from exc
    _worker(request).kick(job.id, job.status)
    return UploadJobDetail.from_model(job)


@router.post("/{upload_id}/targets/{target_id}/verify", response_model=UploadJobDetail)
def verify_target(
    upload_id: int, target_id: int, body: UploadVerifyRequest, request: Request, session: Session = Depends(get_session)
):
    """Full hash check della sorgente contro un torrent già sul tracker, in
    background: se passa, per quel tracker il suggerimento diventa reseed."""
    job = _get_job_or_404(session, upload_id)
    target = next((t for t in job.targets if t.id == target_id), None)
    if target is None:
        raise HTTPException(status_code=404, detail=coded_detail("upload_target_not_found", id=target_id))
    try:
        upload_verify.start(session, job, target, body.torrent_id_remote, _worker(request))
    except UploadJobError as exc:
        raise HTTPException(status_code=400, detail=from_coded_error(exc)) from exc
    session.refresh(job)
    return UploadJobDetail.from_model(job)


@router.post("/{upload_id}/targets/{target_id}/retry-seed", response_model=UploadJobDetail)
def retry_seed(upload_id: int, target_id: int, request: Request, session: Session = Depends(get_session)):
    """Di nuovo l'aggiunta al client di un upload pubblicato ma non in seed
    (seed_failed, no_client): il .torrent del tracker già salvato, nessun
    nuovo upload."""
    job = _get_job_or_404(session, upload_id)
    target = next((t for t in job.targets if t.id == target_id), None)
    if target is None:
        raise HTTPException(status_code=404, detail=coded_detail("upload_target_not_found", id=target_id))
    try:
        upload_execute.retry_seed(session, job, target, request.app.state.settings.data_dir)
    except UploadJobError as exc:
        raise HTTPException(status_code=400, detail=from_coded_error(exc)) from exc
    session.refresh(job)
    return UploadJobDetail.from_model(job)


@router.put("/{upload_id}/overrides", response_model=UploadJobDetail)
def update_overrides(upload_id: int, body: UploadOverridesRequest, session: Session = Depends(get_session)):
    """Correzioni ai valori rilevati: rifà nomi e id proposti per ogni tracker."""
    job = _get_job_or_404(session, upload_id)
    try:
        upload_decision.update_overrides(session, job, body.overrides)
    except UploadJobError as exc:
        raise HTTPException(status_code=400, detail=from_coded_error(exc)) from exc
    return UploadJobDetail.from_model(job)


@router.post("/{upload_id}/approve", response_model=UploadJobDetail)
def approve_upload(
    upload_id: int, body: UploadApproveRequest, request: Request, session: Session = Depends(get_session)
):
    """Secondo punto di approvazione, la conferma umana obbligatoria di
    docs/SPEC.md §9: da qui il worker porta il job fino in fondo."""
    job = _get_job_or_404(session, upload_id)
    try:
        upload_decision.approve(session, job, [d.model_dump(exclude_unset=True) for d in body.targets],
                                scheduled_at=body.scheduled_at)
    except UploadJobError as exc:
        raise HTTPException(status_code=400, detail=from_coded_error(exc)) from exc
    _worker(request).kick(job.id, job.status)
    return UploadJobDetail.from_model(job)


@router.post("/{upload_id}/schedule", response_model=UploadJobDetail)
def schedule_upload(
    upload_id: int, body: UploadScheduleRequest, request: Request, session: Session = Depends(get_session)
):
    """L'ora di partenza di un upload in coda: un'altra, o subito (None)."""
    job = _get_job_or_404(session, upload_id)
    try:
        upload_jobs.schedule(session, job, body.scheduled_at)
    except UploadJobError as exc:
        raise HTTPException(status_code=400, detail=from_coded_error(exc)) from exc
    _worker(request).kick(job.id, job.status)
    return UploadJobDetail.from_model(job)
