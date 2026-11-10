"""API della coda di revisione (docs/SPEC.md sezione 8) — approvazione/
rifiuto manuale dei match sotto soglia, retry delle esecuzioni fallite.
"""

from bisect import bisect_left
from dataclasses import dataclass
from datetime import datetime
from itertools import accumulate

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session, object_session, selectinload

from nazgarr.core.errors import CodedError, coded_detail, from_coded_error
from nazgarr.core.logs import safe_error
from nazgarr.core.models import Candidate, MatchReview, SeedJob, TorrentClient, Tracker
from nazgarr.library import seeding
from nazgarr.reseed import exclusive, review
from nazgarr.reseed.executor import ExecutionError
from nazgarr.torrents import client_labels
from nazgarr.web.deps import get_or_404, get_session

router = APIRouter(prefix="/api/reviews", tags=["reviews"])


class CandidateFileResponse(BaseModel):
    torrent_path: str
    size_bytes: int | None
    is_video: bool
    local_path: str | None  # relative_path del file locale abbinato, None = mancante
    size_match: bool | None
    mediainfo_match: bool | None
    piece_verified: bool | None
    # Come è stato trovato il file locale (candidate_file.match_method):
    # "content" = per size e piece hash, con un nome che non diceva l'episodio.
    match_method: str | None = None


class LayoutSummary(BaseModel):
    """Riepilogo di un torrent multi-file (film con extra, season pack):
    quanti video, quanti verificati coi piece hash, quanti extra dovrà
    scaricare il client perché non presenti in locale."""

    folder: str | None
    video_count: int
    videos_matched: int
    videos_piece_verified: int
    extra_count: int
    extras_missing: int
    extras_missing_bytes: int
    files: list[CandidateFileResponse]


def _layout_summary(c: Candidate) -> LayoutSummary | None:
    if len(c.files) <= 1:
        return None
    files = []
    for f in c.files:
        local = f.media_file or f.seed_file
        files.append(CandidateFileResponse(
            torrent_path=f.torrent_path, size_bytes=f.size_bytes, is_video=f.is_video,
            local_path=local.relative_path if local is not None else None,
            size_match=f.size_match, mediainfo_match=f.mediainfo_match, piece_verified=f.piece_verified,
            match_method=f.match_method,
        ))
    videos = [f for f in files if f.is_video]
    extras_missing = [f for f in files if not f.is_video and f.local_path is None]
    return LayoutSummary(
        folder=c.folder,
        video_count=len(videos),
        videos_matched=sum(1 for f in videos if f.local_path is not None),
        videos_piece_verified=sum(1 for f in videos if f.piece_verified),
        extra_count=len(files) - len(videos),
        extras_missing=len(extras_missing),
        extras_missing_bytes=sum(f.size_bytes or 0 for f in extras_missing),
        files=files,
    )


class SeedJobResponse(BaseModel):
    id: int
    candidate_id: int
    candidate_name: str | None = None
    direction: str | None = None
    tracker: str | None = None
    torrent_client: str | None = None
    final_status: str
    # final_status per l'interfaccia: "removed" se era in seed ma il torrent
    # non è più nel client (review.seed_job_display_status).
    display_status: str | None = None
    recheck_status: str | None
    recheck_skipped: bool = False  # recheck del client saltato: Nazgarr aveva verificato il 100%
    # Il claim della resurrection prima dell'aggiunta: claimed | not_offered | refused, o None.
    resurrection: str | None = None
    error_message: str | None
    hardlink_created_at: datetime | None = None
    torrent_added_at: datetime | None = None

    @classmethod
    def from_model(cls, sj: SeedJob, in_client: set[str] | None = None,
                   clients: dict[int, TorrentClient | None] | None = None) -> "SeedJobResponse":
        """in_client e clients: gli info hash nei client e i client per id,
        se il chiamante li ha già letti per tutto l'elenco (una lettura per
        risposta, non per riga: audit 2026-10-18)."""
        candidate = sj.candidate
        session = object_session(sj)
        if in_client is None and session is not None:
            in_client = review.hashes_in_clients(session)
        if clients is not None and sj.torrent_client_id is not None:
            client = clients.get(sj.torrent_client_id)
        else:
            client = session.get(TorrentClient, sj.torrent_client_id) if session and sj.torrent_client_id else None
        return cls(
            id=sj.id, candidate_id=sj.candidate_id, candidate_name=candidate.name if candidate else None,
            direction=candidate.direction if candidate else None,
            tracker=candidate.tracker.label if candidate and candidate.tracker else None,
            torrent_client=client.label if client else None,
            final_status=sj.final_status,
            display_status=review.seed_job_display_status(sj, in_client or set()),
            recheck_status=sj.recheck_status, recheck_skipped=bool(sj.recheck_skipped),
            resurrection=sj.resurrection,
            error_message=sj.error_message,
            hardlink_created_at=sj.hardlink_created_at, torrent_added_at=sj.torrent_added_at,
        )


