# Nazgarr from the command line

The `nazgarr` command does two jobs:

- **Server commands** (`init`, `serve`, `install-service`, `reset-password`, `version`) run Nazgarr on this machine. They are described in the README, under "Python package"; `reset-password` under "Forgot the password?" below.
- **Client commands** (everything else) talk to a running Nazgarr through its JSON API, the same one the web UI uses. They work on the machine that runs Nazgarr, from another computer, and inside the Docker container.

Every command has its own help: `nazgarr --help`, `nazgarr review --help`, `nazgarr review approve --help`.

The rule of the web UI still holds: **nothing that touches your files or your torrent clients runs without your confirmation.** These commands ask before acting:

- approving a review;
- retrying an execution;
- cancelling a scan.

In a script, add `--yes` to confirm without a prompt. Without a terminal and without `--yes`, they stop with exit code 3.

## Install

- **With the Python package** (`pipx install nazgarr`), the command is already there. It can also be installed only to talk to an instance running elsewhere.
- **In the Docker container**, run the command inside it:

  ```bash
  docker exec -it nazgarr nazgarr login
  docker exec -it nazgarr nazgarr status
  ```

  Inside the container the address is already set (`NAZGARR_URL=http://127.0.0.1:3019`). The login is kept in the config folder (`/app/config/cli.toml`), so it survives updates.

## Connect

### A fresh installation

```bash
nazgarr setup --url http://nas:3019
```

The command asks for:

- **the one-time setup code**, printed in the log at the first start (`docker logs nazgarr`, or the service log);
- **a username and a password.**

It then creates the account and logs in, the same way the web UI does.

### An existing installation

```bash
nazgarr login --url http://nas:3019
```

The password is used once. With it, the CLI creates a dedicated API key named `cli-HOSTNAME`, with write access unless you pass `--read-only`.

The CLI keeps only that key, in a file readable by you alone:

- **Linux:** `~/.config/nazgarr/cli.toml`
- **macOS:** `~/Library/Application Support/Nazgarr/cli.toml`

Passwords are always asked on screen, never passed as arguments. Arguments would end up in your shell history and in `ps`. In a script, pipe the password with `--password-stdin`:

```bash
printf '%s\n' "$NAZGARR_PASSWORD" | nazgarr login --url http://nas:3019 -u admin --password-stdin
```

### More instances

Each login saves a **profile**. The first one is called `default`.

```bash
nazgarr --profile seedbox login --url https://seedbox.example:3019
nazgarr profile ls            # the saved instances, * marks the default
nazgarr profile use seedbox   # make it the default
nazgarr -P default status     # one command on another profile
```

### Log out

```bash
nazgarr logout            # forget the key on this computer
nazgarr logout --revoke   # and revoke it on the server (asks the password)
```

A key can also be revoked in the web UI, under Settings › Extensions › API keys.

### Forgot the password?

On the machine that runs Nazgarr, `reset-password` sets a new one straight in its database, and logs out every open session (API keys keep working):

```bash
docker exec -it nazgarr nazgarr reset-password   # Docker
nazgarr reset-password                           # Python package
```

It asks the new password twice; `--password-stdin` reads it from stdin instead, and `--username NAME` changes the username too. The server can keep running. It works only where the database is: from another computer, nobody can reset it.

### Environment variables

They override the saved profile. Useful for cron jobs and containers:

| Variable | Meaning |
| --- | --- |
| `NAZGARR_URL` | Address of the instance |
| `NAZGARR_API_KEY` | API key to use (create one in the web UI or with `nazgarr login`) |
| `NAZGARR_PROFILE` | Saved profile to use |
| `NAZGARR_CLI_CONFIG` | Path of the profiles file |

## Global options

| Option | Meaning |
| --- | --- |
| `--profile`, `-P` | Saved instance to use |
| `--url` | Address of the instance, overriding the profile |
| `--json` | Print the raw JSON answer, for scripts and `jq` |
| `--install-completion` | Install tab completion for your shell (bash, zsh, fish, PowerShell) |

Global options go **before** the command: `nazgarr --json review ls`.

## Commands

### Overview

```bash
nazgarr status
```

Shows, at a glance:

- the version;
- the setup checklist;
- the library health;
- the counts of orphaned torrents, triage and duplicates;
- the reviews waiting;
- the last scan;
- the uploads in progress.

### Disks

