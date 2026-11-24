"""API di configurazione per i client torrent, multi-istanza (docs/SPEC.md
sezione 5) — un disco può avere più client abilitati contemporaneamente,
gestito dalla tabella ponte disk_torrent_client.
"""

import os
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session, object_session

from nazgarr.api.types import HttpUrlStr, check_destinations, insecure_public_url, require_secrets_for_new_host
from nazgarr.core.errors import coded_detail, from_coded_error
from nazgarr.core.fs_scope import ScopeViolation, resolve_scoped
from nazgarr.core.logs import safe_error
from nazgarr.core.models import ClientTorrent, Disk, DiskTorrentClient, TorrentClient
from nazgarr.integrations import adapter_factory
from nazgarr.plugins import REGISTRY
from nazgarr.plugins import config as plugin_config
from nazgarr.torrents import path_check
from nazgarr.torrents.client_labels import split_tags
from nazgarr.web.deps import get_or_404, get_session

router = APIRouter(prefix="/api/torrent-clients", tags=["torrent-clients"])



class TorrentClientCreateRequest(BaseModel):
    label: str
    adapter_type: str
    base_url: HttpUrlStr
    username: str | None = None
    password: str | None = None
    api_token: str | None = None  # adapter_type="qui": la sua X-API-Key
    qui_instance_id: int | None = None  # adapter_type="qui": quale istanza gestita da quel deployment
    config: dict | None = None  # campi di un adapter di un plugin (GET /api/plugins)


class TorrentClientUpdateRequest(BaseModel):
    label: str | None = None
    base_url: HttpUrlStr | None = None
    username: str | None = None
    password: str | None = None
    api_token: str | None = None
    qui_instance_id: int | None = None
    enabled: bool | None = None
    # Etichette dei torrent aggiunti da Nazgarr (nazgarr/torrents/client_labels.py): un
    # campo inviato vuoto o null le toglie.
    category_movie: str | None = None
    category_tv: str | None = None
    category_anime: str | None = None
    tags_upload: str | None = None
    tags_reseed: str | None = None
    # Campi di un adapter di un plugin: un segreto assente o null resta com'era.
    config: dict | None = None


LABEL_FIELDS = ("category_movie", "category_tv", "category_anime", "tags_upload", "tags_reseed")


class TorrentClientTestResponse(BaseModel):
    status: str  # "ok" | "error"
    torrents_found: int | None = None
    error: str | None = None
    # Preferenze del client che contano per Nazgarr (CLIENT_WARNINGS), lette dal vivo.
    warnings: list[str] = []


class TorrentClientWarningsResponse(BaseModel):
    status: str  # "ok" | "error" (il client non risponde: nessun avviso, e il perché)
    warnings: list[str] = []
    error: str | None = None


# Gli avvisi su una preferenza del client, per codice (i testi sono nella UI):
# - rename_partial_files: Transmission rinomina un file incompleto in
#   "<nome>.part" già durante il recheck (rename-partial-files, il suo
#   default); dopo il recheck fallito di un reseed l'hardlink restava con
#   quel nome. Nazgarr lo rimette ai file che segue, ma conviene spegnerlo
#   (decisione dell'utente, 2026-10-19).
CLIENT_WARNINGS = ("rename_partial_files",)


def _client_warnings(adapter) -> list[str]:
    """Gli avvisi del client; un errore del client esce (il chiamante dice
    "error"). Un adapter senza partial_file_suffix (un plugin senza la
    classe base) non ne ha."""
    ask = getattr(adapter, "partial_file_suffix", None)
    return ["rename_partial_files"] if callable(ask) and ask() else []


class DiskAssociationResponse(BaseModel):
    disk_id: int
    torrent_client_root_path: str | None
    local_rel_path: str | None = None  # quale cartella del disco è torrent_client_root_path (None = la radice)


class AssociateDiskRequest(BaseModel):
    # Solo se questo client vede questo disco a un path diverso da
    # disk.root_path (container/mount diverso) — vuoto/assente se vedono lo
    # stesso path. Per (disk, client): client diversi sullo stesso disco
    # possono avere ciascuno il proprio path, non è un campo del disco.
    torrent_client_root_path: str | None = None
    # Quale cartella del disco il client vede come torrent_client_root_path,
    # relativa alla radice del disco (vuota = la radice): un client montato
    # su una sottocartella, es. Nazgarr /data/qbittorrent = client /download
    # (nazgarr/torrents/client_paths.py).
    local_rel_path: str | None = None