def seed_job_responses(session: Session, rows: list[SeedJob]) -> list[SeedJobResponse]:
    """Un elenco di esecuzioni: info hash nei client e client letti una volta."""
    in_client = review.hashes_in_clients(session)
    clients = {c.id: c for c in session.query(TorrentClient)}
    return [SeedJobResponse.from_model(sj, in_client, clients) for sj in rows]


@dataclass
class ReviewContext:
    """Quello che ogni riga della coda legge oltre alla review stessa, letto
    una volta per tutte le review di una risposta (audit 2026-10-18): prima
    ogni ReviewResponse faceva una quindicina di query, fra cui seeding_on
    con due GROUP BY su tutta la libreria, così 2000 review in coda erano
    30 mila query e diversi secondi."""

    formats_here: dict[tuple[int, int], set[str]]
    seeding_on: dict[int, list[str]]
    seed_jobs: dict[int, SeedJob]  # l'ultima esecuzione di ogni candidato
    clients: dict[int, TorrentClient]  # i client abilitati per id
    default_client: TorrentClient | None
    in_client: set[str] | None = None

    @classmethod
    def build(cls, session: Session, rows: list[MatchReview]) -> "ReviewContext":
        candidate_ids = [r.candidate_id for r in rows]
        seed_jobs: dict[int, SeedJob] = {}
        for sj in (session.query(SeedJob).filter(SeedJob.candidate_id.in_(candidate_ids)).order_by(SeedJob.id)
                   if candidate_ids else []):
            seed_jobs[sj.candidate_id] = sj
        enabled = session.query(TorrentClient).filter_by(enabled=True)
        return cls(
            formats_here=_formats_here(session, rows),
            seeding_on=seeding.seeding_on_many(session, sorted({r.media_file_id for r in rows if r.media_file_id})),
            seed_jobs=seed_jobs,
            clients={c.id: c for c in enabled},
            # Come review.client_row_for_candidate: il primo client abilitato.
            default_client=enabled.first() if rows else None,
            in_client=review.hashes_in_clients(session) if seed_jobs else None,
        )

    def client_for(self, candidate: Candidate) -> TorrentClient | None:
        """Dove andrà il reseed: il client del tracker se abilitato, sennò il primo abilitato."""
        tracker = candidate.tracker
        if tracker is not None and tracker.torrent_client_id in self.clients:
            return self.clients[tracker.torrent_client_id]
        return self.default_client


