"""Esecutore: hardlink + aggiunta al client (direzione media_to_torrent)
o solo aggiunta al client (direzione torrent_to_client, il file esiste
già) — sempre con recheck forzato, mai skip_checking (docs/SPEC.md
sezione 8), salvo l'eccezione opt-in di execute_review(skip_recheck=True):
il controllo completo di Nazgarr appena passato al 100% (decisione
dell'utente, 2026-09-29). Ordine dei passi per media_to_torrent, mai
bypassabile:
1. verifica st_dev sorgente/destinazione (mai un cross-device silente)
2. hardlink col nome esatto atteso dal tracker
3. add_torrent sul client, sempre con force_recheck=True, fermo: parte solo
   a recheck riuscito (reconcile_seed_job, decisione dell'utente 2026-10-18);
   con skip_recheck entra avviato, come prima (_add_to_client)

Un candidato con righe candidate_file (nazgarr/torrents/layout.py: film con
extra, season pack) si ricrea file per file: un hardlink per ogni file del
torrent che ha un file locale, video ed extra; gli extra senza file locale
li scarica il client dopo il recheck, entro seed_job.expected_missing_bytes.
Un video senza file locale non arriva mai qui (candidato a confidence 0).
I candidati salvati prima di candidate_file seguono il percorso a file
singolo originale. Porting/adattamento da ratio-guardian/app/executor.py,
per le due direzioni di docs/SPEC.md sezione 3.

Il client riceve sempre i byte del .torrent scaricato qui dal tracker,
mai il download_link (audit 2026-10-18): con il link il client scaricava
la sua copia, e niente la legava a quella che il matching e il controllo
completo avevano verificato. L'info hash di quei byte deve essere quello
del candidato (scritto dal matching o dal controllo completo), ed è
quello che il client deve far comparire.
"""

import json
import logging
import os
import tempfile
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.exc import NoResultFound
from sqlalchemy.orm import Session, sessionmaker

from nazgarr.adapters.torrent_client.base import (
    CONTENT_LAYOUTS,
    TorrentAddTimeoutError,
    TorrentAlreadyInClientError,
    TorrentClientAdapter,
    TorrentStatus,
    is_stopped_state,
    layout_kwargs,
    stopped_kwargs,
)
from nazgarr.adapters.tracker.base import TrackerWebResult
from nazgarr.core import settings_repo
from nazgarr.core.db_utils import as_utc
from nazgarr.core.errors import CodedError, english
from nazgarr.core.fs_scope import ScopeViolation, resolve_scoped
from nazgarr.core.logs import safe_error
from nazgarr.core.models import Candidate, Disk, DiskTorrentClient, MatchReview, SeedJob, TorrentClient
from nazgarr.integrations import adapter_factory
from nazgarr.library import disk_folders, hardlinks
from nazgarr.library.tmdb_client import TMDBClient
from nazgarr.reseed import download_links, full_check, resurrection
from nazgarr.reseed.placement import Placement, placement_for, possible_placements
from nazgarr.torrents import client_labels, client_paths, partial_files
from nazgarr.torrents.layout import expected_missing_bytes
from nazgarr.torrents.metainfo import TorrentMetainfoError, compute_info_hash, parse_torrent_info

logger = logging.getLogger(__name__)


class ExecutionError(Exception):
    """Errore esplicito che impedisce l'esecuzione — mai un fallimento silente."""


class RetryRefused(CodedError, ExecutionError):
    """Un retry che non si fa, col perché in un codice (nazgarr/core/errors.py):
    l'API lo restituisce così com'è, e interfaccia e CLI lo traducono (audit
    2026-10-18, prima un 409 di testo libero). Resta un ExecutionError per chi
    lo chiama da Python, col testo inglese del codice."""

    def __str__(self) -> str:
        return english(self.code, self.params)


# Il messaggio di un'esecuzione fallita perché il client aveva già il torrent
# prima che questa lo aggiungesse (TorrentAlreadyInClientError): l'ha messo
# un altro programma (cross-seed, autobrr) o l'utente. Il retry lo riconosce
# (_failed_as_duplicate) e non lo prende mai per suo.
ALREADY_IN_CLIENT = ("The client already had this torrent before this execution added it (another program, or "
                     "added by hand): nothing added, and Nazgarr leaves that torrent alone")
# Il testo degli adapter inclusi per lo stesso errore, sui seed job falliti
# prima di ALREADY_IN_CLIENT.
_ADAPTER_DUPLICATE_TEXT = "is already in the client (possibly with another save path)"
# In coda al messaggio di un'esecuzione fallita prima di mandare il torrent
# al client (il .torrent non scaricato, un hardlink, un percorso che il
# client non vede): un torrent con quell'hash che il retry trovasse nel
# client ce l'ha messo qualcun altro, e non si segue.
NOTHING_SENT = "(nothing was sent to the client)"
# Il messaggio di un'esecuzione il cui torrent il client non ha più
# (review.reconcile_pending_seed_jobs, dopo MISSING_TORRENT_GRACE): un
# torrent con quell'hash che il retry ritrovasse nel client si segue solo
# se è dove questa review l'aveva mandato (adopt_for_retry).
GONE_FROM_CLIENT = "The torrent is no longer in the client: retry it to add it again, or delete it"


def _error_text(exc: Exception) -> str:
    """Il messaggio salvato sul seed_job e mostrato nella pagina Reseeding:
    per un errore con codice il suo testo inglese con i parametri (prima
    solo il codice, es. "client_cannot_see_path" senza dire quale percorso)."""
    return english(exc.code, exc.params) if isinstance(exc, CodedError) else str(exc)


def client_visible_path(session: Session, disk: Disk, torrent_client_id: int | None, local_path: str) -> str:
    """Traduce un path lato Nazgarr nel path equivalente visto DA QUESTO
    client torrent, quando i due girano in container/mount diversi per lo
    stesso disco fisico (disk_torrent_client: torrent_client_root_path e
    local_rel_path per la coppia (disk, torrent_client_id), nazgarr/torrents/client_paths.py).
    Senza mappatura, client e Nazgarr vedono lo stesso path. Un path che il
    client non vede (fuori dalla sottocartella mappata): ClientPathError."""
    mapping = _mapping(session, disk, torrent_client_id)
    return local_path if mapping is None else client_paths.to_client(mapping, local_path)


def _mapping(session: Session, disk: Disk, torrent_client_id: int | None) -> client_paths.Mapping | None:
    """Come questo client vede il disco (disk_torrent_client), None se non
    c'è una riga per la coppia: stessi percorsi di Nazgarr."""
    if torrent_client_id is None:
        return None
    link = (
        session.query(DiskTorrentClient)
        .filter_by(disk_id=disk.id, torrent_client_id=torrent_client_id)
        .one_or_none()
    )
    if link is None:
        return None
    return client_paths.Mapping(disk.root_path, link.torrent_client_root_path, link.local_rel_path)


FetchTorrent = Callable[[Candidate], bytes]


def execute_review(
    session: Session, review: MatchReview, adapter: TorrentClientAdapter, torrent_client_id: int | None = None,
    skip_recheck: bool = False, fetch_torrent: FetchTorrent | None = None,
) -> SeedJob:
    """skip_recheck: SOLO dopo un controllo completo di Nazgarr al 100% senza
    extra mancanti, con l'opzione accesa dall'utente (nazgarr/reseed/review.py). In ogni
    altro caso il client fa il suo recheck reale, come sempre.
    fetch_torrent: i byte del .torrent del candidato (default: dal suo
    tracker, _download_torrent)."""
    candidate = review.candidate
    if candidate.files:
        if candidate.direction == "media_to_torrent":
            return _execute_layout_media_to_torrent(
                session, review, adapter, torrent_client_id, skip_recheck, fetch_torrent)
        return _execute_layout_torrent_to_client(
            session, review, adapter, torrent_client_id, skip_recheck, fetch_torrent)
    if candidate.direction == "media_to_torrent":
        return _execute_media_to_torrent(session, review, adapter, torrent_client_id, skip_recheck, fetch_torrent)
    return _execute_torrent_to_client(session, review, adapter, torrent_client_id, skip_recheck, fetch_torrent)


@dataclass(frozen=True)
class _CheckedTorrent:
    content: bytes
    info_hash: str  # minuscolo


def _download_torrent(candidate: Candidate) -> bytes:
    """Il .torrent del candidato dal suo tracker, con il rate limit, la
    regola dell'host e il tetto di dimensione dell'adapter
    (Unit3dTrackerAdapter.download_torrent)."""
    tracker = candidate.tracker
    if tracker is None:
        raise ExecutionError("The candidate's tracker no longer exists: its .torrent cannot be downloaded")
    try:
        with adapter_factory.tracker(tracker) as tracker_adapter:
            return download_links.download(tracker_adapter, candidate)  # la chiave rimessa qui
    except Exception as exc:
        raise ExecutionError(f"Could not download the .torrent from {tracker.label}: {safe_error(exc)}") from exc


