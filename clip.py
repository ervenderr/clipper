#!/usr/bin/env python
"""CLI: python clip.py <url> --count 5 | publish | serve | approve <id> [N ...|all] | auth"""
import argparse
import sys

SUBCMDS = {"publish", "serve", "approve", "auth"}


def main(argv: list[str]) -> int:
    if argv and argv[0] not in SUBCMDS and not argv[0].startswith("-"):
        p = argparse.ArgumentParser(prog="clip.py")
        p.add_argument("url")
        p.add_argument("--count", type=int, default=5)
        a = p.parse_args(argv)
        from clipper import pipeline
        vid = pipeline.run(a.url, a.count)
        print(f"Clips in output/{vid}/ — review, then: python clip.py approve {vid} all")
        return 0
    p = argparse.ArgumentParser(prog="clip.py")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("publish", help="upload approved clips as scheduled private videos")
    sub.add_parser("auth", help="run the one-time YouTube OAuth flow")
    s = sub.add_parser("serve", help="start the local web UI")
    s.add_argument("--port", type=int, default=8790)
    ap = sub.add_parser("approve", help="mark clips approved")
    ap.add_argument("video_id")
    ap.add_argument("indexes", nargs="+", help="clip numbers or 'all'")
    a = p.parse_args(argv)
    if a.cmd == "approve":
        from clipper import store
        data = store.load(a.video_id)
        want = {c["index"] for c in data["clips"]} if a.indexes == ["all"] \
            else {int(i) for i in a.indexes}
        n = 0
        for c in data["clips"]:
            if c["index"] in want and c["status"] == "pending":
                store.update_clip(a.video_id, c["index"], status="approved")
                n += 1
        print(f"Approved {n} clip(s).")
    elif a.cmd == "publish":
        from clipper import youtube
        youtube.publish_all()
    elif a.cmd == "auth":
        from clipper import youtube
        youtube.get_service()
        print("Authorized; token saved to token.json")
    elif a.cmd == "serve":
        import uvicorn
        uvicorn.run("clipper.web:app", host="127.0.0.1", port=a.port)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
