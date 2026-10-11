"""Central config. Secrets come from .env only; never print them."""
import json
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

OUTPUT_DIR = ROOT / "output"
CACHE_DIR = ROOT / "cache"
FACE_MODEL = ROOT / "models" / "face_detection_yunet_2023mar.onnx"
CLIENT_SECRET = ROOT / "client_secret.json"
TOKEN_FILE = ROOT / "token.json"
QUOTA_FILE = ROOT / "quota.json"
SCHEDULE_FILE = ROOT / "schedule.json"

WHISPER_MODEL = "mlx-community/whisper-large-v3-turbo"

MIN_CLIP_S, MAX_CLIP_S = 20.0, 40.0   # short clips hold retention best
CHUNK_CHARS = 14000          # transcript chars per LLM call
OUT_W, OUT_H, OUT_FPS = 1080, 1920, 30

# YouTube
YT_SCOPE = ["https://www.googleapis.com/auth/youtube.upload"]
YT_CATEGORY_ID = "22"        # People & Blogs; change if needed
# Docs disagree on videos.insert cost (1600 units vs. its own 100/day bucket).
# Stay conservative: 1600 units against 10,000/day => 6 uploads/day.
YT_UPLOAD_COST = int(os.getenv("YT_UPLOAD_COST", "1600"))
YT_DAILY_QUOTA = int(os.getenv("YT_DAILY_QUOTA", "10000"))
YT_QUOTA_TZ = "America/Los_Angeles"   # quota resets at midnight Pacific
MAX_UPLOAD_RETRIES = 6

DEFAULT_SCHEDULE = {"timezone": "Asia/Manila", "times": ["12:00", "19:00"]}  # 2/day; fresh > bursty
CTA = "Follow for more."
MAX_HASHTAGS = 3


def llm_settings() -> dict:
    """Return LLM settings; raise with a clear message if any are missing."""
    s = {
        "provider": os.getenv("LLM_PROVIDER", "").strip().lower(),
        "api_key": os.getenv("LLM_API_KEY", "").strip(),
        "base_url": os.getenv("LLM_BASE_URL", "").strip().rstrip("/"),
        "model": os.getenv("LLM_MODEL", "").strip(),
    }
    extra = os.getenv("LLM_EXTRA_BODY", "").strip()  # optional JSON merged into the request
    s["extra_body"] = json.loads(extra) if extra else {}
    if s["provider"] == "anthropic" and not s["base_url"]:
        s["base_url"] = "https://api.anthropic.com"
    missing = [k for k, v in s.items() if not v and k != "extra_body"]
    if missing:
        raise RuntimeError(f"Missing LLM settings in .env: {', '.join(missing)}")
    return s