def _torrent_to_add(session: Session, candidate: Candidate, fetch_torrent: FetchTorrent | None) -> _CheckedTorrent:
    """I byte che il client riceverà, scaricati da Nazgarr e legati al
    candidato: se il matching o il controllo completo ne conoscono l'info
    hash, deve essere quello (un tracker che serve un torrent diverso a
    questa richiesta non ottiene niente); altrimenti è quello di questi
    byte, e si scrive sul candidato per l'attesa del client e i retry."""
    content = (fetch_torrent or _download_torrent)(candidate)
    try:
        parse_torrent_info(content)
        info_hash = compute_info_hash(content)
    except TorrentMetainfoError as exc:
        raise ExecutionError(f"The tracker sent an invalid .torrent: {exc}") from exc
    expected = (candidate.info_hash or "").lower()
    if expected and expected != info_hash:
        raise ExecutionError(
            f"The tracker sent a different .torrent (info hash {info_hash}) from the one that was matched "
            f"and checked ({expected}): nothing added. Delete this execution so the next scan matches it again"
        )
    if not expected:
        candidate.info_hash = info_hash
        session.commit()
    return _CheckedTorrent(content=content, info_hash=info_hash)


def _structure_from_torrent(session: Session, candidate: Candidate, fetch_torrent: FetchTorrent | None) -> FetchTorrent:
    """Un candidato il cui .torrent il matching non ha mai letto (niente
    piece_length, che c'è solo per una struttura letta da un .torrent: il
    download era fallito): la cartella e i nomi dei file vengono solo dal
    catalogo del tracker, cioè da chi ha caricato il torrent. Anche dopo un
    controllo completo passato, che impara l'info hash e corregge la cartella
    ma ritrova i file anche per nome: prima bastava l'info hash per saltare
    questo controllo (audit 2026-10-18). Prima di decidere dove vanno
    gli hardlink, la struttura vera dal .torrent: la cartella si corregge (il
    catalogo UNIT3D spesso non la riporta), e i file devono essere gli
    stessi, altrimenti niente (audit 2026-10-18: un nome del catalogo poteva
    mettere l'hardlink nella cartella di un altro torrent). Restituisce i
    byte già scaricati, per non riscaricarli."""
    content = (fetch_torrent or _download_torrent)(candidate)
    try:
        parsed = parse_torrent_info(content)
    except TorrentMetainfoError as exc:
        raise ExecutionError(f"The tracker sent an invalid .torrent: {exc}") from exc
    if sorted(f.torrent_path for f in candidate.files) != sorted(e.path for e in parsed.files):
        raise ExecutionError(
            "The files of the torrent are not the ones the tracker's catalogue listed: nothing created. "
            "Search it again (Search now) to read its real structure"
        )
    folder = parsed.name if parsed.is_multi_file else None
    if candidate.folder != folder:
        logger.info("Candidate %s: cartella del torrent %r invece di %r (dal .torrent)",
                    candidate.id, folder, candidate.folder)
        candidate.folder = folder
        session.commit()
    return lambda _candidate: content


def _labels(session: Session, torrent_client_id: int | None, candidate, review: MatchReview | None = None) -> dict:
    """Categoria (per tipo di contenuto) e tag per i reseed del client
    (nazgarr/torrents/client_labels.py). TMDB, per sapere se è un anime, solo se il
    client ha una categoria per gli anime; senza TMDB non lo è. Quelli scelti
    a mano sulla review vincono (NULL = default, "" = nessuno)."""
    client = session.get(TorrentClient, torrent_client_id) if torrent_client_id is not None else None
    if client is None:
        return {}
    chosen_category = review.client_category if review is not None else None
    chosen_tags = review.client_tags if review is not None else None
    tags = chosen_tags if chosen_tags is not None else client_labels.default_tags(client, "reseed")
    if chosen_category is not None:
        return client_labels.add_kwargs(chosen_category or None, tags)
    item = candidate.media_item
    content_type = item.content_type if item is not None else None
    anime = False
    if client.category_anime and item is not None and item.tmdb_id:
        api_key = settings_repo.get_setting(session, "tmdb_api_key")
        try:
            anime = bool(api_key) and client_labels.is_anime(
                TMDBClient(api_key=api_key).full_details(content_type, item.tmdb_id)
            )
        except Exception:
            logger.warning("TMDB non disponibile per il riconoscimento anime di %s", item.tmdb_id, exc_info=True)
    return client_labels.add_kwargs(client_labels.default_category(client, content_type, anime), tags)


def _add_to_client(
    adapter: TorrentClientAdapter, torrent: _CheckedTorrent, save_path: str, seed_job: SeedJob, skip_recheck: bool,
    labels: dict | None = None, layout: str = "Original",
) -> str:
    """Aggiunge il torrent al client. Il recheck del client si salta solo se
    il chiamante l'ha verificato lui stesso al 100% (skip_recheck): resta
    scritto sul seed_job, così si vede quali esecuzioni sono passate così.
    labels: categoria e tag del client (_labels), solo se ce ne sono.
    Un client che non sa saltarlo (can_skip_recheck) lo fa comunque.
    layout: sempre esplicito (Placement), "Original" per file già al loro
    posto: la preferenza del client non li deve spostare.

    Con il recheck del client il torrent entra fermo (decisione dell'utente,
    2026-10-18; stopped_kwargs, ogni adapter incluso) e parte solo quando il
    recheck è riuscito (reconcile_seed_job): avviato, con un recheck fallito
    scaricava i piece mancanti dentro gli hardlink, cioè nei file della
    libreria, fino al reconcile che lo fermava (minuti, o una run intera).
    Senza recheck (verificato al 100% da Nazgarr) parte subito, come prima.

    Il .torrent va come file locale temporaneo, che ogni adapter manda al
    client col suo contenuto (docs/SDK.md): mai il link del tracker."""
    skip_recheck = skip_recheck and getattr(adapter, "can_skip_recheck", True)
    extra = {"skip_check_verified": True} if skip_recheck else stopped_kwargs(adapter)
    with tempfile.TemporaryDirectory(prefix="nazgarr-reseed-") as folder:
        path = os.path.join(folder, f"{torrent.info_hash}.torrent")
        with open(path, "wb") as fh:
            fh.write(torrent.content)
        info_hash = adapter.add_torrent(
            path, save_path=save_path, force_recheck=True,
            expected_info_hash=torrent.info_hash, **extra, **(labels or {}), **layout_kwargs(adapter, layout),
        )
    seed_job.recheck_skipped = skip_recheck or None
    return info_hash


def _adopt_after_add_error(
    session: Session, adapter: TorrentClientAdapter, candidate, seed_job: SeedJob, exc: Exception,
    save_path: str,
) -> str:
    """Dopo un errore di add_torrent, cosa sa il client del torrent:
    "adopted" se ce l'ha (il client l'ha accettato e poi qualcosa è andato
    storto, tipicamente l'attesa del suo hash scaduta mentre scaricava il
    .torrent dal tracker): il seed_job lo segue come un'aggiunta riuscita;
    "absent" se il client risponde e non ce l'ha; "unknown" se non si può
    sapere. Prima gli hardlink si toglievano a ogni errore: il torrent
    arrivava lo stesso, non trovava i file e partiva in download.

    TorrentAlreadyInClientError (il client l'aveva già prima di questa
    aggiunta) è "absent", il torrent di qualcun altro, tranne quando
    un'esecuzione precedente della stessa review l'ha mandato al client
    senza saperne l'esito (_earlier_add_unconfirmed, es. l'attesa del suo
    hash scaduta) e il client lo ha proprio a save_path, dove l'ha mandato
    anche questa: è quello, arrivato tardi, e si segue (revisione dell'audit
    2026-10-18: prima questa esecuzione falliva con "l'ha messo un altro
    programma" e il retry non lo seguiva più, fermo e mai ricontrollato)."""
    duplicate = isinstance(exc, TorrentAlreadyInClientError)
    if duplicate and not (candidate.info_hash and _earlier_add_unconfirmed(session, seed_job)):
        return "absent"
    if not candidate.info_hash:
        return "unknown"
    try:
        info = adapter.get_torrent_info(candidate.info_hash)
    except Exception:
        logger.warning("Impossibile chiedere al client se ha %s dopo l'errore", candidate.info_hash, exc_info=True)
        return "absent" if duplicate else "unknown"
    if info is None:
        return "absent"
    if duplicate and _normpath(info.save_path) != _normpath(save_path):
        logger.warning("Il client ha già %s, ma in %s e non in %s: non è di un'esecuzione precedente, non lo seguo",
                       info.info_hash, info.save_path, save_path)
        return "absent"
    logger.warning(
        "Il client ha il torrent %s nonostante l'errore (%s): seed job %s lo segue",
        info.info_hash, _error_text(exc), seed_job.id,
    )
    if not seed_job.recheck_skipped:
        try:  # l'errore può essere arrivato prima del recheck forzato
            adapter.recheck(info.info_hash)
        except Exception:
            logger.warning("Recheck di %s non richiesto al client", info.info_hash, exc_info=True)
    seed_job.info_hash = info.info_hash
    seed_job.torrent_added_at = datetime.now(UTC)
    seed_job.recheck_status = "pending"
    session.commit()
    return "adopted"


def _normpath(path: str | None) -> str | None:
    return os.path.normpath(path) if path else None


