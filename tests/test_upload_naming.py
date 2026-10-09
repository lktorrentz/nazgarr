import json
from pathlib import Path
from types import SimpleNamespace

from pymediainfo import MediaInfo

from nazgarr.core.models import TrackerUploadProfile
from nazgarr.library.mediainfo import summarize
from nazgarr.upload import profiles as upload_profiles
from nazgarr.upload.naming import build_name, detect, release_values, resolution_format, with_tracker_language
from tests.upload_helpers import make_tracker

MEDIAINFO = summarize(MediaInfo((Path(__file__).parent / "fixtures" / "mediainfo_remux.xml").read_text()), "x.mkv")
# Le regole di ITT con la lingua del suo tracker, come nella proposta vera.
ITT_RULES = with_tracker_language(upload_profiles._load_bundled_profile("itt")["upload"]["naming"], "it")


def _job(**extra):
    return SimpleNamespace(title="17 Again", year=2009, content_type="movie", kind="movie", seasons_json="[]",
                           episode=None, **extra)


def test_itt_remux_name_from_mediainfo_with_the_italian_title():
    detected = detect("17.Again.Ritorno.Al.Liceo.2009.REMUX.1080P.VU.AC3.ITA.TRUEHD.ENG.SUBS.ITA.ENG-MaTiTa")

    values = release_values(_job(), detected, MEDIAINFO, {}, ITT_RULES, local_title="17 Again - Ritorno al liceo")

    assert values["video_codec"] == "VC-1"
    # Due tracce DD 5.1 (inglese e italiano): una volta sola.
    assert (values["audio"], values["audio_all"]) == ("TrueHD 5.1", "TrueHD 5.1 DD 5.1")
    assert (values["audio_codec"], values["audio_channels"], values["audio_atmos"]) == ("TrueHD", "5.1", None)
    assert (values["audio_languages"], values["subs_languages"], values["bit_depth"]) == ("ITA ENG", "ITA ENG", "8bit")
    # Il nome non dice la sorgente: per un remux è BluRay (decisione dell'utente, 2026-10-02).
    assert build_name(ITT_RULES, values) == (
        "17 Again - Ritorno al liceo 2009 1080p FullHD BluRay VU REMUX TrueHD 5.1 DD 5.1 ITA ENG SUBS SDR VC-1-MaTiTa"
    )


def test_generic_rules_use_the_main_track_and_overrides_win():
    detected = detect("Movie.2009.1080p.BluRay.REMUX-GRP")
    template = "{title} {year} {resolution} {hdr} {video_codec} {audio} {audio_languages} {group}"
    rules = {"audio": "main", "audio_languages": {"style": "all"}, "sdr_label": "SDR",
             "templates": {"default": template}}

    values = release_values(_job(), detected, MEDIAINFO, {"group": "ME", "audio": "LPCM 2.0"}, rules)

    assert build_name(rules, values) == "17 Again 2009 1080p SDR VC-1 LPCM 2.0 ENG ITA-ME"


def test_without_mediainfo_the_name_fills_in():
    detected = detect("Movie.2019.2160p.WEB-DL.DDP5.1.Atmos.DV.HDR.H.265-GRP")

    values = release_values(_job(), detected, None, {}, None)

    assert (values["resolution"], values["audio"], values["hdr"], values["video_codec"]) == (
        "2160p", "DD+ 5.1 Atmos", "DV HDR", "H.265",
    )


def test_bundled_naming_rules_are_versioned(db_session, monkeypatch):
    tracker = make_tracker(db_session, "itt", with_profile=False)
    profile = upload_profiles.create_upload_profile(db_session, tracker, "itt")
    assert profile.naming_version == ITT_RULES["version"] and profile.naming_customized is False

    newer = {**ITT_RULES, "version": ITT_RULES["version"] + 1, "sdr_label": "SDR"}
    monkeypatch.setattr(upload_profiles, "_bundled_naming", lambda key: newer)

    # Non modificate dall'utente: si aggiornano da sole.
    assert upload_profiles.sync_naming_rules(db_session) == ["itt"]
    assert json.loads(profile.naming_rules_json)["sdr_label"] == "SDR"

    # Modificate: la versione nuova viene solo offerta...
    profile.naming_customized = True
    db_session.commit()
    newest = {**newer, "version": newer["version"] + 1}
    monkeypatch.setattr(upload_profiles, "_bundled_naming", lambda key: newest)
    assert upload_profiles.sync_naming_rules(db_session) == []
    assert profile.naming_update_available == newest["version"]
    # ...a meno che sia forzata.
    monkeypatch.setattr(upload_profiles, "_bundled_naming", lambda key: {**newest, "force": True})
    assert upload_profiles.sync_naming_rules(db_session) == ["itt"]
    refreshed = db_session.get(TrackerUploadProfile, tracker.id)
    assert (refreshed.naming_version, refreshed.naming_customized, refreshed.naming_update_available) == (
        newest["version"], False, None,
    )