class TorrentClientResponse(BaseModel):
    id: int
    label: str
    adapter_type: str
    base_url: str
    # http:// verso un host pubblico: credenziali in chiaro. Salvato lo stesso,
    # con un avviso (decisione dell'utente, 2026-10-18).
    insecure_public_url: bool = False
    username: str | None
    qui_instance_id: int | None  # mai api_token/password: write-only, non tornano mai indietro
    enabled: bool
    category_movie: str | None = None
    category_tv: str | None = None
    category_anime: str | None = None
    tags_upload: str | None = None
    tags_reseed: str | None = None
    # Campi di un adapter di un plugin: {"values": {...}, "secrets_set": [...]}, mai i segreti.
    config: dict = {}
    disks: list[DiskAssociationResponse]  # dischi abilitati per questo client, con l'eventuale path override
    # Dall'indice dell'ultima scan, per la scheda del client.
    torrent_count: int = 0
    last_polled_at: datetime | None = None

    @classmethod
    def from_model(cls, tc: TorrentClient, links: list[DiskTorrentClient]) -> "TorrentClientResponse":
        session = object_session(tc)
        count, last = session.query(func.count(ClientTorrent.id), func.max(ClientTorrent.last_polled_at)).filter(
            ClientTorrent.torrent_client_id == tc.id
        ).one()
        return cls(
            torrent_count=count or 0, last_polled_at=last,
            id=tc.id, label=tc.label, adapter_type=tc.adapter_type,
            base_url=tc.base_url, insecure_public_url=insecure_public_url(tc.base_url),
            username=tc.username, qui_instance_id=tc.qui_instance_id, enabled=tc.enabled,
            **{name: getattr(tc, name) for name in LABEL_FIELDS},
            config=plugin_config.public_for_row(tc, "torrent_client"),
            disks=[
                DiskAssociationResponse(disk_id=link.disk_id, torrent_client_root_path=link.torrent_client_root_path,
                                        local_rel_path=link.local_rel_path)
                for link in links
            ],
        )


def _apply_config(tc: TorrentClient, config: dict | None, *, creating: bool) -> None:
    try:
        plugin_config.apply_to_row(tc, "torrent_client", config, creating=creating)
    except plugin_config.AdapterConfigError as exc:
        raise HTTPException(status_code=400, detail=from_coded_error(exc)) from exc


def _get_torrent_client_or_404(session: Session, torrent_client_id: int) -> TorrentClient:
    return get_or_404(session, TorrentClient, torrent_client_id, "torrent_client_not_found")


def _get_disk_or_404(session: Session, disk_id: int) -> Disk:
    return get_or_404(session, Disk, disk_id, "disk_not_found")


def _links_for(session: Session, torrent_client_id: int) -> list[DiskTorrentClient]:
    return session.query(DiskTorrentClient).filter_by(torrent_client_id=torrent_client_id).all()


@router.get("", response_model=list[TorrentClientResponse])
def list_torrent_clients(session: Session = Depends(get_session)):
    return [
        TorrentClientResponse.from_model(tc, _links_for(session, tc.id))
        for tc in session.query(TorrentClient).all()
    ]


@router.post("", response_model=TorrentClientResponse, status_code=201)
def create_torrent_client(body: TorrentClientCreateRequest, session: Session = Depends(get_session)):
    check_destinations(body.base_url)
    supported = REGISTRY.types("torrent_client")  # integrati e plugin (nazgarr/plugins)
    if body.adapter_type not in supported:
        raise HTTPException(
            status_code=400,
            detail=coded_detail(
                "torrent_client_adapter_type_unsupported",
                adapter_type=body.adapter_type, supported=sorted(supported),
            ),
        )
    tc = TorrentClient(
        label=body.label, adapter_type=body.adapter_type, base_url=body.base_url,
        username=body.username, password=body.password,
        api_token=body.api_token, qui_instance_id=body.qui_instance_id,
    )
    _apply_config(tc, body.config, creating=True)
    session.add(tc)
    session.commit()
    return TorrentClientResponse.from_model(tc, [])


