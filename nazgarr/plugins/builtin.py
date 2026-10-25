"""Gli adapter integrati, iscritti al registro come farebbe un plugin
(nazgarr/plugins/registry.py). Tracker e client leggono le colonne che hanno
sempre avuto (ctx.row). Gli host di immagini sono plugin inclusi
(nazgarr/bundled/), caricati qui a nome loro: si configurano, si accendono e
si spengono come quelli installati.
"""

from nazgarr.adapters.notification.discord import DiscordNotificationAdapter
from nazgarr.adapters.notification.telegram import TelegramNotificationAdapter
from nazgarr.adapters.torrent_client.deluge import DelugeAdapter
from nazgarr.adapters.torrent_client.qbittorrent import QBittorrentAdapter
from nazgarr.adapters.torrent_client.qui import QuiTorrentClientAdapter
from nazgarr.adapters.torrent_client.rtorrent import RTorrentAdapter
from nazgarr.adapters.torrent_client.transmission import TransmissionAdapter
from nazgarr.adapters.tracker.base import Unit3dTrackerAdapter
from nazgarr.adapters.tracker.torznab import TorznabTrackerAdapter
from nazgarr.bundled import imageride, imgbb, passtheima, ptscreens
from nazgarr.plugins import switch
from nazgarr.plugins.registry import AdapterContext, AdapterSpec, ConfigField, register, registering_as

# I plugin inclusi, uno per host (Impostazioni › Plugin li accende e spegne
# uno per uno): il nome con cui si registrano e il loro modulo.
BUNDLED = {module.PLUGIN_NAME: module for module in (ptscreens, passtheima, imageride, imgbb)}


def _qbittorrent(ctx: AdapterContext) -> QBittorrentAdapter:
    tc = ctx.row
    return QBittorrentAdapter(base_url=tc.base_url, username=tc.username, password=tc.password)


def _qui(ctx: AdapterContext) -> QuiTorrentClientAdapter:
    tc = ctx.row
    if tc.qui_instance_id is None:
        raise ValueError(f"TorrentClient {tc.id!r} (qui) senza qui_instance_id configurato")
    if not tc.api_token:
        raise ValueError(f"TorrentClient {tc.id!r} (qui) senza api_token configurato")
    return QuiTorrentClientAdapter(base_url=tc.base_url, api_token=tc.api_token, instance_id=tc.qui_instance_id)


def _deluge(ctx: AdapterContext) -> DelugeAdapter:
    # La Web UI di Deluge ha solo la password, nessun utente.
    return DelugeAdapter(base_url=ctx.row.base_url, password=ctx.row.password)


def _transmission(ctx: AdapterContext) -> TransmissionAdapter:
    tc = ctx.row
    return TransmissionAdapter(base_url=tc.base_url, username=tc.username, password=tc.password)


def _rtorrent(ctx: AdapterContext) -> RTorrentAdapter:
    tc = ctx.row
    return RTorrentAdapter(base_url=tc.base_url, username=tc.username, password=tc.password)


def _unit3d(ctx: AdapterContext) -> Unit3dTrackerAdapter:
    tracker = ctx.row
    return Unit3dTrackerAdapter(
        base_url=tracker.base_url, api_token=tracker.api_token,
        rate_limit_per_min=tracker.rate_limit_per_min or 30, rss_key=tracker.rss_key, shared=True,
    )


def _torznab(ctx: AdapterContext) -> TorznabTrackerAdapter:
    # base_url è l'URL dell'endpoint Torznab, api_token la sua API key.
    tracker = ctx.row
    return TorznabTrackerAdapter(
        base_url=tracker.base_url, api_key=tracker.api_token,
        rate_limit_per_min=tracker.rate_limit_per_min or 30, shared=True,
    )


register(AdapterSpec("torrent_client", "qbittorrent", "qBittorrent", _qbittorrent))
register(AdapterSpec("torrent_client", "qui", "qui", _qui))
register(AdapterSpec("torrent_client", "deluge", "Deluge", _deluge))
register(AdapterSpec("torrent_client", "transmission", "Transmission", _transmission))
register(AdapterSpec("torrent_client", "rutorrent", "rTorrent / ruTorrent", _rtorrent))
register(AdapterSpec("tracker", "unit3d", "UNIT3D", _unit3d))
register(AdapterSpec(
    "tracker", "torznab", "Torznab", _torznab,
    description="Any Torznab endpoint (Prowlarr, Jackett, ...): search, matching, reseed and cross-seed. No upload.",
))

for _name, _module in BUNDLED.items():
    with registering_as(_name):
        _module.setup()
    switch.remember(_name, _module.setup)

# Notifiche: ogni istanza ha la sua riga in notification_service (cifrata), come per i plugin.
register(AdapterSpec(
    "notification", "discord", "Discord",
    lambda ctx: DiscordNotificationAdapter(ctx.config["webhook_url"], ctx.config.get("username")),
    config_fields=(
        ConfigField("webhook_url", "Webhook URL", type="secret", required=True,
                    help="Channel settings › Integrations › Webhooks › Copy webhook URL."),
        ConfigField("username", "Name shown in the channel", default="Nazgarr"),
    ),
))
register(AdapterSpec(
    "notification", "telegram", "Telegram",
    lambda ctx: TelegramNotificationAdapter(
        ctx.config["bot_token"], ctx.config["chat_id"], ctx.config.get("thread_id"),
    ),
    config_fields=(
        ConfigField("bot_token", "Bot token", type="secret", required=True,
                    help="From @BotFather. Add the bot to the chat, group or channel first."),
        ConfigField("chat_id", "Chat id", required=True,
                    help="A user, group or channel id (groups and channels start with -100)."),
        ConfigField("thread_id", "Topic id", type="number", help="Only for a group with topics."),
    ),
))

