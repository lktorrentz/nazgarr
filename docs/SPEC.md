# Nazgarr — Functional and architectural spec (v1)

A document consolidated from a user's stream of ideas, reorganized and grounded by comparing it against four related local projects. Not an open brainstorm: wherever something is explicitly undecided, it's flagged as such at the bottom (section 15).

**Name** (renamed 2026-09-29, user decision): project name **"Nazgarr"**, repo/technical name **`nazgarr`**. It was "The Media Gauntlet*rr" / `gauntletarr`, and the rename carries the data along (`gauntletarr.db` is renamed at startup, `nazgarr/core/db.py`). It's *arr style (like Bazarr/Cleanuparr/Prowlarr) without actually depending on Sonarr/Radarr, the same stylistic "wink" Auditorr already does. "Nazg" is a fantasy word for "ring" and the icon is a plain golden ring: a light fantasy flavour is fine in the UI, but no direct references to the source works or their marks, and none in the README. The **Media Stones** theme below comes from the old name and stays only as internal naming of domains/phases for now; a re-theme around the new name is to be evaluated (it isn't visible in the UI today).

## Theme: the Media Stones

Six "stones," one per main functional domain — used as a reading key to organize the spec and as the basis for the visual identity (icon/color per module in dashboard and sidebar), not as a rename of the underlying technical concepts (the code/API keeps ordinary descriptive names: `media_item`, `TorrentClientAdapter`, etc. — the Stones are a branding layer on top, not a replacement).

| Stone | Color | Domain | Section |
|---|---|---|---|
| **Stone of Bond** | Blue | Disk/hardlink model, unified per-file state (orphaned/ignored) | §3-4 |
| **Stone of Control** | Purple | Multi-client torrent adapters, presence/seeding state | §5 |
| **Stone of Knowledge** | Yellow | Content identification (TMDB), poster cache | §6 |
| **Stone of Reintegration** | Red | Matching and reseeding engine (repairs broken links) | §6, 8 |
| **Stone of Time** | Green | Scheduling, run history, dashboard trends over time | §8, 10 |
| **Stone of Genesis** | Orange | Upload — gives content "new life" by publishing it to a tracker | §9 |

"Wearing the gauntlet" = having all six Stones configured and active (disks mapped, clients connected, resolver working, matching engine active, scheduler configured, upload ready) — also useful as a metaphor for a future onboarding/setup wizard in the UI: a six-item checklist before the system is "complete."

## 0. Provenance — where this project comes from

This doesn't start from scratch. It synthesizes:

- **`ratio-guardian`** (its own `docs/SPEC.md`, a separate project): the most mature architectural analysis and the closest to this scope — a disk/hardlink model with no Unraid/FUSE dependency, a TMDB matching engine with explicit confidence, a reseeding engine with forced recheck, and — decided in that project's most recent session — a second "Upload" mode inspired by Upload-Assistant. Nazgarr **inherits ratio-guardian's entire data architecture and matching/reseeding engine**, which should be read for the verified implementation details (the real shape of the UNIT3D API, already-fixed known bugs like the season pack size comparison, mediainfo edge cases). This document doesn't repeat those details where they haven't changed, it references them.
- **Auditorr**: reference for the visualization experience — library tree view, per-file state (presence/hardlink/seeding), dashboard with a "library health" gauge, reverse hardlink lookup from the torrent side.
- **Upload-Assistant**: domain reference for the upload flow (mediainfo, screenshots, description, dupe-check, ~90 supported trackers). **In development freeze** as declared by the project itself — to be treated as a domain reference to reimplement against our own contracts, never as a live dependency.
- **smartmediareseed**: reference for verifying file↔torrent identity via **piece hashes** (BEP3) against the hash declared in the `.torrent` — a stronger confidence signal than mediainfo Unique ID alone (which doesn't distinguish different audio tracks on otherwise-identical video). To be integrated as an additional signal in the matching engine (section 6), never as a substitute for a real recheck.

## 1. Vision and problem

The user manages a media library (movies/shows) and one or more torrent seeding folders, often on separate physical disks with no RAID/FUSE. Everyday use accumulates drift:

- files moved/renamed in the library that break the hardlink and therefore seeding, without the user noticing (ratio-guardian's core problem);
- files present in the torrent folder that the torrent client no longer tracks (removed from the client, client reinstalled, a migration never completed);
- files seeding that were never organized/linked into the actual media library;
- downloaded/organized files that were never correctly identified (no TMDB match), and are therefore invisible to any matching logic.

Nazgarr has to give **a single, coherent view of every file's state**, on both sides (media and torrent) and on the torrent client itself, plus the tools to fix every kind of drift: reseeding, manual linking, uploading new content.

## 2. Genericity requirements (binding, inherited from ratio-guardian §2)

- **Must not assume Unraid/FUSE.** N separate physical disks, each potentially holding part of the library, with no unifying filesystem.
- **Must not assume Sonarr/Radarr.** Optional integration as an additional media resolver adapter, never a dependency.
- **Adapter architecture for trackers, torrent clients and media resolvers**, to allow future extension without rewrites (detail in section 5).
- Distribution: Docker container, web UI for configuration.
- **Designed for public/open source release** (an explicit decision, unlike the source tools which are for personal use): implies example config with no personal data, no hardcoded secrets, clean `.env.example`/`config.example.yaml`, a LICENSE, and care not to assume the original user's specific setup (paths, trackers, disk names) in any default.

## 3. The two directions of the problem: orphaned and ignored

The central point of the original request, distinct from (and complementary to) the "media→torrent" model ratio-guardian already covers. **Both scan directions** need to be maintained, on the same hardlink graph:

### Media → torrent direction (already covered by ratio-guardian's engine)

Files in the media library **without** a valid hardlink to the disk's torrent folder → candidates for the matching/reseeding engine (section 6). This is the "I moved/renamed the file and broke seeding" case.

### Torrent → client/media direction (new requirement for Nazgarr)

For every file in a disk's torrent folder:

- **Orphaned file**: present on the filesystem (torrent folder) but **not tracked by any configured torrent client** (no torrent in the client whose resolved path points to that file). Typically: a file left behind after removal from the client, a migration that was never finished, a reconfigured client. The natural action: the same reseeding engine as ratio-guardian, but triggered from the torrent side — search the configured trackers for a match for that file (size + mediainfo + piece hash, section 6), and if the match reaches maximum confidence (100%, not the 0.95 threshold used for the media→torrent case — see note below) **download the `.torrent` from the tracker and add it to the client pointing at the file that's already there**, with no need to recreate the hardlink (the file is already in place).
- **Ignored file**: present on the filesystem (torrent folder) and correctly tracked by the client, but **with no hardlink into any enabled `MediaPath`**. It's a seeding file "orphaned from the library": technically healthy, but invisible to the user's media organization. Action: flag only in the UI (never automatic) — the user decides whether it's worth organizing (manual hardlink into a MediaPath) or leaving it as-is (e.g. cross-seeded content that isn't theirs).

**"Tracked by a client"** means the torrent was present at the latest complete, successful listing of that client. A torrent removed from the client leaves the index at the next scan, and its file becomes an orphan again. A file whose torrents are all stopped (paused) in the client stays `seeding` in the state model, with a separate `stopped` flag in the UI. An execution that reached seeding and whose torrent was then removed from the client is shown as "removed from client", and it no longer blocks a new review for that torrent.

**Library ↔ torrent link by inode:** a library file is hardlinked (and seeding, if a client tracks the torrent side) when **any** torrent-side file shares its inode on the same disk. It does not depend only on the single `seed_file.media_file_id` the scanner writes. The same inode reachable from two library paths (e.g. a double import by Sonarr/Radarr) is therefore seeding on both paths, and is never searched as an orphan (`nazgarr/library/hardlinks.py`). Duplicates come in two kinds: `copy` (same content on different inodes, wasting space) and `hardlink` (same inode under several library paths: no extra space, but the content appears twice and one path can be deleted without touching the seed).

**Note on the threshold for torrent-side orphans**: the risk here differs from what's discussed in ratio-guardian §9 (a false positive that leads to seeding the wrong data). Adding a torrent that's already present locally based on a wrong match is still risky (it associates the file with a torrent that isn't a match; the client rechecks it and, worst case, fails — less severe than a false hardlink, but not harmless). So treat it with the **same severity**: a high, configurable threshold, and below it, the manual review queue, exactly as for the media→torrent case — never a bypass "because the file already exists."

### Unified per-file state

Every file (on either side) must expose a composite state, inspired by ratio-guardian's Library page (§12) but extended:

| State | Meaning |
|---|---|
| `seeding` | Valid hardlink + tracked by the client, actively seeding |
| `orphan_media` | In the media library, no valid hardlink (reseeding candidate — media→torrent direction) |
| `orphan_torrent` | In the torrent folder, not tracked by any client (reseeding candidate — torrent→client direction) |
| `ignored` | In the torrent folder, tracked by the client, no hardlink into the media library |
| `unmatched` | No TMDB match resolved (unparsable filename, or no tracker candidate), regardless of side |
| `pending_review` | A match was found but below the confidence threshold, in the manual review queue |

A media file counts as seeding only through a torrent a client tracks. A hardlink in the torrent folder that no client follows doesn't count: for example, a file downloaded by hand into the torrent folder and imported by Radarr is still searched as `orphan_media`. **Cross-seeds** (user decision, 2026-10-02, on by default, setting `cross_seed_search`): for each tracker, the media→torrent matching searches the files that don't seed on *that* tracker, so a file seeding on tracker A is also searched on tracker B. Reviews are kept per file and tracker, so a file can have one proposal on each tracker. A candidate whose info hash is already in a client is dropped. When the destination of a hardlink already exists with the same inode (the same release, under the same name, seeding for another tracker), execution reuses it instead of failing. A different file at that path is still an error and is never overwritten. The review queue marks these proposals as "Cross-seed · seeding on X" (`nazgarr/library/seeding.py`).

**Packs and singles on the same tracker** (user decisions, 2026-10-08). A torrent with more than one video is a *pack* (a season or a series); one with a single video, extras included, is a *single*. Only episodes have formats: a movie with video extras is never a pack.
- **Off (default):** an episode gets one proposal per tracker, and a plausible pack always wins over the singles (usually, once the pack is out, singles are no longer uploaded). A pack queued or approved for another episode keeps this episode's single out of the queue, and proposing a pack takes the singles of its other episodes out of it (system rejection). A pack the user rejected counts as rejected for every episode in it, so it never comes back through another episode.
- **On** (setting `reseed_pack_and_singles`): an episode that seeds nowhere on that tracker gets one proposal per format, and an episode seeding there in one format is searched there in the other one only (the history's single torrent no longer stops the catalog search for the pack). A proposal closes once the episode seeds there in its own format.
- **The queue:** the single episodes of a season on one tracker are one row (series, season, how many, which episodes, the confidence range) with Approve all and Reject all (confirmed), and each episode underneath. A proposal for the format missing on its own tracker is marked "Second format".

## 4. Data architecture (inherits ratio-guardian §3-4, §13 — extended here)

Disk/library model from ratio-guardian: a **Disk** entity (physical root, cached `st_dev` to detect remounts), its **folders** (`disk_folder`, user decision 2026-10-03), paths always relative to the disk (never absolute), two-level validation (scoped file browser in the UI + `st_dev` comparison at runtime before every hardlink). See ratio-guardian SPEC.md §3 for the full reasoning — not repeated here. Full table-by-table DB schema: `docs/schema.sql`.

### Media and seeding folders (user decision, 2026-10-03)

A disk has **any number of media folders and seeding folders** (`disk_folder`, `nazgarr/library/disk_folders.py`). Examples: `movies/` and `tv/` side by side with no common parent, or `torrents/` plus a cross-seed folder. They replace the single `disk.media_rel_path`/`torrents_rel_path`, which are moved into `disk_folder` at startup and cleared (`nazgarr/core/db.py::migrate_disk_folders`).

- **No type per folder:** movie or TV is still detected per file. This matches the earlier decision that removed the typed `media_path`.
- **Rules for a folder:**
  - it is inside the disk, exists, and is not the disk root;
  - it is on the disk's filesystem, because hardlinks cannot cross filesystems. A folder mounted from another filesystem is refused with a hint to add it as a disk of its own;
  - it is never equal to, inside, or containing another folder of the disk, of either kind;
  - it never contains the watched folder.
- **Removing a folder** touches nothing on disk. Its files leave the library at the next scan.
- **The scan** reads every folder. A folder that cannot be read (an unmounted share) keeps its files current: their `last_scan_id` is carried forward. Without this, files would disappear with the share. A file gone from a readable folder still disappears as before.
- **Defaults:** new hardlinks and uploads go to their own folder if set, otherwise to the first seeding folder. The "already seeding" search covers every seeding folder.
- **Disk test** (user report, 2026-10-03): "Test disk" on the card (`POST /api/disks/{id}/verify`, `nazgarr/library/disk_folders.py::test_disk`) runs these checks:
  - every folder exists and is on the disk's filesystem;
  - a test hardlink goes from the first seeding folder to every other folder. The test file is empty, hidden and removed right away. If the disk has a single folder, the link stays inside that folder.

  A changed `st_dev` alone is only a warning: on FUSE (Unraid `/mnt/user`) it changes at every remount, and it never proved hardlinks work. The stored value is updated when the link test passes.
- **API:** `GET /api/disks` lists `folders`, `media_folders` and `seeding_folders`. `POST /api/disks/{id}/folders` and `DELETE /api/disks/{id}/folders/{folder_id}` manage them. `media_rel_path`/`torrents_rel_path` remain as deprecated fields: the first folder in responses, and a single-folder replacement in a PATCH.
- **UI:** Configuration › Storage has one card per disk, like torrent clients and trackers. Each card lists its seeding and media folders, each with add and remove, and then the folders for new hardlinks, uploads and releases.

### Two supported layouts: per-disk mounts or a single TrashGuide-style mount

Where a disk can be (user decision, 2026-10-05, replacing a required `disk_scan_root`): inside a container, the data folders the user mounted. `nazgarr/core/mounts.py` reads `/proc/self/mountinfo` (only when `NAZGARR_CONTAINER=1`, set by the Dockerfile, or `/.dockerenv` exists), drops system filesystems and folders, mounted files (`/etc/hosts`) and Nazgarr's own config and data folders, and what is left is the scope: decided by the Docker template, not by a setting a browser could change, so it stays as safe as a static root. `disk_scan_root` in `config.yaml` is now optional: when set it is the scope (installs from before keep working), and mounts outside it are reported; outside a container `nazgarr init` writes it, and without it the scope is `/data`. Storage lists the detected mounts with their filesystem, proposes the ones that are not a disk yet ("Add as a disk") and refuses a disk inside another one (`disk_root_nested`). Two layouts get a warning, because Nazgarr can see them and the user cannot:

- **`split_mounts`**: two mounts of the same filesystem (same device), e.g. `/data/media` and `/data/torrents` mounted separately, also from the Unraid share. The kernel refuses a hardlink between two different mounts even on the same disk: mount the common folder instead.
- **`share_and_disks`**: the Unraid user share (`fuse.shfs`) mounted next to single disks: the same files would be seen twice. The share alone is fine: shfs creates a hardlink on the same physical disk as its source.

The two usual layouts:

- **Single mount (TrashGuide convention)**: mount one combined torrents+media folder at `/data` — the same host path shared with the download client and media manager, which is what makes hardlinks between them work. In this layout there's a single Disk, registered with `root_path` equal to the mount itself.
- **Per-disk mounts (classic Unraid layout)**: each physical disk is bind-mounted directly (`/mnt/disk1`, `/mnt/disk2`, ...), and each shows up as its own registerable Disk. No common parent and no `disk_scan_root` are needed any more.

Either way, the runtime `st_dev` check before every hardlink (§3, `create_disk`/`verify_disk`) is the actual safety net: even inside a single combined mount, individual files are tracked with their own `st_dev` (not just one value per Disk row) — see "The two FKs" below — so a hardlink attempted across two files that don't really share a device fails with an explicit error rather than silently corrupting anything, whichever layout is in use.

### Why ratio-guardian's model isn't enough as-is

Ratio-guardian merges logical identity and physical file into a single row (`media_item` has both `tmdb_id` and `file_path`/`inode`) and **has no table at all for the torrent client inventory** — it checks "is this already seeding" with a live filesystem check (`find -samefile`) plus a client query only at execution time. That works for a single client with no need to see cross-seeding, but it doesn't hold up against Nazgarr's requirements (multi-client, a grid view grouped by content, explicit visibility of every cross-seed claimant — §3, §5, §7). Auditorr's model was also analyzed, as a negative reference: it keeps everything in JSON blobs recomputed on every run and, for cross-seeding, merges every claimant on the same inode down to just "the healthiest one" (`audit.py::_walk_directory`, lines 106-124) — an efficient choice, but one that **loses information**, exactly the opposite of what's needed here.

### The entities (physical separated from logical, as discussed)

```
media_item            -- resolved logical identity: tmdb_id, season, episode, poster
  media_file           -- physical, media side: disk_id, relative_path, size, st_dev/inode
                        --   "as of last scan", media_item_id (FK)

seed_file              -- physical, torrent side: disk_id, relative_path, size, st_dev/inode
                        --   "as of last scan", media_file_id (FK, nullable — see below)
                        --   one row per hardlink sibling: 3-way cross-seed = 3 rows

torrent_client          -- config (already exists)
  client_torrent          -- ONE torrent for ONE client instance: info_hash, name, save_path,
                          --   category, state, tracker_url — UNIQUE(torrent_client_id, info_hash)
    client_torrent_file     -- ONE file inside a client_torrent: path_in_torrent, size,
                            --   seed_file_id (FK, nullable)
```

`media_item` is separate from `media_file` (unlike ratio-guardian, where they're the same row) because the grid view (§7) needs to group several physical files under one poster — a common case for a season with multiple episodes, or content with several versions/qualities in the library.

### The two FKs, and why they're written differently

- **`seed_file.media_file_id`** (the inode link, cross-seed): **never computed at runtime with a live join** on `(disk_id, st_dev, inode)` — on a large library that would be recomputed every time the tree/grid view loads. Instead it is:
  1. computed **once per scan**, in memory, during the same `os.walk` already needed to read `st_dev`/`inode`/`nlink` (the same technique Auditorr uses — a dict kept for the duration of the scan — but here **without discarding the "losing" claimants**: every sibling stays a row);
  2. written with a **bulk upsert at the end of the scan** (batch insert/update, never a per-file query);
  3. tagged with `last_scan_id` (a FK to `run_log`) — a `seed_file` not seen again in a later scan isn't deleted right away (the review queue still needs to be able to show it as "gone"), but its `media_file_id` stops being trusted for current-state computations until it's reconfirmed. This avoids the concrete risk of inodes being reassigned by the filesystem between one scan and the next (the same problem already flagged as open in ratio-guardian §17 — made explicit and handled here).
- **`client_torrent_file.seed_file_id`** (the path link, not inode-based): resolved by comparing `client_torrent.save_path + path_in_torrent` against `disk.root_path + seed_file.relative_path` — doesn't suffer from reassignment (a path doesn't get "reused" for a different file the way an inode number does), so it's more stable across scans, though still reverified on every scan for consistency.

On read, every state query (§3) and every dashboard count (§10) is an indexed JOIN on these FKs — never a computation on `st_dev`/`inode` at runtime, which stay **write-only** columns for the scan process.

### Other extensions

- **Poster cache**: `media_item.tmdb_poster_path` (relative TMDB path) + a local cache of the downloaded images (filesystem, not a DB blob — a predictable path like `data/posters/{tmdb_id}.jpg`, downloaded once and reused). Needed for the grid view (§7).
- Static YAML (`data_dir`, optional `disk_scan_root`) / dynamic DB (disks, media paths, trackers, clients, thresholds) config split, as in ratio-guardian §4; in a container the disk scope comes from the mounts (above).

Matching/reseeding entities (`candidate`, `match_review`, `seed_job`) and the new upload entities (§9) stay as in ratio-guardian, adapted to reference `media_item`/`media_file` instead of ratio-guardian's merged row — full detail in `docs/schema.sql`. One real gap found while building Fase 4: `candidate.media_item_id` alone isn't enough to know *which physical file* a match applies to when a `media_item` has more than one `media_file` (different quality versions of the same content) — never an issue in ratio-guardian, where a media_item *was* the physical file. Fixed by adding `match_review.media_file_id`/`match_review.seed_file_id` (whichever applies to the candidate's `direction`), so the decision — not just the search result — carries the physical file it's about.

## 5. Torrent clients: multi-client support from v1

An explicit requirement, unlike ratio-guardian (which starts with a single qBittorrent adapter and is generically extensible but with no immediate commitment to other clients). Priority:

1. **qBittorrent** — via `qbittorrent-api`, first adapter, implemented (`nazgarr/adapters/torrent_client/qbittorrent.py`). **Validated against a real instance** (deployed on Unraid, `POST /api/torrent-clients/{id}/test` confirmed a working connection) — tests still use a mocked client, the real-instance check was manual.
2. **qui** (multi-instance manager for qBittorrent, `nazgarr/adapters/torrent_client/qui.py`) — the Phase 2 pragmatic assumption above (treat it as plain qBittorrent pointed elsewhere) turned out to be **wrong**, confirmed against qui's own OpenAPI spec (github.com/autobrr/qui) and against Auditorr's own working qui integration (`sources/_qui.py`), not just deduced: qui exposes its own aggregation API (`X-API-Key` header, paths under `/api/instances/{id}/...`), not the qBittorrent WebUI API `qbittorrent-api` expects. A deployment manages several qBittorrent instances behind one host+key; `add_torrent` has to target one specific instance deterministically, so one `TorrentClient` row maps to exactly one qui-managed instance (`torrent_client.qui_instance_id`), never chosen at runtime like Auditorr's read-only aggregation across "eligible" instances does. No endpoint exists to read a single torrent's status by hash (confirmed absent from both the OpenAPI spec and Auditorr's implementation) — `get_torrent_status()` therefore does a full paginated scan per call, documented as a known inefficiency in the adapter's own docstring rather than hidden. Still unverified against a real qui instance (built and tested against the real, published API contract and mocks only) — same category of open item as qBittorrent's own real-instance check once was.
3. **Deluge**, 4. **Transmission**, 5. **rTorrent / ruTorrent** — implemented 2026-10-03 (`deluge.py`, `transmission.py`, `rtorrent.py` in `nazgarr/adapters/torrent_client/`, `adapter_type` `deluge`, `transmission`, `rutorrent`). **No new dependency**: each API is simple enough for `httpx`, already in the stack, and the standard library (decision taken 2026-10-03, the library choice was open in section 15):
   - **Deluge**: the JSON-RPC API of its **Web UI** (`/json`, cookie from `auth.login`), which forwards every `core.*` daemon method. Chosen over the daemon RPC (`deluge-client`, port 58846, rencode over TLS, a separate auth file) because `base_url` is always an http(s) URL and the Web UI is what users expose. Password only (the Web UI has no user). If the Web UI is not connected to a daemon, the adapter connects it to the first one it lists. Category = label of the Label plugin, only when the user has turned that plugin on (Nazgarr never turns plugins on); labels are lowercase; no tags. `skip_check_verified` uses libtorrent's `seed_mode`. A torrent queued inside libtorrent (paused + auto-managed) is `pending`: right after `force_recheck` Deluge shows it as "Seeding" or "Queued" at 0% for a moment before "Checking" (seen on the real instance; reading it as a result would fail a good recheck).
   - **Transmission**: its JSON RPC (`/transmission/rpc`, `X-Transmission-Session-Id` handshake), chosen over `transmission-rpc` to avoid a dependency for one POST. No categories: tags become `labels` (RPC 16+, Transmission 3.0), `list_categories()` is empty. Recheck with `torrent-verify`.
   - **rTorrent / ruTorrent**: rTorrent's XML-RPC, marshalled with the standard `xmlrpc.client` and sent with httpx, either to rTorrent's own endpoint (`/RPC2`) or through ruTorrent's `plugins/httprpc/action.php`; a bare ruTorrent address gets that path appended. ruTorrent 5.3's proxy (mode "sanitize") forwards calls it cannot rebuild as *untrusted*, and rTorrent 0.16.10+ refuses write commands there (`d.directory.set` on its own, `execute.*`): the save path and the label therefore travel as commands of the `load.raw`/`load.normal` itself, and removing with data through ruTorrent uses its `removewithdata` action (the erasedata plugin, set up the first time the ruTorrent UI is opened). On the direct endpoint the torrent's own files are removed with `execute.throw rm` inside the client. Category = ruTorrent label (`d.custom1`, URL-encoded); no tags.
   - **Transmission and rTorrent cannot add a torrent as already complete** (rTorrent would need fast-resume data written into the `.torrent`): their adapters set `can_skip_recheck = False` and always recheck, so executor and upload flow never record a skipped recheck for them (`seed_job.recheck_skipped`, upload event `added_to_client_verified`).
   - **Torrents always start** (2026-10-05): qBittorrent's "do not start the download automatically" and "torrent stop condition: files checked" left reseeds and uploads stopped after the recheck. qBittorrent now gets `stopped=false` and no stop condition on add, qui `paused=false`, and both start the torrent after adding it; a reseed whose recheck succeeded but is still stopped (qui cannot change the stop condition on add) is started when it is reconciled. The recheck itself never changes.
   - **Category and tags per reseed** (user decision, 2026-10-05): like an upload, a queued reseed can get its own category and tags in the client (`match_review.client_category` / `client_tags`, NULL = the client defaults with the anime check, "" = none), chosen in the review's details and used when it runs.
   - **Verified against real clients in Docker** (`scripts/test_real_clients.sh`, `tests/integration/test_real_clients.py`, skipped unless `NAZGARR_IT_CLIENTS=1`, never in CI): Transmission 4.1.3 (`lscr.io/linuxserver/transmission` 4.1.3-r0-ls363), Deluge 2.2.0 (`lscr.io/linuxserver/deluge` 2.2.0-ls383), rTorrent 0.16.22 with ruTorrent 5.3.14 (`crazymax/rtorrent-rutorrent` 5.3.14-0.16.22), rTorrent tested on both endpoints. The client's folder is mounted at a path the test process does not have (translated with `nazgarr/torrents/client_paths.py`) and the `.torrent` files live where the client cannot see them, so a path sent instead of the content would fail. Checked for single-file and multi-file torrents: info hash, recheck to completion, recheck reporting wrong data as incomplete, files and sizes, save path, category/labels, tracker, duplicate detection, a `.torrent` URL fetched by the client itself, `skip_check_verified`, removal without and with files.

**Every adapter sends a local `.torrent` by content** (`base.local_torrent_bytes`): the client usually runs in another container and never sees Nazgarr's data folder; only http/https/magnet URLs go as URLs (same rule as the qBittorrent fix of 2026-10-03, where the path went in `urls` and failed with "No such file or directory"). **`remove_torrent(info_hash, delete_files)`** (2026-10-03) is part of the contract for every built-in client (qBittorrent `torrents_delete`, qui bulk action `delete` with `deleteFiles`, Deluge `core.remove_torrent`, Transmission `torrent-remove`, rTorrent `d.erase`); only for an explicit user request.

All behind the same `TorrentClientAdapter` contract — `add_torrent`/`get_torrent_status` inherited unchanged from ratio-guardian §14, **`list_torrents()` replaces the original `list_tracked_paths()` sketch** (implemented in `nazgarr/adapters/torrent_client/base.py`, different from this early draft):

```python
class TorrentClientAdapter(ABC):
    def add_torrent(self, torrent_file_or_url, save_path, force_recheck=True) -> str: ...
    def get_torrent_status(self, info_hash) -> TorrentStatus: ...
    def list_torrents(self) -> list[ClientTorrentInfo]:
        """Every torrent known to the client, with its files (path_in_torrent + size).
        Needed to populate client_torrent/client_torrent_file (section 4), not just to
        know whether a path is tracked yes/no — orphan_torrent/ignored/seeding are
        derived from that afterwards, never computed by the adapter itself."""
```

A disk/torrents_rel_path can be associated with several configured clients at once (a common case: qBittorrent for one group of trackers, rutorrent for another, on the same disk) — indexing (`nazgarr/torrents/indexer.py`) therefore aggregates across every client enabled for that disk, never assuming a 1:1 relationship.

### Path mappings between a disk and a client (user report, 2026-10-03)

A client can see a disk elsewhere: in another container, or mounted only on a subfolder. Example: Nazgarr `/data/torrents` is qBittorrent `/downloads`. The disk–client link (`disk_torrent_client`) stores a pair, like the Remote Path Mappings of Sonarr/Radarr:

- `local_rel_path`: a folder of the disk (empty = the whole disk);
- `torrent_client_root_path`: how the client sees that folder.

`nazgarr/torrents/client_paths.py` translates both ways:

- **Client → Nazgarr**, when indexing. The client's path is compared lexically, never resolved: it does not exist in our filesystem.
- **Nazgarr → client**, for the save path of a torrent being added. A path outside the mapped folder raises `client_cannot_see_path`. The client never gets a torrent with a path it cannot see.

The old root-only override is the case with an empty folder. It is configured from the disks dialog of a client (UI) or with `nazgarr client link CLIENT DISK --folder … --client-root …`.

**Checking the paths** (user decision, 2026-10-05, after a setup retrospective: a wrong mapping gave no signal, and the client's torrents silently showed as orphans). "Check paths" on a client card (`POST /api/torrent-clients/{id}/path-check`, `nazgarr/torrents/path_check.py`, `nazgarr client check`) takes the largest file of every torrent, from the last indexing or from the client itself if it was never indexed, translates it with the current mapping and stats the disk: the file must be there, with its size, inside a seeding folder. Read only. It reports how many are fine, which have no disk, which point to a disk path without the file, and which sit outside the seeding folders, with examples. For the files not found it suggests a mapping: it strips leading folders of the client path until the rest exists, with the same size, in a seeding folder (or the root) of a disk; the stripped part is how the client sees that folder, and the most voted pair per disk is offered, applied with one click. The clients tour goes through it after the connection test.

Silent failures that became signals (same decision):

- every scan adds a problem for a client whose files were not found on the disks at all, or with more than 10% of its files pointing to a disk path where they are not (download in progress below that);
- `torrent_client_root_path` must be an absolute Unix path; it is trimmed and loses its trailing slash (`client_root_not_absolute`);
- a path outside the mapped folder, or outside the disk, always raises `client_cannot_see_path`, also with an empty folder: the client never gets an invented save path;
- the folder for new reseed hardlinks and the upload folder must exist, on the disk's filesystem, inside a seeding folder (`folder_not_in_seeding`), checked when saved instead of at execution;
- a reseed that fails on a coded error stores its English text with the parameters, not the bare code.

## 6. Content identification (TMDB) and the matching engine

### Media resolver (inherits ratio-guardian §6)

Default: filename parsing (guessit) → TMDB lookup. Optional Sonarr/Radarr adapter for more reliable mapping, never assumed present.

**Sonarr/Radarr adapter (implemented 2026-09-23, `nazgarr/integrations/arr.py`)**: read-only index built once per run from every enabled instance.
- **Identity:** it gives the identity of every file Radarr/Sonarr know (`tmdbId`, season/episode), used by `ArrResolver` before guessit + TMDB, which stays the fallback. It also works with no TMDB key for the files the *arr instances know.
- **Torrent of origin:** from the history, the torrent a file was imported from. The imported event links `importedPath`/`droppedPath` to a `downloadId`, and the grabbed event gives `guid` (for UNIT3D via Prowlarr, the download URL `…/torrent/download/<id>.<passkey>`) plus the info hash. The matching engine turns this into a single `source='history'` candidate: one `.torrent` download instead of a catalog search, with the same explicit confidence rules (size, then piece hash). It falls back to the catalog search when that candidate isn't plausible.
- **Path matching:** automatic, with no path-mapping config. The folder and file name plus the exact size must match, because *arr paths live in their own container namespace.
- **Posters:** Sonarr only exposes TVDB artwork, so a series' TMDB poster costs one `/tv/{id}` call per series, never a per-episode title search.
- **Season packs:** the imported events also map every file of a download (`droppedPath`) to the library file it became (`importedPath`), even after Sonarr renamed it. This is the most precise way to match a pack's episodes (see "Multi-file torrents" below). On the user's instance: 396 pack folders, 8838 files.

**Extension for the grid view**: at TMDB resolution time, download and cache the poster (`tmdb_poster_path` → local image, section 4). A `media_item` with no poster available (very niche content, or TMDB doesn't have it) shows a placeholder in the UI, never a blocking error.

### Multi-file torrents: season packs and releases with extras (implemented 2026-09-23, `nazgarr/torrents/layout.py`)

Every candidate is evaluated as a whole torrent, file by file. This replaces the Phase 4 scope reduction ("more than one file = confidence 0"), which also excluded every movie released with an `.nfo` or subtitles next to it.
- **The library scan records every file**, not only videos. Exclusions are a filter for views, counts and API lookups; an excluded file that is part of a torrent is still used to recreate it. Only videos get an identity and trigger matching.
- **Radarr/Sonarr webhooks** (user decision, 2026-10-06): instead of waiting for the next full scan, an import, upgrade, rename or file deletion updates only those files (`nazgarr/integrations/arr_webhooks.py`). Each Radarr/Sonarr instance gets its own webhook password (Settings › Integrations, shown once, regenerable); Radarr/Sonarr send it as the Basic auth password of the webhook connection to `POST /api/arr-hooks/{radarr|sonarr}/{id}`, outside Nazgarr's login. Events are queued in `arr_webhook_event` and applied by the scheduler every 15 s after 10 s of quiet, never during a run. The file is found under the media folders by trying the tail of Radarr's path (through `resolve_scoped`, never a symlink) with the same size: one stat and a partial hash, so only its disk wakes up. A new file joins the disk's latest scan, linked to the torrent it was hardlinked from (same inode) and identified with Radarr/Sonarr's data; a deleted or renamed one leaves the current state the scanner's way (back to an older scan number, never deleted). Searching the trackers right after an import is optional (`arr_webhook_search`, off). The full scan stays as the safety net; more frequent client re-indexing and optional inotify are possible next steps.
- **Default exclusion presets** (user decision, 2026-10-05): from the Plex ("Local Media Assets", "Local Files for Trailers and Extras") and Jellyfin naming docs. *Media server artwork and metadata* (on by default) excludes every image by extension (jpg, jpeg, png, tbn, webp), since Plex also takes artwork named like the video, numbered or as episode thumbnails, plus nfo, `theme.*`, `theme-music/` and `backdrops/`. *Extras* (off by default) covers the extras folders and suffixes of both (Featurettes, Behind The Scenes, `-trailer`, `-deleted`...). *macOS and Windows system files* (`._*`, `.DS_Store`, hidden volume folders, Thumbs.db) and *.torrent files* are on by default, also for who had already saved their presets (migration 9).
- **Matching a pack's videos to local files**, most precise first:
  1. Sonarr history (`droppedPath` → `importedPath`);
  2. on the torrent side, the original names under the same root as the orphan;
  3. guessit season/episode on the pack's file names plus exact size.

  Every file must be on the orphan's disk (hardlinks).
- **Extras** (nfo, subtitles, sample) match by name, or by extension plus exact size when renamed. Extras with no local file are left for the client to download after the recheck. The recheck counts as ok only if what is left fits `seed_job.expected_missing_bytes`: those extras plus two pieces each for shared boundary pieces. Anything else below 100% still fails.
- **Confidence:** the lowest of its videos (size → per-file mediainfo Unique ID → per-file piece hash, using the offsets from the `.torrent`, downloaded once per candidate). A pack with any video missing locally is `season_pack_partial`, confidence 0, never executed. User decision: a pack is only recreated whole; seasons collected episode by episode keep using single-episode torrents.
- **One evaluation per pack per run:** the other orphan episodes of the same pack reuse it, and a torrent already queued or running for another file never gets a second review. Every file of the torrent and its local match is stored in `candidate_file`, which the executor recreates from and the Reseeding page shows.

### Matching engine (inherits ratio-guardian §7-8, integrated with smartmediareseed)

Pipeline unchanged in structure (personal history if available → catalog search by tmdb_id → size match → mediainfo Unique ID match → explicit, explainable confidence, never opaque ML). See ratio-guardian SPEC.md §7-8 for every verified detail (UNIT3D API shape, season pack handling, the limits of personal history via scraping).

**New confidence signal**, a recommendation already written up in smartmediareseed's analysis and adopted here as a requirement: **piece hash verification** (BEP3 bencode parsing of the downloaded `.torrent`, byte-exact comparison against the local content) as an additional signal, stronger than mediainfo Unique ID alone because it's deterministic and not subject to the known limitation (same video, different audio → sometimes the same Unique ID). To be used to:
- raise confidence when size+mediainfo already agree but aren't absolutely certain;
- **the only signal acceptable for auto-executing the torrent→client direction** (section 3), where a higher threshold than the media→torrent case is needed, because there the file already exists and a wrong match adds a non-matching torrent in a way that's less recoverable with just a recheck.

**Full hash check (diagnosis):** matching verifies only a sample of pieces. From a candidate or an execution the user can run a check of 100% of the pieces (`nazgarr/reseed/full_check.py`), boundary pieces included. It reads the files where the client sees them first, then from the library, and reports per file the pieces that match, differ or can't be read, plus whether the info hash changed. It's read-only and runs in the background, one check at a time. It tells a different release apart from a wrong path when a recheck keeps failing, and never replaces the client's recheck.

**Full check before executing** (`verify_before_execute`, on by default, user decision): approving a review first runs the full check. Hardlinks and the add to the client happen only if every piece matches and every file is present at the right size. The only exception is extras missing locally, which the client downloads after its recheck. While the check runs, the review stays in the queue as "verifying", with progress shown in the notification. If the check fails, nothing is touched and the review stays in the queue with the reason. Automatic execution, when turned on, applies the same check. The setting can be turned off (Configuration > Matching & approval) to execute straight away on the sampled pieces.

**Exceptions to the recheck.** Uploads (user decision, 2026-09-30): the torrent of a new upload is created by Nazgarr from the very files it will seed, so it is added without the client's recheck, after checking that every file is at the seed path with its size and, once added, that the client reports that save path; otherwise a normal recheck. Reseeds (user decision, 2026-09-29): the opt-in setting `skip_client_recheck_when_verified` is off by default and requires the full check before executing. With it on, a torrent is added with `skip_checking` only when Nazgarr's full check just verified 100% of its pieces, no extra is missing locally (otherwise the client has to download it) and the info hash matches. The seed job records `recheck_skipped`. The known risk is the short window between the check and the add. **A forced recheck on the client remains mandatory in every case** when adding to the client (never `skip_checking`) — the piece hash is a stronger matching signal, not a substitute for the client's own verification.

## 7. Library view: tree + poster grid

Two display modes over the same underlying data (the unified per-file state, section 3), user-selectable, inspired respectively by Auditorr (tree) and the explicit request for a poster grid:

- **Tree view** (revised 2026-09-23, superseding the original "one tree + detail panel" sketch): **two separate, symmetric views** built on the same component — "Media files" (the tree under `disk.media_rel_path`, from `GET /api/media-files`) and "Torrent files" (the tree under `disk.torrents_rel_path`, from `GET /api/seed-files`). Same information, different dataset. Each one is summary cards per state (count + total size) on top, a toolbar (state tabs, filename/path search, min/max size in GB, show excluded, and — Media files only — a Duplicates filter backed by `GET /api/library/duplicates`), and a real table (indented name with folder chevrons, size aggregated recursively on folders, state badge, hardlink info on hover). Filtering is client-side over the full file list. Still deferred: per-file tracker + tracker filter, seed count, "Open in Radarr", a flat view, copy paths / CSV export, inline mini-posters.
- **Grid view**: a card per `media_item` with a TMDB poster (placeholder fallback), title, year, status badge. Meant for quick visual scanning ("what do I have, what's missing, what's broken") rather than technical detail — that stays one click away (the same detail panel as the tree view).
- **Filters shared between both views**: by state (every state from section 3), by disk, by MediaPath/content_type, text search by title.
- **Reverse lookup from the torrent side** (inspired by Auditorr): given a torrent in the client, show which library file(s) it corresponds to (via hardlink) — useful to understand "why is this seeding" without having to search manually.

Both views share the same backend/API — it's just `?view=tree|grid` over the same filtered resource, never two separate data pipelines.

### Tracker filter (user decisions, 2026-09-30)

A global selector in the top bar (dashboard, library and torrent views) shows the seeding status for every torrent ("All trackers", as always), only for the configured trackers (a public tracker doesn't count), or for one tracker. A torrent belongs to a tracker when the host of its announce URL matches the tracker's announce or base URL (`nazgarr/torrents/tracker_scope.py`). With a filter, a library file is seeding only if it's in a torrent of that tracker; torrents on disabled clients don't count at all; the torrent view shows only that tracker's torrents plus the orphans (in no torrent), which can become an upload for any tracker. The choice is a setting (`tracker_filter`), so it stays as the default. The dashboard's current numbers follow the filter; its history is kept per filter from the first scan after the filter existed (`tracker_health_snapshot`), while "All trackers" keeps the global history on `run_log`.

## 8. Reseeding engine and execution

Inherits ratio-guardian §9-11 in full:
- A configurable confidence threshold per direction (defaults 0.95 media→torrent, 0.98 torrent→client, see the note in section 6). A match above its threshold is **recommended** in the review queue, not executed. **Nothing that modifies files or the torrent client runs without the user's explicit approval** (user decision, 2026-09-23, after a run auto-executed 20 reviews unasked). Automatic execution of recommended matches is an opt-in setting (`auto_execute_above_threshold`, Configuration → Mapping), off by default.
- Hardlink with the exact name the tracker expects (media→torrent direction only — in the torrent→client direction the file is already in the right place, only the torrent gets added to the client).
- Forced recheck, never skipped.
- Periodic reconciliation of recheck status (async on the client).
- Bulk import mode (one-off full scan) + scheduled run (cron configurable from the UI), both on the same engine.

## 9. Upload: creating and publishing a new torrent

Inherits ratio-guardian §16 in full, including the reasoning on what to reuse from Upload-Assistant (the `torf` library to create the `.torrent`, extended `pymediainfo`, `ffmpeg-python` for screenshots, dupe-check via the same `TrackerAdapter.search_by_tmdb`) and what not to reuse (no code taken directly from Upload-Assistant, which is in development freeze — only a domain reference for the shape of UNIT3D requests and tracker profiles).

Points that stay unchanged:
- Separate data domain (`upload_job`, `tracker_upload_profile`), never touches the reseeding entities.
- Tracker profiles bundled as seed data versioned in the repo, copied into the DB when a tracker is created, freely editable afterwards and never re-read from the file.
- Mandatory human confirmation before submission, as non-negotiable as the forced recheck in reseeding.
- v1 already includes mediainfo + screenshots (not deferred).

Implemented in Fase 6. Two things found while implementing, not in the original design: `tracker.announce_url` was missing from the schema entirely (`base_url` is the API host, `torf` needs the distinct personal announce URL to create a valid `.torrent`) — added as a nullable column, same additive-migration pattern as every other schema gap found mid-implementation. And `type_id` resolution from a filename is reliable only for `category_id`/`resolution_id` (content type, `screen_size`) — the REMUX/ENCODE/WEBDL/BDMUX distinction is too convention-dependent for a generic guess, so it stays an explicit best-effort default, always editable on the `upload_job` before confirmation, exactly as this section already specified ("resolved, editable before submission").

### Upload flow v2 (user decisions, 2026-09-29 — supersedes the single-file/single-tracker pipeline above)

The Phase 6 pipeline handled one file towards one tracker, synchronously inside the HTTP request, with a hand-typed TMDB id. v2 redesigns the flow around what the user actually does:

- **What can be uploaded**: a single movie, a single episode, a season folder as a *season pack*, a whole-series folder as a *complete pack*. Selection happens in a drawer that reuses the folder view and lets the user pick either a file or a folder.
- **One job, many trackers**: `upload_job` is the source (file or folder), its confirmed identity (TMDB/IMDB/TVDB/MAL ids, season(s)), the user's overrides, and the mediainfo/screenshots, all produced **once**. `upload_target` is one row per selected tracker, with its own action (`upload` / `reseed` / `skip`), proposed and approved release name, flags (anonymous, personal release, internal, stream), dupe-check results, `.torrent`, remote id and outcome. This is what makes "reseed on one tracker, upload on the other" possible. `upload_event` is an append-only log of every step, used both for live monitoring and for the history drawer.
- **Tracker selection**: every tracker with an upload profile is preselected; the generated torrent goes to the client configured on that tracker (`tracker.torrent_client_id`, first enabled client when null), the same rule reseeding follows.
- **One `.torrent` per tracker** (user decision): each tracker gets its own file (its announce URL and `source` field, hence a different info hash). The piece hashes are computed once and reused for every tracker's file.
- **State machine persisted in the DB, driven by an in-process worker** (same single-worker thread pattern as `nazgarr/reseed/full_check.py`, but with state in the DB so a restart resumes the queue instead of losing it). The frontend polls, like runs and full checks.
- **Two human gates, and only two**: (1) confirming the metadata match (and the season, for series); (2) approving, per tracker, the action and the release name. Gate (2) **is** the mandatory human confirmation of this section: once both gates are passed the worker runs to completion on its own, which is what makes a queue of several uploads possible (user decision, 2026-09-29).
- **Scheduled uploads** (user request, 2026-10-09; `upload_job.scheduled_at`): at gate (2) the upload can start at a chosen time instead of as soon as it is its turn. It stays queued until then, the other uploads go ahead, and a scheduler tick every 30 seconds wakes the queue when the time comes; a time already gone means now. While it waits it can start now or move to another time (web UI, `nazgarr upload schedule`, `--at` on `upload new`/`continue`). Since hours or days may pass after the decision, a scheduled upload runs each tracker's dupe check again before anything else: a torrent that showed up in the meantime (identical or in the same slot, and not there at the decision) stops the upload to that tracker (`upload_dupe_appeared`). Reseeds don't need it.
- **Identification**: the resolver returns a list of candidates (not just the first hit) shown as posters with the title overlaid and a Movie/Series badge, with a detail panel on click. Forced ids go through TMDB's external-id lookup (`/find`, which covers IMDB and TVDB). For series the detected season is shown with the episodes found against the ones TMDB expects; an ambiguous season gets a picker. Posters of candidates not in the library are served through a backend proxy (the TMDB key stays server-side).
- **Analysis before the trackers**: size and file list compared with the torrents on every client; Radarr/Sonarr history consulted when configured — a file that the history says was grabbed from a tracker is flagged loudly (it's someone else's release: a reseed is probably what the user wants, not an upload).
- **Dupe check per tracker**: the tracker's results filtered on the dimensions that actually make two releases the same slot (resolution, HDR/DV, source, remux vs not, season/episode, byte-identical size), the same dimensions Upload-Assistant's `dupe_checking.py` uses — reimplemented, not copied. A strong match proposes a full piece-hash check (`nazgarr/reseed/full_check.py`); a passed check turns that tracker's suggested action into `reseed`, executed through the existing reseed path (forced recheck, as always).
- **Release names**: values read from the MediaInfo (resolution, video codec, HDR/DV, every audio track and its languages, subtitles) and from the release name (type, source, service, edition, repack, group), with the user's overrides on top. The release name used is the one of the torrent already seeding the same files (hardlink), else the original name Radarr/Sonarr recorded, else the source name. The variables are the same for every tracker (`nazgarr/upload/naming.py` VARIABLES: title and local title, year, season/episode, edition, repack, resolution, source, type, service, video codec, HDR, bit depth, main audio track split into codec/channels/Atmos, every audio track, audio and subtitle languages, subs label, group); each tracker's **naming rules** (`naming_rules_json`) only order and format them, edited in the tracker's upload profile with one input per release type, the variables as insertable chips and a live preview: a main template, an optional one for series (e.g. without the year) and optional ones per release type, plus a few options (local title in the tracker's language, all audio tracks or only the main one, how to write languages, SDR label, separator). Previewed and editable before gate (2). **Upscales are editions** (user decision, 2026-10-05): "AI Upscaled", "AI.Upscale" and "Upscaled" are read as an edition, next to the ones guessit knows, and go wherever a tracker's template puts `{edition}`. The template decides: the generic default and the bundled ITT templates (movies and series) have it.
- **Freeleech** (user decision, 2026-09-30): each profile lists the freeleech percentages the tracker lets uploaders set (UNIT3D `free`), with a preselected one; chosen per tracker when creating the upload and again at gate (2). None listed = no freeleech choice for that tracker.
- **Bundled profile by address** (user decision, 2026-10-03): a tracker created at the address of a bundled profile (same host, with or without `www.`) gets that upload profile right away; the preset picked in the dialog, or `upload_profile` in the API (`auto`, a bundled key, `none`), decides otherwise. Inside the profile, **Restore** replaces everything (ids, naming rules, description, defaults) with a bundled profile: the one it came from, or any other for a custom profile, after a confirmation.
- **Video codec label by release type** (user decision, 2026-10-03): one table for MediaInfo and for the name alone. Remux and disc AVC/HEVC, untouched web and TV (WEB-DL, WEBMux, DLMux, HDTV) H.264/H.265, encodes (encode, WEBRip, BRRip, DVDRip) x264/x265. The encoder found in MediaInfo only tells whether a file is an encode, never the label. The label follows the type even when the type is corrected by hand; a profile can rewrite it (`video_codecs`, e.g. `H.265: H265`).
- **Naming rules are versioned** (user decision, 2026-09-30, the one exception to "bundled profiles are never re-read"): a bundled profile's `naming` block has a `version`. At startup a higher version in the code replaces the rules of a profile the user hasn't edited; an edited profile is only offered the update (a banner with "use the new rules"), unless the bundled version says `force: true`. Category/type/resolution ids are never touched.
- **Overrides without overwhelming the user**: only the four ids are always visible. A collapsed "Detected details" panel lists category, type, source, resolution, season/episode, year, edition/repack, group, service, each showing the detected value as its placeholder so the user only touches what's wrong. An "Advanced" section keeps a handful more (naming toggles, screenshot count, custom description, don't seed, skip dupe check). The long tail of Upload-Assistant's ~115 CLI arguments is deliberately left out of v1.
- **History**: queue and history tabs; each row shows poster, title, kind and one badge per tracker outcome, with a drawer for the event timeline, names used, links, mediainfo, screenshots, description and, on failure, what went wrong.
- **Where uploads seed** (user decision, 2026-09-30): hardlinks in a per-disk folder for uploads (`disk.upload_rel_path`, set like the folder for new reseeding hardlinks), falling back to the seeding folder (`torrents_rel_path`) when not set. A source already inside the seeding folder seeds in place. A reseed decided in the upload flow places the tracker's torrent the same way, with the tracker's own file names. Same filesystem or an explicit error, like reseeding; always a forced recheck.
- **Existing `upload_job` rows are dropped** when the new tables are created (user decision: no real upload data to keep).


**Releases from a watched folder** (user decisions, 2026-10-02). Each disk can have a watched folder (`disk.watch_rel_path`, `nazgarr/upload/watch.py`). It can never be the disk itself, nor contain the media, torrent or upload folder; a dedicated subfolder inside the torrent folder (e.g. `torrents/watch`) is fine.

- **Starting:** every new video file or folder put there starts an upload on its own, to every tracker that has an upload profile, as soon as nobody writes to it any more (user decision, 2026-10-02). That means: no partial files inside, no write for 15 seconds, and the same size as at the previous check. Checks run every 10 seconds, not through filesystem events: inotify isn't reliable on Unraid FUSE shares, NFS or SMB. A file moved in starts at the next check; a copy starts 15–25 seconds after it ends.
- **Once only:** `watch_entry` remembers each entry, so a release starts one upload even after its job is deleted. The folder is only a way in, so what is already inside when it is chosen starts too.
- **Episode orderings** (user decisions, 2026-10-04): a series can number its episodes in several ways: TMDB default seasons, TMDB episode groups (digital, DVD, absolute…), TVDB's aired order (what Sonarr reads) and TVDB's other season types. Release files follow whichever one their publisher picked.
  - **Model.** `nazgarr/library/episode_orders.py` turns each ordering into the same shape: seasons of episodes, each pointing to one or more reference episodes (TMDB default numbering). The mapping is many-to-many, so an episode aired two at a time (two titles, half the count) points to two references, and the reverse. TMDB groups carry the reference numbers; Sonarr and TVDB are aligned one to one when the counts match, in pairs when one is double the other, else by air date.
  - **Sources.** TMDB default and groups (with the TMDB key); Sonarr's episode list when the series is there; TVDB's API (`tvdb_api_key`) only as the last resort, when Sonarr doesn't have the series or the files don't follow its order (another TVDB order, e.g. "Joined", may cover them). Ties go to the series' last choice, then TVDB aired, then the other TVDB orders, then TMDB.
  - **At the first gate of an upload** (`GET /api/uploads/{id}/episode-orders`): Nazgarr scores how well the files fit each ordering, season by season and by number, never by position. For a pack or a library the number of episodes in each season comes first (75%), then whether the files' numbers exist in the ordering (25%); for a single episode only the numbers count. Separately, the ordering in which the files' numbers exist (ties broken by the season shape) is the numbering the files are translated from: a library numbered by Sonarr can follow TVDB aired in its numbers (three segments per file, the first one recorded) while its files are the episodes of another ordering. The best fit is selected by default; ties go to the ordering last chosen for the series, then TVDB aired, then TMDB. Without episodes in the files, that same priority applies. The user can always change it. A warning appears only when the files do not follow TVDB aired, Sonarr's order, and only if TVDB aired is known (from Sonarr or the TVDB key), with a shortcut to switch to it. The season picker, the episode counts and a file-to-episode preview follow the selected ordering.
  - **After the match.** The chosen ordering is saved on the job (`episode_order`, plus the data to translate numbers) and as the series' preference. Its numbering is used everywhere after the match: release name, generated file names (files numbered after another ordering are renamed into it, a merged episode becomes `E03E04`) and the tracker's season and episode fields. An automatic match uses the best fit too, and logs a warning when it is not TVDB aired.
  - **Reseeding.** When a pack's files can't be found in the library under their own numbers, the matching engine translates them. It works out the torrent's ordering (best fit of its episodes) and the library's (best fit of the library's episodes, usually Sonarr's), then maps each episode through the references (`map_media_side`, `Translator`). Only one-to-one translations onto an existing library episode count: a file holding two merged episodes never becomes a single library file. The orderings of a series are collected once per run and cached for an hour.
  - **Library.** The series sheet offers the same orderings, scored like a pack (13 files out of 13 beat 13 out of 38, e.g. TVDB "Joined" against aired segments) (`GET /api/library/items/tv/{tmdb_id}/episode-orders`). By default it shows the files' own numbering, with the episode titles; picking another ordering regroups seasons and renumbers episodes, and each season shows how many episodes that ordering expects. A note appears when the library doesn't follow TVDB aired.
  - **Not yet.** The upload dupe check doesn't translate the seasons of torrents already on a tracker: their ordering can't be told from the name alone.
- **Resuming a cancelled upload** (user decision, 2026-10-03): a cancelled upload resumes from where it stopped (`POST /api/uploads/{id}/resume`, Resume in the upload page and the history, `nazgarr upload resume`). At an approval gate it comes back to that gate with the choices already made. Identification or analysis start again. A cancelled execution goes back to the queue with only the trackers not yet done: a tracker already uploaded is never uploaded again, and with nothing left the resume is refused. The state at cancellation is stored in the `job_cancelled` event; older jobs infer it from their data. A source that no longer exists cannot be resumed.
- **Specials** (user decision, 2026-10-03): season 0 can always be picked in the match step, listed last as "Specials". A `Specials` folder (the Plex, Jellyfin and Sonarr convention) is season 0 for the episodes inside it.
- **Built-in notification services** (user decision, 2026-10-03): Discord (a channel webhook, sent as an embed coloured by level, mentions disabled) and Telegram (a bot token and a chat id, with an optional topic id, HTML-escaped text). They use the plugin notification contract. The webhook URL and the bot token are secrets: they never appear in error messages.
- **Notification services are instances** (user decision, 2026-10-05): any number per type (a Telegram bot and a Telegram channel, two Discord channels...), each a row of `notification_service` with a name, its encrypted configuration, its events and a message format (`standard` only for now: the column is there so a service can later get a different text, such as a compact one, without a migration). Settings > Extensions > Notifications lists them together with the webhooks: an "Add" button picks the type (built-in, plugin or webhook), the configuration is edited in a dialog that can send a test before saving, and each instance is a card with the type's icon and a recap of its configuration (secrets only as "set"). Deleting an instance deletes its deliveries, including the queued ones.
- **Notices:** each release triggers a notice in the app wherever you are (`GET /api/uploads/notices`), once when it is detected and once when it is ready for your decision, with a button that opens the upload. The events `upload.detected` and `upload.ready` go to webhooks and notification services.
- **Releaser name:** the setting `upload_releaser_name` becomes the `{group}` override of these jobs, and stays editable. On any other upload it fills `{group}` when the release name has none (user decision, 2026-10-03).
- **Automatic match:** TMDB candidates now carry a confidence (`nazgarr/upload/match_score.py`). The file name is also compared with the titles in the job's tracker languages: one extra TMDB search per language, which also finds the candidates that only the translated title reveals. Every upload, by hand or from the watched folder, confirms its best candidate on its own when the confidence is at least `upload_auto_match_threshold` (default 0.9; 0 turns it off). Ambiguous candidates (two titles nearly as sure) are never confirmed on their own.
- **Where it stops:** the job goes as far as the decision and waits there. Nothing is uploaded, linked or added to a client without approval.
- **A way in, not a home:** the watched folder is only a way in. A release from it always seeds from the upload folder, hardlinked there before publishing, also with the original names (user decision, 2026-10-05: names untouched, folder changed; before, original names seeded in place when the watched folder sat inside the torrent or upload folder, and the release stayed there). Once at least one tracker succeeds, the original in the watched folder is removed, file by file, only where this job put the seeding copy (same inode, another path: `_remember_seeded`, `_clear_watch_source`). Before, any second link was enough, so a file already hardlinked elsewhere could be deleted while it was the one seeding. With "don't seed", or when nothing succeeded, it stays where it was.
- **Rollback:** "Change match" (`back_to_match`) takes a job from the decision back to the match and redoes the analysis.
- **Incomplete seasons become episodes** (user decisions, 2026-10-09; setting `upload_watch_split_incomplete`, on by default; `nazgarr/upload/split.py`). A season pack with fewer episodes than its season has, in the numbering chosen at the match (e.g. the first 2 of 8 released together), is not uploaded as a pack but as one upload per episode, like episodes released one at a time. That holds even with a single episode missing.
  - **From the watched folder:** it happens on its own, right after the automatic match. Each episode gets the same trackers (and freeleech), match and episode ordering, goes through the analysis and stops at the decision as always. The pack's upload is closed (cancelled, listing the new ones, never resumed, no "upload finished" event).
  - **By hand:** at the match, an incomplete season pack offers "Split into N episodes". Hand-picked packs are never split.
  - **The leftovers:** once no episode to upload is left in the release's folder of the watched folder (the last one moved to the upload folder after seeding), the folder is removed with whatever is left (nfo, sample). A video still there (another episode not uploaded yet, or cancelled) or a file still being written keeps it.

**The media folder is optional** (user decision, 2026-10-02): Nazgarr can also be used only to scan torrents and to upload releases, with no media folder at all. The storage step only needs the torrent folder. Without a library there is no health: the Dashboard says so instead of showing 100%, and scans store no health point, while the torrent numbers keep their trend.
- **Language check:** each tracker with a language gets a warning when no audio track (commentary excluded) is in that language.
- **Remux detection** (user decision, 2026-10-02): "VU" or "UNTOUCHED" in the name means REMUX, since guessit doesn't know them; so does a joined "BDRemux", "UHDRemux" or "DVDRemux" (common in Italian releases), which guessit only reads when separated, and it also gives the source. A disc source (BluRay, HD DVD, DVD) whose video carries no encoder trace (no x264/x265 writing library, no encoding settings) is a REMUX too, even if the name doesn't say so. A manual choice still wins.
- **Hybrid and remux source** (user decision, 2026-10-02): a REMUX whose Dolby Vision is profile 8 is HYBRID, since a pure remux has profile 7 or 5; so is a name that already says HYBRID. It is available as the `{hybrid}` variable and is editable in the detected details. At ITT a hybrid remux is "HYBRID REMUX", without VU (`hybrid_type_labels`). A REMUX with no source in its name is BluRay, or DVD at standard definition.
- **Disc source from MediaInfo** (user decision, 2026-10-05): when the name gives no source (a renamed file), MediaInfo can: the video's "Original source medium" (written by MakeMKV on every track it extracts, e.g. Blu-ray, HD DVD, DVD-Video) names the disc on its own; Dolby Vision profile 7 with an enhancement layer (UHD Blu-ray only) or VC-1 video say disc on their own; lossless audio (TrueHD, DTS-HD MA, LPCM) with PGS subtitles only when the video is also an encode or has a remux-level bitrate (40 Mb/s at 2160p, 18 at 1080p), since a DLMux/WEBMux takes audio and subtitles from a disc over web video. The source is then BluRay (DVD at standard definition), and the remux rule above applies, requiring the video itself to look like the disc's (DV dual layer, VC-1 or the bitrate). The release's `Movie name` tag is not used: encodes often keep their source's title. Why the type and source are what they are (`analysis.type_basis`) opens from the type tag in the detected details.
- **Automatic rename** (setting `upload_auto_rename`, on by default): the default file names are those of the hardlinked torrent if there is one; otherwise the pattern is used for library files and watched-folder releases. Off: every upload starts with its original names, and each upload can still pick another option.
- **Single file instead of a one-file folder** (user decision, 2026-10-02; setting `upload_single_file`, off by default): when the only file going into the torrent sits in a folder, the torrent is just that file. Samples and system files never enter the torrent, so they don't count; an nfo does. `upload_single_file_folder` decides where it seeds. `keep` (default) puts it inside its folder in the upload folder, with the client pointing there. `remove` puts it directly in the upload folder. A source already in the seeding folder (not from the watched folder) always seeds in place, inside its folder, with no new hardlink. In both modes the watched folder still empties as usual (`nazgarr/upload/file_names.py::_as_single_file`, `nazgarr/upload/execute.py::prepare_content`).
- **MakeMKV title suffix** (user report, 2026-10-03): MakeMKV names its files `Title_t00.mkv`, `_t01`, … (one per disc title). guessit reads the suffix as part of the title ("Title t00") or as the episode title, and the TMDB match goes wrong. Every guessit call on a local file name goes through `nazgarr/library/guess.py`, which strips `[_ ]tNN` at the end of the name first. That covers upload identification, the library resolver, Triage, torrent layout, packs and the naming detection.
- **Profile default for "internal"** (2026-10-03): `tracker_upload_profile.default_internal`, next to anonymous and personal release. It is the starting value of the flag on each upload, which can still change it.
- **Dolby Vision from the stream** (user decision, 2026-10-03): MediaInfo detects Dolby Vision only from the configuration record declared by the container (`dvcC`/`dvvC`). A file whose stream carries the RPU without that record shows up as plain HDR10/HDR10+ (MediaArea/MediaInfo #1312), so the name would lose `DV.Px` and HYBRID.
  - **The fallback** (`nazgarr/upload/dovi_probe.py`): when MediaInfo saw no Dolby Vision and the video can carry it (HEVC/AVC/AV1, 10 bit or more), ffprobe reads the first 16 packets. Reading only the first packet would miss MP4 files with B-frames. Any RPU found is added to the summary in MediaInfo's own form.
  - **The profile** comes from the configuration record when ffmpeg exposes it. Otherwise it is estimated from the RPU the way dovi_tool does: RPU profile 0 is 5; RPU profile 1 is 8 without residual and 7 with it. Anything else stays plain "DV".
  - **What the user sees:** the analysis logs a warning event. The text MediaInfo produces for the tracker is not changed.
  - **The image** ships both ffmpeg and ffprobe as static binaries, with no Debian ffmpeg and no system mediainfo, since pymediainfo bundles libmediainfo.
  - **Verified** with dovi_tool-generated P8.1 and P5 streams in MP4 and MKV without `dvcC`.
- **Packs picked by hand** (user decision, 2026-10-02): the user can make a season pack or a complete pack out of episodes downloaded one at a time. It works even when each episode already seeds with its own torrent, and even without a media folder.
  - **Picking:** "Pick for a pack" in the series sheet (library) and in the folder views (media and torrents). It shows checkboxes on the videos, plus one per season or per folder. Clicking "Create pack" opens the new upload with the list, passed in the navigation state.
  - **The source:** the job stores `pack_json` (`nazgarr/upload/pack.py`): the disk-relative files, each one checked again through `resolve_scoped` on every use, plus a name built from an episode name (`S01E01` → `S01`, `S01-S03` for a complete pack). There must be at least two videos, all on the same disk, with distinct file names and no symlinks. Subtitles next to an episode with its name are included automatically. `source_path` is only the files' common folder, and nothing ever walks it.
  - **The torrent:** it is always built from hardlinks: a new folder in the upload folder, with `Season NN/` subfolders for a complete pack. The episode torrents are left untouched. File names are generated by default, or the original names are kept inside the new folder.
  - **The analysis:** "already seeding here" now needs a client torrent that hardlinks every video, not just one. The name source for detection is the episode name turned into the season name.
  - **Mixed packs:** if resolution, video codec, HDR, audio codec, audio languages, source or group differ between the episodes, `analysis.pack_mixed` lists them. Uploads stay blocked until the user confirms (`pack_mixed_confirmed` override). Reseeds are not blocked.

## 10. UI/UX — general structure

```
Library
  Tree view               (§7)
  Grid view                (§7)
  Orphaned and ignored     [n]  (§3 — both directions, with contextual actions)

Reseeding
  Dashboard
  Review              [n]  (pending match_review, both directions)
  Verify from .torrent
  Runs

Upload
  New upload
  Upload queue        [n]
  Description templates

Configuration
  Disks
  Torrent clients          (multi-client, §5)
  Trackers
  Settings
```

Dashboard: inherits ratio-guardian §15 (library health gauge, pending review/failed/unresolved KPIs, novelty feed) — **additional KPIs** to reflect the two directions: an `orphan_torrent` count and an `ignored` count, each linking directly to the matching filter in Library. Health gauge formula settled in Fase 5 (`nazgarr/library/health.py`): a single explicit, size-weighted ratio (seeding media size / total media size), not Auditorr's multi-factor weighted score (hardlink/orphan/not-imported/duplicates, each independently weighted) — the other KPIs already surface those signals individually and clickably, so folding them again into one composite number would lose clarity rather than add it. Layout (revised 2026-09-29, Auditorr-inspired, user decision): a 7d/30d/90d/All window drives the health ring and its history. The health is still the single hardlink ratio above: no weighted score. Four cards (hardlinked media, orphaned torrents, not imported, duplicates) show value, size and trend against the previous scan (`run_log` keeps their sizes), plus a link to the filtered view. The **Not imported** view lists seeding torrents with no hardlink in the library (per-file state `ignored`, shown as "not imported"), one row per torrent with a reason. The reasons are superseded, copy, removed, never imported and extras only (`nazgarr/library/not_imported.py`). Without any media folder there is no library to be imported into, so nothing is classified and the view explains why (user decision, 2026-10-05). Storage also warns that without a media folder reseeding finds nothing: content is recognized only from library files today (recognizing seeded files directly is future work). The one action in the view (user decision, 2026-10-03) is removing a torrent from its client with its files, never from the library. A button appears on rows with a green "OK", and a confirmation dialog explains that the torrent leaves the client and its files are deleted from the disk, with any remaining warnings. The server checks everything again (`POST /api/torrents/not-imported/{id}/remove`, `delete_files` must be true): the torrent must be among the not imported, its requirement met, and no warning may block it (shared files, a client error, a check or a download in progress). Then it asks the client to remove the torrent with its data, and drops the torrent from the index straight away. A **last seeder** warning (user decision, 2026-10-03) appears when the tracker's scrape says the swarm has a single seeder, us. The count comes from the client (`swarm_seeders`: qBittorrent and qui `num_complete`, Deluge `total_seeds`, Transmission the highest `seederCount`; rTorrent doesn't report it). The warning doesn't block removal, but it takes the torrent out of "safe to remove" and shows in the confirmation. Each tracker can set an optional seeding requirement (minimum seed time and/or minimum ratio; with both, either one is enough unless the tracker is set to require both — user decision, 2026-10-01): the view marks the torrents that already met their tracker's requirement as safe to remove, with what is still missing for the others (`nazgarr/torrents/seed_requirements.py`). Renamed **Triage** (user decision, 2026-10-02), reached from the Torrent sidebar entry with a File | Triage switch at the top of the page; Library likewise has a single sidebar entry with its Folder | Poster switch. The removable column shows a green "OK" when the requirement is met. Other issues sit in a popover next to it: files shared with other client torrents, a client error, a check in progress, or a download in progress (`removal_warnings`, `nazgarr/api/torrents.py`). When there is one, the "OK" turns yellow and the torrent is no longer counted as safe to remove. The novelty feed is **"Changes since last scan"** (`GET /api/dashboard/changes`, `nazgarr/library/file_changes.py`), a per-file diff in the style of Auditorr and a user decision that replaces the earlier "latest candidates" feed. At the end of each scan where every disk and every client completed, each file's state (media and torrent side) is compared with the previous snapshot. The diff lists new and removed media/torrent files, plus state changes: now seeding, now orphaned, now ignored, stopped and resumed. The first scan only takes the baseline, and excluded files never produce a change.

Frontend stack: **React SPA + shadcn/ui** (a decision already made in ratio-guardian on 2026-09-21, inherited here from the start instead of as a later refactor — Nazgarr already starts with a FastAPI backend as a pure JSON API under `/api/*`, no Jinja2/HTMX phase to outgrow). Fase 8 (in corso): scaffold Vite + React + TypeScript + Tailwind + shadcn/ui in `frontend/`, tipi TS generati dallo schema OpenAPI di FastAPI (`openapi-typescript`, mai duplicati a mano), servito in produzione dallo stesso container FastAPI (`nazgarr/web/frontend.py`, nessun processo Node separato) — sidebar di navigazione con la struttura sopra già in piedi, le singole pagine arrivano per sotto-fasi successive.

**Updates and release notes** (user decisions, 2026-10-05): Nazgarr never updates itself (Docker, Unraid or pipx do), so there is no update button. Checking for updates stays a manual button, plus an **automatic check every 12 hours, off by default** (`update_check_auto`: it contacts GitHub without the user asking), presented by the "rest of Nazgarr" tour; the last result is kept (`update_check_last`) and the sidebar shows when a newer version is out. Release notes come in two forms: the detailed `CHANGELOG.md` in the repo (its section is copied into the GitHub Release when a version is promoted to stable) and a short `nazgarr/release_notes.json` shipped with the app, one entry per stable version with highlights and breaking changes in Italian and English. After an update the app shows the entries newer than the last version seen (`release_notes_seen`; a fresh install starts from its own version); before one, the update check reads the file at the new version's tag and shows its notes, breaking changes first. Notes are not written by hand: **before every promotion to stable, Claude proposes the CHANGELOG section and the summary entry and the user approves them**; nightly and dev builds have no notes of their own.

**First-access tour** (user decisions, 2026-10-02): an interactive guided setup starts by itself at the first access, when no tour was ever seen and no disk exists. A welcome dialog asks two questions (uploads? Radarr/Sonarr?) that tailor the path. A "Getting started" checklist on the Dashboard follows the real configuration (`GET /api/system/setup-status`, `nazgarr/library/setup_status.py`): a step completes however it was configured. Optional steps with sensible defaults (exclusions, reseeding thresholds) complete once seen. Each step has a guided tour (`frontend/src/onboarding`, driver.js — MIT; Shepherd was set aside because it is AGPL-3.0). The tour moves between screens, highlights one field at a time and waits for the user's save. It steps aside while another dialog or list is open. Anchors are `data-tour` attributes, checked by a test. After the first scan, a tour of the views follows on its own: Dashboard, Library, Not imported and the review queue. It can also be started from the finished checklist and from Settings › Application. The tone is neutral, with at most one light fantasy wink per step and never a name or a literal quote. The progress is stored in `app_settings` (`onboarding_state`), and the tour can be restarted from Settings › Application. **"The rest of Nazgarr"** (user decision, 2026-10-05): a tour that only makes the other features known, never waiting for an action: uploads, notifications, other instances and their switcher, API keys, plugins, the automatic update check. It follows the tour of the views, is an optional checklist step completed once seen, and can be started from Settings › Application. Without uploads at the welcome it keeps one short step saying they exist. The two decision moments of an upload are shown on an **example upload** (`/upload/demo`, `frontend/src/lib/uploadDemo.ts`): the real match and decision screens, fed by `uploadDemo.json` (generated from the real API by `tests/test_upload_demo.py`, which also checks it keeps the API's shape). While the page is open, every request about upload 0 (no real job has that id) and about metadata is answered in the browser: nothing is created, searched on TMDB or sent to a tracker or client, and confirming or approving only moves the example on.

**Phones and tablets** (user decision, 2026-10-05): rarely used but supported. Below 1024px the sidebar is an off-canvas sheet that closes on navigation, and the settings sections become a sticky select. On touch (`pointer-coarse:`) buttons and list items grow and fields use a 16px font (no iOS zoom), with no change on desktop. Tables keep their main column readable: secondary columns hide below `sm`/`lg` and their values move under the name, or the table becomes a stacked list. What lived only in a `title` or behind a right click is reachable by tap: `InfoPopover`, and a "⋯" button (`RowMenuButton`) with the same items as the context menu. The floating panels and toasts never cover the page's last actions. Deleting a client, tracker, disk, folder, Radarr/Sonarr or Nazgarr instance, or an upload profile asks for confirmation everywhere (`ConfirmButton`), desktop included.

**Install without Docker** (user decision, 2026-10-02): every release also ships a Python package, installable with pipx. It is a wheel with the web UI already built inside (`scripts/build_package.sh`). It is built for stable releases only, attached to the GitHub Release when a version is promoted (`promote-stable.yml`), and never for nightlies, so neither Docker nor Node is needed at runtime: only Python 3.12+, `mediainfo` and `ffmpeg`. The `nazgarr` command (`nazgarr/cli.py`) does what entrypoint and supervisord do in the container:

- `init` writes `config.yaml` and a Fernet secret key (0600) to the platform's config folder;
- `serve` always runs a single process;
- `install-service` writes a systemd user unit (Linux) or a launchd agent (macOS), and only prints the commands to enable it;
- `version`.

Without Docker there is no volume mapping: `disk_scan_root` is the real folder the disks are under. The package's dependencies are the ones in `requirements.in`, minus `supervisor`, and a test keeps them equal. Windows is not tested yet.

### More instances in one web UI (user decisions, 2026-10-03)

One Nazgarr can open others: Configuration › Instances lists them (name, address, one of their API keys, encrypted at rest; `remote_instance`, `nazgarr/integrations/instances.py`).

- **Switching.** A switcher at the top of the sidebar changes the instance every view shows, and a banner always says which one it is. The choice lives in the browser and reloads the page, so caches, queries and the tour start clean. Each instance has its own browser cache.
- **The proxy.** The browser never talks to the other instance and never sees its key. Every call goes to this instance, which forwards it with the key (`/api/remote/{id}/…`), so no CORS is needed.
  - It forwards only `/api/…`, never login, API keys, instances or the proxy itself.
  - It has timeouts and a size limit.
  - It needs this instance's login: an API key of this instance cannot use the keys of the others.
- **Reaching an instance.** LAN or VPN addresses (private, CGNAT/Tailscale) are allowed over `http://`. A public address only over `https://`, so the key never travels in clear. To open a home instance from a VPS, use a VPN such as Tailscale or WireGuard.
- **Versions.** The UI speaks the API of its own version, so it compares major.minor with each instance:
  - the other instance is older by a minor: a warning;
  - a different major, or the other instance is newer: blocked, both in the UI and in the proxy, until they match.
- **What the key decides.** A read-only key only looks. Secrets, safety settings and API keys stay with that instance's own interface: an API key cannot change them. Security and API keys are hidden while on a remote, and every settings page says so.
- **Overview.** `/instances` shows every instance with its library health, reviews waiting, uploads in progress and last scan.
- **Command line.** `nazgarr instance ls/add/test/rm` manages the list, and asks for the password.
- **Key level.** The test of an instance reads its version and, through `GET /api/system/whoami` (added for this), whether the key is read or write.

### Command line (user decisions, 2026-10-03)

The `nazgarr` command is also a thin **client of the JSON API**, beside the server commands (`init`, `serve`, `install-service`, `version`). The server gets no new logic: every command calls the same endpoints, with the same checks, as the web UI. Full guide: `docs/CLI.md`.

- **Stack:** Typer (Click and Rich come with it) in `nazgarr/cli_client/`. The output is English only.
- **Access:**
  - `nazgarr login` uses the password once, to create a dedicated API key (`cli-HOSTNAME`, write access unless `--read-only`). Only that key is kept, in `cli.toml` with mode 600, one profile per instance.
  - `nazgarr setup` creates the account of a fresh install with the one-time code.
  - Passwords and secrets are always asked on screen or read from stdin, never passed as arguments.
  - Secret settings and safety settings, which an API key may not change, ask for the password and log in for that single change.
  - In the container, the command is installed with `NAZGARR_URL=http://127.0.0.1:3019` and `NAZGARR_CLI_CONFIG=/app/config/cli.toml`.
- **Commands:** `status`, `scan`/`runs`, `review`, `disk`, `client`, `tracker` with `tracker profile`, `arr`, `settings`, `schedule`, `config export/import`, `upload`, `library`, `triage`, `logs`, and the raw `api`.
- **Approvals:** everything that touches files or clients asks for confirmation, with `--yes` for scripts and exit code 3 when declined. The upload flow goes through the same two approvals as the web UI. `--yes` accepts the proposed values but still stops on problems: an ambiguous match, missing IDs, a reseed that is not verified, or a mixed pack without `--confirm-mixed`.
- **`config import`** only adds and updates, never removes. It shows its plan first. Secrets are `${NAZGARR_…}` placeholders, resolved from the environment or asked when needed to create something.
- **Error messages** are the web UI's English texts (`nazgarr/cli_client/messages_en.json`, exported from `frontend/src/locales/en/errors.ts` by `scripts/export_cli_messages.py`; a test keeps them in sync).

## 11. Tech stack

Inherits ratio-guardian (CLAUDE.md), with additions for multi-client and posters:

- **Python 3.12**, **FastAPI** (pure JSON API under `/api/*`)
- **SQLite** via SQLAlchemy — enough for this load
- **APScheduler** in-process for scheduling
- **httpx** for tracker/TMDB calls (async-friendly)
- **qbittorrent-api**; Deluge, Transmission and rTorrent/ruTorrent through their own APIs with httpx and the standard `xmlrpc.client`, no extra library (section 5)
- **pymediainfo** for mediainfo/Unique ID
- **guessit** for filename parsing
- **torf** to create `.torrent` files for uploads (pure Python)
- **ffmpeg-python** for upload screenshots (needs `ffmpeg` in the container)
- A minimal BEP3 bencode parser (ratio-guardian's `app/torrent_file.py`, ported as `nazgarr/torrents/metainfo.py`) — used both for the folder-name fallback (ratio-guardian §7) and for the piece hash (section 6)
- **Frontend**: **React + shadcn/ui** SPA, Vite build, served by the FastAPI container
- **Single container with supervisord**, which runs one process: the FastAPI app, with APScheduler and the upload worker inside it (same pattern as ratio-guardian)

## 12. What to reuse from each source project (summary)

| Project | Reuse |
|---|---|
| ratio-guardian | Data architecture, matching/reseeding engine, adapter contracts — **a direct starting point**, not just inspiration. Python code reusable almost as-is where scope overlaps (torrent_file.py, mediainfo_util.py, adapters). |
| Auditorr | UX reference (tree view, dashboard, reverse lookup) — no direct code reuse (stack/language compatibility to verify during implementation, otherwise a design reference only). |
| Upload-Assistant | Domain reference for upload (tracker request shape, profiles, mediainfo/screenshots) — **no code reuse** (development freeze, incompatible stack: Flask/SSE web_ui vs FastAPI+SPA). |
| smartmediareseed | Piece-hash (BEP3) verification technique, to integrate as an additional confidence signal (section 6) — logic to reimplement against our own contracts, not to import (different Postgres/Flask stack). |

## 13. Open source requirements

- No personal data (paths, trackers, user credentials) in any versioned file — `config.example.yaml`/`.env.example` with generic placeholders.
- An explicit LICENSE (still to choose — MIT/AGPL are the typical picks for this kind of self-hosted tool, AGPL if the goal is discouraging closed-SaaS forks).
- Bundled tracker profiles (section 9) contain only publicly verifiable mapping/naming, never credentials.
- Setup documentation (README) good enough for a third-party user with no context from the design sessions — never assume the reader knows ratio-guardian or the other source projects.

## 14. Suggested roadmap

1. Bootstrap the project (stack, folder structure, requirements) — reusing ratio-guardian's structure/config as a reference.
2. DB schema (disks/media paths/torrent index/tracker/client/media_item with poster/candidate/match_review/seed_job) + SQLAlchemy models.
3. Scoped-per-disk file browser API (a pattern reusable as-is from ratio-guardian).
4. Torrent client adapters: qBittorrent first (direct reuse), then Deluge/Transmission/rutorrent/qui by the priority in section 5.
5. Media resolver (guessit + TMDB) + poster download/cache.
6. Matching engine (size + mediainfo + piece hash) for both directions (section 3), explicit confidence.
7. Review queue UI.
8. Executor: hardlink + add-to-client + forced recheck, both directions.
9. Library view (tree + grid) built on the unified state.
10. Scheduler + run history.
11. Upload module (torf, mediainfo/screenshots, tracker profiles, dupe-check, human confirmation).
12. Cleanup for open source release (example config, LICENSE, README).

Not binding to the letter, but respects the logical dependencies (e.g. there's no point building the Library view before a unified state exists to show).

**Repo**: `https://github.com/lktorrentz/nazgarr` (public, GPL-3.0). A project **separate from `ratio-guardian`** (confirmed decision: doesn't replace it, doesn't reuse its code as-is — reuses architecture/patterns as described in this document, but has its own repo and history).

## 15. Things explicitly left open (not decided in this session)

- **"qui"'s API surface — resolved**: see section 5 — a dedicated adapter (`nazgarr/adapters/torrent_client/qui.py`) built and verified against qui's own published OpenAPI spec and Auditorr's real integration, not the earlier "just point qbittorrent-api at it" assumption (which was wrong). Only a real qui instance to test the adapter end-to-end remains open, same category as qBittorrent's own once-open real-instance check.
- **Deluge/Transmission/rutorrent adapters — resolved 2026-10-03**: implemented without new dependencies (Deluge Web UI JSON-RPC, Transmission RPC, rTorrent XML-RPC on `/RPC2` or through ruTorrent's httprpc, all over httpx) and verified against real clients in Docker, see section 5. Still open: a real-instance check of the qui adapter and of rTorrent behind HTTP authentication (the Docker runs used Basic auth only for Transmission), and rTorrent older than 0.16 (only 0.16.22 was run; the commands used exist since 0.9.x, `d.multicall2` since 0.9.7).
- **Confidence threshold for the torrent→client direction** (sections 3, 6): whether it's the same 0.95 or higher — still to decide, no number fixed yet.
- Every point already open in ratio-guardian SPEC.md §17 (UNIT3D history scraping, a match cache persisted independently of the physical path, the exact tracker profile schema) stays open here too, unchanged. **Resolved since**: history for the dashboard chart (Fase 5, `run_log.health_snapshot` + `GET /api/dashboard/history`) and the image host for upload screenshots (Fase 6, below).
- **TVDB and MAL lookups for uploads** (user decision 2026-09-29: fine for v1, to revisit later): in the upload flow v2 (§9) TVDB and MAL ids are only passed through to the tracker, and a forced TVDB id is looked up via TMDB's `/find`. A direct TVDB search (API v4, needs a key) or MAL search (via Jikan, unofficial) would add dependencies and wasn't worth it for v1.
- **Image host for upload screenshots — resolved in Fase 6**: rather than picking one, wired all three realistic options (PTPImg, ImgBB, Imgbox) as a priority-ordered fallback chain (`ImageHostChain`, `nazgarr/integrations/adapter_factory.py::build_image_host_chain`) — user's explicit choice over picking a single adapter.
- **Image hosts as a bundled plugin** (user decision, 2026-10-05): the hosts are native plugins, one per host (`nazgarr/bundled/`, `nazgarr-ptscreens`...), written only against `nazgarr.sdk` and shipped in the image, each with its own on/off switch in Settings > Plugins (live, no restart; the switched-off ones in `app_settings.plugins_disabled`): PTScreens, Passtheima and imageride (Chevereto, through the SDK's `CheveretoImageHost`) and ImgBB. Only hosts with an API key: no anonymous upload, whose reliability is unknown. Lensdump (paid API) is an example plugin to install (`examples/nazgarr-lensdump`). PTPImg (offline, HTTP 500 on 2026-10-05), Imgbox, Pixhost, OnlyImage, Dalexni, utp.pm and Seedpool CDN were removed: anyone who wants one writes a plugin. All hosts, bundled or from plugins, are configured the same way (`adapter_config`, one list in Settings > Upload > Images with order and key); `image_host_priority` only holds the order. Migration 7 moves the kept keys and tells the user which removed hosts they were using.