def _earlier_add_unconfirmed(session: Session, seed_job: SeedJob) -> bool:
    """Un'esecuzione precedente della stessa review è fallita dopo aver
    mandato il torrent al client, senza averne mai l'info hash: l'attesa
    del suo hash scaduta, un errore del client dopo l'invio, un riavvio a
    metà (INTERRUPTED_SEED_JOB). Non quelle fallite prima di mandarlo
    (NOTHING_SENT) o perché il client l'aveva già (_not_sent_by)."""
    earlier = (
        session.query(SeedJob)
        .filter(SeedJob.candidate_id == seed_job.candidate_id, SeedJob.id < seed_job.id,
                SeedJob.final_status == "failed", SeedJob.info_hash.is_(None))
        .filter_by(source_media_file_id=seed_job.source_media_file_id,
                   source_seed_file_id=seed_job.source_seed_file_id)
        .all()
    )
    return any(not _not_sent_by(row) for row in earlier)


def _client_layout(adapter: TorrentClientAdapter) -> str:
    try:
        layout = adapter.content_layout()
    except AttributeError:
        return "Original"  # un adapter senza content_layout segue il .torrent
    except Exception:
        logger.warning("Layout del contenuto del client non letto: uso Original", exc_info=True)
        return "Original"
    return layout if layout in CONTENT_LAYOUTS else "Original"


def _placement(adapter: TorrentClientAdapter, candidate, single_file_name: str | None) -> Placement:
    """Dove vanno i nuovi hardlink perché il torrent li trovi rispettando il
    layout del contenuto scelto dall'utente nel client: la regola è in
    nazgarr/reseed/placement.py, la stessa con cui seed_refresh e il
    controllo completo li cercano dopo."""
    return placement_for(_client_layout(adapter), candidate.folder, single_file_name)


def _torrent_rel_path(candidate, torrent_path: str) -> str:
    return f"{candidate.folder}/{torrent_path}" if candidate.folder else torrent_path


def _missing_bytes_for(candidate) -> int:
    missing = [f for f in candidate.files if not f.is_video and f.media_file_id is None and f.seed_file_id is None]
    return expected_missing_bytes(sum(f.size_bytes or 0 for f in missing), len(missing), candidate.piece_length)


def _require_known_structure(candidate) -> None:
    """Un torrent con più file ha sempre una cartella radice (info.name):
    senza, i file finirebbero sciolti nella cartella dei torrent e il
    recheck non potrebbe mai riuscire. Candidati salvati prima che la
    struttura venisse letta dal .torrent: vanno ricercati ("Cerca ora")."""
    if len(candidate.files) > 1 and not candidate.folder:
        raise ExecutionError(
            "The torrent's folder is unknown for this multi-file candidate: search it again (Search now) "
            "to read its real structure from the .torrent"
        )


def _require_every_video(candidate) -> None:
    for f in candidate.files:
        if f.is_video and f.media_file_id is None and f.seed_file_id is None:
            raise ExecutionError(
                f"Video '{f.torrent_path}' of the torrent has no local file: a partial pack is never executed"
            )


def _source_path(disk: Disk, relative_path: str) -> str:
    """Il file in libreria da cui nasce l'hardlink: dentro il disco
    (fs_scope) e un file vero, non un link simbolico. Lo scanner salta i
    symlink, ma fra la scansione e l'esecuzione il file può cambiare."""
    try:
        path = resolve_scoped(disk.root_path, relative_path)
    except ScopeViolation as exc:
        raise ExecutionError(f"Path outside the allowed scope: {exc.candidate}") from exc
    if os.path.islink(os.path.join(disk.root_path, relative_path)) or os.path.islink(path):
        raise ExecutionError(f"Local file is a symbolic link: {path}")
    if not os.path.isfile(path):
        raise ExecutionError(f"Local file not found: {path}")
    return path


def _check_link(source_path: str, target_path: str, target_root: str) -> str | None:
    """I controlli comuni (nazgarr/library/hardlinks.py::check_link) prima di creare
    un hardlink, sulla destinazione così come è scritta (target_root più il
    percorso del torrent, mai già risolta): risolta, un link simbolico al
    suo posto non si vedeva più, e l'hardlink nasceva dove puntava (audit
    2026-10-18). Restituisce la destinazione risolta da passare a
    create_links, o None se è già lo stesso file (stesso inode): un
    cross-seed della stessa release con lo stesso nome su un altro tracker
    riusa quell'hardlink. Un altro file al suo posto, o un link simbolico,
    resta un errore: mai sovrascritto né seguito."""
    try:
        return hardlinks.check_link(source_path, target_path, target_root)
    except hardlinks.LinkProblem as problem:
        if problem.code == "cross_device":
            raise ExecutionError(
                f"Source and destination are on different devices ({problem.detail}): "
                "a hardlink cannot cross filesystems, see docs/SPEC.md section 4"
            ) from problem
        if problem.code == "target_exists":
            raise ExecutionError(f"Destination path already exists: {target_path}") from problem
        raise ExecutionError(f"Local file is not a regular file: {problem.path}") from problem
    except ScopeViolation as exc:
        raise ExecutionError(f"Path outside the allowed scope: {exc.candidate}") from exc


def _seed(
    session: Session, seed_job: SeedJob, review: MatchReview, adapter: TorrentClientAdapter,
    torrent_client_id: int | None, disk: Disk, save_root: str, skip_recheck: bool,
    fetch_torrent: FetchTorrent | None, links: list[tuple[str, str]] | None = None, layout: str = "Original",
) -> SeedJob:
    """La parte comune a ogni esecuzione, dopo i controlli: il .torrent dal
    tracker, gli hardlink (links: None per torrent -> client, dove i file
    ci sono già), l'aggiunta al client. Un'unica regola sugli errori, prima
    copiata quattro volte con differenze (audit 2026-10-18): se il client ha
    il torrent lo si segue; se risponde che non ce l'ha, si tolgono solo gli
    hardlink appena creati da questa esecuzione (i file in libreria non si
    toccano mai), tranne dopo un timeout, quando potrebbe ancora arrivare e
    senza i file partirebbe in download."""
    candidate = review.candidate
    created: list[str] = []
    add_sent = False
    try:
        torrent = _torrent_to_add(session, candidate, fetch_torrent)  # prima di ogni hardlink
        # Prima degli hardlink e del client: UNIT3D accetta il claim solo con 0 seeder.
        _claim_resurrection(session, seed_job, candidate)
        if links is not None:
            created = hardlinks.create_links(links)  # se uno fallisce, toglie quelli appena creati
            seed_job.hardlink_created_at = datetime.now(UTC)
            session.commit()
            logger.info("Creati %d hardlink per candidate %s (%d già al loro posto)",
                        len(created), candidate.id, len(links) - len(created))
        client_save_path = client_visible_path(session, disk, torrent_client_id, save_root)
        labels = _labels(session, torrent_client_id, candidate, review)
        add_sent = True
        info_hash = _add_to_client(adapter, torrent, client_save_path, seed_job, skip_recheck, labels, layout)
        _keep_added_torrent(seed_job, torrent.content)
        seed_job.info_hash = info_hash
        seed_job.torrent_added_at = datetime.now(UTC)
        seed_job.recheck_status = "pending"
        session.commit()
        logger.info("Torrent aggiunto al client (info_hash=%s), recheck in corso", info_hash)
    except Exception as exc:
        known = (_adopt_after_add_error(session, adapter, candidate, seed_job, exc, client_save_path)
                 if add_sent else "absent")
        if known == "adopted":
            return seed_job
        if known == "absent" and not isinstance(exc, TorrentAddTimeoutError):
            hardlinks.remove_links(created)
        elif created:
            logger.warning("Hardlink di candidate %s lasciati al loro posto: il client potrebbe avere il torrent",
                           candidate.id)
        seed_job.final_status = "failed"
        if not add_sent:
            seed_job.error_message = f"{_error_text(exc)} {NOTHING_SENT}"
        elif isinstance(exc, TorrentAlreadyInClientError):
            seed_job.error_message = (f"{ALREADY_IN_CLIENT}: remove it from the client if it should not be there, "
                                      "then Retry; or delete this execution")
        else:
            seed_job.error_message = _error_text(exc)
        session.commit()
        # Lo stesso testo inglese salvato sul seed_job, senza chiavi: prima
        # str(exc) di un errore con codice era il codice nudo (es.
        # "client_cannot_see_path"), e quello di un client il suo messaggio
        # così com'era (audit 2026-10-18).
        raise ExecutionError(safe_error(seed_job.error_message)) from exc
    return seed_job


def _claim_resurrection(session: Session, seed_job: SeedJob, candidate: Candidate) -> None:
    """Il claim della resurrection di un torrent morto che questa esecuzione
    rimette in seed, con la sessione web salvata sul tracker (decisione
    dell'utente, 2026-11-10; nazgarr/reseed/resurrection.py): i seeder si
    rileggono dall'API, il candidato non li ha (e uno dalla history di
    Radarr/Sonarr non li ha mai avuti). L'esito resta sul seed job
    (resurrection). Una sessione scaduta o un errore fermano l'esecuzione
    prima di hardlink e client: Retry la rifà, dopo il claim a mano o una
    sessione nuova. Senza sessione nessuna richiesta."""
    tracker = candidate.tracker
    if not resurrection.has_session(tracker):
        return
    try:
        with adapter_factory.tracker(tracker) as tracker_adapter:
            result = resurrection.claim_before_seed(tracker, tracker_adapter, candidate.torrent_id_remote)
    except Exception as exc:  # l'adapter non si costruisce
        result = TrackerWebResult("failed", safe_error(exc))
    if result is None:
        return
    if result.status in resurrection.GO_ON:
        seed_job.resurrection = result.status
        session.commit()
        logger.info("Resurrection di %s su %s: %s %s", candidate.torrent_id_remote, tracker.label, result.status,
                    result.detail or "")
        return
    reason = "the website session expired" if result.status == "session_expired" else (
        result.detail or result.status)
    raise ExecutionError(
        f"The resurrection claim on {tracker.label} did not go through ({reason}): claim it on the tracker, "
        "or save a new website session, then Retry")