```bash
nazgarr disk mounts                                  # folders under disk_scan_root not added yet
nazgarr disk add main /data                          # a disk
nazgarr disk folder add main seeding torrents        # its seeding folders (one or more)
nazgarr disk folder add main media media/movies      # its media folders (optional, one or more)
nazgarr disk folder add main media media/tv
nazgarr disk set main --uploads torrents/uploads --watch releases
nazgarr disk ls                                      # every disk with all its folders
nazgarr disk browse main media                       # list a folder, to find the paths
nazgarr disk mkdir main torrents/uploads             # create a folder
nazgarr disk test main                               # folders + a test hardlink between them
nazgarr disk folder rm main media media/tv           # nothing changes on disk
```

Disks, clients and trackers can be named by name or by ID. Names are not case sensitive.

### Torrent clients

```bash
nazgarr client add qbit --url http://qbittorrent:8080 -u admin     # the password is asked
nazgarr client add box --type qui --url http://qui:7476 --qui-instance 1   # the API token is asked
nazgarr client test qbit
nazgarr client link qbit main                              # use it for this disk
nazgarr client link qbit main --client-root /downloads    # it sees the whole disk at /downloads
nazgarr client link qbit main --folder qbittorrent --client-root /download   # it sees only /data/qbittorrent, as /download
nazgarr client edit qbit --category-movie radarr --tags-upload release
nazgarr client edit qbit --password                      # ask a new password
nazgarr client categories qbit
nazgarr client ls
```

### Trackers and upload profiles

```bash
nazgarr tracker presets                                   # known trackers
nazgarr tracker add --preset itt --client qbit --announce # token (and announce URL) asked
nazgarr tracker add Mine --url https://tracker.example --min-seed-time 7d --min-ratio 1
nazgarr tracker edit itt --language it --seed-rule all
nazgarr tracker profile show itt
nazgarr tracker profile set itt --internal --no-anonymous --freeleech 25 --category movie=1
nazgarr tracker profile template itt --edit               # the description template in $EDITOR
nazgarr tracker profile template itt --set description.j2
nazgarr tracker profile naming itt --preview              # names the rules give, on examples
nazgarr tracker profile naming itt --set rules.json
nazgarr tracker profile naming itt --update               # newer rules of the preset
```

A tracker can also be named by the preset key of its profile, as in `itt` above. The description template is BBCode with Jinja blocks (`{% if %}`, `{% for %}`, `{{ }}`), run in a sandbox.

### Radarr and Sonarr

```bash
nazgarr arr add radarr radarr --url http://radarr:7878    # the API key is asked
nazgarr arr add sonarr sonarr --url http://sonarr:8989
nazgarr arr test radarr radarr
nazgarr arr ls
```

### Settings and schedule

```bash
nazgarr settings ls                               # every setting with its value and meaning
nazgarr settings get upload_screenshot_count
nazgarr settings set upload_screenshot_count 6
nazgarr settings set upload_description_signature --file signature.txt
nazgarr settings set tmdb_api_key                 # secrets are asked, never passed as arguments
nazgarr schedule set "0 */6 * * *"                # scans every 6 hours
nazgarr schedule set off
```

Two kinds of settings ask for your password, because an API key is not allowed to change them:

- **secrets**, such as `tmdb_api_key`;
- **safety settings**, marked `[safety]` in `settings ls`: the full check before executing, the automatic execution, the thresholds and the client recheck.

The CLI then logs in for that single change and does not keep the token. Safety settings also ask for confirmation.

### Other instances

The web UI of this instance can open other Nazgarr instances (Configuration › Instances). From the terminal:

```bash
nazgarr instance add seedbox --url https://seedbox.example:3019   # its API key is asked
nazgarr instance ls                                               # with version, key level, compatibility
nazgarr instance test seedbox
nazgarr instance rm seedbox
```

They ask for your password: the list of instances needs the login, not an API key. To work on another instance from the terminal, use a profile instead (`nazgarr --profile seedbox login --url …`).

### The whole configuration in a file

```bash
nazgarr config export -o nazgarr.yaml      # disks, clients, trackers, profiles, *arr, settings, schedule
nazgarr config import nazgarr.yaml --dry-run
nazgarr config import nazgarr.yaml
```

This is useful to rebuild an installation, to copy the setup to another machine, or to keep it under version control.

- **Import adds and updates, and never removes.** A disk, a folder, a client or a tracker that the file does not mention stays as it is.
- **Import shows its plan first**, then asks to apply it. `--dry-run` only shows the plan, and `--yes` applies it without asking.
- **Secrets never go in the file.** Export writes placeholders in their place, such as `${NAZGARR_CLIENT_QBIT_PASSWORD}`.
- **On import, placeholders come from the environment.** A placeholder without a variable is asked on screen when it is needed to create something. When it would only update something, it is skipped, and the value on the server stays.

