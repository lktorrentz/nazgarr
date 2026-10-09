"""Nomi dei file dentro il torrent di un upload (decisione dell'utente,
2026-10-01). Partendo da un file della libreria, il nome è quello di
Plex/Radarr/Sonarr ("Dune Part Two (2024) {imdb-...}.mkv"), non un nome di
release. Tre modalità, una per job (il torrent è uno, uguale per tutti i
tracker):

- "hardlink": i percorsi del torrent che già seeda gli stessi byte su un
  client (se tutti i video della sorgente sono hardlink di uno stesso
  torrent). È il nome della release originale, di solito il migliore.
- "generated": un nome a punti dal pattern in Settings > Upload (le stesse
  variabili dei nomi per tracker), es.
  Dune.Part.Two.2024.2160p.BluRay.REMUX.DV.HDR.TrueHD.7.1.Atmos.ITA.ENG.HEVC-GRP;
  nei pack, un nome per cartella e uno per ogni episodio.
- "original": i nomi della sorgente, come prima.

Di default "hardlink" se c'è, se no "generated".

Il piano è una cartella (o un file) e, per ogni file della sorgente, il
percorso che avrà dentro il torrent. Con nomi diversi dall'originale,
nazgarr/upload/execute.py crea gli hardlink con quei nomi nella cartella di seed
e calcola gli hash da lì.
"""

import json
import os
import re
import unicodedata
from dataclasses import dataclass

from sqlalchemy import tuple_
from sqlalchemy.orm import Session

from nazgarr.core import settings_registry, settings_repo
from nazgarr.core.file_types import is_video
from nazgarr.core.models import ClientTorrent, ClientTorrentFile, SeedFile, TrackerUploadProfile, UploadJob
from nazgarr.library import episode_orders
from nazgarr.upload import inventory as upload_inventory
from nazgarr.upload import pack as upload_pack
from nazgarr.upload.naming import build_name, detect_with_fallback, release_values, with_tracker_language

MODES = ("hardlink", "generated", "original")
SETTING = "upload_file_naming_rules"
# Come in nazgarr/upload/execute.py (torf): mai nel torrent.

_MOVIE = ("{title} {year} {edition} {repack} {resolution} {source_full} {hybrid} {type} {audio} "
          "{audio_languages} {subs} {hdr} {video_codec} {group}")
_TV = ("{title} {season} {edition} {repack} {resolution} {source_full} {hybrid} {type} {audio} "
       "{audio_languages} {subs} {hdr} {video_codec} {group}")
DEFAULT_RULES = {
    "templates": {"default": _MOVIE, "tv": _TV},
    "title": "original",
    "separator": ".",
    "group_separator": "-",
    # Lingue audio nel nome, MULTI da 4 in su; dei sottotitoli solo "SUBS"
    # se ci sono (decisione dell'utente, 2026-10-01).
    "audio_languages": {"style": "all", "multi_from": 4},
    "subs_format": "SUBS",
    # I codec come nelle release scene (DDP, non DD+).
    "audio_codecs": {"E-AC-3": "DDP", "AC-3": "DD"},
    # La sorgente la dice {source_full}: il tipo solo dove aggiunge qualcosa.
    "type_labels": {"REMUX": "REMUX", "WEBDL": "WEB-DL", "WEBRIP": "WEBRip", "WEBMUX": "WEBMux",
                    "DLMUX": "DLMux", "ENCODE": "", "HDTV": "", "DVDRIP": "DVDRip", "BRRIP": "BRRip", "DISC": ""},
}


def rules(session: Session) -> dict:
    raw = settings_repo.get_setting(session, SETTING)
    try:
        return json.loads(raw) if raw else DEFAULT_RULES
    except ValueError:
        return DEFAULT_RULES