def _require_download_link(candidate) -> None:
    if not candidate.download_link:
        raise ExecutionError("Candidate has no download_link: cannot add the torrent to the client")


def _new_seed_job(session: Session, candidate, torrent_client_id: int | None, **source) -> SeedJob:
    seed_job = SeedJob(
        candidate_id=candidate.id, final_status="in_progress", torrent_client_id=torrent_client_id,
        expected_missing_bytes=_missing_bytes_for(candidate) if candidate.files else None, **source,
    )
    session.add(seed_job)
    session.commit()
    _forget_seed_job(seed_job.id)  # SQLite può ridare l'id di un seed job eliminato
    return seed_job


def _scoped_file(disk: Disk, relative_path: str) -> str:
    """Un file già su disco che il torrent userà così com'è (torrent ->
    client): dentro il disco (fs_scope) ed esistente."""
    try:
        path = resolve_scoped(disk.root_path, relative_path)
    except ScopeViolation as exc:
        raise ExecutionError(f"Path outside the allowed scope: {exc.candidate}") from exc
    if not os.path.isfile(path):
        raise ExecutionError(f"Local file not found: {relative_path}")
    return path


def _new_torrents_root(disk: Disk) -> str:
    if not disk.seeding_folders:
        raise ExecutionError(f"Disk '{disk.label}' has no seeding folder configured")
    try:
        target_root = resolve_scoped(disk.root_path, disk.effective_new_torrent_rel_path)
    except ScopeViolation as exc:
        raise ExecutionError(f"Path outside the allowed scope: {exc.candidate}") from exc
    if not os.path.isdir(target_root):
        raise ExecutionError(f"Destination folder for new hardlinks not found: {target_root}")
    return target_root


def _in_scope(root: str, relative: str) -> str:
    try:
        return resolve_scoped(root, relative)
    except ScopeViolation as exc:
        raise ExecutionError(f"Path outside the allowed scope: {exc.candidate}") from exc


def _execute_layout_media_to_torrent(
    session: Session, review: MatchReview, adapter: TorrentClientAdapter, torrent_client_id: int | None = None,
    skip_recheck: bool = False, fetch_torrent: FetchTorrent | None = None,
) -> SeedJob:
    """Ricrea il torrent intero con hardlink dai file in libreria. Tutti i
    controlli PRIMA di creare il primo hardlink (scope, stesso disco, stesso
    filesystem, destinazioni libere): mai un pack ricreato a metà per un
    problema che si poteva vedere prima."""
    candidate = review.candidate
    anchor = review.media_file
    if anchor is None:
        raise ExecutionError(f"MatchReview {review.id} (media_to_torrent) has no linked media_file")
    _require_known_structure(candidate)
    _require_every_video(candidate)
    disk = anchor.disk
    target_root = _new_torrents_root(disk)
    if candidate.piece_length is None:
        _require_download_link(candidate)
        fetch_torrent = _structure_from_torrent(session, candidate, fetch_torrent)

    placement = _placement(adapter, candidate, candidate.files[0].torrent_path if len(candidate.files) == 1 else None)
    links: list[tuple[str, str]] = []
    for f in candidate.files:
        if f.media_file is None:
            continue  # extra senza file locale: lo scarica il client
        if f.media_file.disk_id != disk.id:
            raise ExecutionError(f"'{f.media_file.relative_path}' is on another disk: a hardlink cannot cross disks")
        source_path = _source_path(disk, f.media_file.relative_path)
        target_path = _check_link(source_path, os.path.join(target_root, placement.rel_path(f.torrent_path)),
                                  target_root)
        if target_path is None:
            continue  # già lì, stesso inode: niente da creare (e niente da togliere se fallisce)
        links.append((source_path, target_path))
    _require_download_link(candidate)
    save_root = _in_scope(target_root, placement.save_dir) if placement.save_dir else target_root

    seed_job = _new_seed_job(session, candidate, torrent_client_id, source_media_file_id=anchor.id)
    return _seed(session, seed_job, review, adapter, torrent_client_id, disk, save_root, skip_recheck,
                 fetch_torrent, links=links, layout=placement.layout)


def _execute_layout_torrent_to_client(
    session: Session, review: MatchReview, adapter: TorrentClientAdapter, torrent_client_id: int | None = None,
    skip_recheck: bool = False, fetch_torrent: FetchTorrent | None = None,
) -> SeedJob:
    """La cartella del torrent è ancora su disco: nessun hardlink, solo
    l'aggiunta al client con save_path = la cartella che la contiene,
    ricavata dall'anchor e verificata su ogni altro file abbinato."""
    candidate = review.candidate
    anchor = review.seed_file
    if anchor is None:
        raise ExecutionError(f"MatchReview {review.id} (torrent_to_client) has no linked seed_file")
    _require_known_structure(candidate)
    _require_every_video(candidate)
    _require_download_link(candidate)
    disk = anchor.disk
    if candidate.piece_length is None:
        fetch_torrent = _structure_from_torrent(session, candidate, fetch_torrent)

    root_rel = _torrent_root_rel(candidate, anchor)
    for f in candidate.files:
        if f.seed_file is None:
            continue
        expected = f"{root_rel}/{_torrent_rel_path(candidate, f.torrent_path)}" if root_rel else \
            _torrent_rel_path(candidate, f.torrent_path)
        if f.seed_file.disk_id != disk.id or f.seed_file.relative_path != expected:
            raise ExecutionError(f"'{f.seed_file.relative_path}' is not where the torrent expects it ({expected})")
        _scoped_file(disk, f.seed_file.relative_path)
    save_path_local = _in_scope(disk.root_path, root_rel) if root_rel else disk.root_path

    seed_job = _new_seed_job(session, candidate, torrent_client_id, source_seed_file_id=anchor.id)
    return _seed(session, seed_job, review, adapter, torrent_client_id, disk, save_path_local, skip_recheck,
                 fetch_torrent)


def _torrent_root_rel(candidate, anchor) -> str:
    """La cartella (relativa al disco) che contiene il torrent di un
    torrent -> client, ricavata dal file anchor e dal suo percorso nel torrent."""
    anchor_row = next((f for f in candidate.files if f.seed_file_id == anchor.id), None)
    if anchor_row is None:
        raise ExecutionError(f"Seed file {anchor.id} is not part of candidate {candidate.id}")
    suffix = _torrent_rel_path(candidate, anchor_row.torrent_path)
    if not anchor.relative_path.endswith(suffix):
        raise ExecutionError(f"'{anchor.relative_path}' does not end with the torrent path '{suffix}'")
    return anchor.relative_path[: -len(suffix)].rstrip("/")


def _single_file_name(candidate, media_file) -> str:
    """Il nome del file di un candidato a file singolo senza righe
    candidate_file (salvato prima): dalla lista del catalogo, o il file in libreria."""
    file_list = json.loads(candidate.file_list_json) if candidate.file_list_json else []
    return file_list[0] if file_list else os.path.basename(media_file.relative_path)


def _execute_media_to_torrent(
    session: Session, review: MatchReview, adapter: TorrentClientAdapter, torrent_client_id: int | None = None,
    skip_recheck: bool = False, fetch_torrent: FetchTorrent | None = None,
) -> SeedJob:
    """Candidato a file singolo salvato prima delle righe candidate_file."""
    candidate = review.candidate
    media_file = review.media_file
    if media_file is None:
        raise ExecutionError(f"MatchReview {review.id} (media_to_torrent) has no linked media_file")
    disk = media_file.disk
    target_root = _new_torrents_root(disk)
    source_path = _source_path(disk, media_file.relative_path)

    expected_filename = _single_file_name(candidate, media_file)
    # Un torrent a file singolo può comunque avere una cartella contenitore
    # (candidate.folder) — molte release la usano anche per i film.
    placement = _placement(adapter, candidate, expected_filename)
    # None: già lì, stesso inode (un cross-seed).
    target_path = _check_link(source_path, os.path.join(target_root, placement.rel_path(expected_filename)),
                              target_root)
    save_root = _in_scope(target_root, placement.save_dir) if placement.save_dir else target_root
    _require_download_link(candidate)

    seed_job = _new_seed_job(session, candidate, torrent_client_id, source_media_file_id=media_file.id)
    return _seed(session, seed_job, review, adapter, torrent_client_id, disk, save_root, skip_recheck,
                 fetch_torrent, links=[] if target_path is None else [(source_path, target_path)],
                 layout=placement.layout)


