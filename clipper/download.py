import json
import re
import subprocess
from pathlib import Path

from .config import CACHE_DIR

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


def video_id(url: str) -> str:
    out = subprocess.run(["yt-dlp", "--no-playlist", "--get-id", url],
                         capture_output=True, text=True, check=True).stdout.strip()
    if not _ID_RE.match(out):
        raise ValueError(f"Unexpected video id from yt-dlp: {out!r}")
    return out


def download(url: str, vid: str) -> Path:
    """Download best mp4 (<=1080p) into cache/<id>/source.mp4; reuse if present."""
    d = CACHE_DIR / vid
    d.mkdir(parents=True, exist_ok=True)
    dest = d / "source.mp4"
    if dest.exists():
        return dest
    subprocess.run(
        ["yt-dlp", "--no-playlist", "-f", "bv*[height<=1080]+ba/b[height<=1080]/b",
         "--merge-output-format", "mp4", "-o", str(d / "source.%(ext)s"), url],
        check=True)
    return dest


def metadata(url: str, vid: str) -> dict:
    """Source title/channel for credit lines; cached."""
    f = CACHE_DIR / vid / "meta.json"
    if f.exists() and "duration" in json.loads(f.read_text()):
        return json.loads(f.read_text())
    out = subprocess.run(
        ["yt-dlp", "--no-playlist", "--print", "%(title)s\n%(channel)s\n%(duration)s", url],
        capture_output=True, text=True, check=True).stdout.splitlines()
    meta = {"title": out[0], "channel": out[1] if len(out) > 1 else "",
            "duration": float(out[2]) if len(out) > 2 and out[2] not in ("NA", "") else 0.0}
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(meta))
    return meta
