"""clips.json per video: atomic reads/writes."""
import json
import os
import re
import tempfile
from pathlib import Path

from .config import OUTPUT_DIR

STATUSES = ("pending", "approved", "scheduled", "uploaded", "failed")
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


def video_dir(vid: str) -> Path:
    if not _ID_RE.match(vid):
        raise ValueError("invalid video id")
    return OUTPUT_DIR / vid


def load(vid: str) -> dict:
    return json.loads((video_dir(vid) / "clips.json").read_text())


def save(vid: str, data: dict) -> None:
    d = video_dir(vid)
    d.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, d / "clips.json")


def list_videos() -> list[str]:
    if not OUTPUT_DIR.exists():
        return []
    return sorted(p.name for p in OUTPUT_DIR.iterdir() if (p / "clips.json").exists())


def update_clip(vid: str, index: int, **fields) -> dict:
    data = load(vid)
    clip = next(c for c in data["clips"] if c["index"] == index)
    if "status" in fields and fields["status"] not in STATUSES:
        raise ValueError("invalid status")
    clip.update(fields)
    save(vid, data)
    return clip
