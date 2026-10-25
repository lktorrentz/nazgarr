"""Motore di matching: per ogni file orfano (media_file o seed_file),
cerca candidati sul tracker e scrive righe `candidate` con confidence
esplicita e spiegabile (mai un punteggio ML opaco). Vedi docs/SPEC.md
sezioni 3, 6, 8.

Ogni candidato è un torrent intero, valutato file per file da
nazgarr/torrents/layout.py: un film a file singolo, un film col suo .nfo, un
season pack. Un pack si ricrea solo se ogni suo episodio ha un file locale
verificato ("season_pack_partial" altrimenti, confidence 0).

Le due direzioni (docs/SPEC.md sezione 3) condividono la stessa
valutazione, cambia solo da quale lato si cercano i file locali e quale
soglia si applica poi in fase di revisione (nazgarr/reseed/review.py).

Ogni file si cerca nei formati che gli servono (nazgarr/library/seeding.py):
un orfano in tutti, un episodio già in seed su questo tracker, con "pack e
singoli" acceso, solo in quello che gli manca.
"""

import json
import logging
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import partial

from sqlalchemy import exists
from sqlalchemy.orm import Session, selectinload

from nazgarr.adapters.tracker.base import (
    MediaQuery,
    NotSupportedError,
    TrackerAdapter,
    TrackerRateLimitedError,
    UploadError,
)
from nazgarr.core import settings_registry
from nazgarr.core.db_utils import as_utc, keep_loaded_on_commit
from nazgarr.core.file_types import is_video
from nazgarr.core.logs import safe_error
from nazgarr.core.models import (
    Candidate,
    CandidateFile,
    ClientTorrentFile,
    MatchAttempt,
    MatchReview,
    MediaFile,
    MediaItem,
    SeedFile,
    SeedJob,
    Tracker,
)
from nazgarr.core.run_progress import NULL_PROGRESS
from nazgarr.integrations.arr import ArrGrab, ArrIndex, host_of
from nazgarr.library.exclusions import CompiledExclusions, load_exclusions
from nazgarr.library.hardlinks import seed_copies_by_inode
from nazgarr.library.mediainfo import compute_unique_id
from nazgarr.library.scan_state import is_current, latest_scan_by_disk
from nazgarr.library.seeding import (
    FORMATS,
    PACK,
    SINGLE,
    is_episode,
    pack_and_singles_enabled,
    seeding_formats,
    seeding_media_file_ids,
    torrent_format,
)
from nazgarr.reseed import review
from nazgarr.reseed.download_links import stored_link
from nazgarr.torrents.layout import (
    CONFIDENCE_NO_MATCH,
    CONFIDENCE_PIECE_VERIFIED,  # noqa: F401  (riesportate: le soglie vivono in torrent_layout)
    CONFIDENCE_SIZE_AND_MEDIAINFO_MATCH,  # noqa: F401
    CONFIDENCE_SIZE_ONLY,  # noqa: F401
    Layout,
    LayoutEvaluation,
    LocalFiles,
    catalog_names_safe,
    evaluate,
    layout_from_catalog,
    layout_from_torrent,
    map_media_side,
    map_seed_side,
    other_episode,
    seed_root,
)
from nazgarr.torrents.metainfo import TorrentInfo, TorrentMetainfoError, compute_info_hash, parse_torrent_info

logger = logging.getLogger(__name__)

# Ogni quanto un file orfano già cercato su un tracker viene ricercato
# comunque, anche se nulla è cambiato (nuovi upload sul tracker). Setting
# app_settings "rematch_interval_days" (7 giorni di default,
# nazgarr/core/settings_registry.py), 0 = ricerca a ogni run come prima.

# Motivi per cui un torrent non riguarda proprio il file cercato (es. il
# pack di un'altra stagione, trovato cercando per tmdb_id della serie):
# nessuna riga di audit, sarebbe solo rumore moltiplicato per ogni episodio.
# Il singolo di un altro episodio e, lato torrent, un torrent senza l'anchor
# si scartano già prima di valutarli (_unrelated).
_UNRELATED_REASONS = {"anchor_not_in_torrent"}

# L'esito dell'ultima valutazione di un pack in un passaggio
# (MatchContext.pack_verdicts): plausibile, non plausibile, o .torrent non letto.
_PLAUSIBLE, _IMPLAUSIBLE, _UNREAD = "plausible", "implausible", "unread"


class SharedLocalFiles:
    """Gli indici dei file locali (LocalFiles) costruiti al più una volta per
    tutta una run, per ogni tracker e le due direzioni: dipendono solo da
    media_file e seed_file, che il matching non cambia. Prima si
    ricostruivano per ogni tracker e direzione, rileggendo tutta la libreria
    (audit 2026-10-18). Pigri: senza nessun torrent da valutare non si leggono.
    disk_ids: solo quei dischi (LocalFiles.load)."""

    def __init__(self, session: Session, disk_ids: set[int] | None = None):
        self.session = session
        self.disk_ids = disk_ids
        self._local: LocalFiles | None = None
        # E i piece già verificati (layout._check_pieces), per tutti i tracker:
        # lo stesso torrent su due tracker non si rilegge dal disco.
        self.piece_checks: dict = {}

    def get(self) -> LocalFiles:
        if self._local is None:
            self._local = LocalFiles.load(self.session, self.disk_ids)
        return self._local


# .torrent letti tenuti in memoria in un passaggio: quanto serve perché gli
# episodi vicini (orfani in ordine di serie) ritrovino i loro.
MAX_PARSED_TORRENTS = 256