```bash
export NAZGARR_CLIENT_QBIT_PASSWORD=… NAZGARR_TRACKER_ITT_ITATORRENTS_TOKEN=…
nazgarr config import nazgarr.yaml --yes
```

### Scans

```bash
nazgarr scan                 # start a scan and return
nazgarr scan --wait          # follow it with a progress bar until it ends
nazgarr runs ls -n 20        # the latest scans
nazgarr runs show 42 -f      # one scan, following it while it runs
nazgarr runs cancel 42       # stop it (what it already saved stays)
```

A scan only reads: nothing is linked, added or moved. Its matches wait in the review queue.

If you press Ctrl+C during `scan --wait`, only the CLI stops following: the scan keeps running on the server.

### Reseeding review queue

```bash
nazgarr review ls                  # the proposals waiting for you
nazgarr review show 17             # one proposal
nazgarr review approve 17 18       # approve: hardlinks + torrent added to the client (asks first)
nazgarr review reject 19           # reject: nothing is touched
nazgarr review failed              # executions that failed
nazgarr review retry 7             # retry one (asks first)
```

With the full check on (the default), an approval first reads every piece of the torrent against your files. The hardlinks and the torrent follow only if the check passes.

### Uploads

```bash
nazgarr upload new main torrents/The.Matrix.1999.1080p.mkv
nazgarr upload new main releases/Show.S01 --tracker itt --tmdb tv/1399
nazgarr upload new main --pack torrents/E01/e01.mkv torrents/E02/e02.mkv   # a season pack of single episodes
nazgarr upload ls                    # in progress (--history for the finished ones)
nazgarr upload show 12               # source, match, trackers, events
nazgarr upload continue 12           # pick it up where it waits for you
nazgarr upload continue 12 --at "2026-10-10 21:00"   # approve now, start at that local time
nazgarr upload schedule 12 --now     # a scheduled upload: start it now (or --at another time)
nazgarr upload cancel 12
nazgarr upload rm 12                 # remove it from the history
```

`upload new` walks the upload through the same two approvals as the web UI:

1. **The match.** It shows the TMDB candidates with their confidence and asks which one, or takes the one given with `--tmdb`.
2. **The decision.** For each tracker it shows the suggested action (upload, reseed or skip), the release name, the IDs, the flags and what the tracker already has. It asks the action and the name, and any missing IDs. Before a reseed it runs the full hash check, if not done yet.
3. **The summary.** It sums up what will happen and asks to approve. Then it follows the upload until it is done (`--no-wait` to stop following). With `--at` it is approved now and starts at that time: the command returns right away.

With `--yes` the proposed values are accepted without asking. It still stops on anything that needs you:

- an ambiguous match (pass `--tmdb`);
- missing IDs;
- a reseed that is not verified;
- a mixed pack (pass `--confirm-mixed` if that is really what you want).

An upload from the watched folder waits at the decision too: `nazgarr upload ls` shows it, and `nazgarr upload continue` takes it from there.

### Library and triage

```bash
nazgarr library ls --type tv --orphans          # items with files that seed nowhere
nazgarr library show tv/1399                    # files, states, trackers
nazgarr library search tv/1399                  # search the trackers now (proposals go to review)
nazgarr library exclude media/movies/Extras --folder   # leave it out (it stays on disk)
nazgarr triage ls --safe                        # torrents you can remove safely
nazgarr triage ls --category superseded
nazgarr triage refresh
```

### Logs

```bash
nazgarr logs -n 100 --level WARNING
nazgarr logs -f                                  # keep printing new lines
```

### Any other endpoint

The raw API covers everything that has no command of its own yet. It works like `gh api`:

```bash
nazgarr api GET /api/dashboard
nazgarr api GET /api/library/items -p content_type=tv
nazgarr api POST /api/disks -d '{"label": "main", "root_path": "/data"}'
nazgarr api PATCH /api/trackers/3 -d @tracker.json
```

The full list of endpoints, with the exact request and answer of each, is at `http://HOST:3019/docs`.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | Done |
| 1 | Error, for example a request refused by the server, with its reason |
| 2 | Wrong usage, for example a missing option |
| 3 | Confirmation declined, or needed with `--yes` |
| 4 | Not logged in, wrong key, or a read-only key used for a change |

Error messages are the same as in the web UI, in English.
