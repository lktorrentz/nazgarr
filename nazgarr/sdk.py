"""SDK pubblico di Nazgarr per i plugin (docs/ROADMAP.md Fase 10, docs/SDK.md).

Un plugin importa solo da qui, mai dal resto di nazgarr: questo modulo è il contratto
stabile, versionato da SDK_VERSION (semver: un cambio incompatibile alza la
major). Un plugin dichiara con quali versioni funziona.

Un plugin può registrare adapter (tracker, client torrent, resolver dei
media, host di immagini, notifiche). Non tocca mai file né client
direttamente: le azioni che cambiano qualcosa passano sempre dalla coda di
approvazione dell'utente.
"""

from nazgarr.adapters.image_host.base import ImageHostAdapter, ImageHostError
from nazgarr.adapters.image_host.chevereto import CheveretoImageHost, chevereto_image_url
from nazgarr.adapters.media_resolver.base import MediaResolverAdapter, ResolvedMedia
from nazgarr.adapters.notification.base import Notification, NotificationAdapter, NotificationError
from nazgarr.adapters.torrent_client.base import (
    ClientTorrentFileInfo,
    ClientTorrentInfo,
    TorrentAddTimeoutError,
    TorrentAlreadyInClientError,
    TorrentClientAdapter,
    TorrentStatus,
    reusable_files,
)
from nazgarr.adapters.tracker.base import (
    CatalogResults,
    NotSupportedError,
    TorrentCandidate,
    TorrentRecord,
    TrackerAdapter,
    TrackerRateLimitedError,
    TrackerWebResult,
    UploadedTorrent,
    UploadError,
    UploadFields,
    catalog_truncated,
)
from nazgarr.library.mediainfo_text import summarize as mediainfo_summary_from_text
from nazgarr.plugins.registry import KINDS, AdapterContext, AdapterSpec, ConfigField, register

# 1.1: AdapterSpec.icon; 1.2: CheveretoImageHost, chevereto_image_url; 1.3: stop(), CatalogResults,
# catalog_truncated; 1.4: add_torrent(stopped=...), can_add_stopped e start() per i reseed,
# list_largest_files(); 1.5: partial_file_suffix(), fermi anche i reseed del flusso degli upload;
# 1.6: l'evento upload.seed_checked (docs/SDK.md, Events); 1.7: list_torrents(known=...) e
# reusable_files, per riusare i file dell'ultima indicizzazione; 1.8: TorrentCandidate.category_id,
# type_id, resolution, seeders e media_info_summary (mediainfo_summary_from_text) per il dupe check;
# 1.9: TrackerAdapter.check_web_session e claim_resurrection, con TrackerWebResult
SDK_VERSION = "1.9.0"

__all__ = [
    "KINDS", "SDK_VERSION", "AdapterContext", "AdapterSpec", "CatalogResults", "CheveretoImageHost",
    "ClientTorrentFileInfo", "ClientTorrentInfo", "ConfigField", "ImageHostAdapter", "ImageHostError",
    "MediaResolverAdapter", "NotSupportedError", "Notification", "NotificationAdapter", "NotificationError",
    "ResolvedMedia", "TorrentAddTimeoutError", "TorrentAlreadyInClientError", "TorrentCandidate",
    "TorrentClientAdapter", "TorrentRecord", "TorrentStatus", "TrackerAdapter", "TrackerRateLimitedError",
    "TrackerWebResult", "UploadError", "UploadFields", "UploadedTorrent", "catalog_truncated", "chevereto_image_url",
    "mediainfo_summary_from_text", "register", "reusable_files",
]
