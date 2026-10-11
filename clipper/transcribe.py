import json
from pathlib import Path

from .config import CACHE_DIR, WHISPER_MODEL


def transcribe(video: Path, vid: str) -> list[dict]:
    """Return flat word list [{w, s, e}], cached at cache/<id>/transcript.json."""
    cache = CACHE_DIR / vid / "transcript.json"
    if cache.exists():
        return json.loads(cache.read_text())
    import mlx_whisper  # heavy import, only when needed
    res = mlx_whisper.transcribe(str(video), path_or_hf_repo=WHISPER_MODEL,
                                 word_timestamps=True, verbose=None)
    words = [{"w": w["word"].strip(), "s": round(w["start"], 2), "e": round(w["end"], 2)}
             for seg in res["segments"] for w in seg.get("words", []) if w["word"].strip()]
    cache.write_text(json.dumps(words))
    return words


def sentences(words: list[dict], gap: float = 0.8) -> list[dict]:
    """Group words into sentences: [{s, e, text, i0, i1}] (i0..i1 inclusive word idx)."""
    out, start = [], 0
    for i, w in enumerate(words):
        last = i == len(words) - 1
        ends = w["w"][-1] in ".?!…" or (not last and words[i + 1]["s"] - w["e"] > gap)
        if ends or last:
            chunk = words[start:i + 1]
            out.append({"s": chunk[0]["s"], "e": chunk[-1]["e"],
                        "text": " ".join(x["w"] for x in chunk), "i0": start, "i1": i})
            start = i + 1
    return out
