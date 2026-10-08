<p align="center">
  <img src="https://raw.githubusercontent.com/lktorrentz/nazgarr/main/frontend/public/ring.png" alt="Nazgarr" width="128" height="128">
</p>

<h1 align="center">Nazgarr</h1>

<p align="center">
  Your media library, your seeding folders and your trackers, in one place.
  <br>
  <a href="https://github.com/lktorrentz/nazgarr/releases/latest"><img alt="Latest release" src="https://img.shields.io/github/v/release/lktorrentz/nazgarr?label=stable"></a>
  <a href="LICENSE"><img alt="License: GPL-3.0" src="https://img.shields.io/github/license/lktorrentz/nazgarr"></a>
</p>

Nazgarr is a self-hosted web app for anyone who seeds on private trackers from the same files their media library uses. It reads your disks and finds the hardlinks between library and seeding folders. It also knows what your torrent clients are really seeding, finds what could seed again, and publishes your own uploads to your trackers.

It isn't tied to Unraid or to the \*arr stack. Separate disks without FUSE or RAID work, and Radarr, Sonarr and the rest are optional integrations, never dependencies. The name only borrows the \*arr naming style, like Bazarr or Prowlarr.

**Nothing that touches your files or your torrent clients runs without your approval.** Every hardlink and every torrent added to a client goes through a review queue first. Running them automatically is an opt-in setting, off by default.

## What it does

**See what seeds and what doesn't**
- Every file of the library and of the seeding folders gets a state: seeding (a hardlink is seeding in a client), orphaned, or tracked by a client without a link in the library.
- Folder and poster views, filters by state, size and tracker, duplicates, and an "exclude" for the files that never count.
- A dashboard with the library health (how much of it seeds, by size), its trend over time, and file-by-file changes since the last scan.

**Reseed what you already have**
- Library files that seed nowhere are searched on your trackers by TMDB id. Candidates are confirmed by reading the torrent's piece hashes against your files, not just by names and sizes.
- Optionally, cross-seed: a file seeding on one tracker is also searched on the others.
- Matches wait in a review queue with their confidence and what they would do. After your approval, Nazgarr creates the hardlinks and adds the torrent with a real recheck.

**Triage your seeding folder**
- Torrents seeding without a hardlink in the library are listed with their reason: replaced by an upgrade, a copy, removed from the library, never imported, extras only.
- Each tracker can set its minimum seed time and ratio. Triage then marks with a green OK the torrents you can remove safely. Anything else to check (files shared with another torrent, a client error) sits in a popover next to it.

**Upload your releases**
- A guided flow: TMDB identification with a confidence explained factor by factor, then a dupe check on every tracker. When the tracker already has the same release, a full hash check proves it and you reseed instead of uploading.
- Release names per tracker from editable patterns: resolution, source, HDR and Dolby Vision profile, audio, languages, REMUX and HYBRID detection.
- MediaInfo, screenshots uploaded to an image host (with fallback across hosts), and descriptions from a sandboxed template.
- The new torrent seeds from hardlinks in an upload folder, with the file names you chose, so the library keeps its own names.
- A watched folder for releasers: whatever lands there starts an upload on its own up to the decision, then moves to the releases folder once it seeds.
- Packs from episodes picked by hand: episodes downloaded one at a time, even ones already seeding with their own torrent, become a season pack or a complete pack.