def test_multi_above_a_number_of_languages_also_for_all_and_subtitles():
    from nazgarr.upload.naming import _languages_value

    tracks = [{"language": code} for code in ("en", "it", "fr")]
    assert _languages_value(tracks, {"style": "all", "multi_from": 3}) == "MULTI"
    assert _languages_value(tracks[:2], {"style": "all", "multi_from": 3}) == "ENG ITA"
    assert _languages_value(tracks, {"style": "primary_first", "primary": "ITA", "multi_from": 3}) == "ITA MULTI"


def test_one_pattern_with_type_labels_that_can_hold_variables():
    rules = {"templates": {"default": "{title} {resolution} {type} {video_codec} {group}"},
             "type_labels": {"REMUX": "{source} REMUX VU", "ENCODE": ""}}
    base = {"title": "Dune", "resolution": "2160p", "source": "UHD BluRay", "video_codec": "HEVC", "group": "G"}

    assert build_name(rules, {**base, "type": "REMUX"}) == "Dune 2160p UHD BluRay REMUX VU HEVC-G"
    assert build_name(rules, {**base, "type": "ENCODE"}) == "Dune 2160p HEVC-G"
    # Senza etichetta del profilo: quella di default, non la chiave.
    assert build_name(rules, {**base, "type": "WEBDL"}) == "Dune 2160p WEB-DL HEVC-G"
    # Un pattern specifico vuoto usa quello principale.
    assert build_name({**rules, "templates": {**rules["templates"], "WEBDL": " "}}, {**base, "type": "WEBDL"}) == (
        "Dune 2160p WEB-DL HEVC-G"
    )


def test_subs_writes_a_label_with_the_languages_and_can_be_turned_off():
    detected = detect("Movie.2009.1080p.BluRay.REMUX-GRP")
    rules = {"templates": {"default": "{title} {resolution} {subs} {group}"}}

    values = release_values(_job(), detected, MEDIAINFO, {}, rules)
    assert build_name(rules, values) == "17 Again 1080p SUBS ENG ITA-GRP"
    off = {**rules, "subs_format": ""}
    assert build_name(off, release_values(_job(), detected, MEDIAINFO, {}, off)) == "17 Again 1080p-GRP"
    legacy = {**rules, "subs_label": "SUBS"}  # regole scritte prima di subs_format
    assert build_name(legacy, release_values(_job(), detected, MEDIAINFO, {}, legacy)) == "17 Again 1080p SUBS-GRP"


def test_series_use_their_own_pattern_when_there_is_one():
    rules = {"templates": {"default": "{title} {year} {resolution} {group}",
                           "tv": "{title} {season} {resolution} {group}",
                           "REMUX": "{title} {year} REMUX {group}"}}
    base = {"title": "Severance", "year": 2022, "season": "S02", "resolution": "1080p", "group": "G"}

    assert build_name(rules, {**base, "content_type": "tv", "type": "REMUX"}) == "Severance S02 1080p-G"
    movie = {**base, "content_type": "movie", "type": "REMUX", "season": None}
    assert build_name(rules, movie) == "Severance 2022 REMUX-G"
    no_tv = {"templates": {"default": rules["templates"]["default"]}}
    assert build_name(no_tv, {**base, "content_type": "tv"}) == "Severance 2022 S02 1080p-G"


def test_format_is_the_resolution_in_letters_and_follows_the_override():
    assert [resolution_format(r) for r in ("4320p", "2160p", "1080i", "1080p", "720p", "576p", "480p", None)] == [
        "UHD", "UHD", "FullHD", "FullHD", "HD", "SD", "SD", None,
    ]
    detected = detect("Movie.2019.2160p.WEB-DL.DDP5.1.H.265-GRP")
    rules = {"templates": {"default": "{title} {year} {format} {resolution} {group}"}}

    assert release_values(_job(), detected, None, {}, rules)["format"] == "UHD"
    values = release_values(_job(), detected, None, {"resolution": "720p"}, rules)
    assert build_name(rules, values) == "17 Again 2009 HD 720p-GRP"