@dataclass
class MatchContext:
    """Stato di UN tracker per UNA run di matching, condiviso fra tutti gli
    orfani: indici dei file locali, .torrent già scaricati, torrent multi-
    video già valutati (un pack di 10 episodi orfani si valuta e scarica una
    volta sola, non dieci; lo rivaluta solo il file che lo propone già,
    _record_pack)."""

    session: Session
    tracker_row: Tracker
    tracker_adapter: TrackerAdapter
    direction: str
    arr_index: ArrIndex | None = None
    local: LocalFiles | None = None
    shared_local: SharedLocalFiles | None = None
    translator: object | None = None  # nazgarr/library/episode_orders.py Translator, creato alla prima serie
    evaluated_packs: set[str] = field(default_factory=set)
    # (id sul tracker, disco dell'anchor) -> esito dell'ultima valutazione del
    # pack nel passaggio (_record_pack, pack_gone).
    pack_verdicts: dict[tuple[str, int], str] = field(default_factory=dict)
    # I file che il passaggio cercherà ancora, come (media_file_id,
    # seed_file_id) (_match_orphans, pack_gone): vuoto fuori da un passaggio.
    to_search: set[tuple[int | None, int | None]] = field(default_factory=set)
    both_formats: bool = False  # "pack e singoli dello stesso tracker" acceso
    # Piece già verificati in questa run (nazgarr/torrents/layout.py::_check_pieces).
    piece_checks: dict = field(default_factory=dict)
    _torrents: OrderedDict[str, TorrentInfo] = field(default_factory=OrderedDict)
    _failed_torrents: set[str] = field(default_factory=set)
    _info_hashes: dict[str, str] = field(default_factory=dict)
    _episode_mappings: dict = field(default_factory=dict)
    # media_item_id -> {torrent_id_remote: candidato da aggiornare} (_reusable_candidate).
    _reusable: dict[int, dict[str, Candidate]] = field(default_factory=dict)

    _in_client: set[str] | None = None
    _queue: "review.ReviewQueue | None" = None

    def hashes_in_clients(self) -> set[str]:
        """Gli info hash nei client, letti una volta per passaggio: le
        esecuzioni della coda vengono dopo il matching. Il worker degli upload
        invece può aggiungerne uno durante il passaggio: la coda rilegge i
        suoi, e gli info hash dei candidati, prima di ogni review
        (review.ReviewQueue.refresh_uploads)."""
        if self._in_client is None:
            from nazgarr.reseed.review import hashes_in_clients

            self._in_client = hashes_in_clients(self.session)
        return self._in_client

    def review_queue(self) -> "review.ReviewQueue":
        """Le review attive e le esecuzioni lette una volta per passaggio,
        non a ogni orfano (nazgarr/reseed/review.py::ReviewQueue)."""
        if self._queue is None:
            self._queue = review.ReviewQueue(self.session, self.hashes_in_clients())
        return self._queue

    def holds(self, anchor: MediaFile | SeedFile, torrent_id_remote: str) -> bool:
        """L'anchor ha già questo torrent in una review in coda o approvata
        (o in esecuzione): la sua proposta."""
        return _file_key(anchor) in self.review_queue().holders(self.tracker_row.id, torrent_id_remote)

    def skips(self, anchor: MediaFile | SeedFile, torrent_id_remote: str) -> bool:
        """Un pack già valutato in questo passaggio si salta, tranne dal file
        che lo propone già (_record_pack)."""
        return torrent_id_remote in self.evaluated_packs and not self.holds(anchor, torrent_id_remote)

    def pack_gone(self, anchor: MediaFile, proposal: MatchReview) -> bool:
        """La proposta di pack di un altro episodio non copre più l'anchor
        (review._cover_lost) per quello che il passaggio ha visto del pack sul
        suo disco: non plausibile, o il .torrent non letto mentre il file della
        proposta si cercherà ancora; lui riproverà, non lo leggerà (il link
        fallito non si riprova, fetch_torrent) e toglierà la proposta. Vale
        anche per un pack che l'anchor ha saltato perché già valutato."""
        verdict = self.pack_verdicts.get((proposal.candidate.torrent_id_remote, anchor.disk_id))
        return verdict == _IMPLAUSIBLE or (
            verdict == _UNREAD and proposal.status in review.READY_FOR_DECISION_STATUSES
            and (proposal.media_file_id, proposal.seed_file_id) in self.to_search)

    def local_files(self) -> LocalFiles:
        if self.local is None:
            self.local = self.shared_local.get() if self.shared_local is not None else LocalFiles.load(self.session)
        return self.local

    def episode_translator(self):
        if self.translator is None:
            from nazgarr.library.episode_orders import Translator

            self.translator = Translator(self.session)
        return self.translator

    def translate_episodes(self, tmdb_id: int, torrent: frozenset, library: set, fetch: bool = True) -> dict:
        """Translator.mapping, una volta per serie ed episodi del torrent: i
        singoli di un'altra serie di episodi si ripresentano per ogni episodio
        cercato (layout.other_episode). fetch=False: solo se gli ordinamenti
        della serie sono già stati letti in questa run (per un pack), mai una
        chiamata a TMDB o a Sonarr per questo."""
        key = (tmdb_id, torrent)
        if key not in self._episode_mappings:
            # Translator li tiene per serie in _orders (nazgarr/library/episode_orders.py).
            if not fetch and tmdb_id not in getattr(self.translator, "_orders", ()):
                return {}
            self._episode_mappings[key] = self.episode_translator().mapping(tmdb_id, set(torrent), library)
        return self._episode_mappings[key]

    def fetch_torrent(self, url: str | None) -> TorrentInfo | None:
        """Scarica e analizza un .torrent tramite l'adapter (rate limit del
        tracker), al massimo una volta per URL per run. None su qualunque
        problema, anche inatteso (un plugin, un errore di un altro tipo):
        prima un'eccezione diversa usciva dal file in esame, ne annullava
        tutti i candidati, non registrava la ricerca e lo stesso link si
        riscaricava per ogni file della run che lo incontrava (audit
        2026-10-18). TrackerRateLimitedError risale."""
        if not url:
            return None
        if url in self._failed_torrents:
            return None
        if url in self._torrents:
            self._torrents.move_to_end(url)
            return self._torrents[url]
        try:
            content = self.tracker_adapter.download_torrent(url)
            parsed = parse_torrent_info(content)
            self._info_hashes[url] = compute_info_hash(content)
        except TrackerRateLimitedError:
            raise
        except (UploadError, NotSupportedError, TorrentMetainfoError):
            logger.warning("Download/parsing del .torrent fallito: %s", safe_error(url), exc_info=True)
            self._failed_torrents.add(url)
            return None
        except Exception:
            logger.exception("Errore inatteso col .torrent %s: ignorato per questa run", safe_error(url))
            self._failed_torrents.add(url)
            return None
        # Gli ultimi letti: ogni .torrent tiene gli hash di tutti i suoi piece,
        # e in un primo passaggio lungo su un tracker nuovo erano centinaia di
        # MB tenuti fino alla fine (audit 2026-10-18).
        self._torrents[url] = parsed
        while len(self._torrents) > MAX_PARSED_TORRENTS:
            self._torrents.popitem(last=False)
        return parsed

    def info_hash(self, url: str | None) -> str | None:
        return self._info_hashes.get(url) if url else None


