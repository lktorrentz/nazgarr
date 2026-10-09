"""Nome della release per ogni tracker (docs/SPEC.md §9 "Upload flow v2").

Due metà:
- i VALORI: letti dal MediaInfo quando c'è (risoluzione, codec video, HDR,
  tracce audio e lingue, sottotitoli) e dal nome della release (tipo,
  sorgente, servizio, edizione, repack, gruppo: il nome scelto dall'analisi,
  nazgarr/upload/analysis.py name_source), con sopra gli override dell'utente;
- le REGOLE del tracker (tracker_upload_profile.naming_rules_json, versionate
  nei profili bundlati, nazgarr/upload/profiles.py): un template per tipo di
  release, come vuole il tracker, più poche opzioni (titolo locale, come
  scrivere audio e lingue, etichetta SDR, separatore).

Segnaposto dei template: VARIABLES qui sotto, le stesse per ogni tracker.
Un segnaposto senza valore
sparisce con gli spazi rimasti; {season} si aggiunge da solo dopo titolo e
anno per le serie se il template non lo prevede; il gruppo si attacca alla
fine con group_separator. Il nome resta una proposta, sempre modificabile.
"""

import json
import re

from nazgarr.library.guess import as_list
from nazgarr.library.guess import guess as guess_name
from nazgarr.upload import streaming_services
from nazgarr.upload.dupes import traits_of

# Le variabili dei template, uguali per tutti i tracker, con un esempio:
# servono all'editor delle regole (chip da inserire e anteprima).
VARIABLES = {
    "title": "Dune: Part Two", "local_title": "Dune - Parte due", "year": "2024", "season": "S02",
    "episode": "E03", "edition": "Extended", "repack": "REPACK", "hybrid": "HYBRID", "resolution": "2160p",
    "format": "UHD",
    "source": "BluRay", "source_full": "BluRay",
    "type": "REMUX", "service": "ATVP", "video_codec": "HEVC", "hdr": "DV HDR", "hdr_full": "DV.P7.HDR10",
    "bit_depth": "10bit",
    "audio": "TrueHD 7.1 Atmos", "audio_codec": "TrueHD", "audio_channels": "7.1", "audio_atmos": "Atmos",
    "audio_all": "TrueHD 7.1 DD+ 5.1 Atmos", "audio_languages": "ITA ENG", "subs_languages": "ITA ENG",
    "subs": "SUBS ITA ENG", "group": "GRP",
}

# Campi che l'utente può correggere nei "Detected details".
DETECTED_FIELDS = (
    "type", "resolution", "source", "edition", "repack", "hybrid", "service", "hdr", "video_codec", "audio",
    "audio_languages", "group",
)

DEFAULT_TEMPLATE = "{title} ({year}) {season} {edition} {resolution} {source} {video_codec} {audio} {group}"
# Come si scrive {type} nel nome: la chiave del profilo (REMUX, WEBDL, ...)
# resta per scegliere il type_id, nel nome va la sua etichetta. Un profilo
# può ridefinirla (rules.type_labels), anche con variabili dentro, es.
# REMUX: "{source} REMUX VU"; vuota = nel nome non si scrive.
DEFAULT_TYPE_LABELS = {
    "REMUX": "REMUX", "WEBDL": "WEB-DL", "WEBRIP": "WEBRip", "ENCODE": "", "HDTV": "HDTV", "DVDRIP": "DVDRip",
    "BRRIP": "BRRip", "DISC": "", "WEBMUX": "WEBMux", "DLMUX": "DLMux",
}
# I tipi la cui sorgente è un servizio di streaming ({source_full}).
WEB_TYPES = frozenset({"WEBDL", "WEBRIP", "WEBMUX", "DLMUX"})
# {format}: la risoluzione in lettere. Un profilo può ridefinire le etichette
# (rules.format_labels), es. ITT non ha HD e scrive SD anche per il 720p.
DEFAULT_FORMAT_LABELS = {"UHD": "UHD", "FullHD": "FullHD", "HD": "HD", "SD": "SD"}
DEFAULT_RULES = {
    "version": 0,
    "templates": {"default": DEFAULT_TEMPLATE},
    "title": "original",  # original | local | local_original (titolo originale seguito da quello locale)
    "title_language": None,  # es. "it": titolo TMDB in quella lingua per {local_title}
    "audio_languages": {"style": "none"},  # none | all | primary_first (+ primary, multi_from)
    "subs_languages": {"style": "all"},  # come audio_languages, per {subs_languages}
    "subs_format": "SUBS {subs_languages}",  # come si scrive {subs} se ci sono sottotitoli; "" = mai
    "subs_multi_format": None,  # es. "MULTI SUBS": {subs} quando le lingue diventano MULTI (multi_from)
    # "tracker_language": {subs} è SUB (una lingua) o SUBS (più lingue), più la
    # lingua del tracker se l'audio non ce l'ha e un sottotitolo sì. Altrimenti
    # subs_format.
    "subs_style": None,
    "sdr_label": None,  # es. "SDR": scritto al posto dell'HDR quando non c'è
    "separator": " ",
    "group_separator": "-",
}

