from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from clipper import highlights, pipeline, youtube
from clipper.config import MAX_CLIP_S
from clipper.render import caption_chunks
from clipper.transcribe import sentences


def words(spec):  # [(text, start, end)]
    return [{"w": t, "s": s, "e": e} for t, s, e in spec]


def test_sentences_split_on_punctuation_and_gap():
    w = words([("Hi", 0, .3), ("there.", .3, .6), ("Next", .7, 1), ("one", 3, 3.3)])
    s = sentences(w)
    assert [x["text"] for x in s] == ["Hi there.", "Next", "one"]


def test_parse_items_valid_and_fenced():
    raw = '```json\n[{"start":1,"end":30,"hook":"h","score":9,"reason":"r","yt_title":"%s",' \
          '"description":"d","tags":["a"]}]\n```' % ("x" * 150)
    out = highlights.parse_items(raw)
    assert len(out[0]["yt_title"]) < 100


@pytest.mark.parametrize("raw", ["no json", '[{"start":1}]', '{"a":1}'])
def test_parse_items_rejects_bad(raw):
    with pytest.raises(ValueError):
        highlights.parse_items(raw)


def sents():
    return [{"s": i * 10.0, "e": i * 10.0 + 9.0, "text": f"s{i}"} for i in range(12)]


def test_snap_to_sentence_bounds():
    r = highlights.snap({"start": 12, "end": 41}, sents())
    assert r["start"] == 10.0 and r["end"] == 39.0


def test_snap_rejects_too_short_and_trims_long():
    assert highlights.snap({"start": 10, "end": 19}, sents()) is None
    r = highlights.snap({"start": 0, "end": 110}, sents())
    assert r["end"] - r["start"] <= MAX_CLIP_S == 40


def test_select_no_overlap_and_top_score():
    items = [{"start": 0, "end": 30, "score": 5}, {"start": 20, "end": 50, "score": 9},
             {"start": 60, "end": 90, "score": 7}]
    out = highlights.select(items, 2)
    assert [i["score"] for i in out] == [9, 7]


def test_chunking_limits():
    chunks = highlights.chunk_lines(["x" * 100] * 10, limit=250)
    assert len(chunks) == 5 and all(len(c) <= 2 for c in chunks)


def test_caption_chunks_relative_times():
    w = words([("a", 10, 10.2), ("b", 10.2, 10.4), ("c", 10.4, 10.6), ("d", 10.6, 10.8)])
    ch = caption_chunks(w, 10, 11)
    assert ch[0]["s"] == 0 and len(ch[0]["words"]) == 3 and len(ch) == 2


def test_next_slot_manila_and_skips_taken():
    sched = {"timezone": "Asia/Manila", "times": ["12:00", "19:00"]}
    now = datetime(2026, 10, 11, 3, 0, tzinfo=timezone.utc)  # 11:00 Manila
    first = youtube.next_slot(set(), now, sched)
    assert first.astimezone(ZoneInfo("Asia/Manila")).strftime("%H:%M") == "12:00"
    second = youtube.next_slot({youtube.iso(first)}, now, sched)
    assert second.astimezone(ZoneInfo("Asia/Manila")).strftime("%H:%M") == "19:00"


def test_build_body_shorts_private_synthetic():
    clip = {"yt_title": "t" * 120, "description": "d", "tags": ["a"], "ai_altered": True}
    b = youtube.build_body(clip, datetime(2026, 10, 12, tzinfo=timezone.utc))
    assert b["status"]["privacyStatus"] == "private" and b["status"]["publishAt"]
    assert b["status"]["containsSyntheticMedia"] is True
    assert "#Shorts" in b["snippet"]["description"] and len(b["snippet"]["title"]) <= 100
    clip["ai_altered"] = False
    assert "containsSyntheticMedia" not in youtube.build_body(clip, datetime.now(timezone.utc))["status"]


def test_backoff_gives_up():
    with pytest.raises(RuntimeError):
        r = 0
        for _ in range(20):
            r = youtube._backoff(r, OSError("x"), lambda s: None)


