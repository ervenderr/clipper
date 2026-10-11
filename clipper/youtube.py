"""YouTube upload: OAuth, schedule, quota tracking, resumable upload with backoff."""
import json
import os
import random
import threading
import time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import store
from .config import (CLIENT_SECRET, DEFAULT_SCHEDULE, MAX_UPLOAD_RETRIES, QUOTA_FILE,
                     SCHEDULE_FILE, TOKEN_FILE, YT_CATEGORY_ID, YT_DAILY_QUOTA, YT_QUOTA_TZ,
                     YT_SCOPE, YT_UPLOAD_COST)

RETRIABLE = {500, 502, 503, 504}
MIN_LEAD = timedelta(minutes=20)  # publishAt must be comfortably in the future


class QuotaExhausted(Exception):
    pass


# ---------- auth ----------
_upload_lock = threading.Lock()  # one upload at a time: keeps slots and quota consistent


def get_service(interactive: bool = True):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build
    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), YT_SCOPE)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not interactive:
                raise RuntimeError("YouTube login expired; run: python clip.py auth")
            if not CLIENT_SECRET.exists():
                raise RuntimeError("client_secret.json missing — see README 'YouTube setup'")
            creds = InstalledAppFlow.from_client_secrets_file(
                str(CLIENT_SECRET), YT_SCOPE).run_local_server(port=0)
        fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(creds.to_json())
    return build("youtube", "v3", credentials=creds)


# ---------- quota ----------
def _quota_day() -> str:
    return datetime.now(ZoneInfo(YT_QUOTA_TZ)).date().isoformat()


def quota_used() -> int:
    if QUOTA_FILE.exists():
        q = json.loads(QUOTA_FILE.read_text())
        if q.get("date") == _quota_day():
            return q["used"]
    return 0


def quota_add(units: int) -> None:
    QUOTA_FILE.write_text(json.dumps({"date": _quota_day(), "used": quota_used() + units}))


def can_afford() -> bool:
    return quota_used() + YT_UPLOAD_COST <= YT_DAILY_QUOTA


# ---------- schedule ----------
def load_schedule() -> dict:
    if SCHEDULE_FILE.exists():
        return {**DEFAULT_SCHEDULE, **json.loads(SCHEDULE_FILE.read_text())}
    return DEFAULT_SCHEDULE


def next_slot(taken: set[str], now: datetime | None = None, sched: dict | None = None) -> datetime:
    """Earliest configured local-time slot > now+MIN_LEAD not in `taken` (UTC iso strings)."""
    sched = sched or load_schedule()
    tz = ZoneInfo(sched["timezone"])
    now = now or datetime.now(timezone.utc)
    day = now.astimezone(tz).date()
    for offset in range(0, 3650):
        for t in sorted(sched["times"]):
            h, m = map(int, t.split(":"))
            slot = datetime.combine(day + timedelta(days=offset), datetime.min.time(),
                                    tzinfo=tz).replace(hour=h, minute=m).astimezone(timezone.utc)
            if slot > now + MIN_LEAD and iso(slot) not in taken:
                return slot
    raise RuntimeError("no free schedule slot")


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


# ---------- request body ----------
def build_body(clip: dict, publish_at: datetime | None) -> dict:
    desc = clip["description"].strip()
    if "#shorts" not in desc.lower():
        desc += "\n\n#Shorts"
    status = {"privacyStatus": "private", "selfDeclaredMadeForKids": False}
    if publish_at:
        status["publishAt"] = iso(publish_at)
    if clip.get("ai_altered"):
        status["containsSyntheticMedia"] = True  # status.containsSyntheticMedia (A/S content)
    return {"snippet": {"title": clip["yt_title"][:100], "description": desc[:5000],
                        "tags": clip["tags"], "categoryId": YT_CATEGORY_ID},
            "status": status}


# ---------- upload ----------
def upload_one(service, path: str, body: dict, sleep=time.sleep) -> dict:
    """Resumable upload with exponential backoff. Raises QuotaExhausted on quota errors."""
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload
    media = MediaFileUpload(path, mimetype="video/mp4", chunksize=8 * 1024 * 1024, resumable=True)
    req = service.videos().insert(part="snippet,status", body=body, media_body=media)
    resp, retries = None, 0
    while resp is None:
        try:
            _, resp = req.next_chunk()
        except HttpError as e:
            if e.resp.status == 403 and b"quota" in (e.content or b"").lower():
                raise QuotaExhausted() from e
            if e.resp.status not in RETRIABLE:
                raise
            retries = _backoff(retries, e, sleep)
        except (ConnectionError, TimeoutError, OSError) as e:
            retries = _backoff(retries, e, sleep)
    return resp