_NAME_AUDIO = {
    "Dolby Digital Plus": "DD+", "Dolby Digital": "DD", "Dolby TrueHD": "TrueHD", "DTS-HD": "DTS-HD",
    "DTS": "DTS", "AAC": "AAC", "FLAC": "FLAC", "Opus": "Opus", "MP3": "MP3", "PCM": "LPCM",
}
# Formato MediaInfo -> nome usato nelle release. Un profilo può
# ridefinirlo (rules.audio_codecs), es. "DDP" invece di "DD+".
DEFAULT_AUDIO_CODECS = {
    "MLP FBA": "TrueHD", "E-AC-3": "DD+", "AC-3": "DD", "DTS": "DTS", "DTS-HD MA": "DTS-HD MA",
    "DTS-HD HRA": "DTS-HD HRA", "DTS:X": "DTS:X", "AAC": "AAC", "FLAC": "FLAC", "PCM": "LPCM", "Opus": "Opus",
    "MPEG Audio": "MP3", "Vorbis": "Vorbis",
}
# ISO 639-1 (MediaInfo) -> codice a tre lettere scritto nei nomi.
LANG3 = {
    "it": "ITA", "en": "ENG", "fr": "FRA", "de": "DEU", "es": "SPA", "pt": "POR", "ja": "JPN", "ko": "KOR",
    "zh": "ZHO", "ru": "RUS", "nl": "NLD", "sv": "SWE", "da": "DAN", "no": "NOR", "nb": "NOR", "fi": "FIN",
    "pl": "POL", "cs": "CES", "hu": "HUN", "tr": "TUR", "el": "ELL", "he": "HEB", "ar": "ARA", "hi": "HIN",
    "th": "THA", "uk": "UKR", "ro": "RON",
}


# "VU" (video untouched, la convenzione dei remux su ITT) e "UNTOUCHED": un
# remux anche se il nome non dice REMUX. guessit non li conosce.
_UNTOUCHED = re.compile(r"(?:^|[ ._\-\[(])(?:VU|UNTOUCHED)(?:$|[ ._\-\])])")
# "BDRemux", "UHDRemux", "DVDRemux" attaccati: guessit li riconosce solo
# separati (BD-Remux, BluRay.Remux), ma nelle release italiane sono comuni.
_JOINED_REMUX = re.compile(r"(?:^|[ ._\-\[(])(BD|UHD|BluRay|Blu-?Ray|HDDVD|DVD)Remux(?:$|[ ._\-\])])", re.IGNORECASE)


def _joined_remux_source(name: str) -> str | None:
    match = _JOINED_REMUX.search(name)
    if not match:
        return None
    disc = match.group(1).upper().replace("-", "")
    return {"DVD": "DVD", "HDDVD": "HDDVD"}.get(disc, "BluRay")


def release_type(guess: dict, name: str = "") -> str:
    """Chiave type_id dei profili (REMUX, ENCODE, WEBDL, ...)."""
    others = {str(o) for o in as_list(guess.get("other"))}
    source = str(guess.get("source") or "").lower()
    if "Remux" in others or _UNTOUCHED.search(name) or _JOINED_REMUX.search(name):
        return "REMUX"
    if source == "web":
        if "Mux" in others:  # guessit non distingue WEBMux da DLMux
            return "DLMUX" if "dlmux" in name.lower() else "WEBMUX"
        return "WEBRIP" if "Rip" in others else "WEBDL"
    if source == "hdtv":
        return "HDTV"
    if source == "dvd":
        return "DVDRIP"
    return "ENCODE"


def _source_label(guess: dict) -> str | None:
    """La sorgente come si scrive nei nomi di remux ed encode (BluRay, 3D
    BluRay, HDDVD, PAL/NTSC DVD, HDTV, UHDTV, UHDRip). Un Blu-ray UHD è
    "BluRay" come gli altri: che sia UHD lo dicono già risoluzione e
    {format} (decisione dell'utente, 2026-09-30). Per il DVD senza PAL/NTSC
    nel nome decide la risoluzione, in release_values."""
    source = str(guess.get("source") or "")
    others = {str(o) for o in as_list(guess.get("other"))}
    if source == "Web":
        return "WEBRip" if "Rip" in others else "WEB-DL"
    if source in ("Ultra HD Blu-ray", "Blu-ray") and "3D" not in others:
        return "BluRay"
    if source == "Blu-ray":
        return "3D BluRay" if "3D" in others else "BluRay"
    if source == "HD-DVD":
        return "HDDVD"
    if source == "Ultra HDTV":
        return "UHDRip" if "Rip" in others else "UHDTV"
    if source == "DVD":
        standard = next((o for o in ("PAL", "NTSC") if o in others), None)
        return f"{standard} DVD" if standard else "DVD"
    return source or None


# La sorgente di un full disc si scrive come il disco (Blu-ray, HD DVD, ...);
# un Blu-ray in 2160p è un UHD Blu-ray.
_DISC_SOURCES = {"BluRay": "Blu-ray", "3D BluRay": "3D Blu-ray", "HDDVD": "HD DVD"}