def _execute_torrent_to_client(
    session: Session, review: MatchReview, adapter: TorrentClientAdapter, torrent_client_id: int | None = None,
    skip_recheck: bool = False, fetch_torrent: FetchTorrent | None = None,
) -> SeedJob:
    """Il file è già presente sul filesystem (docs/SPEC.md sezione 3): mai
    un nuovo hardlink, solo l'aggiunta al client puntando alla cartella che
    già lo contiene. Candidato a file singolo salvato prima delle righe
    candidate_file."""
    candidate = review.candidate
    seed_file = review.seed_file
    if seed_file is None:
        raise ExecutionError(f"MatchReview {review.id} (torrent_to_client) has no linked seed_file")
    disk = seed_file.disk
    _require_download_link(candidate)
    _scoped_file(disk, seed_file.relative_path)
    # La cartella com'è scritta (non quella risolta dai link simbolici): è
    # quella che il client vede, tradotta da client_visible_path.
    save_path_local = os.path.dirname(os.path.join(disk.root_path, seed_file.relative_path))

    seed_job = _new_seed_job(session, candidate, torrent_client_id, source_seed_file_id=seed_file.id)
    return _seed(session, seed_job, review, adapter, torrent_client_id, disk, save_path_local, skip_recheck,
                 fetch_torrent)


def reconcile_seed_job(session: Session, seed_job: SeedJob, adapter: TorrentClientAdapter) -> SeedJob:
    """Aggiorna recheck_status/final_status interrogando lo stato reale nel
    client. Il recheck è asincrono lato client: va richiamata finché non
    raggiunge uno stato definitivo (mai un'attesa bloccante qui dentro).

    Un reseed entra fermo (_add_to_client): qui parte, con start(), solo a
    recheck riuscito (al 100%, o con i soli extra che si sapeva mancanti);
    con il recheck fallito resta fermo, e il seed job fallito lo dice."""
    if seed_job.info_hash is None:
        raise ExecutionError(f"SeedJob {seed_job.id} does not have an info_hash yet")
    return _reconcile_with(session, seed_job, adapter, adapter.get_torrent_status(seed_job.info_hash))


def recheck_outcome(status: TorrentStatus, allowed: int | None) -> str:
    """L'esito di un recheck: quello del client, salvo un recheck finito a
    cui mancano solo i file extra che si sapeva di non avere (nfo,
    sottotitoli, sample: allowed byte, expected_missing_bytes), che è "ok":
    il client li scarica dopo l'avvio. Quell'"ok" lo conferma poi Nazgarr,
    piece per piece, per ogni reseed: della coda (_recheck_outcome) e, dal
    2026-10-21, del flusso degli upload (nazgarr/upload/extras_check.py)."""
    allowed = allowed or 0
    if (
        status.recheck_status == "failed" and status.incomplete and allowed > 0
        and status.amount_left is not None and status.amount_left <= allowed
    ):
        return "ok"
    return status.recheck_status


def _recheck_outcome(session: Session, seed_job: SeedJob, status: TorrentStatus,
                     adapter: TorrentClientAdapter) -> tuple[str, str | None]:
    """L'esito del recheck di un reseed della coda, e il perché quando lo
    decide Nazgarr. Un recheck fallito a cui mancano byte entro il margine
    degli extra mancanti (recheck_outcome) è riuscito solo se ogni piece
    che non tocca quegli extra coincide (decisione dell'utente, 2026-10-21):
    il client dice solo quanti byte mancano, e un piece diverso del video
    in quel margine passava per un extra, e partiva a riscriverlo dentro
    gli hardlink. Lo decide _extras_gate. Anche un 100% del client mentre
    quella verifica non è passata: un torrent che girava (un plugin senza
    can_add_stopped, o avviato a mano) può aver scaricato proprio quei byte,
    e il 100% non dice niente dei piece che aveva prima."""
    outcome = recheck_outcome(status, seed_job.expected_missing_bytes)
    if outcome != "ok" or (status.recheck_status != "failed" and not _extras_check_open(seed_job.id)):
        return outcome, None
    try:
        outcome, detail = _extras_gate(session, seed_job, adapter)
    except Exception:
        # Mai un avvio senza la verifica: si riprova al reconcile dopo.
        logger.warning("Seed_job %s: piece fuori dagli extra mancanti non controllati", seed_job.id, exc_info=True)
        return "pending", EXTRAS_NOT_CHECKED
    if outcome == "ok":
        logger.info("Seed_job %s: recheck ok, il client scarica %d byte di file extra mancanti",
                    seed_job.id, status.amount_left or 0)
    return outcome, detail


@dataclass
class _ExtrasGate:
    """La verifica di Nazgarr per la tolleranza degli extra mancanti di un
    seed job: l'ultima avviata (nazgarr/reseed/full_check.py, tenuta qui
    anche quando la lista dei controlli la dimentica) e quante se ne sono
    avviate dall'ultimo retry."""
    state: full_check.CheckState | None = None
    started: int = 0


# Per seed job, in memoria come i controlli: dopo un riavvio se ne fa una nuova.
_extras_checks: dict[int, _ExtrasGate] = {}
# Le verifiche per seed job, fra errori (il .torrent non si scarica) e file
# cambiati dopo una verifica passata: ognuna legge l'intero pack, e prima
# una che non poteva tornare "uguale" ripartiva a ogni reconcile, per sempre.
MAX_EXTRAS_CHECKS = 3
EXTRAS_CHECKING = ("The client's recheck left only the extras known to be missing: Nazgarr is checking every other "
                   "piece itself before starting it")
EXTRAS_NOT_CHECKED = ("The client's recheck left only the extras known to be missing, but Nazgarr could not check the "
                      "other pieces yet: it tries again at the next check, and starts nothing before")

# Il .torrent aggiunto al client, per i seed job che possono arrivare alla
# tolleranza (expected_missing_bytes): la verifica legge quei byte, già
# scaricati e controllati da _torrent_to_add, invece di riscaricarli dal
# tracker, che irraggiungibile o col rate limit faceva fallire un reseed
# che sarebbe partito. Gli ultimi MAX_KEPT_TORRENTS; dopo un riavvio, o
# per uno più vecchio, si riscarica.
_added_torrents: dict[int, bytes] = {}
MAX_KEPT_TORRENTS = 50
_added_lock = threading.Lock()


def _keep_added_torrent(seed_job: SeedJob, content: bytes) -> None:
    if not seed_job.expected_missing_bytes:
        return
    with _added_lock:
        _added_torrents.pop(seed_job.id, None)
        _added_torrents[seed_job.id] = content
        while len(_added_torrents) > MAX_KEPT_TORRENTS:
            _added_torrents.pop(next(iter(_added_torrents)))


def _forget_seed_job(seed_job_id: int) -> None:
    """Verifica e .torrent di un seed job appena creato: SQLite può ridargli
    l'id di uno eliminato, e quelli erano dell'altro."""
    _extras_checks.pop(seed_job_id, None)
    seen_in_client(seed_job_id)
    with _added_lock:
        _added_torrents.pop(seed_job_id, None)


def _extras_check_open(seed_job_id: int) -> bool:
    """Una verifica degli extra per questo seed job è partita e non è
    (ancora) passata."""
    gate = _extras_checks.get(seed_job_id)
    if gate is None:
        return False
    state = gate.state
    return state is None or not (state.status == "done" and state.verdict == "passed")


def _missing_extras(candidate: Candidate) -> set[str]:
    """Gli extra del torrent senza file locale, quelli di expected_missing_bytes
    (_missing_bytes_for): i loro percorsi nel torrent."""
    return {f.torrent_path for f in candidate.files
            if not f.is_video and f.media_file_id is None and f.seed_file_id is None}


def _fetch_for_check(seed_job: SeedJob) -> Callable[[Candidate], bytes]:
    """Il .torrent per la verifica: quello aggiunto al client se c'è ancora
    (e ha l'info hash del seed job), se no quello del tracker."""
    info_hash = (seed_job.info_hash or "").lower()
    seed_job_id = seed_job.id

    def fetch(candidate: Candidate) -> bytes:
        with _added_lock:
            content = _added_torrents.get(seed_job_id)
        if content is not None and compute_info_hash(content) == info_hash:
            return content
        return _download_torrent(candidate)

    return fetch


