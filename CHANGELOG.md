# Changelog

Every stable release of Nazgarr, newest first. The app shows a short version of each entry after an update and, when a newer stable is available, before updating (`nazgarr/release_notes.json`). Test builds (`:nightly`, `:dev`) have no notes of their own: they get the next stable's notes when it is promoted.

Each section starts with `## X.Y.Z - YYYY-MM-DD` and has up to four parts: **Breaking changes** (what you need to do before or after updating), **New**, **Bug fixes** and **Chores** (technical work you will hardly notice). Promoting a version to stable copies its section into the GitHub Release.


## 0.9.22 - 2026-11-27

### Breaking changes

- **The first start after the update copies the database** to `<data_dir>/backups` before changing it, so the data folder needs free space for one more copy. If the copy or the update fails, Nazgarr starts in recovery mode with instructions instead of restarting in a loop.
- **Reseeds are added to the client stopped** and start only once the client's recheck has passed. A failed recheck leaves the torrent stopped until you retry it.
- **Deleting a tracker or a disk that was used deletes its history too**, after a confirmation that says what goes away.
- The first scan after the update reads again the library files identified by their name.

### New

- **Radarr/Sonarr webhooks:** the library is updated at every import, with no scan, and the imported torrent is registered from the client right away, so it is never proposed for reseeding.
- **Reseeding:**
  - "Approve all" at the top of the queue, recommended matches only by default.
  - One row per season with Approve all / Reject all; the pack wins over single episodes, with an option to keep both on the same tracker.
  - Pack episodes whose names don't say the episode are found by content.
  - A dead torrent gets its resurrection claimed before anything is linked or added, when the tracker has a website session saved (Settings › Trackers, with a Check button).
- **Upload dupe check:**
  - A percentage score and the verdicts "possible" and "trump", from type, codec, resolution, audio languages and size as the tracker states them.
  - A dead dupe no longer stops an upload.
  - New threshold setting.
  - An identical torrent can get its hash check, and at 100% its reseed, by itself (off by default).
- **Uploads:**
  - Scheduled uploads, from the CLI too (`--at`, `nazgarr upload schedule`).
  - Retry after a failure, without going back through the match.
  - From the watched folder, a folder with several seasons becomes one upload per season, and an incomplete season one per episode.
  - A wrongly detected value can be left out of the name, and the file name can be written by hand.
  - Resolution from MediaInfo follows ITTup's table.
- **Database:** an automatic verified backup before every update, with `nazgarr db backups` and `nazgarr db restore`.
- `nazgarr reset-password` for a forgotten password.
- **Disks:** the test explains a hardlink with different inodes (SMB, mergerfs, other FUSE); info icons explain each folder type.
- A warning when a tracker, client or *arr on a public host is saved with `http://`.
- A scheduled scan missed while Nazgarr was stopped runs once at startup.
- **For plugin authors:** SDK 1.9, with additions only (stopped adds, `upload.seed_checked`, dupe check fields, website session and resurrection claim).

### Bug fixes

- A failed upload from the watched folder followed by a successful reseed could delete every copy of the release.
- qBittorrent logged a login for every operation; Nazgarr now keeps one session per client.
- Choosing some seasons of a multi-season folder at the match uploaded the whole folder.
- A series folder with no year in its name never matched by itself.
- In "08 - Title 426.mkv" the episode was read from the title's numbers.
- Generated file names: the season chosen at the match, the tracker's language title, and every episode of a multi-episode file.
- Episode orders from TVDB/Sonarr are aligned by title first, and no longer guessed from an air date.
- Pack proposals turned into single episodes when episodes were searched in another order.
- A movie and a series sharing a TMDB id were mixed in the dupe check and in reseed matching.
- Screenshots of SDR sources without an HDR tag failed.
- Copy buttons did not work over plain http on a LAN.
- Deleting an upload left its folder in `data/uploads`.
- Interface: a long path pushed Browse/Start out of the card; double scrollbars on Windows.

### Chores