class QuiInstancesRequest(BaseModel):
    base_url: HttpUrlStr
    api_token: str | None = None  # assente: quello salvato del client (stesso host)
    torrent_client_id: int | None = None


class QuiInstance(BaseModel):
    id: int
    name: str
    host: str | None
    active: bool
    connected: bool


class QuiInstancesResponse(BaseModel):
    status: str  # "ok" | "error"
    instances: list[QuiInstance] = []
    error: str | None = None


@router.post("/qui-instances", response_model=QuiInstancesResponse)
def qui_instances(body: QuiInstancesRequest, session: Session = Depends(get_session)):
    """Le istanze di un qui, per sceglierne una da un elenco (sola lettura).
    Senza api_token si usa quello del client salvato, ma solo verso lo
    stesso host: mai mandare un token salvato a un indirizzo nuovo."""
    from nazgarr.adapters.torrent_client.qui import QuiTorrentClientAdapter

    check_destinations(body.base_url)
    token = (body.api_token or "").strip()
    if not token and body.torrent_client_id is not None:
        tc = _get_torrent_client_or_404(session, body.torrent_client_id)
        require_secrets_for_new_host(tc.base_url, body.base_url, ["api_token"] if tc.api_token else [])
        token = tc.api_token or ""
    if not token:
        return QuiInstancesResponse(status="error", error="api_token required")
    try:
        instances = QuiTorrentClientAdapter.list_instances(body.base_url, token)
    except Exception as exc:
        return QuiInstancesResponse(status="error", error=safe_error(exc))
    return QuiInstancesResponse(status="ok", instances=[QuiInstance(**i) for i in instances])


@router.post("/{torrent_client_id}/test", response_model=TorrentClientTestResponse)
def test_torrent_client(torrent_client_id: int, session: Session = Depends(get_session)):
    """Sola lettura: conta i torrent del client (adapter.count_torrents) e
    riporta successo/errore, senza bisogno di dischi configurati né di
    passare da uno scan completo — utile per verificare le credenziali
    subito dopo aver creato/modificato un client (docs/SPEC.md sezione 5).
    Prima chiedeva l'elenco completo coi file di ogni torrent solo per
    contarli: con migliaia di torrent il proxy davanti scadeva (audit
    2026-10-18)."""
    tc = _get_torrent_client_or_404(session, torrent_client_id)
    try:
        with adapter_factory.torrent_client(tc) as adapter:
            # Un login vero, non la sessione tenuta dalle operazioni di prima
            # (qBittorrent): la prova è delle credenziali salvate.
            forget_session = getattr(adapter, "forget_session", None)
            if callable(forget_session):
                forget_session()
            count = getattr(adapter, "count_torrents", None)  # un plugin senza la classe base
            torrents_found = count() if callable(count) else len(adapter.list_torrents())
            try:
                warnings = _client_warnings(adapter)
            except Exception:
                warnings = []  # la connessione funziona: la prova riesce, senza avvisi
    except Exception as exc:
        return TorrentClientTestResponse(status="error", error=safe_error(exc))
    return TorrentClientTestResponse(status="ok", torrents_found=torrents_found, warnings=warnings)


@router.get("/{torrent_client_id}/warnings", response_model=TorrentClientWarningsResponse)
def torrent_client_warnings(torrent_client_id: int, session: Session = Depends(get_session)):
    """Sola lettura: le preferenze del client che contano per Nazgarr
    (CLIENT_WARNINGS), chieste al client adesso, per l'avviso nella sua
    scheda. Un adapter che non le ha non fa nessuna richiesta."""
    tc = _get_torrent_client_or_404(session, torrent_client_id)
    try:
        with adapter_factory.torrent_client(tc) as adapter:
            warnings = _client_warnings(adapter)
    except Exception as exc:
        return TorrentClientWarningsResponse(status="error", error=safe_error(exc))
    return TorrentClientWarningsResponse(status="ok", warnings=warnings)


class PathCheckExample(BaseModel):
    status: str  # outside_seeding | missing | unmapped
    client_path: str
    local_path: str | None  # dove Nazgarr l'ha cercato (o trovato, fuori dalle cartelle dei torrent)


