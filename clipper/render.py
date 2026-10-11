"""Cut a clip, reframe to 9:16 with smoothed face tracking, burn word-highlight captions."""
import subprocess
import tempfile
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from .config import FACE_MODEL, OUT_FPS, OUT_H, OUT_W

FONT = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"
CAP_Y, CAP_H, FONT_PX, MAX_TEXT_W = 1330, 320, 88, 960
WHITE, YELLOW = (255, 255, 255, 255), (255, 214, 10, 255)
DETECT_EVERY = 3        # run the face detector every Nth frame
SMOOTH_S = 0.6          # smoothing window (seconds) for camera motion
DEADBAND_PX = 6         # ignore camera moves smaller than this


# ---------- cut ----------
def cut(src: Path, start: float, end: float, dest: Path) -> None:
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{start:.2f}", "-i", str(src),
                    "-t", f"{end - start:.2f}", "-c:v", "libx264", "-preset", "veryfast",
                    "-crf", "18", "-r", str(OUT_FPS), "-c:a", "aac", "-b:a", "160k", str(dest)],
                   check=True)


# ---------- face tracking ----------
def track_centers(video: Path) -> tuple[np.ndarray, int, int]:
    """Per-frame smoothed face center x (source px). Falls back to frame center."""
    cap = cv2.VideoCapture(str(video))
    w, h = int(cap.get(3)), int(cap.get(4))
    scale = 640 / w
    det = cv2.FaceDetectorYN.create(str(FACE_MODEL), "", (640, int(h * scale)), 0.7)
    raw, i = [], 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        if i % DETECT_EVERY == 0:
            small = cv2.resize(frame, (640, int(h * scale)))
            _, faces = det.detect(small)
            if faces is not None and len(faces):
                f = max(faces, key=lambda f: f[2] * f[3])  # largest face
                raw.append((f[0] + f[2] / 2) / scale)
            else:
                raw.append(np.nan)
        i += 1
    cap.release()
    return _smooth(np.array(raw), i, w), w, h


def _smooth(raw: np.ndarray, n_frames: int, width: int) -> np.ndarray:
    if raw.size == 0 or np.isnan(raw).all():
        return np.full(n_frames, width / 2)
    idx = np.arange(len(raw))
    good = ~np.isnan(raw)
    filled = np.interp(idx, idx[good], raw[good])  # hold/interpolate across gaps
    k = max(3, int(SMOOTH_S * OUT_FPS / DETECT_EVERY))
    pad = np.pad(filled, k, mode="edge")
    smooth = np.convolve(pad, np.ones(k) / k, mode="same")[k:-k]
    per_frame = np.interp(np.arange(n_frames) / DETECT_EVERY, idx, smooth)
    out, cur = np.empty(n_frames), per_frame[0]
    for j, v in enumerate(per_frame):  # deadband: only move on meaningful change
        if abs(v - cur) > DEADBAND_PX:
            cur = v
        out[j] = cur
    return np.convolve(np.pad(out, 8, mode="edge"), np.ones(9) / 9, mode="same")[8:-8]


# ---------- captions ----------
def caption_chunks(words: list[dict], start: float, end: float, max_words: int = 3) -> list[dict]:
    """Words inside [start,end], grouped for display; times relative to clip start."""
    ws = [{"w": w["w"], "s": w["s"] - start, "e": w["e"] - start}
          for w in words if w["s"] >= start - 0.05 and w["e"] <= end + 0.05]
    chunks, cur = [], []
    for w in ws:
        if cur and (len(cur) >= max_words or w["s"] - cur[-1]["e"] > 0.6
                    or cur[-1]["w"][-1] in ".?!,"):
            chunks.append(cur)
            cur = []
        cur.append(w)
    if cur:
        chunks.append(cur)
    return [{"s": c[0]["s"], "e": c[-1]["e"], "words": c} for c in chunks]


class Captioner:
    def __init__(self, chunks: list[dict]):
        self.chunks, self._cache = chunks, {}

    def active(self, t: float):
        for ci, c in enumerate(self.chunks):
            if c["s"] <= t < c["e"] + 0.25:
                wi = 0
                for k, w in enumerate(c["words"]):
                    if w["s"] <= t:
                        wi = k
                return ci, wi
        return None

    def overlay(self, key):
        if key not in self._cache:
            self._cache[key] = self._draw(*key)
        return self._cache[key]

    def _draw(self, ci: int, wi: int):
        words = [w["w"].upper() for w in self.chunks[ci]["words"]]
        px = FONT_PX
        while True:
            font = ImageFont.truetype(FONT, px)
            total = sum(font.getlength(w + " ") for w in words)
            if total <= MAX_TEXT_W or px < 40:
                break
            px -= 4
        img = Image.new("RGBA", (OUT_W, CAP_H), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        x = (OUT_W - total) / 2
        for k, w in enumerate(words):
            d.text((x, CAP_H / 2), w, font=font, anchor="lm", stroke_width=7,
                   stroke_fill=(0, 0, 0, 255), fill=YELLOW if k == wi else WHITE)
            x += font.getlength(w + " ")
        arr = np.asarray(img)
        return arr[..., :3][..., ::-1].astype(np.float32), arr[..., 3:].astype(np.float32) / 255


# ---------- render ----------
def reframe_and_caption(cut_path: Path, words: list[dict], start: float, end: float,
                        dest: Path) -> None:
    centers, w, h = track_centers(cut_path)
    crop_w = min(w, int(h * OUT_W / OUT_H))
    cap = Captioner(caption_chunks(words, start, end))
    ff = subprocess.Popen(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "bgr24",
         "-s", f"{OUT_W}x{OUT_H}", "-r", str(OUT_FPS), "-i", "-", "-i", str(cut_path),
         "-map", "0:v", "-map", "1:a?", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
         "-pix_fmt", "yuv420p", "-c:a", "copy", "-shortest", "-movflags", "+faststart",
         str(dest)], stdin=subprocess.PIPE)
    vc = cv2.VideoCapture(str(cut_path))
    i = 0
    try:
        while True:
            ok, frame = vc.read()
            if not ok:
                break
            cx = centers[min(i, len(centers) - 1)]
            x0 = int(np.clip(cx - crop_w / 2, 0, w - crop_w))
            out = cv2.resize(frame[:, x0:x0 + crop_w], (OUT_W, OUT_H),
                             interpolation=cv2.INTER_LANCZOS4)
            key = cap.active(i / OUT_FPS)
            if key:
                rgb, alpha = cap.overlay(key)
                reg = out[CAP_Y:CAP_Y + CAP_H].astype(np.float32)
                out[CAP_Y:CAP_Y + CAP_H] = (reg * (1 - alpha) + rgb * alpha).astype(np.uint8)
            ff.stdin.write(out.tobytes())
            i += 1
    finally:
        vc.release()
        ff.stdin.close()
        if ff.wait() != 0:
            raise RuntimeError("ffmpeg render failed")


def make_clip(src: Path, words: list[dict], start: float, end: float, dest: Path) -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td) / "cut.mp4"
        cut(src, start, end, tmp)
        reframe_and_caption(tmp, words, start, end, dest)
