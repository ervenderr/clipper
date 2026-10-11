"""Local UI (127.0.0.1 only). Uploads happen only when you press Post, or opt in to auto-upload."""
import json
import re
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from . import download as dl
from . import pipeline, store, youtube
from .config import CACHE_DIR, TOKEN_FILE, YT_DAILY_QUOTA, YT_UPLOAD_COST

app = FastAPI()
STATIC = Path(__file__).resolve().parent.parent / "static"
EDITABLE = ("pending", "approved", "failed")
jobs: dict[str, dict] = {}
_lock = threading.Lock()


class UrlReq(BaseModel):
    url: str


class ProcessReq(BaseModel):
    url: str
    count: int = Field(5, ge=1, le=pipeline.MAX_AUTO_CLIPS)
    auto: str | None = None   # None | "next_slot" | "private": upload everything when ready


class PostReq(BaseModel):
    mode: str = "next_slot"   # next_slot | at | private
    at: str | None = None


class EditReq(BaseModel):
    yt_title: str | None = Field(None, max_length=100)
    description: str | None = Field(None, max_length=5000)
    tags: list[str] | None = None
    ai_altered: bool | None = None


def _clip(vid: str, n: int) -> dict:
    try:
        return next(c for c in store.load(vid)["clips"] if c["index"] == n)
    except (FileNotFoundError, ValueError, StopIteration):
        raise HTTPException(404, "clip not found")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


def _title(vid: str) -> str:
    data = store.load(vid)
    if data.get("title"):
        return data["title"]
    meta = CACHE_DIR / vid / "meta.json"   # older batches: fall back to cached metadata
    return json.loads(meta.read_text()).get("title", vid) if meta.exists() else vid


@app.get("/api/videos")
def videos():
    out = [{"id": v, "title": _title(v), "clips": len(store.load(v)["clips"])}
           for v in store.list_videos()]
    return sorted(out, key=lambda x: (store.video_dir(x["id"]) / "clips.json").stat().st_mtime,
                  reverse=True)


@app.get("/api/queue")
def queue():
    """Clips scheduled on YouTube, soonest first."""
    rows = []
    for v in store.list_videos():
        for c in store.load(v)["clips"]:
            if c["status"] == "scheduled" and c.get("publish_at") and \
                    c["publish_at"] > youtube.iso(datetime.now(timezone.utc)):
                rows.append({"video_id": v, "index": c["index"], "title": c["yt_title"],
                             "publish_at": c["publish_at"], "youtube_id": c["youtube_id"]})
    return sorted(rows, key=lambda r: r["publish_at"])


@app.get("/thumb/{vid}/{n}")
def thumb(vid: str, n: int):
    """Poster frame for a clip; generated once from the mp4, then cached on disk."""
    mp4 = store.video_dir(vid) / f"clip_{n:02d}.mp4"
    jpg = mp4.with_suffix(".poster.jpg")
    if not mp4.exists():
        raise HTTPException(404)
    if not jpg.exists():
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", "3", "-i", str(mp4),
                        "-frames:v", "1", "-vf", "scale=540:-2", "-q:v", "4", str(jpg)],
                       check=True)
    return FileResponse(jpg, media_type="image/jpeg")


@app.get("/api/videos/{vid}")
def video(vid: str):
    try:
        return store.load(vid)
    except (FileNotFoundError, ValueError):
        raise HTTPException(404, "not found")


@app.get("/files/{vid}/{name}")
def file(vid: str, name: str):
    if not re.fullmatch(r"clip_\d+\.mp4", name):
        raise HTTPException(404)
    p = store.video_dir(vid) / name
    if not p.exists():
        raise HTTPException(404)
    return FileResponse(p, media_type="video/mp4")


def _check_url(url: str) -> None:
    u = urlparse(url)
    host = (u.hostname or "").lower()
    if u.scheme not in ("http", "https") or not (
            host in ("youtu.be", "youtube.com") or host.endswith(".youtube.com")):
        raise HTTPException(400, "Provide a YouTube URL")


posting: dict = {"state": "idle", "done": 0, "total": 0, "log": []}


def _post_many(vid: str, mode: str) -> None:
    """Upload every not-yet-uploaded clip of a video, stopping cleanly on quota."""
    clips = [c for c in store.load(vid)["clips"]
             if c["status"] in ("pending", "approved") and not c.get("upload_started")]
    posting.update(state="running", done=0, total=len(clips), log=[])
    try:
        service = youtube.get_service(interactive=False)
        for c in clips:
            youtube.upload_clip(service, vid, c["index"], mode, log=posting["log"].append)
            posting["done"] += 1
        posting["state"] = "done"
    except youtube.QuotaExhausted as e:
        posting["log"].append(f"Quota reached ({e}); the rest can go out tomorrow.")
        posting["state"] = "quota"
    except Exception as e:
        posting["log"].append(f"ERROR: {e}")
        posting["state"] = "error"