def sanitize(name: str) -> str:
    """Un nome da file come nelle release: niente accenti, niente segni
    (":" "'" "," ...), "&" diventa "and", un solo punto alla volta."""
    text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    text = text.replace("&", "and").replace("'", "").replace("`", "")
    text = re.sub(r"[^A-Za-z0-9.+\-]+", ".", text)
    text = re.sub(r"\.{2,}", ".", text)
    text = re.sub(r"\.?-\.?", "-", text)  # niente punti attaccati al trattino del gruppo
    return text.strip(".-")


@dataclass
class FilePlan:
    mode: str
    content_name: str  # la cartella del torrent, o il file se è uno solo
    files: list[tuple[str, str]]  # (file della sorgente, percorso dentro il torrent)
    # Un file solo tolto dalla sua cartella (single_file): il torrent è il
    # file, e folder la cartella (relativa a quella di seed) in cui seeda, se
    # resta; None, direttamente nella cartella di seed.
    single_file: bool = False
    folder: str | None = None

    @property
    def renamed(self) -> bool:
        return self.mode != "original"


def _source_files(job: UploadJob) -> list[tuple[str, str]]:
    """(percorso assoluto, relativo alla sorgente) dei file che vanno nel
    torrent: la stessa regola dell'hashing (nazgarr/upload/inventory.py)."""
    return [(f.path, f.relative) for f in upload_inventory.job_files(job)]


def _season_dirs(job: UploadJob, files: list[tuple[str, str]]) -> dict[str, str]:
    """Per un complete pack di file scelti a mano (nazgarr/upload/pack.py),
    la sottocartella di ogni file ("Season 01/"), decisione dell'utente del
    2026-10-02: un sottotitolo segue il suo episodio. Vuoto altrimenti: i
    file stanno tutti nella cartella del torrent."""
    if not upload_pack.is_pack(job) or job.kind != "complete_pack":
        return {}
    layout = json.loads(job.layout_json or "{}")
    seasons = {v.get("relative_path"): v.get("season") for v in layout.get("videos", [])}
    stems = {os.path.splitext(name)[0]: season for name, season in seasons.items()}
    out = {}
    for _path, relative in files:
        season = seasons.get(relative)
        if season is None:
            stem = next((s for s in sorted(stems, key=len, reverse=True) if relative.startswith(s + ".")), None)
            season = stems.get(stem) if stem else None
        out[relative] = f"Season {season:02d}/" if isinstance(season, int) else ""
    return out


def _original(job: UploadJob, files: list[tuple[str, str]]) -> FilePlan:
    name = upload_pack.name(job)
    if upload_pack.is_pack(job):
        # I nomi dei file come sono, nella cartella nuova del pack.
        folder = sanitize(name) or "pack"
        dirs = _season_dirs(job, files)
        return FilePlan("original", folder, [(p, f"{folder}/{dirs.get(r, '')}{r}") for p, r in files])
    if not job.is_dir:
        return FilePlan("original", name, [(files[0][0], name)])
    return FilePlan("original", name, [(path, f"{name}/{relative}") for path, relative in files])