def source_full(values: dict) -> str | None:
    """Il campo "Source" come lo intendono i tracker, per ogni tipo: il
    servizio per le release web, il disco per un full disc, altrimenti la
    sorgente (BluRay, HDTV, ...)."""
    kind = values.get("type")
    if kind in WEB_TYPES:
        return values.get("service")
    source = values.get("source")
    if kind == "DISC":
        disc = _DISC_SOURCES.get(source, source)
        uhd = resolution_format(values.get("resolution")) == "UHD" or values.get("format") == "UHD"
        return f"UHD {disc}" if disc == "Blu-ray" and uhd else disc
    return source


# Come si scrive il codec, secondo il tipo di release (decisione dell'utente,
# 2026-10-03): una sola tabella per il MediaInfo e per il nome. Il video di
# un disco è AVC/HEVC, quello web o tv non ricodificato H.264/H.265, un
# encode x264/x265. L'encoder letto dal MediaInfo dice solo se un file è un
# encode (has_encoder, per i remux), mai l'etichetta.
_UNTOUCHED_DISC = frozenset({"REMUX", "DISC"})
_UNTOUCHED_STREAM = frozenset({"WEBDL", "WEBMUX", "DLMUX", "HDTV"})
_CODEC_LABELS = {
    "AVC": {"disc": "AVC", "stream": "H.264", "encode": "x264"},
    "HEVC": {"disc": "HEVC", "stream": "H.265", "encode": "x265"},
}
# Un'etichetta (dal nome o già scritta) torna al suo formato.
_CODEC_FAMILY = {
    "avc": "AVC", "h.264": "AVC", "h264": "AVC", "x264": "AVC",
    "hevc": "HEVC", "h.265": "HEVC", "h265": "HEVC", "x265": "HEVC",
    "mpeg video": "MPEG-2", "mpeg-2": "MPEG-2",
}


def codec_label(codec: str | None, release: str | None, rules: dict | None = None) -> str | None:
    """L'etichetta del codec video per quel tipo di release, poi quella del
    profilo se la ridefinisce (rules.video_codecs, es. "H.265": "H265")."""
    if not codec:
        return None
    family = _CODEC_FAMILY.get(codec.lower(), codec)
    labels = _CODEC_LABELS.get(family)
    if labels is None:
        label = family
    elif release in _UNTOUCHED_DISC:
        label = labels["disc"]
    elif release in _UNTOUCHED_STREAM:
        label = labels["stream"]
    else:  # ENCODE, WEBRIP, BRRIP, DVDRIP, o tipo sconosciuto
        label = labels["encode"]
    return ((rules or {}).get("video_codecs") or {}).get(label, label)


def _name_video_codec(guess: dict, release: str) -> str | None:
    return codec_label(str(guess.get("video_codec") or "") or None, release)


def _name_audio(guess: dict) -> str | None:
    codecs = [str(c) for c in as_list(guess.get("audio_codec"))]
    atmos = "Dolby Atmos" in codecs
    main = next((c for c in codecs if c != "Dolby Atmos"), None)
    label = _NAME_AUDIO.get(main or "", main)
    if label == "DTS-HD" and "Master Audio" in {str(p) for p in as_list(guess.get("audio_profile"))}:
        label = "DTS-HD MA"
    if label is None:
        return "Atmos" if atmos else None
    parts = [label]
    if guess.get("audio_channels"):
        parts.append(str(guess["audio_channels"]))
    if atmos:
        parts.append("Atmos")
    return " ".join(parts)


def detect(source_name: str) -> dict:
    """I valori che si leggono dal nome della release."""
    guess = guess_name(source_name)
    traits = traits_of(source_name)
    release = release_type(guess, source_name)
    service = guess.get("streaming_service")
    if service or release in WEB_TYPES:
        service = streaming_services.abbreviation(service, source_name)
    return {
        "type": release,
        "resolution": guess.get("screen_size"),
        "source": _source_label(guess) or _joined_remux_source(source_name),
        "edition": _edition(guess, source_name),
        "repack": "REPACK" if traits.repack else None,
        "hybrid": "HYBRID" if _HYBRID.search(source_name) else None,
        "service": service or None,
        "hdr": " ".join(tag for tag in ("DV", "HDR") if tag in traits.hdr) or None,
        "video_codec": _name_video_codec(guess, release),
        "audio": _name_audio(guess),
        "audio_languages": None,
        "group": guess.get("release_group"),
    }


def detect_with_fallback(name: str, fallback: str | None = None) -> dict:
    """detect() del nome, con quello che manca preso da un secondo nome: il
    file di una cartella con un solo contenuto, e la cartella per il resto."""
    detected = detect(name)
    if not fallback:
        return detected
    other = detect(fallback)
    return {key: value if value not in (None, "") else other.get(key) for key, value in detected.items()}


# --- dal MediaInfo --------------------------------------------------------------


def mi_resolution(video: dict) -> str | None:
    width, height = video.get("width") or 0, video.get("height") or 0
    if not width and not height:
        return None
    interlaced = str(video.get("scan_type") or "").lower() in ("interlaced", "mbaff", "paff")
    if height >= 2000 or width >= 3800:
        base = "2160"
    elif height >= 1000 or width >= 1900:
        base = "1080"
    elif height >= 700 or width >= 1260:
        base = "720"
    elif height >= 560:
        base = "576"
    else:
        base = "480"
    return f"{base}{'i' if interlaced and base in ('1080', '576', '480') else 'p'}"