def _extras_gate(session: Session, seed_job: SeedJob, adapter: TorrentClientAdapter) -> tuple[str, str | None]:
    """"ok" se un controllo completo passato ha già letto proprio questi
    file (nessuna rilettura), o se la verifica di Nazgarr dei piece fuori
    dagli extra mancanti è passata e i file sono ancora quelli che ha letto;
    "pending" mentre gira (in background, nella coda dei controlli completi:
    legge l'intero video) o dopo un suo errore (si riprova al reconcile
    dopo); "failed" col perché se un piece non coincide, se è stata
    annullata, o dopo MAX_EXTRAS_CHECKS verifiche senza un esito che valga.
    L'esito resta finché il seed job non si ritenta (adopt_for_retry lo
    dimentica)."""
    candidate = seed_job.candidate
    missing = _missing_extras(candidate)
    suffix = partial_files.suffix_of(adapter)  # Transmission rinomina i file incompleti anche fermo
    if full_check.verified_for(session, candidate, seed_job, missing, suffix):
        return "ok", None
    gate = _extras_checks.setdefault(seed_job.id, _ExtrasGate())
    state = gate.state
    if state is not None and state.status == "done" and state.verdict == "passed":
        if full_check.unchanged_since(session, candidate, seed_job, missing, state.recorded, suffix):
            return "ok", None
        if gate.started >= MAX_EXTRAS_CHECKS:
            return "failed", ("the client only lacks the extras known to be missing, but the files changed after "
                              f"Nazgarr checked their pieces, {gate.started} times: Retry checks them again")
        state = None  # cambiati dopo: una nuova, sui file di adesso
    if state is None:
        # Mai fatta, dimenticata (un riavvio, un retry), andata in errore al
        # giro prima, o passata su file che da allora sono cambiati: una nuova.
        factory = sessionmaker(bind=session.get_bind(), autoflush=False, autocommit=False, future=True)
        state = full_check.start_extras_check(factory, seed_job, missing, _fetch_for_check(seed_job), suffix)
        gate.state, gate.started = state, gate.started + 1
        logger.info("Seed_job %s: recheck entro il margine degli extra, Nazgarr controlla gli altri piece (%d)",
                    seed_job.id, gate.started)
    if state.status in ("queued", "running"):
        return "pending", EXTRAS_CHECKING
    if state.status == "done":
        if state.verdict == "passed":
            return "ok", None
        return "failed", ("the client only lacks the extras known to be missing, but Nazgarr's own check of the "
                          f"other pieces failed: {state.verdict_reason}")
    if state.status == "failed":
        # Un errore, non un esito (il .torrent non si scarica, il disco non
        # risponde): una nuova al reconcile dopo, che fa anche da attesa.
        if gate.started >= MAX_EXTRAS_CHECKS:
            return "failed", ("the client only lacks the extras known to be missing, but Nazgarr could not check "
                              f"the other pieces, {gate.started} times ({state.error}): Retry checks them again")
        gate.state = None
        return "pending", f"{EXTRAS_NOT_CHECKED} (last error: {state.error})"
    return "failed", ("the client only lacks the extras known to be missing, and Nazgarr's check of the other "
                      "pieces was cancelled: Retry checks them again")


def _stop_while_checking(adapter: TorrentClientAdapter, seed_job: SeedJob, state: str | None) -> str:
    """Un reseed che gira mentre Nazgarr verifica i suoi piece (_extras_gate:
    un plugin senza can_add_stopped, o un torrent avviato a mano) si ferma,
    come dopo un recheck fallito (_stop_after_failed_recheck, un'azione di
    protezione che l'utente ha accettato): intanto scaricherebbe i byte
    "mancanti", e con loro un piece diverso del video, dentro gli hardlink.
    Fermo, _start_after_recheck lo avvia quando la verifica passa. Ritorna
    la coda del messaggio del seed job ("" se era già fermo)."""
    if is_stopped_state(state):
        return ""
    result, error = stop_torrent(adapter, seed_job.info_hash)
    if result == "stopped":
        logger.info("Seed_job %s: torrent %s fermato mentre Nazgarr controlla i piece", seed_job.id,
                    seed_job.info_hash)
        return "; stopped in the client meanwhile, so it downloads nothing into the linked files"
    logger.warning("Seed_job %s: torrent %s non fermato durante la verifica dei piece", seed_job.id,
                   seed_job.info_hash, exc_info=error)
    if result == "unsupported":
        return "; this client cannot be told to stop it: stop it by hand, or it downloads into the linked files"
    return "; it could not be stopped in the client: stop it by hand, or it downloads into the linked files"


def _reconcile_with(session: Session, seed_job: SeedJob, adapter: TorrentClientAdapter,
                    status: TorrentStatus) -> SeedJob:
    recheck_status, detail = _recheck_outcome(session, seed_job, status, adapter)
    seed_job.recheck_status = recheck_status
    if recheck_status == "pending" and detail:  # la verifica di Nazgarr (_extras_gate)
        seed_job.error_message = detail + _stop_while_checking(adapter, seed_job, status.state)
    if recheck_status == "ok":
        seed_job.error_message = None  # un retry riuscito: il fallimento di prima non vale più
        if is_stopped_state(status.state) and not _start_after_recheck(adapter, seed_job):
            session.commit()
            return seed_job  # resta in corso: il prossimo reconcile riprova ad avviarlo
        seed_job.final_status = "seeding"
        with _added_lock:
            _added_torrents.pop(seed_job.id, None)
        logger.info("Recheck ok, seed_job %s in seeding (info_hash=%s)", seed_job.id, seed_job.info_hash)
    elif recheck_status == "failed":
        seed_job.final_status = "failed"
        why = f"client state: {status.state}" if detail is None else f"{detail} (client state: {status.state})"
        seed_job.error_message = f"Recheck failed, {why}" + _stop_after_failed_recheck(
            adapter, seed_job, status.state) + _restore_partial_names(session, seed_job, adapter)
        logger.warning("Recheck fallito per seed_job %s: stato client %s", seed_job.id, status.state)
    session.commit()
    return seed_job


STARTED_LATE = ("The recheck passed, but the client did not start the torrent: Nazgarr tries again at the "
                "next check, or start it in the client")
CANNOT_START = "The recheck passed, but this client cannot be told to start it: start it in the client"


def start_torrent(adapter: TorrentClientAdapter, info_hash: str) -> tuple[str, Exception | None]:
    """Avvia un torrent: ("started", None), ("unsupported", None) se
    l'adapter non sa farlo (un plugin senza start(), o che solleva
    NotImplementedError), ("failed", errore) se il client ha rifiutato."""
    start = getattr(adapter, "start", None)
    try:
        if not callable(start):
            raise NotImplementedError
        start(info_hash)
    except NotImplementedError:
        return "unsupported", None
    except Exception as exc:
        return "failed", exc
    return "started", None


def stop_torrent(adapter: TorrentClientAdapter, info_hash: str) -> tuple[str, Exception | None]:
    """Ferma un torrent: ("stopped", None), ("unsupported", None) o ("failed", errore)."""
    stop = getattr(adapter, "stop", None)
    try:
        if not callable(stop):
            raise NotImplementedError
        stop(info_hash)
    except NotImplementedError:
        return "unsupported", None
    except Exception as exc:
        return "failed", exc
    return "stopped", None


# Quanto si aspetta un torrent che il client dice di non avere prima di
# darlo per sparito: un client appena riavviato può non averlo ancora
# ricaricato. Per i reseed del motore (review.reconcile_pending_seed_jobs)
# e per quelli del flusso degli upload (nazgarr/upload/execute.py).
MISSING_TORRENT_GRACE = timedelta(minutes=10)

# Da quando questo processo vede il torrent di un seed job mancare dal
# client, per seed job: i 10 minuti vanno contati di fila, come nel flusso
# degli upload (audit 2026-10-18, revisione). Prima contavano da
# torrent_added_at, e un reseed in corso da più di 10 minuti (un recheck in
# coda dopo "Approva tutti", un pack grande, un retry col suo vecchio
# torrent_added_at) falliva alla prima lettura mentre il client riavviato
# ricaricava i suoi torrent. Un riavvio di Nazgarr ricomincia il conto.
_missing_since: dict[int, datetime] = {}


def missing_long_enough(seed_job: SeedJob) -> bool:
    """Il torrent del seed job manca dal client (appena letto) da almeno
    MISSING_TORRENT_GRACE di fila, contati dalla prima lettura che non l'ha
    trovato e mai da prima della sua aggiunta."""
    now = datetime.now(UTC)
    since = _missing_since.setdefault(seed_job.id, now)
    if seed_job.torrent_added_at is not None:
        since = max(since, as_utc(seed_job.torrent_added_at))
    return now - since >= MISSING_TORRENT_GRACE


def seen_in_client(seed_job_id: int) -> None:
    """Il client ha risposto per il torrent del seed job (o il seed job è
    ripartito, finito, eliminato): il conto dei minuti di assenza ricomincia."""
    _missing_since.pop(seed_job_id, None)


def can_look_up_torrent(adapter: TorrentClientAdapter) -> bool:
    """L'adapter sa chiedere al client un torrent solo: il None di default di
    TorrentClientAdapter.get_torrent_info non chiede niente al client (un
    plugin), e lì None non vuol dire "non c'è"."""
    method = getattr(type(adapter), "get_torrent_info", None)
    return method is not None and method is not TorrentClientAdapter.get_torrent_info


def lookup_torrent(adapter: TorrentClientAdapter, info_hash: str) -> str:
    """Cosa dice il client di un torrent solo: "present"; "missing" se
    risponde e non ce l'ha (get_torrent_info None, il contratto degli
    adapter); "unknown" se l'adapter non sa cercarlo (can_look_up_torrent);
    "unreachable" se la domanda è fallita (il client non risponde)."""
    if not can_look_up_torrent(adapter):
        return "unknown"
    try:
        return "missing" if adapter.get_torrent_info(info_hash) is None else "present"
    except Exception:
        return "unreachable"


def torrent_missing(adapter: TorrentClientAdapter, info_hash: str) -> bool:
    """Il client risponde, e quel torrent non ce l'ha (lookup_torrent). Un
    client che non risponde non è "mancante", e nemmeno uno il cui adapter
    non sa cercare un torrent solo (can_look_up_torrent): un client
    irraggiungibile diventava "il torrent non c'è più", con l'esecuzione
    fallita e la notifica."""
    return lookup_torrent(adapter, info_hash) == "missing"