def _hardlink(session: Session, job: UploadJob, files: list[tuple[str, str]]) -> FilePlan | None:
    """I percorsi del torrent di un client che ha in hardlink tutti i video
    della sorgente, se ce n'è uno."""
    by_inode: dict[tuple[int, int], list[str]] = {}
    for path, _relative in files:
        try:
            st = os.stat(path)
        except OSError:
            continue
        by_inode.setdefault((st.st_dev, st.st_ino), []).append(path)
    paths: dict[str, dict[int, str]] = {path: {} for group in by_inode.values() for path in group}
    keys = list(by_inode)
    for start in range(0, len(keys), 500):  # una query ogni 500 file, non una per file
        for st_dev, inode, torrent_id, in_torrent in (
            session.query(SeedFile.st_dev, SeedFile.inode, ClientTorrentFile.client_torrent_id,
                          ClientTorrentFile.path_in_torrent)
            .join(ClientTorrentFile, ClientTorrentFile.seed_file_id == SeedFile.id)
            .filter(tuple_(SeedFile.st_dev, SeedFile.inode).in_(keys[start:start + 500]))
            .all()
        ):
            for path in by_inode.get((st_dev, inode), ()):
                paths[path][torrent_id] = in_torrent
    videos = [path for path, relative in files if is_video(relative)]
    common = set.intersection(*(set(paths.get(v, {})) for v in videos)) if videos else set()
    if not common:
        return None
    torrent = session.get(ClientTorrent, min(common))
    planned = []
    for path, relative in files:
        in_torrent = paths.get(path, {}).get(torrent.id)
        if in_torrent is None:
            continue  # un file che quel torrent non ha (un nfo nostro): resta fuori
        planned.append((path, in_torrent.replace("\\", "/")))
    if not planned:
        return None
    roots = {p.split("/", 1)[0] for _s, p in planned}
    if len(roots) == 1:
        # Una cartella comune ("Release/file.mkv"), o un torrent di un file solo.
        return FilePlan("hardlink", roots.pop(), planned)
    # File sparsi senza cartella comune: sotto il nome del torrent.
    return FilePlan("hardlink", torrent.name, [(s, f"{torrent.name}/{p}") for s, p in planned])


def _tracker_title(session: Session, job: UploadJob, analysis: dict) -> tuple[str | None, str | None]:
    """Il titolo dei nomi generati come lo vogliono i tracker dell'upload,
    se sono d'accordo su quale (locale, locale e originale) e in che lingua:
    es. ITT, il titolo in italiano (segnalato 2026-10-09: i file prendevano
    sempre quello originale, in inglese). (scelta, titolo in quella lingua,
    già letto da TMDB per i nomi delle release: analysis["titles"]);
    (None, None) se non c'è una scelta comune: le regole dei nomi dei file."""
    from nazgarr.upload.decision import profile_rules  # import qui: decision importa questo modulo

    choices = set()
    for target in job.targets:
        if target.action == "skip":
            continue
        profile = session.get(TrackerUploadProfile, target.tracker_id)
        tracker_rules = with_tracker_language(profile_rules(profile), target.tracker.language) or {}
        choices.add((tracker_rules.get("title") or "original", tracker_rules.get("title_language")))
    if len(choices) != 1:
        return None, None
    mode, language = choices.pop()
    if mode == "original" or not language:
        return None, None
    return mode, (analysis.get("titles") or {}).get(language)