def _map(ctx: MatchContext, layout: Layout, anchor: MediaFile | SeedFile):
    if ctx.direction == "media_to_torrent":
        return map_media_side(layout, anchor, ctx.local_files(), ctx.arr_index, ctx.episode_translator())
    return map_seed_side(layout, anchor, ctx.local_files())


def _unrelated(ctx: MatchContext, layout: Layout, anchor: MediaFile | SeedFile) -> bool:
    """Un torrent del catalogo che non riguarda il file cercato, prima di
    valutarlo: lato libreria il singolo di un altro episodio della serie,
    lato torrent un torrent in cui l'anchor non c'è (un altro torrent dello
    stesso contenuto). Né l'uno né l'altro diventa una riga di audit, come
    il pack di un'altra stagione (audit 2026-10-18: erano righe a confidence
    0 per ogni episodio cercato, rifatte a ogni nuova ricerca; e un pack
    senza l'anchor finiva in evaluated_packs, così il seed_file che ne fa
    parte lo saltava per tutta la run)."""
    if ctx.direction == "media_to_torrent":
        return other_episode(layout, anchor, ctx.local_files(), ctx.translate_episodes)
    return seed_root(layout, anchor) is None


def _file_key(anchor: MediaFile | SeedFile) -> tuple[int | None, int | None]:
    return (anchor.id, None) if isinstance(anchor, MediaFile) else (None, anchor.id)


def _record_pack(ctx: MatchContext, anchor: MediaFile | SeedFile, torrent_id_remote: str,
                 evaluation: LayoutEvaluation) -> None:
    """Un pack valutato in questo passaggio: gli altri suoi episodi lo
    saltano (MatchContext.skips) e ne usano l'esito per sapere se li copre
    ancora (MatchContext.pack_gone). Non il file che lo propone già: quella
    proposta la conferma, la aggiorna o la toglie solo lui, valutandolo
    anche lui (coi .torrent e i piece già letti nel passaggio). Prima lo
    saltava se un episodio cercato prima l'aveva già valutato: col suo
    singolo al posto del pack, e un singolo per ogni episodio cercato dopo
    di lui, ogni volta che non era il primo orfano della stagione, perché
    gli orfani si cercano per episodio (2026-10-22)."""
    if evaluation.confidence > CONFIDENCE_NO_MATCH:
        verdict = _PLAUSIBLE
    else:
        verdict = _UNREAD if evaluation.ambiguity_reason == review.UNREAD_TORRENT else _IMPLAUSIBLE
    ctx.pack_verdicts[(torrent_id_remote, anchor.disk_id)] = verdict
    ctx.evaluated_packs.add(torrent_id_remote)


def _evaluate(ctx: MatchContext, layout: Layout, anchor, *, download_link: str | None,
              unique_ids: dict[str, str] | None, single_unique_id: str | None) -> LayoutEvaluation:
    return evaluate(
        _map(ctx, layout, anchor), layout,
        anchor_id=anchor.id,
        unique_ids=unique_ids,
        single_unique_id=single_unique_id,
        # lambda, non il riferimento diretto: i test sostituiscono matching.compute_unique_id
        compute_unique_id=lambda path: compute_unique_id(path),
        fetch_torrent=lambda: ctx.fetch_torrent(download_link),
        piece_checks=ctx.shared_local.piece_checks if ctx.shared_local is not None else ctx.piece_checks,
    )