def test_sources_are_written_as_in_the_tracker_rules():
    assert [detect(name)["source"] for name in (
        "Movie.2010.1080p.3D.BluRay.x264-GRP", "Movie.2010.1080p.HDDVD.x264-GRP", "Show.S01E01.2160p.UHDTV.x265-GRP",
        "Movie.2010.2160p.UHDRip.x265-GRP", "Movie.2010.PAL.DVD9-GRP", "Movie.2010.2160p.UHD.BluRay.x265-GRP",
    )] == ["3D BluRay", "HDDVD", "UHDTV", "UHDRip", "PAL DVD", "BluRay"]
    # Un DVD senza PAL/NTSC nel nome: lo dice la risoluzione.
    values = release_values(_job(), detect("Movie.2010.DVDRip.x264-GRP"), None, {"resolution": "576p"}, None)
    assert values["source"] == "PAL DVD"


def test_web_releases_get_the_service_abbreviation_and_mux_types():
    amazon = detect("Show.S01E01.1080p.AMZN.WEB-DL.DDP5.1.H.264-GRP")
    timvision = detect("Show.S01E01.1080p.TIMV.WEB-DL.H.264-GRP")  # guessit non conosce TIMvision
    # Una parola del titolo non è un servizio.
    title_only = detect("Max.2020.1080p.WEB-DL.H.264-GRP")

    assert (amazon["service"], timvision["service"], title_only["service"]) == ("AMZN", "TIMV", None)
    assert detect("Show.S01E01.720p.iP.WEBMux-GRP")["type"] == "WEBMUX"
    assert detect("Show.S01E01.1080p.NF.DLMux-GRP")["type"] == "DLMUX"
    assert detect("Movie.2010.1080p.BluRay.x264-GRP")["service"] is None


def test_source_full_is_the_service_for_web_the_disc_for_a_full_disc_and_the_source_otherwise():
    web = release_values(_job(), detect("Movie.2019.1080p.NF.WEB-DL.H.264-GRP"), None, {}, None)
    remux = release_values(_job(), detect("Movie.2019.2160p.UHD.BluRay.REMUX.HEVC-GRP"), None, {}, None)
    disc = release_values(_job(), detect("Movie.2019.2160p.UHD.BluRay.REMUX.HEVC-GRP"), None, {"type": "DISC"}, None)

    assert (web["source_full"], remux["source_full"], disc["source_full"]) == ("NF", "BluRay", "UHD Blu-ray")


def test_itt_names_follow_the_wiki_source_and_format():
    web = release_values(_job(), detect("Movie.2009.1080p.NF.WEB-DL.DDP5.1.H.264-GRP"), None, {}, ITT_RULES)
    encode = release_values(_job(), detect("Movie.2009.720p.BluRay.DD5.1.x264-GRP"), None, {}, ITT_RULES)

    assert build_name(ITT_RULES, web) == "17 Again 2009 1080p FullHD NF WEB-DL DD+ 5.1 SDR H.264-GRP"
    assert build_name(ITT_RULES, encode) == "17 Again 2009 720p SD BluRay DD 5.1 SDR x264-GRP"
    assert (web["format"], encode["format"]) == ("FullHD", "SD")  # ITT non ha HD


def test_an_untouched_bundled_description_moves_to_the_current_one(db_session):
    bundled = upload_profiles._load_bundled_profile("itt")["upload"]
    old = bundled["replaces_description_templates"][0]
    tracker = make_tracker(db_session, "itt", with_profile=False)
    profile = upload_profiles.create_upload_profile(db_session, tracker, "itt")
    assert "mediainfo" not in profile.description_template

    profile.description_template = old
    db_session.commit()
    assert upload_profiles.sync_description_templates(db_session) == ["itt"]
    assert profile.description_template == bundled["description_template"]

    # Modificata dall'utente: non si tocca.
    profile.description_template = old + "mine"
    db_session.commit()
    assert upload_profiles.sync_description_templates(db_session) == []
    assert profile.description_template == old + "mine"