- A whole-repo audit (security, performance, reliability): request size limits, bounds on tracker data, pinned outbound connections, database indexes, paginated lists.
- Lower memory use (427 to 188 MB with 18k files) and a library tree that only draws the visible rows.
- Stricter linting and typing, more tests, CI time limits.
- README: Proxmox LXC and network shares.


## 0.9.0 - 2026-10-05

### Breaking changes

- **The default port is now 3019** (it was 8080), inside the container too. A container created with `"8080:8080"` no longer answers after the update: change the mapping to `"8080:3019"` to keep the old address, or `"3019:3019"`. On Unraid, set the WebUI Port's container port to 3019. The Python package (`nazgarr serve`, `install-service`) defaults to 3019 too.
- **Image hosts changed.** PTPImg (offline), Imgbox, Pixhost, OnlyImage, Dalexni, utp.pm and Seedpool CDN are gone, and there are no anonymous hosts any more. The available ones are PTScreens, Passtheima, imageride and ImgBB, each with its API key; the PTScreens and ImgBB keys move over by themselves. Enter the key of at least one host in Settings › Upload › Images, or uploads with screenshots stop with a clear error. Lensdump (paid API) is now an example plugin to install.
- **For plugin authors:** a plugin must import only `nazgarr.sdk` (now 1.2); Nazgarr's internal modules moved.

### New

- **Reseeding:** a queued reseed can get its own category and tags in the client, like an upload; a failed or stuck execution can be deleted so the next scan proposes it again.
- **Setup:** "Check paths" on each torrent client shows whether its files map to a disk and proposes the mapping; disks come from the folders mounted in the container; path and folder problems show up as warnings instead of failing silently. The tutorial follows the real setup, and a new tour, "The rest of Nazgarr", presents uploads (on an example that creates nothing), notifications, instances, API keys, plugins and updates.
- **Uploads:** remux and source are read from MediaInfo when the name says nothing (Dolby Vision profile 7, VC-1, lossless audio with PGS, remux bitrate, MakeMKV's "Original source medium"), and the type tag explains why. An AI upscale is an edition, and `{edition}` is in the default naming templates. A warning above the release name and in the confirmation when no source was found.
- **Exclusions:** the defaults follow Plex's and Jellyfin's naming (every image, nfo, theme music); new presets for extras (off by default), macOS and Windows system files and `.torrent` files.
- **Notifications:** services are instances, next to the webhooks, each with its type, settings, test and last delivery.
- **Updates:** an optional automatic check every 12 hours (off by default); the notes of a new version before updating, and what is new after.
- **Plugins:** each one can be switched on and off from Settings › Plugins without a restart; image hosts are plugins too.

### Bug fixes

- Reseeds and uploads stayed stopped in qBittorrent set to "do not start the download automatically" or to stop after checking files.
- With qBittorrent's "create subfolder" content layout, a single-file reseed downloaded again instead of seeding the hardlink.
- When adding a torrent failed but the client got it anyway (a slow tracker), the hardlinks were removed and the client downloaded the file; "Retry" could not recover it.
- A renamed remux (e.g. `film.mkv`) came out as an encode with no source; a joined "BDRemux" was not a remux, so Dolby Vision profile 8 never made it HYBRID.
- An edition (e.g. an AI upscale) could disappear from the release name; a film with "Sample" in its title came out empty.
- Posters and covers not named exactly as expected, and macOS `._` files, showed up as library files.
- The watched folder could leave files behind when they kept their original names.
- Two runs could start at the same time; removal warnings ignored some clients.
- The dupe check read traits from the wrong name; auto-match numbered episodes in the default order instead of the chosen one.
- The interface is optimized for phones and tablets: a sidebar that slides in, bigger touch targets, tables and dialogs that fit the screen, info popovers that open with a tap, and a confirmation before every delete.

### Chores

- Database migrations are numbered steps; settings are declared once, typed.
- The Python package is split by domain.
- Faster views and scans (cached dashboard and library, pages loaded on demand, hashes only for changed files, shared tracker rate limit), clients closed after use.
- Tests, documentation and CI updates.