def _persist(
    ctx: MatchContext,
    anchor: MediaFile | SeedFile,
    *,
    media_item_id: int,
    torrent_id_remote: str,
    name: str,
    size_bytes: int,
    layout: Layout,
    evaluation: LayoutEvaluation,
    download_link: str | None,
    info_hash: str | None,
    source: str,
) -> Candidate:
    """Una valutazione salvata. Se lo stesso torrent era già stato valutato
    per lo stesso contenuto in una ricerca precedente e nessuna review né
    esecuzione usa quella riga, si aggiorna lei (con i suoi candidate_file
    rifatti) invece di aggiungerne un'altra: prima ogni nuova ricerca, ogni
    rematch_interval_days, riscriveva tutti i risultati, e le tabelle
    crescevano senza limite (audit 2026-10-18). Una riga che una review o
    un seed_job usa non si tocca mai: è quella che l'utente ha visto o che
    è stata eseguita."""
    candidate = _reusable_candidate(ctx, media_item_id, torrent_id_remote, anchor)
    if candidate is None:
        candidate = Candidate(media_item_id=media_item_id, tracker_id=ctx.tracker_row.id,
                              torrent_id_remote=torrent_id_remote, direction=ctx.direction)
        ctx.session.add(candidate)
    else:
        candidate.files.clear()
        candidate.created_at = datetime.now(UTC)  # l'ultima valutazione, per l'audit
    candidate.info_hash = info_hash or ctx.info_hash(download_link)
    candidate.name = name
    candidate.size_bytes = size_bytes
    candidate.file_list_json = json.dumps([f.path for f in layout.files])
    candidate.folder = layout.folder
    candidate.download_link = stored_link(ctx.tracker_adapter, download_link)  # mai la chiave nel DB
    candidate.source = source
    candidate.size_match = evaluation.size_match
    candidate.mediainfo_match = evaluation.mediainfo_match
    candidate.piece_verified = evaluation.piece_verified
    candidate.piece_boundary_count = evaluation.piece_boundary_count
    candidate.confidence = evaluation.confidence
    candidate.ambiguity_reason = evaluation.ambiguity_reason
    candidate.piece_length = layout.piece_length
    for m in evaluation.files:
        local = m.local
        candidate.files.append(CandidateFile(
            torrent_path=m.layout_file.path,
            size_bytes=m.layout_file.size,
            is_video=m.layout_file.is_video,
            media_file_id=local.id if local is not None and local.kind == "media" else None,
            seed_file_id=local.id if local is not None and local.kind == "seed" else None,
            size_match=m.size_match,
            mediainfo_match=m.mediainfo_match,
            piece_verified=m.piece_verified,
            match_method=m.method if local is not None else None,
        ))
    return candidate


def _anchor_column(anchor: MediaFile | SeedFile) -> str:
    return "media_file_id" if isinstance(anchor, MediaFile) else "seed_file_id"


def _for_anchor(candidate: Candidate, anchor: MediaFile | SeedFile) -> bool:
    column = _anchor_column(anchor)
    return any(getattr(f, column) == anchor.id for f in candidate.files)


def _unused_candidates(ctx: MatchContext, media_item_id: int) -> dict[str, list[Candidate]]:
    """Le righe di questo contenuto su questo tracker e in questa direzione
    che nessuna review e nessun seed_job usa, per torrent, dalla più recente:
    lette una volta per contenuto in ogni passaggio. Durante il matching
    nessun'altra parte crea review o esecuzioni su queste righe."""
    if media_item_id not in ctx._reusable:
        rows = (
            ctx.session.query(Candidate)
            .options(selectinload(Candidate.files))
            .filter(Candidate.media_item_id == media_item_id, Candidate.tracker_id == ctx.tracker_row.id,
                    Candidate.direction == ctx.direction,
                    ~exists().where(MatchReview.candidate_id == Candidate.id),
                    ~exists().where(SeedJob.candidate_id == Candidate.id))
            .order_by(Candidate.id.desc())
            .all()
        )
        by_remote: dict[str, list[Candidate]] = {}
        for row in rows:
            by_remote.setdefault(row.torrent_id_remote, []).append(row)
        ctx._reusable[media_item_id] = by_remote
    return ctx._reusable[media_item_id]


def _reusable_candidate(ctx: MatchContext, media_item_id: int, torrent_id_remote: str,
                        anchor: MediaFile | SeedFile) -> Candidate | None:
    """La riga da aggiornare per questo torrent, se c'è: quella valutata per
    lo stesso file, altrimenti la più recente. Ognuna si usa una volta sola
    per passaggio: in una run due file dello stesso contenuto (due versioni
    di un film) tengono ciascuno la sua valutazione."""
    rows = _unused_candidates(ctx, media_item_id).get(torrent_id_remote)
    if not rows:
        return None
    pick = next((row for row in rows if _for_anchor(row, anchor)), rows[0])
    rows.remove(pick)
    return pick


def _drop_stale(ctx: MatchContext, media_item_id: int, anchor: MediaFile | SeedFile, formats: frozenset[str],
                skip: frozenset[str]) -> None:
    """Dopo una ricerca: le righe di prima per questo file (nei formati
    cercati) che la ricerca non ha riscritto, cioè torrent che il tracker non
    restituisce più o che ora non lo riguardano (il singolo di un altro
    episodio), o un pack che in questa run ha valutato un altro episodio.
    Solo righe che nessuna review e nessun seed_job usa, controllato di nuovo
    nella DELETE stessa: una review in cascata non si perde mai."""
    stale = []
    for remote, rows in _unused_candidates(ctx, media_item_id).items():
        if remote in skip:
            continue
        for row in [r for r in rows if _for_anchor(r, anchor) and review.candidate_format(r) in formats]:
            rows.remove(row)
            stale.append(row)
    if stale:
        ctx.session.query(Candidate).filter(
            Candidate.id.in_([row.id for row in stale]),
            ~exists().where(MatchReview.candidate_id == Candidate.id),
            ~exists().where(SeedJob.candidate_id == Candidate.id),
        ).delete(synchronize_session=False)
        # Fuori dalla sessione anche gli oggetti (coi loro candidate_file, in
        # cascata): SQLite riusa l'id più alto, e la riga nuova dello stesso
        # passaggio trovava l'id ancora occupato da quella tolta.
        for row in stale:
            ctx.session.expunge(row)


# Un torrent di cui il catalogo non elenca i file (Torznab) si scarica per
# valutarlo solo se la sua dimensione può contenere il file cercato: per un
# file singolo, al più questo in più (un .nfo, i sottotitoli, un sample).
_EXTRAS_SLACK_BYTES = 100 * 1024 * 1024


