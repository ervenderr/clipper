import time
from datetime import datetime, timezone

from . import download as dl
from . import highlights, store, transcribe as tr
import re

from .config import CTA, MAX_HASHTAGS, OUTPUT_DIR
from .render import make_clip


MAX_AUTO_CLIPS = 20
SECONDS_PER_CLIP = 150  # about one good moment per 2.5 minutes of source


def suggest_count(duration_s: float) -> int:
    return max(1, min(MAX_AUTO_CLIPS, round(duration_s / SECONDS_PER_CLIP)))


def build_description(hook: str, tags: list[str], meta: dict, url: str) -> str:
    """Line 1 hook, line 2 source credit, line 3 CTA, then a few niche hashtags."""
    credit = f'From "{meta["title"]}"' + (f' by {meta["channel"]}' if meta.get("channel") else "")
    tags_ = []
    for t in tags:
        h = re.sub(r"[^a-z0-9]", "", t.lower())
        if h and f"#{h}" not in tags_:
            tags_.append(f"#{h}")
    return f"{hook.strip()}\n\n{credit}: {url}\n\n{CTA}\n\n{' '.join(tags_[:MAX_HASHTAGS])}".strip()


def run(url: str, count: int, log=print, emit=lambda **kw: None) -> str:
    t0 = time.time()
    lap = lambda msg: log(f"[{time.time() - t0:6.1f}s] {msg}")
    vid = dl.video_id(url)
    emit(stage="download", video_id=vid)
    meta = dl.metadata(url, vid)
    emit(meta=meta)
    src = dl.download(url, vid)
    lap("downloaded")
    emit(stage="transcribe")
    words = tr.transcribe(src, vid)
    lap(f"transcribed ({len(words)} words)")
    emit(stage="select", words=len(words))
    sents = tr.sentences(words)
    picks = highlights.find_highlights(sents, count)
    lap(f"selected {len(picks)} clips")
    emit(stage="render", picks=[{"index": i, "start": p["start"], "end": p["end"],
                                 "title": p["yt_title"], "score": p["score"]}
                                for i, p in enumerate(picks, 1)])
    out = OUTPUT_DIR / vid
    if (out / 'clips.json').exists() and any(
            c['status'] != 'pending' for c in store.load(vid)['clips']):
        raise RuntimeError(f'{vid} already has approved/uploaded clips; refusing to overwrite')
    out.mkdir(parents=True, exist_ok=True)
    clips = []
    for n, p in enumerate(picks, 1):
        name = f"clip_{n:02d}.mp4"
        (out / name).with_suffix(".jpg").unlink(missing_ok=True)  # drop stale thumbnail
        make_clip(src, words, p["start"], p["end"], out / name)
        lap(f"rendered {name} ({p['end'] - p['start']:.0f}s)")
        emit(rendered=n)
        clips.append({"index": n, "file": name, "start": p["start"], "end": p["end"],
                      "hook": p["hook"], "score": p["score"], "reason": p["reason"],
                      "yt_title": p["yt_title"], "description": build_description(p["description"], p["tags"], meta, url),
                      "tags": p["tags"], "ai_altered": False, "status": "pending",
                      "publish_at": None, "youtube_id": None, "error": None})
    store.save(vid, {"video_id": vid, "url": url, "title": meta["title"], "channel": meta["channel"],
                     "created": datetime.now(timezone.utc).isoformat(), "clips": clips})
    lap("done")
    emit(stage="done")
    return vid