@app.post("/api/probe")
def probe(req: UrlReq):
    _check_url(req.url)
    try:
        vid = dl.video_id(req.url)
        meta = dl.metadata(req.url, vid)
    except Exception:
        raise HTTPException(400, "Could not read that video. Check the link is public.")
    return {"video_id": vid, **meta, "suggested": pipeline.suggest_count(meta["duration"]),
            "max": pipeline.MAX_AUTO_CLIPS}


@app.get("/api/status")
def status():
    used = youtube.quota_used()
    return {"job": next(reversed(jobs.values()), None), "posting": posting,
            "quota": {"used": used, "limit": YT_DAILY_QUOTA,
                      "left": max(0, (YT_DAILY_QUOTA - used) // YT_UPLOAD_COST)},
            "signed_in": TOKEN_FILE.exists(), "schedule": youtube.load_schedule()}


@app.post("/api/process")
def process(req: ProcessReq):
    _check_url(req.url)
    if req.auto not in (None, "next_slot", "private"):
        raise HTTPException(400, "bad auto mode")
    with _lock:
        if any(j["state"] == "running" for j in jobs.values()) or posting["state"] == "running":
            raise HTTPException(409, "A job is already running")
        job_id = str(len(jobs) + 1)
        jobs[job_id] = job = {"id": job_id, "state": "running", "stage": "starting", "url": req.url,
                              "count": req.count, "auto": req.auto, "meta": None, "picks": [],
                              "rendered": [], "video_id": None, "words": 0, "log": [],
                              "t0": time.time(), "stage_times": {}}

    def emit(**kw):
        if "stage" in kw:
            job["stage_times"][kw["stage"]] = round(time.time() - job["t0"], 1)
        if "rendered" in kw:
            job["rendered"].append(kw.pop("rendered"))
        job.update(kw)

    def work():
        try:
            job["video_id"] = pipeline.run(req.url, req.count, log=job["log"].append, emit=emit)
            job["stage"] = "done"
            if req.auto:
                job["stage"] = "uploading"
                _post_many(job["video_id"], req.auto)
                job["stage"] = "done"
            job["state"] = "done"
        except Exception as e:
            job["log"].append(f"ERROR: {e}")
            job["state"], job["error"] = "error", str(e)[:300]

    threading.Thread(target=work, daemon=True).start()
    return {"ok": True, "id": job_id}


@app.post("/api/videos/{vid}/clips/{n}/post")
def post_clip(vid: str, n: int, req: PostReq):
    _clip(vid, n)
    try:
        return youtube.upload_clip(youtube.get_service(interactive=False), vid, n,
                                   req.mode, req.at)
    except youtube.QuotaExhausted as e:
        raise HTTPException(429, f"Daily upload quota reached ({e}). Try tomorrow.")
    except ValueError as e:
        raise HTTPException(409, str(e))
    except Exception as e:
        raise HTTPException(502, f"Upload failed: {str(e)[:200]}")


@app.post("/api/videos/{vid}/post-all")
def post_all(vid: str, req: PostReq):
    if req.mode not in ("next_slot", "private"):
        raise HTTPException(400, "mode must be next_slot or private")
    with _lock:
        if posting["state"] == "running" or any(j["state"] == "running" for j in jobs.values()):
            raise HTTPException(409, "A job is already running")
        posting["state"] = "running"
    threading.Thread(target=_post_many, args=(vid, req.mode), daemon=True).start()
    return {"ok": True}


@app.patch("/api/videos/{vid}/clips/{n}")
def edit(vid: str, n: int, req: EditReq):
    if _clip(vid, n)["status"] not in EDITABLE:
        raise HTTPException(409, "clip already scheduled/uploaded")
    fields = {k: v for k, v in req.model_dump().items() if v is not None}
    return store.update_clip(vid, n, **fields)


@app.post("/api/videos/{vid}/clips/{n}/{action}")
def set_state(vid: str, n: int, action: str):
    st = _clip(vid, n)["status"]
    if action == "approve" and st in ("pending", "failed"):
        return store.update_clip(vid, n, status="approved", error=None)
    if action == "unapprove" and st == "approved":
        return store.update_clip(vid, n, status="pending")
    raise HTTPException(409, f"cannot {action} a {st} clip")


@app.delete("/api/videos/{vid}/clips/{n}")
def delete(vid: str, n: int):
    c = _clip(vid, n)
    if c["status"] not in EDITABLE or c.get("upload_started"):
        raise HTTPException(409, "clip already scheduled/uploaded")
    data = store.load(vid)
    data["clips"] = [x for x in data["clips"] if x["index"] != n]
    store.save(vid, data)
    (store.video_dir(vid) / c["file"]).unlink(missing_ok=True)
    return {"ok": True}