def _search(ctx: MatchContext, media_item_id: int, tmdb_id: int):
    """La ricerca sul catalogo: con search_media gli altri dati del
    contenuto (imdb, titolo, stagione) per un adapter che li usa (Torznab),
    se no per tmdb_id (un plugin o un adapter che non deriva da
    TrackerAdapter)."""
    search_media = getattr(ctx.tracker_adapter, "search_media", None)
    if search_media is None:
        return ctx.tracker_adapter.search_by_tmdb(tmdb_id)
    item = ctx.session.get(MediaItem, media_item_id)
    return search_media(MediaQuery(
        tmdb_id=tmdb_id,
        content_type=item.content_type if item is not None else None,
        imdb_id=item.imdb_id if item is not None else None,
        title=item.title if item is not None else None,
        year=item.year if item is not None else None,
        season=item.season_number if item is not None and item.content_type == "tv" else None,
    ))


def _may_contain(tc, anchor: MediaFile | SeedFile, formats: frozenset[str]) -> bool:
    """Per un torrent senza elenco dei file: se nome e dimensione lo rendono
    un candidato da scaricare. Più piccolo del file cercato no; più grande di
    un file singolo coi suoi extra solo se si cercano anche i pack."""
    if tc.size_bytes < anchor.size_bytes:
        return False
    return PACK in formats or tc.size_bytes <= anchor.size_bytes * 1.01 + _EXTRAS_SLACK_BYTES


def match_file(ctx: MatchContext, anchor: MediaFile | SeedFile, media_item_id: int, tmdb_id: int,
               formats: frozenset[str] = FORMATS, skip: frozenset[str] = frozenset(),
               returned: set[str] | None = None) -> list[Candidate]:
    """Cerca sul catalogo del tracker per tmdb_id e valuta ogni torrent
    trovato contro i file locali. Persiste anche i candidati a confidence 0
    (audit trail), tranne quelli che non riguardano affatto questo file.
    formats: solo i torrent di questi formati; skip: torrent già valutati
    (dalla history). returned, se dato, riceve gli id di tutti i torrent
    restituiti dal catalogo, anche quelli scartati prima di valutarli: per
    togliere dalla coda una review su un torrent che il tracker non ha più
    (review.CatalogSearch)."""
    persisted = []
    for tc in _search(ctx, media_item_id, tmdb_id):
        if returned is not None:
            returned.add(tc.torrent_id_remote)
        if tc.torrent_id_remote in skip:
            continue
        if not catalog_names_safe(tc.file_list, tc.folder):
            logger.warning("Torrent %s del tracker %r con nomi di file non validi: ignorato",
                           tc.torrent_id_remote, ctx.tracker_row.label)
            continue
        # Senza elenco dei file il nome è un titolo, non un percorso: la
        # struttura vera la dà solo il .torrent.
        unlisted = tc.file_list is None
        if unlisted and getattr(tc, "file_count", None) != 1:
            if not _may_contain(tc, anchor, formats):
                continue
            parsed = ctx.fetch_torrent(tc.download_link)
            if parsed is None:
                continue
            layout = layout_from_torrent(parsed)
        else:
            layout = layout_from_catalog(tc.file_list, tc.file_sizes, tc.folder, tc.size_bytes, tc.name)
        multi_video = len(layout.videos) > 1
        if multi_video and ctx.skips(anchor, tc.torrent_id_remote):
            continue  # già valutato in questa run per un altro episodio dello stesso pack
        if torrent_format(len(layout.videos)) not in formats:
            continue
        if _unrelated(ctx, layout, anchor):
            continue
        evaluation = _evaluate(
            ctx, layout, anchor, download_link=tc.download_link,
            unique_ids=tc.mediainfo_unique_ids_by_filename, single_unique_id=tc.mediainfo_unique_id,
        )
        if evaluation.ambiguity_reason in _UNRELATED_REASONS:
            continue
        if evaluation.confidence > CONFIDENCE_NO_MATCH:
            # Il catalogo UNIT3D spesso non riporta la cartella del torrent (né
            # sempre gli stessi percorsi): ricreare il seed con quei dati mette
            # i file nel posto sbagliato e il recheck fallisce. Per un
            # candidato plausibile la struttura vera la dà solo il .torrent, un
            # download per candidato, mai per quelli non pertinenti. Anche con
            # un file solo: può stare in una cartella (2026-10-06, segnalato).
            parsed = layout.torrent or ctx.fetch_torrent(tc.download_link)
            if parsed is not None:
                layout = layout_from_torrent(parsed)
                if torrent_format(len(layout.videos)) not in formats:
                    continue  # il catalogo non elencava tutti i video
                evaluation = _evaluate(
                    ctx, layout, anchor, download_link=tc.download_link,
                    unique_ids=tc.mediainfo_unique_ids_by_filename, single_unique_id=tc.mediainfo_unique_id,
                )
            elif len(layout.files) > 1 or unlisted:
                evaluation.confidence = CONFIDENCE_NO_MATCH
                evaluation.ambiguity_reason = review.UNREAD_TORRENT
            # Un file solo senza .torrent resta come il catalogo lo descrive:
            # il controllo completo prima di eseguire ne corregge la cartella.
            # Non se il catalogo non elenca i file: il nome lì è un titolo.
        if multi_video:
            _record_pack(ctx, anchor, tc.torrent_id_remote, evaluation)
        persisted.append(_persist(
            ctx, anchor, media_item_id=media_item_id, torrent_id_remote=tc.torrent_id_remote, name=tc.name,
            size_bytes=tc.size_bytes, layout=layout, evaluation=evaluation, download_link=tc.download_link,
            info_hash=tc.info_hash, source="catalog_search",
        ))
    _drop_stale(ctx, media_item_id, anchor, formats, skip)
    ctx.session.commit()
    return persisted