def _start_after_recheck(adapter: TorrentClientAdapter, seed_job: SeedJob) -> bool:
    """Avvia un torrent fermo dopo un recheck riuscito: un reseed, che entra
    sempre fermo (decisione dell'utente, 2026-10-18), o la condizione di
    arresto "file controllati" di un client. False se il client non l'ha
    avviato: il seed job resta in corso, col perché, e il reconcile dopo
    riprova (prima diventava "seeding" con il torrent fermo per sempre).
    Un adapter che start() non ce l'ha (un plugin senza la classe base) o
    non lo implementa non lo farà mai: "seeding", col messaggio di avviarlo
    nel client, invece di riprovare per sempre a ogni reconcile."""
    result, error = start_torrent(adapter, seed_job.info_hash)
    if result == "unsupported":
        logger.warning("Seed_job %s: recheck riuscito, il client non sa avviare %s", seed_job.id,
                       seed_job.info_hash)
        seed_job.error_message = CANNOT_START
        return True
    if result == "failed":
        logger.warning("Seed_job %s: recheck riuscito, avvio fallito", seed_job.id, exc_info=error)
        seed_job.error_message = f"{STARTED_LATE} ({safe_error(error)})"
        return False
    logger.info("Seed_job %s: recheck riuscito, torrent avviato", seed_job.id)
    return True


def _stop_after_failed_recheck(adapter: TorrentClientAdapter, seed_job: SeedJob, state: str | None) -> str:
    """Un reseed col recheck fallito resta fermo nel client: entra fermo
    (decisione dell'utente, 2026-10-18), e uno che non lo è (aggiunto da un
    plugin senza can_add_stopped, o avviato a mano durante il recheck) si
    ferma (audit 2026-10-18). Avviato, scaricherebbe i piece che mancano
    scrivendoli nei file a save_path, che sono hardlink dei file in libreria
    (gli stessi byte), o i file già nella cartella dei torrent. Niente di
    approvato dall'utente. Ritorna la coda del messaggio del seed job. Un
    retry lo fa ricontrollare (adopt_for_retry), e il reconcile dopo lo
    avvia se il recheck riesce (_start_after_recheck)."""
    if is_stopped_state(state):
        return ("; left stopped in the client, so it downloads nothing into the linked files: recheck it in the "
                "client, or Retry once the files are right")
    result, error = stop_torrent(adapter, seed_job.info_hash)
    if result == "unsupported":
        logger.warning("Seed_job %s: il client non sa fermare il torrent %s", seed_job.id, seed_job.info_hash)
        return "; this client cannot be told to stop it: stop it by hand, or it downloads into the linked files"
    if result == "failed":
        logger.warning("Seed_job %s: torrent %s non fermato", seed_job.id, seed_job.info_hash, exc_info=error)
        return "; it could not be stopped in the client: stop it by hand, or it downloads into the linked files"
    logger.info("Seed_job %s: torrent %s fermato dopo il recheck fallito", seed_job.id, seed_job.info_hash)
    return "; stopped in the client so it does not download into the linked files"


def _restore_partial_names(session: Session, seed_job: SeedJob, adapter: TorrentClientAdapter) -> str:
    """Dopo un recheck fallito, il nome ai file che il client ha rinominato
    perché incompleti (Transmission, rename-partial-files: "<nome>.part";
    decisione dell'utente, 2026-10-19). Solo i file del torrent che
    Nazgarr segue: stesso dispositivo e inode di un file in libreria o
    nella cartella di seeding abbinato al candidato (gli hardlink creati da
    qui hanno quello del file in libreria), dentro le cartelle di seeding
    del disco (nazgarr/torrents/partial_files.py). Dove il client li cerca
    lo dice il client stesso (save path e file del torrent), tradotto nei
    percorsi di Nazgarr: la disposizione scelta all'esecuzione non si
    ricalcola. Ritorna la coda del messaggio del seed job ("" se niente):
    un errore qui si scrive nel log e non cambia l'esito del recheck."""
    suffix = partial_files.suffix_of(adapter)
    if suffix is None:
        return ""
    try:
        restored = _restore_partial_names_of(session, seed_job, adapter, suffix)
    except Exception:
        logger.warning("Seed_job %s: file rinominati dal client non controllati", seed_job.id, exc_info=True)
        return ""
    if not restored:
        return ""
    logger.warning("Seed_job %s: %d file rinominati %s dal client riportati al loro nome", seed_job.id,
                   len(restored), suffix)
    return f"; {len(restored)} file(s) the client had renamed to *{suffix} got their name back"


def _restore_partial_names_of(session: Session, seed_job: SeedJob, adapter: TorrentClientAdapter,
                              suffix: str) -> list[str]:
    review = review_of(session, seed_job)
    info = adapter.get_torrent_info(seed_job.info_hash)
    anchor = review.media_file or review.seed_file
    if info is None or anchor is None or anchor.disk is None:
        return []
    disk = anchor.disk
    tracked = [review.media_file, review.seed_file]
    tracked += [f for row in review.candidate.files for f in (row.media_file, row.seed_file)]
    identities = {(f.st_dev, f.inode) for f in tracked if f is not None and f.disk_id == disk.id}
    mapping = _mapping(session, disk, seed_job.torrent_client_id) or client_paths.Mapping(disk.root_path)
    base = os.path.realpath(disk.root_path)
    paths = []
    for file in info.files:
        name = file.path_in_torrent[:-len(suffix)] if file.path_in_torrent.endswith(suffix) else file.path_in_torrent
        client_path = os.path.join(info.save_path, name)
        relative = client_paths.to_disk_relative(mapping, client_path)
        if relative is None and not mapping.mapped:  # il client ha il percorso reale, il disco uno con link
            relative = client_paths.to_disk_relative(client_paths.Mapping(base), client_path)
        if relative is not None:
            paths.append(os.path.join(base, relative))
    return partial_files.restore_names(paths, identities, suffix, disk_folders.absolute(disk, "seeding"))


def adopt_for_retry(session: Session, seed_job: SeedJob, adapter: TorrentClientAdapter) -> SeedJob | None:
    """La parte di un retry che segue un torrent già nel client, senza
    creare niente: con un info_hash si reinterroga il client; senza, se il
    client ha comunque il torrent del candidato (l'aggiunta fallita può
    essere arrivata lo stesso, es. dopo un timeout), lo si segue. None = va
    rifatto da zero, anche quando il torrent che il seed job seguiva non è
    più nel client (audit 2026-10-18: lì il retry finiva in un errore 500
    e restava solo l'eliminazione): il retry lo riaggiunge.

    Un torrent che questa esecuzione non ha mandato al client (l'aggiunta è
    fallita con TorrentAlreadyInClientError, cioè lo aveva già messo un
    altro programma o l'utente, o l'esecuzione è fallita prima di mandarlo)
    non si segue mai (RetryRefused): seguirlo voleva dire ricontrollarlo,
    fermarlo o avviarlo, senza che nessuno l'avesse approvato (revisione
    dell'audit 2026-10-18). Nemmeno uno tornato nel client dopo essere
    sparito (GONE_FROM_CLIENT) in una cartella dove questa review non
    l'aveva mandato (_back_where_it_was).

    Il torrent fermo col recheck fallito (un reseed entra fermo e ci resta,
    decisione dell'utente del 2026-10-18), e quello appena trovato nel
    client dopo un'aggiunta fallita, si fanno ricontrollare: un torrent
    fermo il client non lo ricontrolla da solo, e rileggerlo diceva di
    nuovo "failed" anche coi file ora giusti, o mai controllati. Il retry è
    l'approvazione dell'utente, ed è un controllo, mai un avvio: il seed job
    torna in corso e il reconcile dopo lo avvia solo se riesce."""
    seen_in_client(seed_job.id)  # i minuti di assenza contano da adesso
    _extras_checks.pop(seed_job.id, None)  # un retry è un controllo nuovo, anche di Nazgarr
    if not seed_job.info_hash:
        return _adopt_candidate_torrent(session, seed_job, adapter)
    logger.info("Retry seed_job %s: info_hash già presente, reinterrogo il client", seed_job.id)
    status = _status_for_retry(adapter, seed_job.info_hash)
    if status is None:
        logger.info("Retry seed_job %s: il torrent %s non è più nel client, riparto da zero", seed_job.id,
                    seed_job.info_hash)
        return None
    if seed_job.error_message == GONE_FROM_CLIENT and not _back_where_it_was(session, seed_job, adapter):
        return None
    return _retry_with(session, seed_job, adapter, status)