def _generated(session: Session, job: UploadJob, files: list[tuple[str, str]], mediainfo: dict | None,
               overrides: dict, detected: dict, analysis: dict | None = None) -> FilePlan:
    rules_ = rules(session)
    title_mode, local_title = _tracker_title(session, job, analysis or {})
    if title_mode is not None:
        rules_ = {**rules_, "title": title_mode}
    base_values = release_values(job, detected, mediainfo, overrides, rules_, local_title)
    base = sanitize(build_name(rules_, base_values))
    if not job.title or not base:
        # Senza un titolo (nessun match TMDB) non c'è un nome da costruire:
        # meglio i nomi della sorgente che un ".mkv".
        return _original(job, files)
    main_ext = os.path.splitext(max(files, key=lambda f: os.path.getsize(f[0]))[0])[1].lower()
    if not job.is_dir:
        return FilePlan("generated", base + main_ext, [(files[0][0], base + main_ext)])

    layout = json.loads(job.layout_json or "{}")
    episodes = {
        v.get("relative_path"): (v.get("season"), (v.get("episodes") or [None])[0])
        for v in layout.get("videos", [])
    }
    dirs = _season_dirs(job, files)
    planned = []
    renamed: dict[str, str] = {}  # stem del video originale -> stem nuovo, per i suoi sottotitoli
    videos = [(p, r) for p, r in files if is_video(r)]
    for path, relative in files:
        if not is_video(relative):
            continue
        ext = os.path.splitext(relative)[1].lower()
        season, episode = episodes.get(relative, (None, None))
        if job.content_type == "tv" and episode is not None:
            one = _EpisodeJob(job, season, episode)
            name = sanitize(build_name(rules_, release_values(one, detected, mediainfo, overrides, rules_,
                                                              local_title)))
        elif len(videos) == 1:
            name = base
        else:
            name = sanitize(os.path.splitext(os.path.basename(relative))[0])
        renamed[os.path.splitext(os.path.basename(relative))[0]] = name
        planned.append((path, f"{base}/{dirs.get(relative, '')}{name}{ext}"))
    # Gli altri file nella cartella del torrent: un sottotitolo
    # ("Show - S01E01 - Pilot.it.srt") segue il suo episodio, il resto (nfo,
    # immagini) tiene il suo nome. Niente sottocartelle "Season 01".
    taken = {target for _s, target in planned}
    for path, relative in files:
        if is_video(relative):
            continue
        filename = os.path.basename(relative)
        stem = next((old for old in sorted(renamed, key=len, reverse=True) if filename.startswith(old)), None)
        folder = f"{base}/{dirs.get(relative, '')}"
        target = f"{folder}{renamed[stem]}{filename[len(stem):]}" if stem else f"{folder}{filename}"
        if target in taken:  # due file con lo stesso nome in sottocartelle diverse
            target = f"{base}/{relative}"
        taken.add(target)
        planned.append((path, target))
    return FilePlan("generated", base, planned)


class _EpisodeJob:
    """Il job visto come un solo episodio: stagione ed episodio nel nome, nella
    numerazione scelta al match (nazgarr/library/episode_orders.py) anche quando i file
    ne seguono un'altra; un episodio accorpato diventa E03E04."""

    def __init__(self, job: UploadJob, season: int | None, episode: int):
        self._job = job
        self.kind = "episode"
        if season is not None:
            refs = episode_orders.translate_files(job, season, episode)
            season = refs[0][0]
            numbers = [e for s, e in refs if s == season]
            episode = numbers if len(numbers) > 1 else numbers[0]
        self.episode = episode
        self.seasons_json = json.dumps([season] if season is not None else json.loads(job.seasons_json or "[]")[:1])

    def __getattr__(self, name):
        return getattr(self._job, name)


class NameInputs:
    """I file della sorgente e il torrent in hardlink, letti una volta per
    tutte le modalità: l'anteprima della decisione chiede available_modes,
    default_mode e il piano di ogni modalità, e ognuno rileggeva la
    sorgente e cercava gli hardlink da capo."""

    _UNSET = object()

    def __init__(self, session: Session, job: UploadJob):
        self.session, self.job = session, job
        self.files = _source_files(job)
        self._hardlink = self._UNSET

    def hardlink(self) -> FilePlan | None:
        if self._hardlink is self._UNSET:
            self._hardlink = _hardlink(self.session, self.job, self.files)
        return self._hardlink


def available_modes(session: Session, job: UploadJob, inputs: NameInputs | None = None) -> list[str]:
    inputs = inputs or NameInputs(session, job)
    return [m for m in MODES if m != "hardlink" or inputs.hardlink() is not None]


def _in_media_library(job: UploadJob) -> bool:
    disk = getattr(job, "disk", None)
    if disk is None:
        return False
    source = os.path.realpath(job.source_path)
    return any(source.startswith(os.path.realpath(os.path.join(disk.root_path, media)) + os.sep)
               for media in disk.media_folders)


AUTO_RENAME_SETTING = "upload_auto_rename"


def auto_rename(session: Session) -> bool:
    """Rinominare in automatico (Settings › Releases), acceso di default:
    spento, ogni upload parte con i nomi originali, e gli altri restano da
    scegliere a mano."""
    return settings_registry.get_bool(session, AUTO_RENAME_SETTING)