def match_from_history(ctx: MatchContext, anchor: MediaFile | SeedFile, media_item_id: int,
                       grab: ArrGrab, formats: frozenset[str] = FORMATS) -> list[Candidate] | None:
    """Candidato dal torrent che Radarr/Sonarr ricordano per questo file
    (nazgarr/integrations/arr.py): un solo download del .torrent al posto della ricerca sul
    catalogo. Stesse regole di confidence — la provenienza dalla history non
    alza da sola la confidence: un file può essere stato sostituito dopo
    l'import, sono size e piece hash a dirlo. None se il .torrent non è
    scaricabile/leggibile o non è di uno dei formati cercati: il chiamante
    decide se ripiegare sulla ricerca. [] se è un pack già valutato in
    questa run, e non è la proposta di questo file (MatchContext.skips)."""
    if ctx.skips(anchor, grab.torrent_id_remote):
        return []
    # Prima il link della history con la chiave attuale (se il tracker la
    # conosce: nessuna chiamata in più), poi, se rifiutato, il dettaglio.
    rewrite = getattr(ctx.tracker_adapter, "rewrite_download_link", None)
    download_link = rewrite(grab.download_url) if rewrite else grab.download_url
    parsed = ctx.fetch_torrent(download_link)
    if parsed is None:
        # Il link della history contiene la chiave di quando è stato fatto il
        # grab: se nel frattempo è cambiata il tracker lo rifiuta. Il
        # dettaglio del torrent (una chiamata) dà il link con la chiave
        # attuale — ed è quello che deve finire nel candidato, perché è il
        # link che poi scaricherà il client.
        download_link = _current_download_link(ctx, grab.torrent_id_remote)
        parsed = ctx.fetch_torrent(download_link) if download_link else None
    if parsed is None:
        return None
    layout = layout_from_torrent(parsed)
    if torrent_format(len(layout.videos)) not in formats:
        return None
    evaluation = _evaluate(ctx, layout, anchor, download_link=download_link, unique_ids=None,
                           single_unique_id=None)
    if len(layout.videos) > 1 and evaluation.ambiguity_reason not in _UNRELATED_REASONS:
        _record_pack(ctx, anchor, grab.torrent_id_remote, evaluation)
    candidate = _persist(
        ctx, anchor, media_item_id=media_item_id, torrent_id_remote=grab.torrent_id_remote, name=parsed.name,
        size_bytes=parsed.total_length, layout=layout, evaluation=evaluation, download_link=download_link,
        info_hash=grab.info_hash, source="history",
    )
    ctx.session.commit()
    return [candidate]


def _current_download_link(ctx: MatchContext, torrent_id_remote: str) -> str | None:
    get_detail = getattr(ctx.tracker_adapter, "get_torrent_detail", None)
    if get_detail is None:
        return None
    try:
        return get_detail(torrent_id_remote).download_link
    except TrackerRateLimitedError:
        raise
    except (UploadError, NotSupportedError, KeyError, ValueError):
        logger.warning("Dettaglio del torrent %s non disponibile", torrent_id_remote, exc_info=True)
        return None


def _anchor_path(anchor: MediaFile | SeedFile) -> str:
    return f"{anchor.disk.root_path}/{anchor.relative_path}"


def find_candidates(
    ctx: MatchContext, anchor: MediaFile | SeedFile, media_item_id: int, tmdb_id: int,
    formats: frozenset[str] = FORMATS,
) -> tuple[list[Candidate], bool, "review.CatalogSearch | None"]:
    """(candidati, da_history, ricerca sul catalogo). Prima la history di
    Radarr/Sonarr se ricorda un torrent di QUESTO tracker per il file;
    ricerca sul catalogo solo se non c'è o non ha dato un candidato
    plausibile. Un episodio che la history dà come singolo cerca comunque il
    pack nel catalogo: è il formato preferito (review.py), o il secondo con
    "pack e singoli". La ricerca sul catalogo è None se non c'è stata."""
    returned: set[str] = set()

    def searched() -> review.CatalogSearch:
        return review.CatalogSearch(ctx.tracker_row.id, frozenset(returned))

    grab = ctx.arr_index.grab_for(_anchor_path(anchor), anchor.size_bytes) if ctx.arr_index is not None else None
    if grab is not None and grab.tracker_host == host_of(ctx.tracker_row.base_url):
        candidates = match_from_history(ctx, anchor, media_item_id, grab, formats)
        if candidates is not None and (
            not candidates or any(c.confidence > CONFIDENCE_NO_MATCH for c in candidates)
        ):
            if PACK in formats and _wants_pack_search(ctx, anchor, candidates):
                seen = frozenset(c.torrent_id_remote for c in candidates)
                candidates += match_file(ctx, anchor, media_item_id, tmdb_id, frozenset({PACK}), skip=seen,
                                         returned=returned)
                return candidates, True, searched()
            return candidates, True, None
    return match_file(ctx, anchor, media_item_id, tmdb_id, formats, returned=returned), False, searched()


def _wants_pack_search(ctx: MatchContext, anchor: MediaFile | SeedFile, candidates: list[Candidate]) -> bool:
    return (ctx.direction == "media_to_torrent" and isinstance(anchor, MediaFile) and is_episode(anchor.media_item)
            and bool(candidates) and all(review.candidate_format(c) == SINGLE for c in candidates))


def _media_files(session: Session, media_file_ids: set[int] | None):
    query = session.query(MediaFile).options(selectinload(MediaFile.media_item)).filter(
        MediaFile.media_item_id.isnot(None))
    return query.filter(MediaFile.id.in_(media_file_ids)) if media_file_ids is not None else query


