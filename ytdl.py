"""
ytdl.py — Resolve YouTube (and other yt-dlp-supported) URLs to a local file.

ASCILINE downscales every frame to a tiny character grid, so there is no point
pulling high resolution. We cap at <=480p and mux to a single mp4 with audio
(the /audio endpoint runs ffmpeg on the same file). Downloads are cached in
videos/ by video id so re-runs are instant.
"""
import os
import sys
import subprocess

_URL_HINTS = ("http://", "https://", "youtube.com", "youtu.be")


def is_url(s: str) -> bool:
    s = s.lower()
    return s.startswith(("http://", "https://")) or "youtube.com" in s or "youtu.be" in s


def _ytdlp(*args: str) -> subprocess.CompletedProcess:
    # Use the running interpreter's yt_dlp so it always matches the venv.
    return subprocess.run([sys.executable, "-m", "yt_dlp", *args],
                          capture_output=True, text=True)


def download(url: str, cache_dir: str = "videos") -> str:
    """Download `url` (<=480p, muxed mp4) into cache_dir and return the path."""
    os.makedirs(cache_dir, exist_ok=True)

    probe = _ytdlp("--no-playlist", "--print", "id", url)
    if probe.returncode != 0 or not probe.stdout.strip():
        raise RuntimeError(f"yt-dlp could not read {url!r}: {probe.stderr.strip()[:200]}")
    video_id = probe.stdout.strip().splitlines()[0]

    out = os.path.join(cache_dir, f"{video_id}.mp4")
    if os.path.exists(out):
        print(f"[YT] cached: {out}")
        return out

    print(f"[YT] downloading {url}  (<=480p) ...")
    # Prefer H.264 (avc1): OpenCV decodes it everywhere, unlike AV1/VP9 which
    # need hardware support. Fall back to anything <=480p, then re-encode below.
    fmt = ("bv*[vcodec^=avc1][height<=480]+ba/"
           "b[vcodec^=avc1][height<=480]/"
           "bv*[height<=480]+ba/b[height<=480]/b")
    res = _ytdlp("--no-playlist", "-f", fmt,
                 "--merge-output-format", "mp4", "-o", out, url)
    if res.returncode != 0 or not os.path.exists(out):
        raise RuntimeError(f"yt-dlp download failed: {res.stderr.strip()[-300:]}")

    if not _decodable(out):
        print("[YT] codec not decodable (likely AV1/VP9) — re-encoding to H.264 ...")
        _reencode_h264(out)
    print(f"[YT] saved: {out}")
    return out


def _decodable(path: str) -> bool:
    """True if OpenCV can actually read the first frame."""
    try:
        import cv2
    except ImportError:
        return True  # can't check; assume fine
    cap = cv2.VideoCapture(path)
    ok, _ = cap.read()
    cap.release()
    return ok


def _reencode_h264(path: str) -> None:
    """Transcode in place to H.264 + AAC so OpenCV/ffmpeg can read it."""
    tmp = path + ".h264.mp4"
    res = subprocess.run(
        ["ffmpeg", "-y", "-i", path, "-c:v", "libx264", "-preset", "veryfast",
         "-crf", "23", "-c:a", "aac", "-b:a", "128k", "-loglevel", "error", tmp],
        capture_output=True, text=True)
    if res.returncode != 0 or not os.path.exists(tmp):
        raise RuntimeError(f"re-encode failed: {res.stderr.strip()[-300:]}")
    os.replace(tmp, path)