def default_mode(session: Session, job: UploadJob, inputs: NameInputs | None = None) -> str:
    """Il nome del torrent in hardlink se c'è; se no un nome generato, per un
    file della libreria (nomi alla Plex) o una release della cartella
    osservata (la tua, da chiamare come vuole il pattern). Una sorgente già
    nella cartella dei torrent ha già il suo nome di release, e lo tiene.
    Con il rename automatico spento, sempre i nomi originali."""
    if not auto_rename(session):
        return "original"
    if "hardlink" in available_modes(session, job, inputs):
        return "hardlink"
    # Un pack di file scelti a mano ha una cartella nuova: il suo nome di release.
    if _in_media_library(job) or job.origin == "watch" or upload_pack.is_pack(job):
        return "generated"
    return "original"


SINGLE_FILE_SETTING = "upload_single_file"
SINGLE_FILE_FOLDER_SETTING = "upload_single_file_folder"


def single_file_folder(session: Session) -> str | None:
    """Con l'impostazione accesa (Settings › Releases, spenta di default),
    "keep" o "remove": cosa fare della cartella che conteneva il file."""
    if not settings_registry.get_bool(session, SINGLE_FILE_SETTING):
        return None
    return settings_registry.get_choice(session, SINGLE_FILE_FOLDER_SETTING)


def _as_single_file(found: FilePlan, folder_choice: str | None) -> FilePlan:
    """Una cartella con un file solo dentro il torrent (decisione
    dell'utente, 2026-10-02): il torrent diventa quel file, senza cartella.
    Conta quello che entra nel torrent: un sample o un Thumbs.db non ne
    fanno parte, un nfo sì (e la cartella resta). La cartella può restare
    come cartella di seed (il client punta lì dentro) o sparire: il file
    seeda direttamente nella cartella di seed."""
    if folder_choice is None or len(found.files) != 1 or "/" not in found.files[0][1]:
        return found
    source, target = found.files[0]
    folder, name = target.rsplit("/", 1)
    return FilePlan(found.mode, name, [(source, name)], single_file=True,
                    folder=folder if folder_choice == "keep" else None)


def plan(session: Session, job: UploadJob, mode: str | None = None, inputs: NameInputs | None = None) -> FilePlan:
    """Il piano dei nomi per la modalità scelta (o quella di default), un
    file solo senza la sua cartella se l'impostazione è accesa."""
    return _as_single_file(_plan(session, job, mode, inputs or NameInputs(session, job)), single_file_folder(session))


def name_detected(session: Session, job: UploadJob) -> dict:
    """I valori letti dal nome scelto dall'analisi (torrent in hardlink, nome
    originale di Radarr/Sonarr, o il nome della sorgente:
    nazgarr/upload/analysis.py), con quello che il nome del file non dice
    preso dal nome della cartella. Senza un gruppo nel nome, il nome del
    releaser delle impostazioni (decisione dell'utente, 2026-10-03)."""
    from nazgarr.upload.watch import releaser_name

    name_source = json.loads(job.analysis_json or "{}").get("name_source") or {}
    detected = detect_with_fallback(name_source.get("name") or upload_pack.name(job), name_source.get("fallback"))
    if not detected.get("group"):
        detected["group"] = releaser_name(session)
    return detected


def _plan(session: Session, job: UploadJob, mode: str | None, inputs: NameInputs) -> FilePlan:
    files = inputs.files
    analysis = json.loads(job.analysis_json or "{}")
    overrides = json.loads(job.overrides_json or "{}")
    mode = mode or overrides.get("file_naming") or default_mode(session, job, inputs)
    if mode == "hardlink":
        found = inputs.hardlink()
        if found is not None:
            return found
        mode = "generated"
    if mode == "generated" and files:
        return _generated(session, job, files, analysis.get("mediainfo"), overrides, name_detected(session, job),
                          analysis)
    return _original(job, files)