def orphan_media_files(
    session: Session, exclusions: CompiledExclusions | None = None, tracker: Tracker | None = None,
    media_file_ids: set[int] | None = None,
) -> list[MediaFile]:
    """media_file con identità risolta e non in seed (orphan_media, docs/SPEC.md
    sezione 3): stessa regola della Library (nazgarr/library/seeding.py), un hardlink che
    nessun client segue non conta. Con un tracker e la ricerca dei cross-seed
    accesa, "non in seed su questo tracker". Solo quelli risolvibili e non
    esclusi (Configuration > Exclusions). media_file_ids: solo questi (una
    ricerca mirata), letti e controllati in SQL invece che su tutta la
    libreria (audit 2026-10-18)."""
    ids = list(media_file_ids) if media_file_ids is not None else None
    linked_ids = seeding_media_file_ids(session, tracker, ids)
    latest = latest_scan_by_disk(session, MediaFile)
    return [
        mf
        for mf in _media_files(session, media_file_ids).all()
        if mf.id not in linked_ids
        and is_current(mf, latest)  # un file sparito dal disco non si cerca più
        and is_video(mf.relative_path)
        and not (exclusions and exclusions.is_excluded(mf.relative_path))
    ]


def second_format_media_files(
    session: Session, tracker: Tracker, exclusions: CompiledExclusions | None = None,
    media_file_ids: set[int] | None = None,
) -> list[tuple[MediaFile, frozenset[str]]]:
    """Con "pack e singoli dello stesso tracker" (nazgarr/library/seeding.py): gli
    episodi in seed su questo tracker in un formato solo, con quello che
    manca. Mai insieme agli orfani: un orfano non è in seed su questo tracker."""
    ids = list(media_file_ids) if media_file_ids is not None else None
    missing = {mf_id: FORMATS - found for mf_id, found in seeding_formats(session, tracker, ids).items()}
    missing = {mf_id: wanted for mf_id, wanted in missing.items() if wanted}
    if not missing:
        return []
    latest = latest_scan_by_disk(session, MediaFile)
    # Per id solo in una ricerca mirata: nella run gli episodi in seed in un
    # formato solo possono essere decine di migliaia, troppi per una IN.
    return [
        (mf, frozenset(missing[mf.id]))
        for mf in _media_files(session, media_file_ids).all()
        if mf.id in missing
        and is_episode(mf.media_item)
        and is_current(mf, latest)
        and is_video(mf.relative_path)
        and not (exclusions and exclusions.is_excluded(mf.relative_path))
    ]


def orphan_seed_files_with_identity(
    session: Session, exclusions: CompiledExclusions | None = None
) -> list[SeedFile]:
    """seed_file non tracciato da alcun client (orphan_torrent) il cui
    media_file collegato ha già un'identità risolta (docs/SPEC.md sezione 3,
    direzione torrent->client), esclusi i file esclusi.

    Semplificazione esplicita di questa fase: un seed_file senza
    media_file_id (mai stato organizzato nella libreria, quindi mai
    passato dal resolver) non viene risolto qui — richiederebbe girare il
    resolver anche sul lato torrent, non ancora fatto (docs/ROADMAP.md
    Fase 4)."""
    tracked_ids = {
        row[0]
        for row in session.query(ClientTorrentFile.seed_file_id)
        .filter(ClientTorrentFile.seed_file_id.isnot(None))
        .all()
    }
    # Un'altra copia (hardlink) di un file in seed da un altro percorso: gli
    # stessi byte sono già in seed, niente da cercare (nazgarr/library/states.py).
    for group in seed_copies_by_inode(session).values():
        if group & tracked_ids:
            tracked_ids |= group
    result = []
    latest = latest_scan_by_disk(session, SeedFile)
    for sf in session.query(SeedFile).options(
        selectinload(SeedFile.media_file).selectinload(MediaFile.media_item),
    ).filter(SeedFile.media_file_id.isnot(None)).all():
        if sf.id in tracked_ids or not is_current(sf, latest):
            continue
        if not is_video(sf.relative_path):
            continue
        if exclusions and exclusions.is_excluded(sf.relative_path):
            continue
        if sf.media_file is not None and sf.media_file.media_item_id is not None:
            result.append(sf)
    return result


def get_rematch_interval(session: Session) -> timedelta:
    return timedelta(days=max(settings_registry.get_float(session, "rematch_interval_days"), 0))


def _attempts_by_file(session: Session, tracker_row: Tracker, side: str) -> dict[int, MatchAttempt]:
    """I tentativi precedenti su questo tracker, per media_file_id o
    seed_file_id: letti in blocco invece che con una query per file."""
    column = MatchAttempt.media_file_id if side == "media" else MatchAttempt.seed_file_id
    return {
        getattr(a, column.key): a
        for a in session.query(MatchAttempt).filter(MatchAttempt.tracker_id == tracker_row.id, column.isnot(None))
    }


def _is_fresh(attempt: MatchAttempt | None, size_bytes: int, tmdb_id: int, interval: timedelta) -> bool:
    """Un tentativo precedente vale ancora se il file e la sua identità non
    sono cambiati e non è passato rematch_interval_days — in quel caso i
    candidati e la review già salvati restano validi, nessuna nuova ricerca."""
    if attempt is None or interval <= timedelta(0):
        return False
    if attempt.size_bytes != size_bytes or attempt.tmdb_id != tmdb_id:
        return False
    return datetime.now(UTC) - as_utc(attempt.attempted_at) < interval


def _record_attempt(
    session: Session,
    attempt: MatchAttempt | None,
    tracker_row: Tracker,
    size_bytes: int,
    tmdb_id: int,
    *,
    media_file_id: int | None = None,
    seed_file_id: int | None = None,
) -> None:
    if attempt is None:
        attempt = MatchAttempt(tracker_id=tracker_row.id, media_file_id=media_file_id, seed_file_id=seed_file_id)
        session.add(attempt)
    attempt.size_bytes = size_bytes
    attempt.tmdb_id = tmdb_id
    attempt.attempted_at = datetime.now(UTC)
    session.commit()


def _new_totals() -> dict:
    return {
        "files": 0, "from_history": 0, "candidates": 0, "skipped_fresh": 0, "failed": 0, "rate_limited": False,
    }