def _back_where_it_was(session: Session, seed_job: SeedJob, adapter: TorrentClientAdapter) -> bool:
    """Il torrent del seed job era sparito dal client (GONE_FROM_CLIENT) e
    ora c'è di nuovo: lo ha riaggiunto l'utente, o un altro programma
    (cross-seed, autobrr) nella sua cartella. Si segue (True) solo se è in
    uno dei posti in cui questa review l'aveva mandato (_client_save_paths),
    come fa il flusso degli upload (_keep_existing); altrove, o se non si
    sa dove, è RetryRefused: seguirlo voleva dire ricontrollarlo, fermarlo
    o avviarlo senza che nessuno l'avesse approvato (revisione dell'audit
    2026-10-18). False se il client ora dice di non averlo: si riaggiunge."""
    try:
        info = adapter.get_torrent_info(seed_job.info_hash) if can_look_up_torrent(adapter) else None
    except Exception as exc:
        raise ExecutionError(f"Could not read torrent {seed_job.info_hash} from the client ({safe_error(exc)}): "
                             "try again shortly, or delete this execution") from exc
    if info is None and can_look_up_torrent(adapter):
        return False
    seen = _normpath(info.save_path) if info is not None else None
    if seen is None or seen not in _client_save_paths(session, seed_job):
        logger.warning("Retry seed_job %s: il torrent %s è tornato nel client in %s, non dove l'aveva mandato: "
                       "non lo seguo", seed_job.id, seed_job.info_hash, seen)
        raise RetryRefused("seed_job_torrent_not_ours", id=seed_job.id, info_hash=seed_job.info_hash)
    return True


def _client_save_paths(session: Session, seed_job: SeedJob) -> set[str]:
    """Ogni save path (come lo vede il client, normalizzato) in cui
    un'esecuzione di questa review può aver mandato il torrent, con lo
    stesso calcolo dell'esecuzione: per media -> torrent la cartella dei
    nuovi hardlink, con ogni disposizione possibile (il layout del client di
    quel momento non si salva, placement.possible_placements); per torrent
    -> client la cartella che contiene già i file. Un problema (la review
    o la cartella che non ci sono più) è un ExecutionError."""
    try:
        review = review_of(session, seed_job)
    except NoResultFound as exc:
        raise ExecutionError(f"The review of SeedJob {seed_job.id} no longer exists: delete this execution") from exc
    candidate = review.candidate
    if candidate.direction == "media_to_torrent":
        anchor = review.media_file
        if anchor is None:
            raise ExecutionError(f"MatchReview {review.id} (media_to_torrent) has no linked media_file")
        disk = anchor.disk
        target_root = _new_torrents_root(disk)
        if candidate.files:
            single = candidate.files[0].torrent_path if len(candidate.files) == 1 else None
        else:
            single = _single_file_name(candidate, anchor)
        roots = [_in_scope(target_root, placement.save_dir) if placement.save_dir else target_root
                 for placement in possible_placements(candidate.folder, single)]
    else:
        anchor = review.seed_file
        if anchor is None:
            raise ExecutionError(f"MatchReview {review.id} (torrent_to_client) has no linked seed_file")
        disk = anchor.disk
        if candidate.files:
            root_rel = _torrent_root_rel(candidate, anchor)
            roots = [_in_scope(disk.root_path, root_rel) if root_rel else disk.root_path]
        else:
            roots = [os.path.dirname(os.path.join(disk.root_path, anchor.relative_path))]
    try:
        return {os.path.normpath(client_visible_path(session, disk, seed_job.torrent_client_id, root))
                for root in roots}
    except client_paths.ClientPathError as exc:
        raise ExecutionError(_error_text(exc)) from exc


def _retry_with(session: Session, seed_job: SeedJob, adapter: TorrentClientAdapter, status: TorrentStatus,
                rechecked: bool = False) -> SeedJob:
    outcome, _detail = _recheck_outcome(session, seed_job, status, adapter)
    if (not rechecked and outcome == "failed" and is_stopped_state(status.state)
            and _recheck_for_retry(adapter, seed_job)):
        return _waiting_for_recheck(session, seed_job)
    seed_job.final_status = "in_progress"
    session.commit()
    return _reconcile_with(session, seed_job, adapter, status)


def _waiting_for_recheck(session: Session, seed_job: SeedJob) -> SeedJob:
    seed_job.final_status, seed_job.recheck_status, seed_job.error_message = "in_progress", "pending", None
    session.commit()
    return seed_job


def _adopt_candidate_torrent(session: Session, seed_job: SeedJob, adapter: TorrentClientAdapter) -> SeedJob | None:
    """Un seed job senza info_hash: se il client ha il torrent del candidato,
    arrivato dopo l'aggiunta fallita, lo si segue e si fa ricontrollare
    (revisione dell'audit 2026-10-18: un reseed entra fermo, e il recheck
    forzato parte solo dopo che l'attesa del suo hash è riuscita, così
    rileggerlo diceva "Recheck failed" di un recheck mai fatto, e serviva
    un secondo retry). None se il client non ce l'ha: va rifatto da zero."""
    candidate = session.get(Candidate, seed_job.candidate_id)
    if candidate is None or not candidate.info_hash:
        return None
    try:
        info = adapter.get_torrent_info(candidate.info_hash)
    except Exception:
        logger.warning("Retry seed_job %s: impossibile chiedere al client se ha %s", seed_job.id,
                       candidate.info_hash, exc_info=True)
        info = None
    if info is None:
        return None
    if _not_sent_by(seed_job):
        logger.warning("Retry seed_job %s: il torrent %s nel client non l'ha mandato questa esecuzione, non lo seguo",
                       seed_job.id, info.info_hash)
        raise RetryRefused("seed_job_torrent_not_ours", id=seed_job.id, info_hash=info.info_hash)
    status = _status_for_retry(adapter, info.info_hash)
    if status is None:
        return None
    logger.info("Retry seed_job %s: il client ha già il torrent %s, lo seguo", seed_job.id, info.info_hash)
    asked = not seed_job.recheck_skipped and _recheck_for_retry(adapter, seed_job, info.info_hash)
    seed_job.info_hash = info.info_hash
    seed_job.torrent_added_at = seed_job.torrent_added_at or datetime.now(UTC)
    if asked:
        return _waiting_for_recheck(session, seed_job)
    # Il client non sa ricontrollarlo (un plugin senza recheck): si legge e basta.
    return _retry_with(session, seed_job, adapter, status, rechecked=True)


def _not_sent_by(seed_job: SeedJob) -> bool:
    """L'esecuzione fallita non ha mandato lei il torrent al client: il
    client lo aveva già (ALREADY_IN_CLIENT, o il testo degli adapter inclusi
    sui seed job falliti prima), o è fallita prima di mandarlo (NOTHING_SENT).
    Un'esecuzione interrotta da un riavvio può averlo mandato: si segue."""
    message = seed_job.error_message or ""
    return (message.startswith(ALREADY_IN_CLIENT) or _ADAPTER_DUPLICATE_TEXT in message
            or message.endswith(NOTHING_SENT))


def _status_for_retry(adapter: TorrentClientAdapter, info_hash: str) -> TorrentStatus | None:
    """Lo stato del torrent nel client; None se il client risponde e non ce
    l'ha (torrent_missing). Un client che non risponde è un retry fallito,
    col perché: prima l'errore dell'adapter usciva com'era, un 500."""
    try:
        return adapter.get_torrent_status(info_hash)
    except Exception as exc:
        if torrent_missing(adapter, info_hash):
            return None
        raise ExecutionError(f"Could not read torrent {info_hash} from the client ({safe_error(exc)}): try again "
                             "shortly, or delete this execution") from exc


def _recheck_for_retry(adapter: TorrentClientAdapter, seed_job: SeedJob, info_hash: str | None = None) -> bool:
    """Il recheck chiesto da un retry, del torrent del seed job (o di
    info_hash, quello che sta per seguire). False se l'adapter non lo sa
    fare (un plugin senza recheck): il retry rilegge soltanto, come prima.
    Un errore del client è un retry fallito, e il seed job resta com'era."""
    info_hash = info_hash or seed_job.info_hash
    recheck = getattr(adapter, "recheck", None)
    try:
        if not callable(recheck):
            raise NotImplementedError
        recheck(info_hash)
    except NotImplementedError:
        logger.warning("Retry seed_job %s: il client non sa ricontrollare %s", seed_job.id, info_hash)
        return False
    except Exception as exc:
        raise ExecutionError(f"The client did not take the recheck ({safe_error(exc)}): try again, or recheck "
                             "it in the client") from exc
    logger.info("Retry seed_job %s: recheck chiesto al client, resta fermo fino all'esito", seed_job.id)
    return True


def review_of(session: Session, seed_job: SeedJob) -> MatchReview:
    """La review che ha lanciato l'esecuzione."""
    return (
        session.query(MatchReview)
        .filter(MatchReview.candidate_id == seed_job.candidate_id)
        .filter(
            (MatchReview.media_file_id == seed_job.source_media_file_id)
            if seed_job.source_media_file_id is not None
            else (MatchReview.seed_file_id == seed_job.source_seed_file_id)
        )
        .one()
    )


def retry_from_scratch(
    session: Session, seed_job: SeedJob, adapter: TorrentClientAdapter, torrent_client_id: int | None = None,
    skip_recheck: bool = False,
) -> SeedJob:
    """Una nuova esecuzione della stessa review: gli hardlink già al loro
    posto con lo stesso inode si riusano, e il .torrent si riscarica e si
    ricontrolla contro l'info hash del candidato."""
    logger.info("Retry seed_job %s: riparto da zero (nessun info_hash mai ottenuto)", seed_job.id)
    return execute_review(session, review_of(session, seed_job), adapter, torrent_client_id, skip_recheck=skip_recheck)