class ReviewResponse(BaseModel):
    id: int
    candidate_id: int
    media_item_id: int
    media_file_id: int | None
    seed_file_id: int | None
    status: str
    direction: str
    confidence: float
    candidate_name: str
    ambiguity_reason: str | None
    layout: LayoutSummary | None = None  # solo per torrent con più di un file
    # L'esecuzione partita da questa review (dopo un'approvazione): per il
    # feedback in interfaccia (aggiunto al client, recheck in corso, fallito).
    seed_job: SeedJobResponse | None = None
    # Controllo completo prima dell'esecuzione (verify_before_execute).
    verify_status: str | None = None  # verifying | passed | failed
    verify_detail: str | None = None
    verify_check_id: str | None = None
    # Dove il file è già in seed (tracker o host): non vuoto = un cross-seed.
    seeding_on: list[str] = []
    tracker_id: int
    tracker: str | None = None
    # "pack" (più video) o "single" (nazgarr/library/seeding.py), e i formati in
    # cui il file è già in seed sul tracker di questa review: non vuoto = il
    # secondo formato dello stesso tracker ("pack e singoli").
    format: str
    seeding_here: list[str] = []
    # Il contenuto: per raggruppare in coda gli episodi della stessa stagione.
    content_type: str | None = None
    tmdb_id: int | None = None
    title: str | None = None
    season_number: int | None = None
    episode_number: int | None = None
    # Il client dove andrà il reseed, coi suoi default per categoria e tag e
    # quelli scelti a mano su questa review (None = i default, "" = nessuno).
    # Il default della categoria non sa se è un anime: lo decide TMDB quando
    # il reseed parte, se non ne è stata scelta una.
    torrent_client_id: int | None = None
    default_client_category: str | None = None
    default_client_tags: str | None = None
    client_category: str | None = None
    client_tags: str | None = None

    @classmethod
    def many(cls, session: Session, rows: list[MatchReview]) -> list["ReviewResponse"]:
        """Un elenco di review con una lettura per tutto l'elenco (ReviewContext)."""
        context = ReviewContext.build(session, rows)
        return [cls.from_model(r, context) for r in rows]

    @classmethod
    def from_model(cls, r: MatchReview, context: ReviewContext | None = None) -> "ReviewResponse":
        """context: quello che serve, già letto per tutto un elenco (many);
        None = solo per questa review."""
        if context is None:
            context = ReviewContext.build(object_session(r), [r])
        seed_job = context.seed_jobs.get(r.candidate_id)
        client = context.client_for(r.candidate)
        item = r.candidate.media_item
        here = context.formats_here.get((r.candidate.tracker_id, r.media_file_id), set())
        return cls(
            torrent_client_id=client.id if client else None,
            default_client_category=client_labels.default_category(client, item.content_type if item else None),
            default_client_tags=client_labels.default_tags(client, "reseed"),
            client_category=r.client_category, client_tags=r.client_tags,
            id=r.id, candidate_id=r.candidate_id, media_item_id=r.candidate.media_item_id,
            media_file_id=r.media_file_id, seed_file_id=r.seed_file_id,
            status=r.status, direction=r.candidate.direction, confidence=r.candidate.confidence,
            candidate_name=r.candidate.name, ambiguity_reason=r.candidate.ambiguity_reason,
            layout=_layout_summary(r.candidate),
            seed_job=(SeedJobResponse.from_model(seed_job, context.in_client, context.clients)
                      if seed_job is not None else None),
            verify_status=r.verify_status, verify_detail=r.verify_detail, verify_check_id=r.verify_check_id,
            seeding_on=context.seeding_on.get(r.media_file_id, []) if r.media_file_id else [],
            tracker_id=r.candidate.tracker_id, tracker=r.candidate.tracker.label if r.candidate.tracker else None,
            format=review.candidate_format(r.candidate), seeding_here=sorted(here),
            content_type=item.content_type if item else None, tmdb_id=item.tmdb_id if item else None,
            title=item.title if item else None, season_number=item.season_number if item else None,
            episode_number=item.episode_number if item else None,
        )


def _formats_here(session: Session, rows: list[MatchReview]) -> dict[tuple[int, int], set[str]]:
    """Per ogni tracker della coda, i formati in cui i suoi file sono già in
    seed lì: una lettura per tracker, non per review."""
    by_tracker: dict[int, list[int]] = {}
    for r in rows:
        if r.media_file_id is not None:
            by_tracker.setdefault(r.candidate.tracker_id, []).append(r.media_file_id)
    result = {}
    for tracker_id, media_file_ids in by_tracker.items():
        tracker = session.get(Tracker, tracker_id)
        if tracker is None:
            continue
        for media_file_id, found in seeding.seeding_formats(session, tracker, media_file_ids).items():
            result[(tracker_id, media_file_id)] = found
    return result


def _get_review_or_404(session: Session, review_id: int) -> MatchReview:
    return get_or_404(session, MatchReview, review_id, "review_not_found")


@router.get("", response_model=list[ReviewResponse])
def list_reviews(session: Session = Depends(get_session)):
    """Tutta la coda, nell'ordine della pagina Reseeding (la CLI). La pagina
    usa /queue, a pagine."""
    ids = [review_id for group in review.queue_groups(session) for review_id in group]
    return ReviewResponse.many(session, review.load_reviews(session, ids))


class ReviewQueuePage(BaseModel):
    items: list[ReviewResponse]
    total: int  # review in coda, tutte
    offset: int
    next_offset: int | None  # None = finite


QUEUE_PAGE_MAX = 500


@router.get("/queue", response_model=ReviewQueuePage)
def review_queue(offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=QUEUE_PAGE_MAX),
                 session: Session = Depends(get_session)):
    """La coda a pagine (audit 2026-10-18: prima sempre tutta, ricalcolata
    dopo ogni decisione). Una pagina finisce sempre alla fine di un gruppo
    (gli episodi singoli di una stagione, review.queue_groups), anche se
    supera limit: la riga di una stagione non resta mai a metà fra due
    pagine. La pagina dopo parte da next_offset."""
    groups = review.queue_groups(session)
    ends = list(accumulate(len(group) for group in groups))
    total = ends[-1] if ends else 0
    ids = [review_id for group in groups for review_id in group]
    wanted = min(offset + limit, total)
    end = ends[bisect_left(ends, wanted)] if wanted > offset else offset
    return ReviewQueuePage(
        items=ReviewResponse.many(session, review.load_reviews(session, ids[offset:end])),
        total=total, offset=offset, next_offset=end if end < total else None,
    )