def run_media_to_torrent_matching(
    session: Session, tracker_row: Tracker, tracker_adapter: TrackerAdapter, arr_index: ArrIndex | None = None,
    progress=NULL_PROGRESS, only_media_file_ids: set[int] | None = None, force: bool = False,
    local: "SharedLocalFiles | None" = None,
) -> dict:
    """Un errore su un singolo file lo salta (loggato, contato in "failed")
    senza fermare gli altri; un TrackerRateLimitedError invece ferma il
    matching su questo tracker per il resto della run ("rate_limited"),
    lasciando i file rimanenti al prossimo giro — la ricerca di quelli
    completati è già registrata in match_attempt, quindi non si ripete.
    local: gli indici dei file locali condivisi da tutta la run
    (SharedLocalFiles), invece di ricostruirli per ogni tracker."""
    ctx = MatchContext(session, tracker_row, tracker_adapter, "media_to_torrent", arr_index, shared_local=local)
    exclusions = load_exclusions(session)
    # "Cerca ora" dalla scheda di dettaglio, o la ricerca dopo un import: solo
    # i file di quel contenuto, filtrati nella query.
    orphans = [(mf, FORMATS) for mf in orphan_media_files(session, exclusions, tracker_row, only_media_file_ids)]
    if pack_and_singles_enabled(session):
        ctx.both_formats = True
        orphans += second_format_media_files(session, tracker_row, exclusions, only_media_file_ids)
    if local is None and only_media_file_ids is not None:
        # Gli indici dei file locali solo per i dischi di quei file: ogni
        # abbinamento resta sul disco dell'anchor (LocalFiles.load).
        ctx.shared_local = SharedLocalFiles(session, {mf.disk_id for mf, _ in orphans})
    return _match_orphans(ctx, orphans, "media", progress, force)


def run_torrent_to_client_matching(
    session: Session, tracker_row: Tracker, tracker_adapter: TrackerAdapter, arr_index: ArrIndex | None = None,
    progress=NULL_PROGRESS, local: "SharedLocalFiles | None" = None,
) -> dict:
    """Stesse regole di run_media_to_torrent_matching, direzione opposta."""
    ctx = MatchContext(session, tracker_row, tracker_adapter, "torrent_to_client", arr_index, shared_local=local)
    orphans = [(sf, FORMATS) for sf in orphan_seed_files_with_identity(session, load_exclusions(session))]
    return _match_orphans(ctx, orphans, "seed", progress)


def _identity(orphan, side: str):
    """Il media_item dell'orfano: il suo, o lato torrent quello del media_file collegato."""
    return orphan.media_item if side == "media" else orphan.media_file.media_item


def _series_order(orphan, side: str) -> tuple:
    item = _identity(orphan, side)
    return (item.tmdb_id, item.season_number or 0, item.episode_number or 0, orphan.id)


def _match_orphans(ctx: MatchContext, orphans: list[tuple], side: str, progress, force: bool = False) -> dict:
    """Il giro sugli orfani di una direzione: side "media" (media_file, la
    sua identità) o "seed" (seed_file, l'identità del media_file collegato),
    ognuno coi formati da cercare."""
    session, tracker_row = ctx.session, ctx.tracker_row
    totals = _new_totals()
    interval = get_rematch_interval(session)
    progress.add_total(len(orphans))
    attempts = _attempts_by_file(session, tracker_row, side)
    # Per serie, stagione ed episodio: gli episodi di una serie si cercano
    # vicini, mentre la ricerca è ancora nella cache dell'adapter (10 minuti),
    # e un pack nuovo lo valuta per primo il suo primo episodio. Prima l'ordine
    # era quello delle righe nel DB (audit 2026-10-18). Le proposte non
    # dipendono dall'ordine: un pack già proposto per un episodio lo rivaluta
    # anche lui (_record_pack, 2026-10-22).
    orphans = sorted(orphans, key=lambda entry: _series_order(entry[0], side))
    # Quali si cercano lo si decide all'inizio: to_search deve dire il vero
    # per tutto il passaggio (MatchContext.pack_gone).
    fresh = set() if force else {
        orphan.id for orphan, _formats in orphans
        if _is_fresh(attempts.get(orphan.id), orphan.size_bytes, _identity(orphan, side).tmdb_id, interval)
    }
    ctx.to_search = {_file_key(orphan) for orphan, _formats in orphans if orphan.id not in fresh}
    with keep_loaded_on_commit(session):
        for orphan, formats in orphans:
            item = _identity(orphan, side)
            attempt = attempts.get(orphan.id)
            if orphan.id in fresh:
                totals["skipped_fresh"] += 1
                progress.advance(skipped=1)
                continue
            ctx.to_search.discard(_file_key(orphan))
            try:
                candidates, from_history, searched = find_candidates(ctx, orphan, item.id, item.tmdb_id, formats)
                if side == "media":
                    review.create_review_for_media_file(session, orphan, candidates, formats=formats,
                                                        both_formats=ctx.both_formats, queue=ctx.review_queue(),
                                                        searched=searched, pack_gone=partial(ctx.pack_gone, orphan))
                else:
                    review.create_review_for_seed_file(session, orphan, candidates, queue=ctx.review_queue(),
                                                       searched=searched)
            except TrackerRateLimitedError:
                logger.warning("Tracker %r in rate limit: matching interrotto per questa run", tracker_row.label)
                session.rollback()
                totals["rate_limited"] = True
                break
            except Exception:
                logger.exception("Matching fallito per %s_file %s su tracker %r", side, orphan.id, tracker_row.label)
                session.rollback()
                totals["failed"] += 1
                progress.advance()
                continue
            _record_attempt(session, attempt, tracker_row, orphan.size_bytes, item.tmdb_id,
                            **({"media_file_id": orphan.id} if side == "media" else {"seed_file_id": orphan.id}))
            totals["files"] += 1
            totals["from_history"] += int(from_history)
            totals["candidates"] += len(candidates)
            progress.advance()
            progress.result(candidates=len(candidates))
    return totals