class PathCheckSuggestion(BaseModel):
    disk_id: int
    disk_label: str
    local_rel_path: str | None  # None = tutto il disco
    client_root_path: str | None  # None = stessi percorsi di Nazgarr
    matches: int


class PathCheckResponse(BaseModel):
    status: str  # "ok" | "error"
    error: str | None = None
    source: str | None = None  # "index" (ultima scansione) | "live" (chiesto ora al client)
    verdict: str | None = None  # ok | partial | none | empty
    checked: int = 0
    ok: int = 0
    outside_seeding: int = 0
    missing: int = 0
    unmapped: int = 0
    examples: list[PathCheckExample] = []
    suggestions: list[PathCheckSuggestion] = []


# Torrent guardati da una verifica chiesta al client (path_check.MAX_SAMPLES
# vale per l'indice, letto dal DB). qBittorrent non ha un endpoint per i file
# di più torrent: una richiesta per torrent, 4 alla volta, e con 5000 una
# richiesta HTTP sincrona poteva superare il timeout di un proxy davanti
# (audit 2026-10-18). Per una corrispondenza dei percorsi 500 bastano.
LIVE_SAMPLES = 500


def _largest_files(adapter) -> list:
    """Il file più grande dei primi LIVE_SAMPLES torrent, chiesto al client
    col minimo (adapter.list_largest_files): prima l'elenco completo di
    list_torrents, coi tracker e i file di ogni torrent anche oltre quelli
    guardati, e con migliaia di torrent il proxy davanti scadeva (audit
    2026-10-18). Un plugin senza la classe base: list_torrents."""
    largest = getattr(adapter, "list_largest_files", None)
    return largest(limit=LIVE_SAMPLES) if callable(largest) else adapter.list_torrents()[:LIVE_SAMPLES]


@router.post("/{torrent_client_id}/path-check", response_model=PathCheckResponse)
def check_client_paths(torrent_client_id: int, live: bool = False, session: Session = Depends(get_session)):
    """Sola lettura (nazgarr/torrents/path_check.py): i file che il client
    ha in seed si trovano sui dischi con la corrispondenza dei percorsi di
    adesso? Con una proposta di corrispondenza se no. Usa l'ultima
    indicizzazione; il client stesso con live=true o se non è mai stato
    indicizzato."""
    tc = _get_torrent_client_or_404(session, torrent_client_id)
    samples = [] if live else path_check.samples_from_index(session, tc)
    source = "index"
    if not samples:
        source = "live"
        try:
            with adapter_factory.torrent_client(tc) as adapter:
                samples = path_check.samples_from_torrents(_largest_files(adapter))
        except Exception as exc:
            return PathCheckResponse(status="error", error=safe_error(exc))
    result = path_check.check(session, tc, samples, source)
    return PathCheckResponse(
        status="ok", source=result.source, verdict=result.verdict, checked=result.checked, **result.counts,
        examples=[PathCheckExample(**vars(e)) for e in result.examples],
        suggestions=[PathCheckSuggestion(**vars(s)) for s in result.suggestions],
    )


@router.patch("/{torrent_client_id}", response_model=TorrentClientResponse)
def update_torrent_client(
    torrent_client_id: int, body: TorrentClientUpdateRequest, session: Session = Depends(get_session)
):
    check_destinations(body.base_url)
    tc = _get_torrent_client_or_404(session, torrent_client_id)
    missing = [name for name, stored, sent in (
        ("password", tc.password, body.password), ("api_token", tc.api_token, body.api_token),
    ) if stored and sent is None] + plugin_config.secrets_not_resent(tc, "torrent_client", body.config)
    require_secrets_for_new_host(tc.base_url, body.base_url, missing)
    if body.label is not None:
        tc.label = body.label
    if body.base_url is not None:
        tc.base_url = body.base_url
    if body.username is not None:
        tc.username = body.username
    if body.password is not None:
        tc.password = body.password
    if body.api_token is not None:
        tc.api_token = body.api_token
    if body.qui_instance_id is not None:
        tc.qui_instance_id = body.qui_instance_id
    if body.enabled is not None:
        tc.enabled = body.enabled
    if "config" in body.model_fields_set:
        _apply_config(tc, body.config, creating=False)
    for name in LABEL_FIELDS:
        if name in body.model_fields_set:
            value = (getattr(body, name) or "").strip()
            if name.startswith("tags_"):
                value = ", ".join(split_tags(value))
            setattr(tc, name, value or None)
    session.commit()
    return TorrentClientResponse.from_model(tc, _links_for(session, tc.id))


