"""Pick the best self-contained clips with an LLM, then snap to sentence bounds."""
import json
import re

from . import llm
from .config import CHUNK_CHARS, MAX_CLIP_S, MIN_CLIP_S

SYSTEM = (
    "You pick viral short-form clips from video transcripts. Reply with ONLY a JSON array, "
    "no prose, no code fences."
)
PROMPT = """Transcript lines look like: [start-end seconds] sentence.
Pick the {n} best segments for YouTube Shorts, each {lo}-{hi} seconds, no overlaps. Rules:
- HOOK FIRST: the segment must open on its most surprising/curious/bold sentence (a claim, \
question or story peak), never a greeting, setup or filler. Start exactly where that sentence begins.
- Self-contained: makes sense with no prior context, ends on a complete thought or payoff.
- Prefer concrete, emotional or counter-intuitive moments; skip rambling and housekeeping.
Use start/end times from the lines. Each array item must have exactly these keys:
start (number), end (number), hook (the opening line as short on-screen text), \
score (integer 1-10, how likely viewers watch to the end), reason (one sentence), \
yt_title (<60 chars, surprising claim FIRST, no clickbait lies, no hashtags; never first person \
"I/my/we" because the speaker is not the uploader, name the person or use neutral phrasing), \
description (ONE sentence restating the hook as a question or bold claim, never first person), \
tags (array of 2-3 specific lowercase topic words, e.g. "investing", "mindset").

TRANSCRIPT:
{text}"""

REQUIRED = {"start": (int, float), "end": (int, float), "hook": str, "score": (int, float),
            "reason": str, "yt_title": str, "description": str, "tags": list}


def sentence_lines(sents: list[dict]) -> list[str]:
    return [f"[{s['s']:.0f}-{s['e']:.0f}] {s['text']}" for s in sents]


def chunk_lines(lines: list[str], limit: int = CHUNK_CHARS) -> list[list[str]]:
    chunks, cur, size = [], [], 0
    for ln in lines:
        if cur and size + len(ln) > limit:
            chunks.append(cur)
            cur, size = [], 0
        cur.append(ln)
        size += len(ln) + 1
    if cur:
        chunks.append(cur)
    return chunks


def parse_items(raw: str) -> list[dict]:
    """Extract and validate the JSON array; raises ValueError if malformed."""
    m = re.search(r"\[.*\]", raw, re.S)
    if not m:
        raise ValueError("no JSON array found")
    items = json.loads(m.group(0))
    if not isinstance(items, list):
        raise ValueError("not a list")
    out = []
    for it in items:
        if not isinstance(it, dict):
            raise ValueError("item is not an object")
        for k, t in REQUIRED.items():
            if k not in it or not isinstance(it[k], t) or isinstance(it[k], bool):
                raise ValueError(f"bad or missing key {k!r}")
        out.append({**it, "yt_title": it["yt_title"][:99],
                    "tags": [str(t).strip().lower() for t in it["tags"]][:5],
                    "score": max(1, min(10, float(it["score"])))})
    return out


def snap(item: dict, sents: list[dict]) -> dict | None:
    """Snap start to a sentence start and end to a sentence end; enforce duration."""
    first = min(sents, key=lambda s: abs(s["s"] - item["start"]))
    ends = [s for s in sents if s["s"] >= first["s"]]
    last = min(ends, key=lambda s: abs(s["e"] - item["end"]))
    while last["e"] - first["s"] > MAX_CLIP_S and last["s"] > first["s"]:
        last = sents[sents.index(last) - 1]  # drop trailing sentences until <= max
    dur = last["e"] - first["s"]
    if not (MIN_CLIP_S <= dur <= MAX_CLIP_S):
        return None
    return {**item, "start": first["s"], "end": last["e"]}


def select(items: list[dict], n: int) -> list[dict]:
    """Highest score first, drop overlaps, keep n, return in time order."""
    chosen = []
    for it in sorted(items, key=lambda x: -x["score"]):
        if all(it["end"] <= c["start"] or it["start"] >= c["end"] for c in chosen):
            chosen.append(it)
        if len(chosen) == n:
            break
    return sorted(chosen, key=lambda x: x["start"])


def _ask(lines: list[str], n: int) -> list[dict]:
    prompt = PROMPT.format(n=n, lo=int(MIN_CLIP_S), hi=int(MAX_CLIP_S), text="\n".join(lines))
    err = ""
    for attempt in range(2):  # retry once if malformed
        raw = llm.chat(SYSTEM, prompt + (f"\n\nYour previous reply was invalid ({err}). "
                                         "Return ONLY the JSON array." if err else ""))
        try:
            return parse_items(raw)
        except (ValueError, json.JSONDecodeError) as e:
            err = str(e)
    raise RuntimeError(f"LLM returned malformed JSON twice: {err}")


def find_highlights(sents: list[dict], n: int) -> list[dict]:
    chunks = chunk_lines(sentence_lines(sents))
    per = n if len(chunks) == 1 else max(2, -(-n * 2 // len(chunks)))
    items = []
    for ch in chunks:
        items += _ask(ch, per)
    snapped = [s for s in (snap(i, sents) for i in items) if s]
    return select(snapped, n)