def test_itt_subs_follow_the_tracker_language():
    def subs(audio: list[str], subtitles: list[str]) -> str | None:
        mediainfo = {
            "video": {}, "audio": [{"language": lang} for lang in audio],
            "subtitles": [{"language": lang} for lang in subtitles],
        }
        return release_values(_job(), detect("Movie.2009.1080p.BluRay.x264-GRP"), mediainfo, {}, ITT_RULES)["subs"]

    # Audio già in italiano: solo SUB/SUBS, secondo le lingue (non le tracce).
    assert subs(["it", "en"], ["en"]) == "SUB"
    assert subs(["it"], ["it", "it"]) == "SUB"
    assert subs(["it", "en"], ["it", "en"]) == "SUBS"
    # Audio non in italiano e un sottotitolo italiano: la lingua.
    assert subs(["en"], ["it"]) == "SUB ITA"
    assert subs(["en"], ["it", "en", "fr"]) == "SUBS ITA"
    # Nessun sottotitolo italiano: senza lingua. Nessun sottotitolo: niente.
    assert subs(["en"], ["en", "fr"]) == "SUBS"
    assert subs(["en"], []) is None


def test_the_tracker_language_replaces_the_one_in_the_rules():
    rules = with_tracker_language({"audio_languages": {"style": "primary_first", "primary": "ENG"}}, "it")

    assert (rules["title_language"], rules["language"], rules["audio_languages"]["primary"]) == ("it", "ITA", "ITA")
    assert with_tracker_language({"title": "local"}, None) == {"title": "local"}


def test_atmos_comes_once_after_all_the_audio_codecs():
    def track(language, fmt, channels, features=None):
        return {"language": language, "format": fmt, "channels": channels, "format_additional_features": features}

    series = {"video": {}, "subtitles": [], "audio": [
        {**track("en", "E-AC-3", 6, "JOC"), "default": True}, track("it", "E-AC-3", 6), track("es", "E-AC-3", 6),
    ]}
    remux = {"video": {}, "subtitles": [], "audio": [
        {**track("en", "MLP FBA", 8, "16-ch"), "default": True}, track("it", "E-AC-3", 6), track("en", "DTS", 6, "XLL"),
    ]}
    detected = detect("Movie.2009.1080p.BluRay.x264-GRP")

    assert release_values(_job(), detected, series, {}, ITT_RULES)["audio_all"] == "DD+ 5.1 Atmos"
    values = release_values(_job(), detected, remux, {}, ITT_RULES)
    assert values["audio_all"] == "TrueHD 7.1 DD+ 5.1 DTS-HD MA 5.1 Atmos"
    # La traccia principale da sola resta codec, canali, oggetto.
    assert values["audio"] == "TrueHD 7.1 Atmos"


def test_the_tracker_language_must_be_in_an_audio_track():
    from nazgarr.upload.naming import audio_language_check

    def info(*tracks):
        return {"audio": [dict(t) for t in tracks]}

    assert audio_language_check("it", info({"language": "it-IT"})) == "present"
    assert audio_language_check("it", info({"language": "ita"})) == "present"
    assert audio_language_check("it", info({"language": "en"})) == "missing"
    # Una traccia di commento non basta, i sottotitoli non contano.
    commentary = {"language": "it", "title": "Director Commentary"}
    assert audio_language_check("it", info({"language": "en"}, commentary)) == "missing"
    assert audio_language_check("it", info({"language": None})) == "unknown"
    assert audio_language_check(None, info({"language": "en"})) is None
    assert audio_language_check("it", {"audio": []}) is None


def test_hdr_full_writes_every_hdr_format_with_the_dolby_vision_profile():
    from nazgarr.upload.naming import release_values

    job = _job()

    def values(video, overrides=None):
        return release_values(job, {}, {"video": {"format": "HEVC", "height": 2160, "width": 3840, **video}},
                              overrides or {}, None)

    # Solo Dolby Vision: DV con il profilo.
    assert values({"hdr_format": "Dolby Vision", "hdr_format_profile": "dvhe.05"})["hdr_full"] == "DV.P5"
    # Con HDR10 (o HDR10+, HLG) si aggiungono, uniti da un punto.
    p7 = values({"hdr_format": "Dolby Vision / SMPTE ST 2086", "hdr_format_profile": "dvhe.07 / ",
                 "hdr_format_compatibility": "Blu-ray / HDR10"})
    assert (p7["hdr"], p7["hdr_full"]) == ("DV HDR", "DV.P7.HDR10")
    p8 = values({"hdr_format_string": "Dolby Vision, Version 1.0, Profile 8.1, dvhe.08.06, BL+RPU",
                 "hdr_format": "Dolby Vision / SMPTE ST 2094 App 4", "transfer_characteristics": "HLG"})
    assert p8["hdr_full"] == "DV.P8.HDR10+.HLG"
    assert values({"hdr_format": "SMPTE ST 2094 App 4"})["hdr_full"] == "HDR10+"
    assert values({"hdr_format": "SMPTE ST 2086", "hdr_format_compatibility": "HDR10"})["hdr_full"] == "HDR10"
    # Corretto a mano: vale anche per {hdr_full}.
    assert values({"hdr_format": "Dolby Vision", "hdr_format_profile": "dvhe.05"}, {"hdr": "HDR"})["hdr_full"] == "HDR"