def _backoff(retries: int, err: Exception, sleep) -> int:
    retries += 1
    if retries > MAX_UPLOAD_RETRIES:
        raise RuntimeError(f"upload failed after {MAX_UPLOAD_RETRIES} retries: {err}") from err
    delay = random.uniform(0, 2 ** retries)
    print(f"  retry {retries}/{MAX_UPLOAD_RETRIES} in {delay:.1f}s ({type(err).__name__})")
    sleep(delay)
    return retries


# ---------- publish ----------
def _all_clips():
    for vid in store.list_videos():
        for c in store.load(vid)["clips"]:
            yield vid, c


def mark_published() -> None:
    """scheduled -> uploaded once publish_at has passed."""
    now = iso(datetime.now(timezone.utc))
    for vid, c in list(_all_clips()):
        if c["status"] == "scheduled" and c["publish_at"] and c["publish_at"] <= now:
            store.update_clip(vid, c["index"], status="uploaded")


def upload_clip(service, vid: str, index: int, mode: str = "next_slot", at: str | None = None,
                log=print) -> dict:
    """Upload ONE clip. mode: next_slot | at (ISO time) | private (no schedule).
    Calling this is the approval. Refuses anything already started/uploaded."""
    with _upload_lock:
        clip = next((c for c in store.load(vid)["clips"] if c["index"] == index), None)
        if clip is None:
            raise ValueError("clip not found")
        if clip.get("youtube_id") or clip.get("upload_started") or \
                clip["status"] in ("scheduled", "uploaded"):
            raise ValueError("clip was already uploaded or is mid-upload")
        if not can_afford():
            raise QuotaExhausted(f"quota {quota_used()}/{YT_DAILY_QUOTA} used today")
        if mode == "private":
            slot = None
        elif mode == "at":
            slot = datetime.fromisoformat((at or "").replace("Z", "+00:00"))
            if slot.tzinfo is None or slot <= datetime.now(timezone.utc) + MIN_LEAD:
                raise ValueError("pick a time with timezone, at least 20 minutes from now")
        elif mode == "next_slot":
            slot = next_slot({c["publish_at"] for _, c in _all_clips() if c.get("publish_at")})
        else:
            raise ValueError(f"unknown mode {mode!r}")
        # Mark BEFORE the network call so a crash can never cause a duplicate upload.
        store.update_clip(vid, index, upload_started=iso(datetime.now(timezone.utc)),
                          status="approved")
        try:
            resp = upload_one(service, str(store.video_dir(vid) / clip["file"]),
                              build_body(clip, slot))
        except QuotaExhausted:
            store.update_clip(vid, index, upload_started=None)
            quota_add(YT_DAILY_QUOTA)
            raise
        except Exception as e:
            store.update_clip(vid, index, status="failed", error=str(e)[:300],
                              upload_started=None)
            raise
        quota_add(YT_UPLOAD_COST)
        done = store.update_clip(vid, index, status="scheduled" if slot else "uploaded",
                                 youtube_id=resp["id"], error=None,
                                 publish_at=iso(slot) if slot else None)
    log(f"uploaded {vid}/clip_{index:02d} -> https://youtu.be/{resp['id']} "
        f"({'publishes ' + iso(slot) if slot else 'private, unscheduled'})")
    _warn_if_locked(service, resp["id"], log)
    return done


def publish_all(service=None, log=print) -> dict:
    """CLI/scheduler entry: upload every approved clip into the next free slots."""
    mark_published()
    queue = [(v, c) for v, c in _all_clips() if c["status"] == "approved"]
    result = {"uploaded": 0, "failed": 0, "carried": 0, "skipped": 0}
    if not queue:
        log("No approved clips to upload.")
        return result
    service = service or get_service()
    for n, (vid, c) in enumerate(queue):
        if c.get("youtube_id") or c.get("upload_started"):
            log(f"skip {vid}/clip_{c['index']:02d}: already started/uploaded "
                "(check YouTube Studio; clear 'upload_started' in clips.json to retry)")
            result["skipped"] += 1
            continue
        try:
            upload_clip(service, vid, c["index"], "next_slot", log=log)
            result["uploaded"] += 1
        except QuotaExhausted:
            result["carried"] = len(queue) - n - result["skipped"]
            log(f"Quota reached; {result['carried']} clip(s) carried to the next day.")
            break
        except Exception as e:
            result["failed"] += 1
            log(f"FAILED {vid}/clip_{c['index']:02d}: {e}")
    return result


def _warn_if_locked(service, yt_id: str, log) -> None:
    """Best-effort check that the schedule stuck; unverified projects may be forced private."""
    try:
        st = service.videos().list(part="status", id=yt_id).execute()["items"][0]["status"]
    except Exception:
        return
    if st.get("privacyStatus") != "private" or not st.get("publishAt"):
        log("WARNING: video did not keep private+publishAt. See README 'Unverified project'.")