**Fits your setup**
- Several torrent clients at once ([qBittorrent](https://www.qbittorrent.org/), [qui](https://github.com/autobrr/qui), [Deluge](https://deluge-torrent.org/), [Transmission](https://transmissionbt.com/), [rTorrent](https://github.com/rakshasa/rtorrent)/[ruTorrent](https://github.com/Novik/ruTorrent)), [UNIT3D](https://github.com/HDInnovations/UNIT3D) trackers, optional [Radarr](https://radarr.video/) and [Sonarr](https://sonarr.tv/).
- Works without a media folder too, just for your torrents and uploads.
- More instances in one web UI: add another Nazgarr (say a seedbox) with one of its API keys and switch between them, or see them all in one overview.
- Notifications on Discord and Telegram (as many services as you like), plus plugins, signed webhooks and API keys to extend it ([docs/SDK.md](docs/SDK.md)).
- English and Italian interface, and a guided tour that sets everything up at the first access.

## Who it is for

- **You seed from hardlinks of your library** and want to know, at a glance, what seeds and what doesn't.
- **You lost your torrents** (a reinstall, a moved disk, a client wiped) and want the library to seed again without downloading anything.
- **Your seeding folder keeps growing** and you want to know what can go, and whether it's safe to remove.
- **You release or re-upload** and want naming, dupe checks, screenshots and seeding handled in one flow, across several trackers.

## Install

### Docker Compose

```yaml
services:
  nazgarr:
    image: ghcr.io/lktorrentz/nazgarr:stable
    container_name: nazgarr
    restart: unless-stopped
    ports:
      - "3019:3019"
    environment:
      - PUID=1000            # the user that owns your media and torrent folders
      - PGID=1000
      - TZ=Europe/Rome
      - APP_SECRET_KEY=change-me   # see below
    volumes:
      - ./config:/app/config # config.yaml, database, cache
      - /srv/data:/data      # torrents/ and media/, the same path your torrent client sees
```

Generate `APP_SECRET_KEY` once and keep it with your backups. It encrypts the stored credentials (tracker tokens, client passwords, API keys), and without it they can't be read:

```bash
openssl rand -base64 32 | tr '+/' '-_'
```

Then `docker compose up -d` and open `http://<host>:3019`. Building from source instead: clone the repository and run `docker compose up -d` with the [`docker-compose.yml`](docker-compose.yml) in it, which reads the key from a `.env` file.

### Unraid

A template is in [`unraid/nazgarr-template.xml`](unraid/nazgarr-template.xml). Save it to `/boot/config/plugins/dockerMan/templates-user/`, then pick it under Docker › Add Container › Template. It uses the `:stable` image, `99:100` as the user, and mounts `/mnt/user/data` as `/data`, like the TRaSH Guides layout.

### Python package (no Docker)

Every stable release is also on [PyPI](https://pypi.org/project/nazgarr/), with the web UI already built inside, so neither Docker nor Node is needed. You need Python 3.12 or newer, [pipx](https://pipx.pypa.io), `mediainfo` and `ffmpeg`:

```bash
sudo apt install pipx mediainfo ffmpeg      # Debian/Ubuntu (macOS: brew install pipx media-info ffmpeg)
pipx install nazgarr
nazgarr init --scan-root /mnt               # the folder your disks live under
nazgarr serve                               # or install it as a service, below
```

`init` writes `config.yaml` and a secret key (`secret.key`, readable only by you) to `~/.config/nazgarr` (macOS: `~/Library/Application Support/Nazgarr`). The database goes in `~/.local/share/nazgarr`. To start it at boot:

```bash
nazgarr install-service
systemctl --user daemon-reload && systemctl --user enable --now nazgarr        # Linux (systemd)
sudo loginctl enable-linger $USER                                              # Linux: start without logging in
launchctl load -w ~/Library/LaunchAgents/io.github.lktorrentz.nazgarr.plist    # macOS (launchd)
```

Run it as the user that owns your media and torrent folders, and as a single process (the upload worker and the scheduler live inside it). Update with `pipx upgrade nazgarr`. Windows is not tested yet.

### Release channels

| Tag | What it is |
| --- | --- |
| `:stable` (also `:latest`) | Releases promoted by hand after testing. Use this one. The Python package follows it. |
| `:nightly` | A test build for every push to `main`. It moves fast and may break. |
| `:X.Y.Z` | Every published version, pinned. |

Configuration › Application › Check for updates follows the channel you're on. It can also check on its own every 12 hours (off by default: it contacts GitHub), and then the sidebar says when a new version is out. Before you update, it shows what changes and anything you need to do; after the update, Nazgarr shows what's new once. The detailed notes of each stable release are in [CHANGELOG.md](CHANGELOG.md).

## Basic configuration

Nazgarr needs very little up front. Disks, folders, clients, trackers and thresholds are set from the web UI and saved in its database, with no restart.

**Port:** `3019`. Map it to whatever you like on the host (`"9000:3019"`). The Python package takes `nazgarr serve --host 0.0.0.0 --port 3019`. Up to version 0.8.14 the port was `8080`: if you are updating a container created earlier, change its mapping to `"8080:3019"` (or `"3019:3019"`), and on Unraid set the WebUI Port's container port to 3019.

**Environment**

| Variable | |
| --- | --- |
| `APP_SECRET_KEY` | **Required.** Encrypts the stored credentials. Keep it with your backups. |
| `PUID` / `PGID` | The user and group the app runs as. They need write access to your folders, because hardlinks are created there. Default `1000`. |
| `TZ` | Time zone for the schedule and the logs. |
| `NAZGARR_SETUP_CODE` | Optional: your own one-time code to create the account (otherwise one is generated and printed in the log). |

**`config/config.yaml`** is created on the first start. It holds the only settings that need a restart:

```yaml
data_dir: /app/config/data # database and cache
# disk_scan_root: /data    # optional: restrict disks to one folder
```

In Docker the folders you mount are where disks can be: Nazgarr reads its own mounts, proposes each one as a disk and warns about the layouts that break hardlinks. `disk_scan_root` only restricts that further (outside Docker, `nazgarr init` writes it).

**Disks and paths.** Each disk is a folder that holds the torrents and the media of one filesystem. Hardlinks only work inside one filesystem, so a disk's folders must all be on the same one. A disk can have several seeding folders and several media folders (say `movies/` and `tv/` side by side, or a separate cross-seed folder). With the TRaSH Guides layout there is a single disk:

```
/data
├── torrents/      seeding folder (what your torrent client downloads into)
│   └── uploads/   optional: where your uploads seed
├── media/
│   ├── movies/
│   └── tv/
└── releases/      optional: the watched folder for your own releases
```

With several separate disks, mount each one on its own:

```yaml
    volumes:
      - ./config:/app/config
      - /mnt/disk1:/mnt/disk1
      - /mnt/disk2:/mnt/disk2
```

Every mounted folder shows up in Configuration › Storage, ready to be added as a disk. Two layouts get a warning there: media and torrents mounted as two separate folders (hardlinks cannot cross mounts, even on the same disk: mount their common parent), and the Unraid user share mounted next to the single disks (the same files would be seen twice). The user share alone is fine: Unraid creates the hardlink on the same disk.

**Torrent clients.** Added in Configuration › Clients, as many as you like, each with the address Nazgarr reaches it at:

| Client | Address | Login | Categories and tags |
| --- | --- | --- | --- |
| qBittorrent | the Web UI, `http://qbittorrent:8080` | Web UI user and password | categories and tags |
| qui | `http://qui:7476` and the instance number | its API key | as qBittorrent |
| Deluge | the Web UI, `http://deluge:8112` | the Web UI password (no user) | category = label of the Label plugin, if on; no tags |
| Transmission | the Web UI, `http://transmission:9091` | RPC user and password, if set | no categories; tags become labels |
| rTorrent / ruTorrent | rTorrent's XML-RPC (`http://rtorrent:8000/RPC2`) or the ruTorrent address | the web server login, if any | category = ruTorrent label; no tags |

Every torrent Nazgarr adds is rechecked by the client. Transmission and rTorrent always recheck, even a fresh upload that the others take as already complete.

**Same paths as your torrent client.** Mount the data folder at the same path in Nazgarr and in your client (for example `/data` in both), and it just works. If the client sees a disk elsewhere (say `/downloads`), set that path for the disk in the client's settings.

## First access

1. **Create the account.** Nazgarr stays closed until its single account exists. The log prints a one-time setup code (`docker logs nazgarr`, or the service log). Open the web UI, enter the code, and choose a username and password. Forgot it later? `docker exec -it nazgarr nazgarr reset-password` (or `nazgarr reset-password` for the Python package) sets a new one.
2. **Follow the guided setup.** A short welcome asks whether you upload and whether you use Radarr/Sonarr. The tour then walks you through each screen:
   - disks and their folders;
   - torrent clients;
   - the TMDB API key ([free](https://www.themoviedb.org/settings/api));
   - your trackers, each with its API token;
   - for uploads only, image hosts and naming.

   A checklist on the dashboard follows what you have configured.
3. **Run the first scan.** It only reads: nothing is linked, added or moved. Then the tour shows you around the views. From there, scans run on the schedule you set.

## Command line

Besides running the server, the `nazgarr` command talks to a running instance: log in once with `nazgarr login --url http://HOST:3019`, then check the status, start scans, approve reviews and more from the terminal or a script (`--json`, `--yes`). Inside the container: `docker exec -it nazgarr nazgarr status`. Full guide: [docs/CLI.md](docs/CLI.md).

## API and automation

Everything the UI does goes through a JSON API under `/api`. The interactive reference is at `http://<host>:3019/docs`. Scripts authenticate with an API key created in Configuration › API keys. See [docs/SDK.md](docs/SDK.md) for plugins, webhooks and API keys, and [examples/nazgarr-ntfy](examples/nazgarr-ntfy) for a complete plugin.

## Development

```bash
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
cp config.example.yaml config.yaml          # point data_dir (and disk_scan_root) at local folders
export APP_SECRET_KEY=$(openssl rand -base64 32 | tr '+/' '-_')
./.venv/bin/uvicorn nazgarr.main:app --reload --port 3019

cd frontend && npm install && npm run dev   # http://localhost:5173, proxies /api to the backend
```

Checks, the same as CI: `ruff check .` and `pytest -q` for the backend, `npm run lint`, `npx tsc -b` and `npx vitest run` in `frontend/`. The torrent client adapters also have tests against real clients in Docker (Transmission, Deluge, rTorrent/ruTorrent), never run in CI: `scripts/test_real_clients.sh` starts the containers, runs them and removes everything. Python dependencies are locked with hashes: edit `requirements.in`, then run `uv pip compile requirements.in --python-version 3.12 --generate-hashes -o requirements.txt`.

Design decisions live in [`docs/SPEC.md`](docs/SPEC.md), the plan in [`docs/ROADMAP.md`](docs/ROADMAP.md), the schema in [`docs/schema.sql`](docs/schema.sql), and the frontend notes in [`frontend/README.md`](frontend/README.md). Issues and pull requests are welcome. If something in the spec looks wrong, open an issue before working around it.

## Credits

- **Metadata:** this product uses the TMDB API but is not endorsed or certified by TMDB. Episode orders are provided by [TheTVDB](https://thetvdb.com).
- **Fonts and images:** [Geist](https://vercel.com/font) (SIL Open Font License 1.1). Logos of the clients, trackers and services Nazgarr connects to from [Dashboard Icons](https://github.com/homarr-labs/dashboard-icons) (Apache-2.0), used only to identify them: the marks belong to their owners, with no affiliation implied. Country flags on systems without them come from [Twemoji](https://github.com/jdecked/twemoji) graphics (CC-BY 4.0, by Twitter, Inc. and other contributors), through [country-flag-emoji-polyfill](https://github.com/talkjs/country-flag-emoji-polyfill) (MIT).
- **Backend:** FastAPI, SQLAlchemy, APScheduler, httpx, qbittorrent-api, pymediainfo and MediaInfo, guessit, torf, FFmpeg. Deluge, Transmission and rTorrent are reached through their own APIs (JSON-RPC, RPC, XML-RPC) with httpx and the Python standard library, no extra client library.
- **Frontend:** React, TanStack Query, Base UI and shadcn/ui, Tailwind CSS, three.js, GSAP, Recharts, driver.js, Lucide icons.
- **Domain reference:** [Upload-Assistant](https://github.com/Audionut/Upload-Assistant), for tracker conventions in the upload flow. No code is reused from it.

## License

[GPL-3.0](LICENSE)