def test_the_naming_editor_offers_every_variable():
    # L'editor (NamingRulesEditor.tsx) ha la sua lista: deve restare uguale,
    # se no una variabile nuova esiste ma non si può inserire.
    import re
    from pathlib import Path

    from nazgarr.upload.naming import VARIABLES

    editor = (Path(__file__).resolve().parent.parent / "frontend/src/components/NamingRulesEditor.tsx").read_text()
    listed = re.search(r"const VARIABLE_NAMES = \[(.*?)\]", editor, re.S).group(1)
    assert re.findall(r"'(\w+)'", listed) == list(VARIABLES)


def test_a_remux_is_recognised_from_vu_or_from_a_disc_without_an_encoder():
    from nazgarr.upload.naming import detect, release_values

    # "VU": la convenzione ITT dei remux, che guessit non conosce.
    assert detect("Film.2023.2160p.UHD.BluRay.VU.DV.HDR.TrueHD.7.1-GRP")["type"] == "REMUX"
    assert detect("Film.2023.1080p.BluRay.UNTOUCHED.DTS-HD.MA-GRP")["type"] == "REMUX"
    assert detect("Vuelta.2023.1080p.BluRay.x264-GRP")["type"] == "ENCODE"  # "VU" dentro una parola non conta

    job = _job()
    untouched = {"video": {"format": "HEVC", "height": 2160, "width": 3840, "writing_library": None,
                           "encoding_settings": False}}
    encoded = {"video": {"format": "HEVC", "height": 2160, "width": 3840, "writing_library": "x265 3.5",
                         "encoding_settings": True}}
    # Un disco senza traccia di encoder è un remux, e il codec si scrive HEVC.
    remux = release_values(job, detect("Film.2023.2160p.BluRay-GRP"), untouched, {}, None)
    assert (remux["type"], remux["video_codec"]) == ("REMUX", "HEVC")
    # Con x265 dentro resta un encode.
    encode = release_values(job, detect("Film.2023.2160p.BluRay-GRP"), encoded, {}, None)
    assert (encode["type"], encode["video_codec"]) == ("ENCODE", "x265")
    # Un WEB-DL senza encoder non diventa un remux, e la scelta a mano vince.
    assert release_values(job, detect("Film.2023.2160p.WEB-DL-GRP"), untouched, {}, None)["type"] == "WEBDL"
    assert release_values(job, detect("Film.2023.2160p.BluRay-GRP"), untouched, {"type": "ENCODE"}, None)["type"] \
        == "ENCODE"


def test_a_dolby_vision_profile_8_remux_is_hybrid_and_drops_vu_at_itt():
    from nazgarr.upload.naming import build_name, detect, release_values
    from nazgarr.upload.profiles import _bundled_naming

    job = _job()
    itt_rules = _bundled_naming("itt")
    p8 = {"video": {"format": "HEVC", "height": 2160, "width": 3840, "hdr_format": "Dolby Vision",
                    "hdr_format_profile": "dvhe.08"}}
    p7 = {"video": {"format": "HEVC", "height": 2160, "width": 3840, "hdr_format": "Dolby Vision",
                    "hdr_format_profile": "dvhe.07"}}

    hybrid = release_values(job, detect("Film.2023.2160p.BluRay.REMUX-GRP"), p8, {}, itt_rules)
    assert hybrid["hybrid"] == "HYBRID"
    assert "HYBRID REMUX" in build_name(itt_rules, hybrid) and "VU" not in build_name(itt_rules, hybrid)
    pure = release_values(job, detect("Film.2023.2160p.BluRay.REMUX-GRP"), p7, {}, itt_rules)
    assert pure["hybrid"] is None and "VU REMUX" in build_name(itt_rules, pure)
    # Già scritto nel nome, anche senza MediaInfo.
    assert detect("Film.2023.2160p.BluRay.Hybrid.REMUX-GRP")["hybrid"] == "HYBRID"