class TorrentClientCategoriesResponse(BaseModel):
    status: str  # "ok" | "error"
    categories: list[str] = []
    error: str | None = None


@router.get("/{torrent_client_id}/categories", response_model=TorrentClientCategoriesResponse)
def torrent_client_categories(torrent_client_id: int, session: Session = Depends(get_session)):
    """Le categorie che esistono nel client, lette dal vivo: si sceglie solo
    fra queste (nessuna scritta a mano), in impostazioni e nel job."""
    tc = _get_torrent_client_or_404(session, torrent_client_id)
    try:
        with adapter_factory.torrent_client(tc) as adapter:
            categories = adapter.list_categories()
    except Exception as exc:
        return TorrentClientCategoriesResponse(status="error", error=safe_error(exc))
    return TorrentClientCategoriesResponse(status="ok", categories=categories)


@router.delete("/{torrent_client_id}", status_code=204)
def delete_torrent_client(torrent_client_id: int, session: Session = Depends(get_session)):
    tc = _get_torrent_client_or_404(session, torrent_client_id)
    session.delete(tc)
    session.commit()


@router.post("/{torrent_client_id}/disks/{disk_id}", status_code=204)
def associate_disk(
    torrent_client_id: int, disk_id: int, body: AssociateDiskRequest = AssociateDiskRequest(),
    session: Session = Depends(get_session),
):
    """Idempotente: associare un disco già associato aggiorna il path
    override invece di fallire — comodo per modificarlo senza dover prima
    disassociare (docs/SPEC.md §5)."""
    _get_torrent_client_or_404(session, torrent_client_id)
    _get_disk_or_404(session, disk_id)
    link = (
        session.query(DiskTorrentClient)
        .filter_by(disk_id=disk_id, torrent_client_id=torrent_client_id)
        .one_or_none()
    )
    if link is None:
        link = DiskTorrentClient(disk_id=disk_id, torrent_client_id=torrent_client_id)
        session.add(link)
    local_rel = (body.local_rel_path or "").strip().strip("/") or None
    if local_rel is not None:
        if not body.torrent_client_root_path:
            raise HTTPException(status_code=400, detail=coded_detail("client_mapping_needs_client_root"))
        disk = session.get(Disk, disk_id)
        try:
            folder = resolve_scoped(disk.root_path, local_rel)
        except ScopeViolation as exc:
            raise HTTPException(status_code=400, detail=coded_detail("path_outside_scope", path=local_rel)) from exc
        if not os.path.isdir(folder):
            raise HTTPException(status_code=400, detail=coded_detail("folder_not_found", path=local_rel))
        local_rel = os.path.relpath(folder, os.path.realpath(disk.root_path))
        if local_rel == ".":
            local_rel = None
    link.torrent_client_root_path = _client_root(body.torrent_client_root_path)
    link.local_rel_path = local_rel
    session.commit()


def _client_root(value: str | None) -> str | None:
    """Come il client vede la cartella: un percorso assoluto in stile Unix
    (i client girano in Docker o su Linux), senza spazi attorno né barra
    finale. Prima si salvava com'era, e un " /downloads" o un "downloads"
    non corrispondevano mai a niente, senza nessun errore."""
    value = (value or "").strip()
    if not value:
        return None
    if not value.startswith("/") or "\\" in value:
        raise HTTPException(status_code=400, detail=coded_detail("client_root_not_absolute", path=value))
    return os.path.normpath(value) if value != "/" else value


@router.delete("/{torrent_client_id}/disks/{disk_id}", status_code=204)
def dissociate_disk(torrent_client_id: int, disk_id: int, session: Session = Depends(get_session)):
    _get_torrent_client_or_404(session, torrent_client_id)
    _get_disk_or_404(session, disk_id)
    session.query(DiskTorrentClient).filter_by(disk_id=disk_id, torrent_client_id=torrent_client_id).delete()
    session.commit()