def resolution_format(resolution: str | None, labels: dict | None = None) -> str | None:
    """La risoluzione in lettere, come la chiamano i tracker (Format):
    UHD da 2160p in su, FullHD a 1080, HD a 720, SD sotto; labels
    (rules.format_labels) cambia come si scrive ognuna."""
    match = re.match(r"(\d+)", resolution or "")
    if not match:
        return None
    height = int(match.group(1))
    if height >= 2160:
        key = "UHD"
    elif height >= 1080:
        key = "FullHD"
    elif height >= 720:
        key = "HD"
    else:
        key = "SD"
    return {**DEFAULT_FORMAT_LABELS, **(labels or {})}.get(key) or None


def _mi_hdr(video: dict) -> str | None:
    hdr = f"{video.get('hdr_format') or ''} {video.get('hdr_format_compatibility') or ''}"
    transfer = str(video.get("transfer_characteristics") or "")
    tags = []
    if "Dolby Vision" in hdr:
        tags.append("DV")
    if "HDR10+" in hdr or "2094" in hdr:
        tags.append("HDR10+")
    elif "HDR10" in hdr or "2086" in hdr or "PQ" in transfer:
        tags.append("HDR")
    if "HLG" in transfer or "HLG" in hdr:
        tags.append("HLG")
    return " ".join(tags) or None


# Il profilo Dolby Vision come lo scrive MediaInfo: "dvhe.07.06", "dvav.08",
# o "Profile 7" nelle versioni che lo spiegano a parole.
_DV_PROFILE = re.compile(r"dv(?:he|av|h1|a1)\.0?(\d{1,2})|profile\s*(\d{1,2})", re.IGNORECASE)


def dv_profile(video: dict) -> int | None:
    text = " ".join(str(video.get(key) or "") for key in ("hdr_format_profile", "hdr_format", "hdr_format_string"))
    if "dolby vision" not in text.lower() and "dv" not in text.lower():
        return None
    match = _DV_PROFILE.search(text)
    return int(match.group(1) or match.group(2)) if match else None


def _mi_hdr_full(video: dict) -> str | None:
    """Tutti i formati HDR per esteso, uniti da un punto (decisione
    dell'utente, 2026-10-02): Dolby Vision con il suo profilo, poi HDR10 /
    HDR10+ e HLG se ci sono. "DV.P5", "DV.P7.HDR10", "DV.P8.HDR10+.HLG",
    "HDR10", "HDR10+". Senza profilo leggibile, "DV"."""
    tags = []
    for tag in (_mi_hdr(video) or "").split():
        if tag == "DV":
            profile = dv_profile(video)
            tags.append(f"DV.P{profile}" if profile is not None else "DV")
        else:
            tags.append("HDR10" if tag == "HDR" else tag)
    return ".".join(tags) or None


# Per chi confronta l'HDR di più file (nazgarr/upload/pack.py).
hdr_full = _mi_hdr_full


_HYBRID = re.compile(r"(?:^|[ ._\-\[(])HYBRID(?:$|[ ._\-\])])", re.IGNORECASE)
# Un upscale (decisione dell'utente, 2026-10-05): un'edizione a tutti gli
# effetti, nel nome dove il modello del tracker mette {edition}. guessit
# non lo riconosce: "AI Upscaled", "AI.Upscale", "Upscaled", in qualunque forma.
_UPSCALE = re.compile(r"(?<![a-z0-9])(AI[ ._-]?)?upscal(?:ed|e)?(?![a-z0-9])", re.IGNORECASE)


def _edition(guess: dict, name: str) -> str | None:
    parts = [str(e) for e in as_list(guess.get("edition"))]
    upscale = _UPSCALE.search(name)
    if upscale:
        label = "AI Upscaled" if upscale.group(1) else "Upscaled"
        if label.lower() not in " ".join(parts).lower():
            parts.append(label)
    return " ".join(parts) or None

# Le sorgenti da disco: un video da lì senza un encoder è un remux.
_DISC_LABELS = ("BluRay", "3D BluRay", "HDDVD", "PAL DVD", "NTSC DVD", "DVD")


def has_encoder(video: dict) -> bool:
    """Il video porta le tracce di un encode: la libreria x264/x265 (o
    un'altra) che l'ha scritto, o le sue impostazioni di encoding."""
    library = str(video.get("writing_library") or "").lower()
    return bool(video.get("encoding_settings")) or any(
        name in library for name in ("x264", "x265", "nvenc", "qsv", "svt", "aomenc", "rav1e", "xvid", "divx")
    )


# Il bitrate video sotto cui un 2160p o un 1080p non è un remux (un encode o
# una release web di solito ci sta molto sotto), in bit/s.
_REMUX_BITRATE = {"2160": 40_000_000, "1080": 18_000_000}