def test_a_remux_without_a_source_comes_from_a_blu_ray_or_a_dvd():
    from nazgarr.upload.naming import detect, release_values

    job = _job()
    hd = release_values(job, detect("Film.2023.1080p.VU-GRP"), {"video": {"format": "AVC", "height": 1080}}, {}, None)
    sd = release_values(job, detect("Film.2023.576p.VU-GRP"), {"video": {"format": "MPEG Video", "height": 576}},
                        {}, None)
    assert (hd["type"], hd["source"]) == ("REMUX", "BluRay")
    assert (sd["type"], sd["source"]) == ("REMUX", "PAL DVD")


def test_itt_writes_sdr_when_there_is_no_hdr():
    from types import SimpleNamespace

    from nazgarr.upload.naming import build_name, detect, release_values, with_tracker_language

    rules = with_tracker_language(ITT_RULES, "it")
    job = SimpleNamespace(title="Dune", year=2021, content_type="movie", seasons_json="[]", kind="movie", episode=None)
    video = {"format": "AVC", "width": 1920, "height": 800, "transfer_characteristics": "BT.709"}
    mediainfo = {"video": video, "audio": [{"language": "it", "format": "E-AC-3", "channels": 6, "default": True}]}
    name = build_name(rules, release_values(job, detect("Dune.2021.1080p.WEB-DL.H.264-GRP.mkv"), mediainfo, {}, rules))
    assert " SDR H.264-GRP" in name
    hdr = {**video, "format": "HEVC", "hdr_format": "SMPTE ST 2086", "transfer_characteristics": "PQ"}
    values = release_values(job, detect("Dune.2021.WEB-DL-GRP.mkv"), {**mediainfo, "video": hdr}, {}, rules)
    name = build_name(rules, values)
    assert " HDR " in name and "SDR" not in name


def test_the_video_codec_follows_the_release_type_with_or_without_mediainfo():
    from types import SimpleNamespace

    from nazgarr.upload.naming import codec_label, detect, release_values

    job = SimpleNamespace(title="Dune", year=2021, content_type="movie", seasons_json="[]", kind="movie", episode=None)

    def codec(name, video=None, overrides=None, rules=None):
        mediainfo = {"video": {"width": 1920, "height": 800, **video}} if video else None
        return release_values(job, detect(name), mediainfo, overrides or {}, rules)["video_codec"]

    avc, x264 = {"format": "AVC"}, {"format": "AVC", "writing_library": "x264 core 164"}
    # Un DLMux è video web non ricodificato: H.264, col MediaInfo come dal nome.
    assert codec("Dune.2021.1080p.DLMux.H264-GRP.mkv", avc) == codec("Dune.2021.1080p.DLMux.H264-GRP.mkv") == "H.264"
    # Un WEB-DL resta H.264 anche se il servizio ha lasciato le impostazioni di x264.
    assert codec("Dune.2021.1080p.AMZN.WEB-DL.H.264-GRP.mkv", x264) == "H.264"
    # Un encode è x264 anche senza l'encoder dichiarato (o con un altro encoder).
    assert codec("Dune.2021.1080p.WEBRip.x264-GRP.mkv", avc) == "x264"
    assert codec("Dune.2021.1080p.WEBRip.x264-GRP.mkv", {"format": "AVC", "writing_library": "NVENC"}) == "x264"
    assert codec("Dune.2021.2160p.BluRay.REMUX.HEVC-GRP.mkv", {"format": "HEVC"}) == "HEVC"
    assert codec("Dune.2021.2160p.BluRay.x265-GRP.mkv", {"format": "HEVC", "writing_library": "x265"}) == "x265"
    # Il tipo corretto a mano porta con sé il codec; il codec scritto a mano resta.
    assert codec("Dune.2021.1080p.WEB-DL.H.264-GRP.mkv", avc, {"type": "WEBRIP"}) == "x264"
    assert codec("Dune.2021.1080p.WEB-DL.H.264-GRP.mkv", avc, {"video_codec": "AVC"}) == "AVC"
    # Un profilo può scriverlo a modo suo.
    rules = {"video_codecs": {"H.265": "H265"}}
    assert codec("Dune.2021.2160p.WEB-DL.H.265-GRP.mkv", {"format": "HEVC"}, rules=rules) == "H265"
    assert codec_label("VC-1", "REMUX") == "VC-1" and codec_label("MPEG Video", "REMUX") == "MPEG-2"


