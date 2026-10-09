"""Richiede il binario ffmpeg reale (nessun mock: la generazione di uno
screenshot è I/O binario, non ha senso mockare ffmpeg stesso). Skippato se
ffmpeg non è installato nell'ambiente che esegue i test — presente nel
Dockerfile di produzione, non garantito ovunque in dev/CI."""

import os
import shutil
import subprocess

import pytest

from nazgarr.upload.screenshots import ScreenshotError, generate_screenshots, is_blank, luma_stats

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="richiede il binario ffmpeg")


def _make_test_video(path, duration=6):
    subprocess.run(
        [
            "ffmpeg", "-y", "-f", "lavfi", "-i", f"testsrc=duration={duration}:size=160x120:rate=5",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path),
        ],
        check=True, capture_output=True,
    )


def test_generate_screenshots_creates_requested_count(tmp_path):
    video = tmp_path / "video.mp4"
    _make_test_video(video)

    paths = generate_screenshots(str(video), str(tmp_path / "shots"), count=3)

    assert len(paths) == 3
    for p in paths:
        assert os.path.isfile(p)
        assert os.path.getsize(p) > 0


def test_generate_screenshots_raises_on_undecodable_file(tmp_path):
    fake_video = tmp_path / "not_a_video.mp4"
    fake_video.write_bytes(b"not actually a video file" * 10)

    with pytest.raises(ScreenshotError):
        generate_screenshots(str(fake_video), str(tmp_path / "shots"))


def test_a_black_frame_is_retried_a_bit_later(tmp_path):
    video = tmp_path / "video.mp4"
    # Nero fra 4 e 6 secondi: l'unico screenshot (a metà, 5s) cadrebbe lì.
    subprocess.run(
        [
            "ffmpeg", "-y", "-f", "lavfi", "-i",
            "testsrc=duration=10:size=160x120:rate=5,drawbox=enable='between(t,4,6)':color=black:t=fill",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(video),
        ],
        check=True, capture_output=True,
    )

    [path] = generate_screenshots(str(video), str(tmp_path / "shots"), count=1)

    assert not is_blank(luma_stats(path))
    assert os.listdir(tmp_path / "shots") == ["screenshot_0.png"]  # nessun tentativo rimasto


def test_black_and_flat_frames_are_blank(tmp_path):
    black, gray = tmp_path / "black.png", tmp_path / "gray.png"
    for path, color in ((black, "black"), (gray, "gray")):
        subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", f"color={color}:size=64x64", "-frames:v", "1", str(path)],
                       check=True, capture_output=True)

    assert is_blank(luma_stats(str(black)))
    assert is_blank(luma_stats(str(gray)))  # piatto: nessuna escursione
    assert is_blank(None) is False


def test_a_grainy_black_frame_from_a_10_bit_source_is_blank(tmp_path):
    # Da una sorgente a 10 bit ffmpeg scrive PNG a 16 bit: il nero con un po'
    # di grana non deve passare per un'immagine vera.
    path = tmp_path / "grain16.png"
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "color=black:size=64x64,noise=alls=6:allf=t",
                    "-frames:v", "1", "-pix_fmt", "rgb48be", str(path)], check=True, capture_output=True)

    assert is_blank(luma_stats(str(path)))


def _bit_depth(path):
    # Byte 24 del PNG (IHDR): bit per canale.
    with open(path, "rb") as f:
        return f.read(25)[24]


def test_a_10_bit_source_gives_an_8_bit_png(tmp_path):
    # Un PNG a 16 bit da una sorgente 4K a 10 bit passa i 30 MB: gli host lo rifiutano.
    video = tmp_path / "video10.mkv"
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=duration=4:size=160x120:rate=5",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p10le", str(video)], check=True, capture_output=True)

    paths = generate_screenshots(str(video), str(tmp_path / "shots"), count=1)

    assert paths[0].endswith(".png") and _bit_depth(paths[0]) == 8


def test_a_screenshot_over_the_cap_is_kept_as_a_full_size_jpeg(tmp_path, monkeypatch):
    monkeypatch.setattr("nazgarr.upload.screenshots.MAX_SCREENSHOT_BYTES", 1)
    video = tmp_path / "video.mp4"
    _make_test_video(video)

    [path] = generate_screenshots(str(video), str(tmp_path / "shots"), count=1)

    assert path.endswith(".jpg") and os.path.isfile(path)
    assert not os.path.exists(path[:-4] + ".png")
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=width,height", "-of", "csv=p=0", path],
                           check=True, capture_output=True, text=True)
    assert probe.stdout.strip() == "160,120"


def test_tonemap_only_touches_an_hdr_source(tmp_path, monkeypatch):
    # Segnalato (2026-10-09): con "Tonemap HDR" acceso, un WEB-DL SDR senza
    # metadati di colore faceva fallire ogni screenshot (zscale).
    from types import SimpleNamespace

    from nazgarr.upload import screenshots

    def track(**values):
        return SimpleNamespace(**{"transfer_characteristics": None, "hdr_format": None,
                                  "hdr_format_compatibility": None, **values})

    for found, expected in (
        (track(), None),
        (track(transfer_characteristics="BT.709"), None),
        (track(transfer_characteristics="PQ", hdr_format="SMPTE ST 2086"), "smpte2084"),
        (track(hdr_format="Dolby Vision", hdr_format_compatibility="HDR10"), "smpte2084"),
        (track(transfer_characteristics="HLG"), "arib-std-b67"),
    ):
        parsed = SimpleNamespace(video_tracks=[found])
        monkeypatch.setattr(screenshots.mediainfo_util, "parse", lambda path, parsed=parsed: parsed)
        assert screenshots._hdr_transfer("x.mkv") == expected


def test_a_tonemap_ffmpeg_refuses_falls_back_to_plain_screenshots(tmp_path, monkeypatch, caplog):
    from nazgarr.upload import screenshots

    video = tmp_path / "video.mp4"
    _make_test_video(video)
    monkeypatch.setattr(screenshots, "_hdr_transfer", lambda path: "smpte2084")
    monkeypatch.setattr(screenshots, "_tonemap_filter", lambda transfer: "nosuchfilter")

    paths = generate_screenshots(str(video), str(tmp_path / "shots"), count=2, tonemap=True)

    assert len(paths) == 2
    failures = [r.getMessage() for r in caplog.records if "Cattura screenshot fallita" in r.getMessage()]
    assert len(failures) == 1 and "nosuchfilter" in failures[0]  # il motivo di ffmpeg nel log, una volta sola
    assert any("Tonemap HDR non riuscito" in r.getMessage() for r in caplog.records)