def disc_evidence(video: dict, tracks: list[dict], subtitles: list[dict]) -> list[str]:
    """I segni nel MediaInfo che il file viene da un disco, per quando il
    nome non dice la sorgente (un file rinominato, "film.mkv"):
    origin   il MediaInfo lo dice: "Original source medium" del video (MakeMKV
             lo scrive su ogni traccia estratta da un disco);
    dv_el    Dolby Vision profilo 7 con l'enhancement layer: solo sui UHD Blu-ray;
    vc1      video VC-1: solo Blu-ray e HD DVD;
    lossless audio TrueHD, DTS-HD MA/DTS:X o LPCM;
    pgs      sottotitoli PGS, quelli dei Blu-ray;
    bitrate  un bitrate video da remux per la risoluzione."""
    found = []
    if origin_source(video):
        found.append("origin")
    hdr = str(video.get("hdr_format_string") or video.get("hdr_format") or "")
    if dv_profile(video) == 7 and "EL" in re.split(r"[^A-Z]+", hdr):
        found.append("dv_el")
    if str(video.get("format") or "").upper() == "VC-1":
        found.append("vc1")
    for track in tracks:
        name = f"{track.get('format') or ''} {track.get('commercial_name') or ''}"
        if "MLP FBA" in name or "TrueHD" in name or _audio_codec_key(track) in ("DTS-HD MA", "DTS:X") \
                or str(track.get("format") or "") == "PCM":
            found.append("lossless")
            break
    if any(str(sub.get("format") or "") == "PGS" for sub in subtitles):
        found.append("pgs")
    threshold = _REMUX_BITRATE.get(str(mi_resolution(video) or "")[:4])
    if threshold and (video.get("bit_rate") or 0) >= threshold:
        found.append("bitrate")
    return found


# L'encoder audio degli streaming di Disney (Disney+, Hulu, ESPN+, Star+):
# chi codifica un WEB-DL lo lascia nel MediaInfo dell'audio.
_STREAMING_AUDIO = ("bamtech",)


def streaming_audio(tracks: list[dict], mediainfo_text: str | None = None) -> bool:
    """L'audio viene da uno streaming: BAMTech nel titolo o nella libreria
    di una traccia audio, o in una sezione Audio del MediaInfo completo (il
    riepilogo salvato prima non aveva la libreria). Segnalato 2026-10-10."""
    for track in tracks:
        text = f"{track.get('title') or ''} {track.get('writing_library') or ''}".lower()
        if any(name in text for name in _STREAMING_AUDIO):
            return True
    for block in re.split(r"\n\s*\n", mediainfo_text or ""):
        if block.lstrip().lower().startswith("audio") and any(name in block.lower() for name in _STREAMING_AUDIO):
            return True
    return False


# "Original source medium" del MediaInfo -> la sorgente nei nomi.
_ORIGIN_SOURCES = (("blu-ray", "BluRay"), ("bd", "BluRay"), ("hd dvd", "HDDVD"), ("dvd", "DVD"))


def origin_source(video: dict) -> str | None:
    """La sorgente dal campo "Original source medium" del video, se c'è e
    dice un disco (Blu-ray, HD DVD, DVD-Video)."""
    medium = str(video.get("original_source_medium") or "").strip().lower()
    if not medium:
        return None
    return next((label for key, label in _ORIGIN_SOURCES if medium.startswith(key)), None)


def _disc_source(evidence: list[str], encoded: bool) -> bool:
    """Basta per dire disco il video (DV a doppio strato, VC-1). Audio
    lossless e PGS insieme solo se il video è un encode o ha un bitrate da
    remux: un mux con video web (DLMux, WEBMux) può prendere audio e
    sottotitoli da un Blu-ray, e il suo video non ha né l'uno né l'altro."""
    if "origin" in evidence or "dv_el" in evidence or "vc1" in evidence:
        return True
    return {"lossless", "pgs"} <= set(evidence) and (encoded or "bitrate" in evidence)


def _disc_video(evidence: list[str]) -> bool:
    """Il video stesso è quello del disco, non solo audio e sottotitoli. Un
    encode di solito perde "Original source medium" della traccia video; se
    lo tiene, la traccia dell'encoder lo tiene comunque encode."""
    return "origin" in evidence or "dv_el" in evidence or "vc1" in evidence or "bitrate" in evidence


def _audio_codec_key(track: dict) -> str | None:
    fmt = str(track.get("format") or "")
    features = str(track.get("format_additional_features") or "")
    if fmt == "DTS":
        if "XLL X" in features:
            return "DTS:X"
        if "XLL" in features:
            return "DTS-HD MA"
        if "XBR" in features:
            return "DTS-HD HRA"
    return fmt or None


def _audio_parts(track: dict, codecs: dict) -> tuple[str | None, str | None, str | None]:
    """(codec, canali, "Atmos") di una traccia, come si scrivono nei nomi."""
    key = _audio_codec_key(track)
    codec = codecs.get(key, key) if key else None
    channels = track.get("channels")
    layout_channels = None
    if channels:
        layout = str(track.get("channel_layout") or "")
        lfe = "LFE" in layout if layout else channels >= 6
        layout_channels = f"{channels - 1}.1" if lfe else f"{channels}.0"
    features = f"{track.get('format_additional_features') or ''} {track.get('commercial_name') or ''}"
    atmos = "Atmos" if ("Atmos" in features or "JOC" in features or "16-ch" in features) else None
    return codec, layout_channels, atmos