def test_an_upscale_is_an_edition():
    """Decisione dell'utente, 2026-10-05: "AI Upscaled" (e varianti) è
    un'edizione, nel nome dove il modello del tracker mette {edition} (i
    modelli di ITT, film e serie)."""
    cases = {
        "Absolute.Cinema.1895.2160p.AI.Upscaled.BluRay.x265-MaTiTa.mkv": "AI Upscaled",
        "Movie.2001.Extended.AI-Upscale.1080p.WEB-DL.x264-GRP": "Extended AI Upscaled",
        "Film.1999.1080p.Upscaled.BluRay.x264-X": "Upscaled",
        "The.Upscalers.2020.1080p.WEB-DL-G": None,  # nel titolo, non un upscale
    }
    for name, edition in cases.items():
        assert detect(name)["edition"] == edition, name

    job = SimpleNamespace(title="Absolute Cinema", year=1895, content_type="movie", kind="movie", seasons_json="[]",
                          episode=None)
    values = release_values(job, detect("Absolute.Cinema.1895.2160p.AI.Upscaled.BluRay.x265-MaTiTa.mkv"), None, {},
                            None)
    with_edition = {"templates": {"default": "{title} {year} {edition} {resolution} {group}"}}
    assert build_name(with_edition, values) == "Absolute Cinema 1895 AI Upscaled 2160p-MaTiTa"
    # Il modello predefinito la prevede; uno senza {edition} non la mette: decide il profilo.
    assert build_name(None, values) == "Absolute Cinema (1895) AI Upscaled 2160p BluRay x265-MaTiTa"
    without = {"templates": {"default": "{title} {year} {resolution} {group}"}}
    assert build_name(without, values) == "Absolute Cinema 1895 2160p-MaTiTa"


# Il file segnalato (2026-10-05): "ballerina.mkv", un remux UHD che il nome non
# descrive. MediaInfo ridotto ai campi che contano.
_UHD_DISC_VIDEO = {
    "format": "HEVC", "height": 2160, "width": 3840, "bit_rate": 74_400_000, "writing_library": None,
    "encoding_settings": False, "hdr_format": "Dolby Vision / SMPTE ST 2086", "hdr_format_profile": "dvhe.07.06",
    "hdr_format_string": "Dolby Vision, Version 1.0, Profile 7.6, dvhe.07.06, BL+EL+RPU, no metadata compression",
}
_TRUEHD = {"format": "MLP FBA 16-ch", "commercial_name": "Dolby TrueHD with Dolby Atmos", "channels": 8,
           "language": "en"}
_PGS = {"format": "PGS", "language": "en"}


def test_a_renamed_uhd_remux_is_recognized_from_its_mediainfo():
    from nazgarr.upload.naming import detect, release_values

    mediainfo = {"video": _UHD_DISC_VIDEO, "audio": [_TRUEHD], "subtitles": [_PGS]}
    values = release_values(_job(), detect("ballerina.mkv"), mediainfo, {}, None)

    assert (values["type"], values["source"], values["video_codec"]) == ("REMUX", "BluRay", "HEVC")
    assert values["type_basis"]["type"] == "disc_no_encoder"
    assert values["type_basis"]["source"] == "mediainfo"
    assert values["type_basis"]["evidence"] == ["dv_el", "lossless", "pgs", "bitrate"]


def test_a_mux_with_web_video_and_disc_audio_gets_no_disc_source():
    # DLMux: video web (niente encoder, bitrate basso), audio e sub dal Blu-ray.
    from nazgarr.upload.naming import detect, release_values

    video = {"format": "HEVC", "height": 2160, "width": 3840, "bit_rate": 16_000_000, "writing_library": None}
    values = release_values(_job(), detect("film.mkv"), {"video": video, "audio": [_TRUEHD], "subtitles": [_PGS]},
                            {}, None)

    assert values["type"] == "ENCODE" and not values["source"]
    assert values["type_basis"] == {"type": "default", "source": None, "evidence": ["lossless", "pgs"],
                                    "encoder": None}


def test_a_disc_encode_gets_its_source_and_stays_an_encode():
    from nazgarr.upload.naming import detect, release_values

    video = {"format": "HEVC", "height": 1080, "width": 1920, "bit_rate": 9_000_000, "writing_library": "x265 3.5",
             "encoding_settings": True}
    values = release_values(_job(), detect("film.mkv"), {"video": video, "audio": [_TRUEHD], "subtitles": [_PGS]},
                            {}, None)

    assert (values["type"], values["source"], values["video_codec"]) == ("ENCODE", "BluRay", "x265")
    assert (values["type_basis"]["type"], values["type_basis"]["encoder"]) == ("encoder", "x265 3.5")