@router.post("/{review_id}/approve", response_model=ReviewResponse)
def approve_review(review_id: int, request: Request, session: Session = Depends(get_session)):
    """Con la verifica attiva (default) risponde subito con verify_status
    "verifying": approvazione ed esecuzione arrivano a controllo superato,
    l'avanzamento è in /api/full-checks/{verify_check_id}."""
    row = _get_review_or_404(session, review_id)
    try:
        review.request_approval(session, row, request.app.state.session_factory)
    except review.AlreadyVerifyingError as exc:
        raise HTTPException(status_code=409, detail=coded_detail("review_already_verifying", id=exc.review_id)) from exc
    except review.ReviewAlreadyDecidedError as exc:
        # Già approvata, rifiutata o eseguita: un'esecuzione fallita si
        # ritenta da /api/reviews/failed/{id}/retry, mai approvando di nuovo.
        raise HTTPException(status_code=409, detail=coded_detail("review_already_decided", id=review_id)) from exc
    return ReviewResponse.from_model(row)


class ClientLabelsRequest(BaseModel):
    client_category: str | None = None  # None = default del client, "" = nessuna
    client_tags: str | None = None  # None = tag dei reseed del client, "" = nessuno


@router.put("/{review_id}/client-labels", response_model=ReviewResponse)
def set_client_labels(review_id: int, body: ClientLabelsRequest, session: Session = Depends(get_session)):
    """Categoria e tag nel client per questo reseed, scelti a mano come per un
    upload: valgono quando parte. Solo finché la review è in coda."""
    row = _get_review_or_404(session, review_id)
    if row.status not in review.READY_FOR_DECISION_STATUSES:
        raise HTTPException(status_code=409, detail=coded_detail("review_already_decided", id=review_id))
    review.set_client_labels(session, row, body.client_category, body.client_tags)
    return ReviewResponse.from_model(row)


@router.post("/{review_id}/reject", response_model=ReviewResponse)
def reject_review(review_id: int, session: Session = Depends(get_session)):
    row = _get_review_or_404(session, review_id)
    try:
        review.reject(session, row)
    except review.ReviewAlreadyDecidedError as exc:
        raise HTTPException(status_code=409, detail=coded_detail("review_already_decided", id=review_id)) from exc
    return ReviewResponse.from_model(row)


class ReconcileResponse(BaseModel):
    reconciled: int
    errors: int


@router.get("/seed-jobs/recent", response_model=list[SeedJobResponse])
def recent_seed_jobs(limit: int = 50, session: Session = Depends(get_session)):
    """Ultime esecuzioni, dalla più recente: l'interfaccia le osserva per
    avvisare quando un recheck in background finisce (seeding o fallito)."""
    rows = (
        session.query(SeedJob).options(selectinload(SeedJob.candidate).joinedload(Candidate.tracker))
        .order_by(SeedJob.id.desc()).limit(max(1, min(limit, 200))).all()
    )
    return seed_job_responses(session, rows)


@router.post("/seed-jobs/reconcile", response_model=ReconcileResponse)
def reconcile_now(session: Session = Depends(get_session)):
    """Controlla subito l'esito dei recheck in attesa (lo scheduler lo fa
    comunque ogni 2 minuti). Nessun torrent aggiunto né file modificato:
    avvia i reseed il cui recheck è riuscito (entrano fermi, decisione
    dell'utente del 2026-10-18) e ferma quelli falliti che girano. Mai
    insieme a una run o ad altro lavoro a lato (nazgarr/reseed/exclusive.py,
    audit 2026-10-18): prima controllava solo che nessuna run fosse in
    corso, e una run poteva partire a metà e riconciliare gli stessi job."""
    try:
        with exclusive.side_work(session, holder="seed_jobs", wait_seconds=exclusive.API_WAIT_SECONDS):
            return review.reconcile_pending_seed_jobs(session)
    except exclusive.Busy as exc:
        raise HTTPException(status_code=409, detail=coded_detail(exc.code)) from exc