def _audio_label(track: dict, codecs: dict) -> str | None:
    codec, channels, atmos = _audio_parts(track, codecs)
    if codec is None:
        return None
    return " ".join(part for part in (codec, channels, atmos) if part)


def _is_commentary(track: dict) -> bool:
    return "comment" in str(track.get("title") or "").lower()


def _audio_values(tracks: list[dict], rules: dict) -> dict:
    codecs = {**DEFAULT_AUDIO_CODECS, **(rules.get("audio_codecs") or {})}
    usable = [t for t in tracks if not _is_commentary(t)]
    if not usable:
        return {}
    main = next((t for t in usable if t.get("default")), usable[0])
    codec, channels, atmos = _audio_parts(main, codecs)
    # Stesso codec e stessi canali una volta sola (due tracce DD 5.1 in lingue
    # diverse sono "DD 5.1", le lingue le dice {audio_languages}). L'oggetto
    # (Atmos) è a parte: una volta sola, dopo tutti i codec, come vuole la
    # convenzione ACodec Channels ... Object (decisione dell'utente, 2026-09-30).
    parts = [_audio_parts(t, codecs) for t in usable]
    labels = dict.fromkeys(" ".join(p for p in (c, ch) if p) for c, ch, _obj in parts if c)
    objects = dict.fromkeys(obj for c, _ch, obj in parts if c and obj)
    every = " ".join([*labels, *objects]) or None
    return {
        "audio": every if rules.get("audio") == "all" else _audio_label(main, codecs),  # "all": regole v1
        "audio_codec": codec, "audio_channels": channels, "audio_atmos": atmos, "audio_all": every,
    }


def _lang3(language: str) -> str:
    code = language.split("-")[0].lower()
    return LANG3.get(code, code.upper()[:3])


def audio_language_check(tracker_language: str | None, mediainfo: dict | None) -> str | None:
    """La regola dei tracker sulla lingua (decisione dell'utente, 2026-10-02):
    nel file ci deve essere una traccia audio (non un commento) nella lingua
    del tracker. "present", "missing", "unknown" (le tracce non dicono la loro
    lingua), o None se non c'è niente da controllare (nessuna lingua sul
    tracker, nessun MediaInfo o nessuna traccia audio)."""
    if not tracker_language or not mediainfo:
        return None
    tracks = [t for t in mediainfo.get("audio") or [] if not _is_commentary(t)]
    if not tracks:
        return None
    wanted = {tracker_language.lower(), LANG3.get(tracker_language.lower(), "").lower()} - {""}
    codes = {str(t.get("language") or "").split("-")[0].split("_")[0].lower() for t in tracks}
    if codes & wanted:
        return "present"
    return "unknown" if codes == {""} else "missing"


def _languages_value(tracks: list[dict], config: dict | None) -> str | None:
    config = config or {"style": "none"}
    style = config.get("style", "none")
    if style == "none":
        return None
    langs = list(dict.fromkeys(_lang3(t["language"]) for t in tracks if t.get("language") and not _is_commentary(t)))
    if not langs:
        return None
    # Da multi_from lingue in su, "MULTI" al posto dell'elenco; con
    # primary_first la lingua principale del tracker resta davanti.
    many = len(langs) >= (config.get("multi_from") or 99)
    if style == "all":
        return "MULTI" if many else " ".join(langs)
    primary = config.get("primary")
    head = [primary] if primary in langs else []
    if many:
        return " ".join([*head, "MULTI"])
    return " ".join([*head, *(lang for lang in langs if lang != primary)])


def episode_token(episode: int | list[int]) -> str:
    """E03, o E03E04 per un file con più episodi (un episodio accorpato in un
    altro ordinamento, nazgarr/library/episode_orders.py)."""
    episodes = episode if isinstance(episode, list) else [episode]
    return "".join(f"E{e:02d}" for e in episodes)


def season_token(kind: str, seasons: list[int], episode: int | list[int] | None) -> str | None:
    if not seasons:
        return None
    if kind == "episode" and episode is not None:
        return f"S{seasons[0]:02d}{episode_token(episode)}"
    if kind == "complete_pack" and len(seasons) > 1:
        return f"S{min(seasons):02d}-S{max(seasons):02d}"
    return f"S{seasons[0]:02d}"


def with_tracker_language(rules: dict | None, language: str | None) -> dict | None:
    """La lingua del tracker (tracker.language, ISO 639-1) al posto di quelle
    delle regole: titolo localizzato, lingua messa per prima fra audio e
    sottotitoli, e "language" (codice a tre lettere) per subs_style."""
    lang3 = LANG3.get(language or "")
    if not lang3:
        return rules
    rules = dict(rules or {})
    rules["title_language"] = language
    rules["language"] = lang3
    for field in ("audio_languages", "subs_languages"):
        config = dict(rules.get(field) or {})
        if config.get("style") == "primary_first" or config.get("primary"):
            config["primary"] = lang3
        rules[field] = config
    return rules