def test_the_original_source_medium_of_mediainfo_names_the_disc():
    # MakeMKV scrive da quale disco viene ogni traccia: basta da solo.
    from nazgarr.upload.naming import detect, release_values

    video = {"format": "AVC", "height": 1080, "width": 1920, "bit_rate": 9_000_000, "writing_library": None,
             "original_source_medium": "Blu-ray"}
    remux = release_values(_job(), detect("film.mkv"), {"video": video}, {}, None)
    assert (remux["type"], remux["source"], remux["video_codec"]) == ("REMUX", "BluRay", "AVC")
    assert remux["type_basis"]["evidence"] == ["origin"]

    hd_dvd = release_values(_job(), detect("film.mkv"), {"video": {**video, "original_source_medium": "HD DVD"}},
                            {}, None)
    assert hd_dvd["source"] == "HDDVD"
    # Encodato da quel disco: la sorgente sì, il remux no.
    encode = release_values(_job(), detect("film.mkv"),
                            {"video": {**video, "writing_library": "x264 core 164"}}, {}, None)
    assert (encode["type"], encode["source"]) == ("ENCODE", "BluRay")


def test_a_joined_bdremux_is_a_remux_and_profile_8_makes_it_hybrid():
    # Segnalato (2026-10-05): guessit legge "BD-Remux" ma non "BDRemux" attaccato,
    # e senza REMUX il controllo del Dolby Vision profilo 8 non partiva.
    from nazgarr.upload.naming import detect, release_values

    p8 = {"video": {"format": "HEVC", "height": 2160, "width": 3840, "bit_rate": 43_300_000, "writing_library": None,
                    "hdr_format": "Dolby Vision / SMPTE ST 2086", "hdr_format_profile": "dvhe.08.06",
                    "hdr_format_string": "Dolby Vision, Version 1.0, dvhe.08.06, BL+RPU, HDR10 compatible"}}
    values = release_values(_job(), detect("The.Movie.2004.4K.HDR.DV.2160p.BDRemux Ita Eng x265-GRP"), p8, {}, None)
    assert (values["type"], values["source"], values["video_codec"], values["hybrid"]) == (
        "REMUX", "BluRay", "HEVC", "HYBRID")

    assert (detect("Movie.2004.2160p.UHDRemux-GRP")["type"], detect("Movie.2004.2160p.UHDRemux-GRP")["source"]) == (
        "REMUX", "BluRay")
    assert detect("Movie.2004.576p.DVDRemux-GRP")["source"] == "DVD"


def test_bamtech_audio_means_a_web_dl(db_session):
    # Segnalato (2026-10-10): l'audio codificato da BAMTech (Disney+, Hulu,
    # ESPN+) dice che la sorgente è un WEB-DL, anche quando il nome non lo dice.
    from nazgarr.upload.naming import detect, release_values, streaming_audio

    video = {"format": "HEVC", "height": 2160, "writing_library": "x265 - 3.5"}  # certi servizi lo lasciano
    bamtech = {"language": "en", "format": "E-AC-3", "channels": 6, "writing_library": "BAMTech"}
    mediainfo = {"video": video, "audio": [bamtech]}

    plain = release_values(_job(), detect("film.mkv"), mediainfo, {}, None)
    assert (plain["type"], plain["source"]) == ("WEBDL", "WEB-DL")
    assert plain["type_basis"]["type"] == "web_audio" and plain["type_basis"]["source"] == "web_audio"
    # Il nome dice x265: un encode da quel WEB-DL.
    encode = release_values(_job(), detect("Film.2023.2160p.x265-GRP"), mediainfo, {}, None)
    assert (encode["type"], encode["source"]) == ("WEBRIP", "WEBRip")
    # La sorgente del nome vince sempre.
    named = release_values(_job(), detect("Film.2023.2160p.BluRay.x265-GRP"), mediainfo, {}, None)
    assert named["source"] == "BluRay"
    # Solo nel MediaInfo completo (riepiloghi salvati prima, senza la libreria dell'audio).
    text = "General\nFormat : Matroska\n\nAudio\nID : 2\nWriting library : BAMTech\n"
    from_text = release_values(_job(mediainfo_text=text), detect("film.mkv"),
                               {"video": video, "audio": [{"format": "E-AC-3"}]}, {}, None)
    assert from_text["source"] == "WEB-DL"
    assert not streaming_audio([{"title": "English"}], "General\nTitle : BAMTech\n")  # solo nell'audio
