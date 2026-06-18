"""
Deterministic tests for ytdl.normalize() — no network required.

We synthesize the exact kinds of files YouTube serves that broke the engine
(VP9 video + Opus audio inside an mp4 container, and variable-frame-rate video)
and assert that normalize() turns them into canonical H.264 / AAC / CFR mp4s the
engine can open and time correctly.
"""
import json
import subprocess

import cv2
import pytest

import ytdl


def _run(*args):
    return subprocess.run(args, capture_output=True, text=True)


def _make_vp9_opus_mp4(path):
    """A VP9+Opus stream copied into an mp4 — non-standard, exactly what
    `--merge-output-format mp4` produces from YouTube's 'best' streams."""
    src = str(path) + ".src.mkv"
    _run("ffmpeg", "-y",
         "-f", "lavfi", "-i", "testsrc=size=320x240:rate=24:duration=1",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
         "-c:v", "libvpx-vp9", "-b:v", "200k", "-c:a", "libopus",
         "-loglevel", "error", src)
    # copy (not re-encode) into mp4 -> opus-in-mp4, the broken container
    r = _run("ffmpeg", "-y", "-i", src, "-c", "copy", "-loglevel", "error", str(path))
    assert r.returncode == 0, r.stderr


def _make_vfr_mp4(path):
    """An H.264 mp4 whose nominal rate disagrees with its average rate (VFR)."""
    r = _run("ffmpeg", "-y",
             "-f", "lavfi", "-i", "testsrc=size=320x240:rate=60:duration=1",
             "-vf", "select='not(mod(n,3))'",   # drop 2 of every 3 frames -> VFR
             "-fps_mode", "vfr", "-c:v", "libx264", "-an",
             "-loglevel", "error", str(path))
    assert r.returncode == 0, r.stderr


def _audio_decodes(path):
    r = _run("ffmpeg", "-v", "error", "-i", str(path), "-t", "0.5", "-f", "null", "-")
    return r.returncode == 0 and "Invalid data" not in r.stderr


def test_vp9_opus_mp4_is_repaired(tmp_path):
    bad = tmp_path / "bad.mp4"
    _make_vp9_opus_mp4(bad)

    info = ytdl._probe(str(bad))
    assert info["vcodec"] == "vp9"      # confirms we built the broken input
    assert info["acodec"] == "opus"

    assert ytdl.normalize(str(bad)) is True   # it had to be repaired

    fixed = ytdl._probe(str(bad))
    assert fixed["vcodec"] == "h264"
    assert fixed["acodec"] == "aac"
    assert fixed["cfr"] is True
    assert _audio_decodes(bad)                # /audio extraction now works

    cap = cv2.VideoCapture(str(bad))          # OpenCV can open + read it
    ok, _ = cap.read()
    fps = cap.get(cv2.CAP_PROP_FPS)
    cap.release()
    assert ok
    assert abs(fps - 24) < 0.5                # engine sees the real, stable FPS


def test_vfr_is_made_constant(tmp_path):
    bad = tmp_path / "vfr.mp4"
    _make_vfr_mp4(bad)
    assert ytdl._probe(str(bad))["cfr"] is False

    assert ytdl.normalize(str(bad)) is True
    assert ytdl._probe(str(bad))["cfr"] is True


def test_clean_file_is_left_alone(tmp_path):
    good = tmp_path / "good.mp4"
    r = _run("ffmpeg", "-y",
             "-f", "lavfi", "-i", "testsrc=size=320x240:rate=24:duration=1",
             "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", "24",
             "-c:a", "aac", "-loglevel", "error", str(good))
    assert r.returncode == 0, r.stderr

    assert ytdl.normalize(str(good)) is False   # fast path: no re-encode