def _tracker_language_subs(rules: dict, tracks: list[dict], subtitles: list[dict]) -> str:
    """subs_style "tracker_language": SUB o SUBS secondo le lingue dei
    sottotitoli, con la lingua del tracker solo se l'audio non ce l'ha e un
    sottotitolo sì (es. audio ENG + sub ITA su un tracker italiano: SUB ITA)."""
    # Un sottotitolo senza lingua conta come una lingua a sé.
    languages = {_lang3(s["language"]) if s.get("language") else None for s in subtitles}
    word = "SUBS" if len(languages) > 1 else "SUB"
    lang = rules.get("language")
    audio = {_lang3(t["language"]) for t in tracks if t.get("language") and not _is_commentary(t)}
    return f"{word} {lang}" if lang and lang in languages and lang not in audio else word


def effective_rules(rules: dict | None) -> dict:
    return {**DEFAULT_RULES, **(rules or {})}


def release_values(
    job, detected: dict, mediainfo: dict | None, overrides: dict, rules: dict | None, local_title: str | None = None
) -> dict:
    """Tutti i valori dei segnaposto per un tracker: MediaInfo, poi il nome
    della release, poi gli override dell'utente sopra tutto."""
    rules_in = rules
    rules = effective_rules(rules)
    values = {key: detected.get(key) for key in DETECTED_FIELDS}
    video = (mediainfo or {}).get("video") or {}
    tracks = (mediainfo or {}).get("audio") or []
    subtitles = (mediainfo or {}).get("subtitles") or []
    evidence = disc_evidence(video, tracks, subtitles) if video else []
    named_encode = str(detected.get("video_codec") or "").lower() in ("x264", "x265")
    basis = {"type": "name" if values["type"] not in (None, "ENCODE") or named_encode else None,
             "source": "name" if values.get("source") else None, "evidence": evidence}
    # Il nome non dice la sorgente, ma l'audio viene da uno streaming
    # (BAMTech): un WEB-DL, o un WEBRip se il nome dice x264/x265. La
    # traccia di un encoder nel video non basta: certi servizi la lasciano
    # anche nei WEB-DL. Prima dei segni di un disco, che a quel punto non servono.
    if video and not values.get("source") and values["type"] in (None, "ENCODE") \
            and streaming_audio(tracks, getattr(job, "mediainfo_text", None)):
        values["type"], values["source"] = ("WEBRIP", "WEBRip") if named_encode else ("WEBDL", "WEB-DL")
        basis["type"] = "web_audio_encode" if named_encode else "web_audio"
        basis["source"] = "web_audio"
    # Il nome non dice la sorgente, il MediaInfo sì: un disco (decisione
    # dell'utente, 2026-10-05). Prima del remux qui sotto, che ne ha bisogno.
    if video and not values.get("source") and _disc_source(evidence, has_encoder(video)):
        disc = origin_source(video) or "BluRay"
        values["source"] = "DVD" if disc == "DVD" or 0 < int(video.get("height") or 0) <= 576 else disc
        basis["source"] = "mediainfo"
    # Il nome dice encode (o niente), ma è un disco senza traccia di encoder:
    # è un remux (decisione dell'utente, 2026-10-02). Prima del codec, che
    # per un remux si scrive AVC/HEVC e non x264/x265. Con la sorgente letta
    # dal MediaInfo anche il video deve essere quello del disco.
    if (video and not overrides.get("type") and values["type"] in (None, "ENCODE")
            and values.get("source") in _DISC_LABELS and not has_encoder(video)
            and (basis["source"] != "mediainfo" or _disc_video(evidence))):
        values["type"] = "REMUX"
        basis["type"] = "disc_no_encoder"
    if basis["type"] is None:
        basis["type"] = "encoder" if video and has_encoder(video) else "default"
    basis["encoder"] = str(video.get("writing_library") or "") or None if video and has_encoder(video) else None
    release = overrides.get("type") or values["type"] or "ENCODE"
    # Un remux con Dolby Vision profilo 8: il DV è stato aggiunto a un altro
    # video (un remux puro ha profilo 7 o 5), quindi è un ibrido.
    if video and release == "REMUX" and not values.get("hybrid") and dv_profile(video) == 8:
        values["hybrid"] = "HYBRID"
    if video:
        values["resolution"] = mi_resolution(video) or values["resolution"]
        values["video_codec"] = video.get("format") or values["video_codec"]
        values["hdr"] = _mi_hdr(video)
        values["hdr_full"] = _mi_hdr_full(video)
        values["bit_depth"] = f"{video['bit_depth']}bit" if video.get("bit_depth") else None
    if tracks:
        audio = _audio_values(tracks, rules)
        values.update({k: v for k, v in audio.items() if k != "audio"})
        values["audio"] = audio.get("audio") or values["audio"]
        values["audio_languages"] = _languages_value(tracks, rules.get("audio_languages"))
    # Senza MediaInfo le tracce le dice solo il nome della release.
    values["audio_all"] = values.get("audio_all") or values.get("audio")
    if subtitles:
        values["subs_languages"] = _languages_value(subtitles, rules.get("subs_languages") or {"style": "all"})
    if not values.get("hdr") and rules.get("sdr_label"):
        values["hdr"] = rules["sdr_label"]
    # Senza MediaInfo (o senza profilo DV) è uguale a {hdr}.
    values["hdr_full"] = values.get("hdr_full") or values.get("hdr")
    values["subs"] = None
    if subtitles:
        # Regole v1-v3 avevano solo un'etichetta fissa (subs_label).
        subs_format = rules.get("subs_format")
        if "subs_format" not in (rules_in or {}) and (rules_in or {}).get("subs_label"):
            subs_format = rules_in["subs_label"]
        if rules.get("subs_multi_format") and "MULTI" in (values.get("subs_languages") or "").split():
            subs_format = rules["subs_multi_format"]
        values["subs"] = re.sub(r"\s+", " ", _render(subs_format or "", values)).strip() or None
        if rules.get("subs_style") == "tracker_language":
            values["subs"] = _tracker_language_subs(rules, tracks, subtitles)
    for key in DETECTED_FIELDS:
        if overrides.get(key) not in (None, ""):
            values[key] = overrides[key]
    if overrides.get("hdr") not in (None, ""):
        values["hdr_full"] = overrides["hdr"]  # corretto a mano: vale per tutti e due
    # Il codec segue il tipo scelto (anche se corretto a mano); scritto a
    # mano resta com'è.
    if overrides.get("video_codec") in (None, ""):
        values["video_codec"] = codec_label(values.get("video_codec"), values.get("type"), rules)
    # Un remux viene da un disco: senza una sorgente nel nome, BluRay (o DVD
    # se è a definizione standard). Decisione dell'utente, 2026-10-02.
    if values.get("type") == "REMUX" and not values.get("source"):
        height = int(video.get("height") or 0) if video else 0
        values["source"] = "DVD" if 0 < height <= 576 else "BluRay"
        basis["source"] = "remux"
    # Perché tipo e sorgente sono questi (analysis.type_basis): nel popover
    # del tipo fra i valori rilevati.
    values["type_basis"] = basis
    # Dopo gli override: seguono la risoluzione, il tipo e la sorgente scelti.
    if values.get("source") == "DVD" and values.get("resolution"):
        standard = {"576": "PAL", "480": "NTSC"}.get(str(values["resolution"])[:3])
        values["source"] = f"{standard} DVD" if standard else "DVD"
    values["format"] = resolution_format(values.get("resolution"), rules.get("format_labels"))
    values["source_full"] = source_full(values)

    title = job.title
    if rules.get("title") == "local" and local_title:
        title = local_title
    elif rules.get("title") == "local_original" and local_title and local_title != job.title:
        title = f"{job.title} {local_title}"
    values["title"] = title
    values["content_type"] = job.content_type
    values["local_title"] = local_title if local_title and local_title != job.title else None
    values["year"] = overrides.get("year") or job.year
    seasons = json.loads(job.seasons_json or "[]")
    values["season"] = season_token(job.kind or "movie", seasons, job.episode) if job.content_type == "tv" else None
    values["episode"] = episode_token(job.episode) if job.kind == "episode" and job.episode is not None else None
    return values