@router.delete("/seed-jobs/{seed_job_id}", status_code=204)
def delete_seed_job(seed_job_id: int, session: Session = Depends(get_session)):
    """Elimina un'esecuzione fallita o rimasta in corso: il prossimo scan
    ripropone il torrent. Niente cambia nel client né sul disco. Mai
    insieme a una run (che ricontrolla i job in corso) o ad altro lavoro a
    lato (nazgarr/reseed/exclusive.py)."""
    seed_job = session.get(SeedJob, seed_job_id)
    if seed_job is None:
        raise HTTPException(status_code=404, detail=coded_detail("seed_job_not_found", id=seed_job_id))
    try:
        with exclusive.side_work(session, wait_seconds=exclusive.API_WAIT_SECONDS):
            review.delete_seed_job(session, seed_job)
    except exclusive.Busy as exc:
        raise HTTPException(status_code=409, detail=coded_detail(exc.code)) from exc
    except review.SeedJobNotDeletable as exc:
        raise HTTPException(status_code=409, detail=from_coded_error(exc)) from exc


@router.get("/failed", response_model=list[SeedJobResponse])
def list_failed(session: Session = Depends(get_session)):
    """Le esecuzioni fallite (la CLI): prima ogni riga rileggeva tutti gli
    info hash dei client."""
    rows = (
        session.query(SeedJob).options(selectinload(SeedJob.candidate).joinedload(Candidate.tracker))
        .filter(SeedJob.final_status == "failed").order_by(SeedJob.id).all()
    )
    return seed_job_responses(session, rows)


class RetryResponse(SeedJobResponse):
    # Retry da zero con il controllo completo acceso: è partito quel
    # controllo (avanzamento in /api/full-checks/{id}), l'esecuzione arriva
    # solo se passa. Il seed_job qui è ancora quello fallito.
    verify_check_id: str | None = None


@router.post("/failed/{seed_job_id}/retry", response_model=RetryResponse)
def retry_failed(seed_job_id: int, request: Request, session: Session = Depends(get_session)):
    seed_job = session.get(SeedJob, seed_job_id)
    if seed_job is None:
        raise HTTPException(status_code=404, detail=coded_detail("seed_job_not_found", id=seed_job_id))
    try:
        result = review.retry_failed(session, seed_job, request.app.state.session_factory)
    except CodedError as exc:
        # Un retry che non si fa (executor.RetryRefused: non fallito, già
        # ritentato, nessun client, un torrent nel client non suo), col suo codice.
        raise HTTPException(status_code=409, detail=from_coded_error(exc)) from exc
    except ExecutionError as exc:
        # L'esecuzione rifatta è fallita: il suo testo inglese, senza chiavi o
        # passkey, dentro un codice che interfaccia e CLI riconoscono (audit
        # 2026-10-18: prima era un testo libero, a volte italiano).
        raise HTTPException(status_code=409, detail=coded_detail(
            "seed_job_retry_failed", id=seed_job_id, error=safe_error(exc))) from exc
    if isinstance(result, SeedJob):
        return RetryResponse(**SeedJobResponse.from_model(result).model_dump())
    session.refresh(seed_job)
    return RetryResponse(**SeedJobResponse.from_model(seed_job).model_dump(), verify_check_id=result.id)


class CandidateAuditResponse(BaseModel):
    id: int
    tracker_id: int
    name: str
    direction: str
    confidence: float
    size_match: bool | None
    mediainfo_match: bool | None
    piece_verified: bool | None
    ambiguity_reason: str | None
    layout: LayoutSummary | None = None


# Torrent mostrati nell'audit di un contenuto: abbastanza per vederne ogni
# release su più tracker, mai migliaia di righe.
MAX_AUDIT_CANDIDATES = 100


@router.get("/candidates/{media_item_id}", response_model=list[CandidateAuditResponse])
def list_candidates_for_media_item(media_item_id: int, session: Session = Depends(get_session)):
    """Sola lettura, per audit: ogni torrent valutato per un media_item, non
    solo quello scelto per la review (docs/SPEC.md sezione 6), con la sua
    valutazione più recente, dal più plausibile."""
    rows = review.one_per_torrent(session, [media_item_id], limit=MAX_AUDIT_CANDIDATES, newest=True,
                                  by_direction=True)
    return [
        CandidateAuditResponse(
            id=c.id, tracker_id=c.tracker_id, name=c.name, direction=c.direction,
            confidence=c.confidence, size_match=c.size_match, mediainfo_match=c.mediainfo_match,
            piece_verified=c.piece_verified, ambiguity_reason=c.ambiguity_reason,
            layout=_layout_summary(c),
        )
        for c in rows
    ]