def test_publish_never_uploads_unapproved_or_twice(tmp_path, monkeypatch):
    from clipper import store
    monkeypatch.setattr(store, "OUTPUT_DIR", tmp_path / "out")
    monkeypatch.setattr(youtube, "QUOTA_FILE", tmp_path / "q.json")
    monkeypatch.setattr(youtube, "SCHEDULE_FILE", tmp_path / "s.json")
    vid = "abcdefghijk"
    base = {"file": "c.mp4", "start": 0, "end": 30, "yt_title": "t", "description": "d",
            "tags": [], "publish_at": None, "youtube_id": None, "error": None}
    store.save(vid, {"video_id": vid, "clips": [
        {**base, "index": 1, "status": "pending"},
        {**base, "index": 2, "status": "approved"},
        {**base, "index": 3, "status": "approved", "youtube_id": "ZZZ"}]})
    (store.video_dir(vid) / "c.mp4").write_bytes(b"x")
    calls = []
    monkeypatch.setattr(youtube, "upload_one", lambda s, p, b: calls.append(b) or {"id": "NEW"})
    monkeypatch.setattr(youtube, "_warn_if_locked", lambda *a: None)
    youtube.publish_all(service=object(), log=lambda *_: None)
    youtube.publish_all(service=object(), log=lambda *_: None)
    assert len(calls) == 1
    clips = {c["index"]: c for c in store.load(vid)["clips"]}
    assert clips[1]["status"] == "pending" and clips[2]["youtube_id"] == "NEW"


def test_build_description_format():
    d = pipeline.build_description("Why do 30-day goals work?", ["Money Tips", "mindset", "x", "y"],
                                   {"title": "Talk", "channel": "TED"}, "https://youtu.be/abc")
    lines = d.split("\n\n")
    assert lines[0].startswith("Why do") and 'by TED' in lines[1] and lines[2] == "Follow for more."
    assert lines[3] == "#moneytips #mindset #x"


def test_suggest_count_scales_with_length():
    assert pipeline.suggest_count(60) == 1
    assert pipeline.suggest_count(17 * 60) == 7
    assert pipeline.suggest_count(3 * 3600) == pipeline.MAX_AUTO_CLIPS


def _setup_store(tmp_path, monkeypatch, statuses=("pending",)):
    from clipper import store
    monkeypatch.setattr(store, "OUTPUT_DIR", tmp_path / "out")
    monkeypatch.setattr(youtube, "QUOTA_FILE", tmp_path / "q.json")
    monkeypatch.setattr(youtube, "SCHEDULE_FILE", tmp_path / "s.json")
    vid = "abcdefghijk"
    base = {"file": "c.mp4", "start": 0, "end": 30, "yt_title": "t", "description": "d",
            "tags": [], "publish_at": None, "youtube_id": None, "error": None}
    store.save(vid, {"video_id": vid, "clips": [
        {**base, "index": i + 1, "status": st} for i, st in enumerate(statuses)]})
    (store.video_dir(vid) / "c.mp4").write_bytes(b"x")
    monkeypatch.setattr(youtube, "_warn_if_locked", lambda *a: None)
    return store, vid


def test_upload_private_without_schedule(tmp_path, monkeypatch):
    store, vid = _setup_store(tmp_path, monkeypatch)
    bodies = []
    monkeypatch.setattr(youtube, "upload_one", lambda s, p, b: bodies.append(b) or {"id": "X1"})
    c = youtube.upload_clip(object(), vid, 1, mode="private", log=lambda *_: None)
    assert "publishAt" not in bodies[0]["status"] and bodies[0]["status"]["privacyStatus"] == "private"
    assert c["status"] == "uploaded" and c["youtube_id"] == "X1" and c["publish_at"] is None


def test_upload_at_requires_future(tmp_path, monkeypatch):
    store, vid = _setup_store(tmp_path, monkeypatch)
    monkeypatch.setattr(youtube, "upload_one", lambda s, p, b: {"id": "X"})
    with pytest.raises(ValueError):
        youtube.upload_clip(object(), vid, 1, mode="at", at="2020-01-01T00:00:00Z", log=print)
    assert store.load(vid)["clips"][0]["status"] == "pending"   # nothing started


def test_upload_clip_refuses_duplicate(tmp_path, monkeypatch):
    store, vid = _setup_store(tmp_path, monkeypatch)
    monkeypatch.setattr(youtube, "upload_one", lambda s, p, b: {"id": "X"})
    youtube.upload_clip(object(), vid, 1, mode="next_slot", log=lambda *_: None)
    with pytest.raises(ValueError):
        youtube.upload_clip(object(), vid, 1, mode="next_slot", log=lambda *_: None)