_TOKEN = re.compile(r"\{(\w+)\}")


def template_for(rules: dict, release_type_key: str | None, content_type: str | None = None) -> str:
    """Il pattern per le serie se è una serie e il profilo ne ha uno (di
    solito senza anno), poi quello del tipo di release, poi il principale."""
    templates = rules.get("templates") or {}
    for key in (("tv",) if content_type == "tv" else ()) + ((release_type_key,) if release_type_key else ()):
        template = (templates.get(key) or "").strip()
        if template:
            return template
    return templates.get("default") or DEFAULT_TEMPLATE


def _render(template: str, values: dict) -> str:
    return _TOKEN.sub(lambda m: "" if m.group(1) == "group" else str(values.get(m.group(1)) or ""), template)


def type_label(rules: dict, values: dict) -> str | None:
    key = values.get("type")
    if not key:
        return None
    labels = {**DEFAULT_TYPE_LABELS, **(rules.get("type_labels") or {})}
    # Un ibrido può avere la sua etichetta (ITT: "REMUX" invece di "VU REMUX",
    # perché non è più video untouched).
    if values.get("hybrid"):
        labels.update(rules.get("hybrid_type_labels") or {})
    label = labels.get(key, key)
    return re.sub(r"\s+", " ", _render(label, {**values, "type": key})).strip() or None


def build_name(rules: dict | None, values: dict) -> str:
    rules = effective_rules(rules)
    template = template_for(rules, values.get("type"), values.get("content_type"))
    values = {**values, "type": type_label(rules, values)}
    if values.get("season") and "{season}" not in template:
        anchor = "({year})" if "({year})" in template else "{year}" if "{year}" in template else "{title}"
        template = template.replace(anchor, f"{anchor} {{season}}", 1)
    group = values.get("group")
    rendered = _render(template, values)
    rendered = re.sub(r"\(\s*\)|\[\s*\]", "", rendered)
    rendered = re.sub(r"\s+", " ", rendered).strip()
    separator = rules.get("separator") or " "
    if separator != " ":
        rendered = rendered.replace(" ", separator)
    if group and "{group}" in template:
        rendered = f"{rendered}{rules.get('group_separator') or '-'}{group}"
    return rendered


def rules_from_convention(convention: str | None) -> dict | None:
    """Un profilo senza regole ma con il vecchio naming_convention: quello
    come template unico."""
    return {"templates": {"default": convention}} if convention else None
