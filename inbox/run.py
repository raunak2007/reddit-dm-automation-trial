"""run.py - the reply inbox: draft answers to everyone who wrote back, and a page to edit and approve them.

    python inbox/run.py                   draft new answers, then open the page
    python inbox/run.py draft [--limit N] [--dry-run]
    python inbox/run.py serve [--port 8767] [--no-open]
    python inbox/run.py stats             answers by platform and status, and replies not drafted yet

Sending is done by each platform: python reddit/run.py answer, python facebook/run.py answer,
python instagram/run.py answer (and the daily run), from the entries approved here (inbox/data/answers.json).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import draft   # noqa: E402
import server  # noqa: E402
import store   # noqa: E402


def stats() -> dict:
    answers = store.load().values()
    waiting = draft.pending()
    table = {p: {s: 0 for s in store.STATUSES} for p in store.PLATFORMS}
    for a in answers:
        table[a["platform"]][a["status"]] += 1
    shown = [s for s in store.STATUSES if any(table[p][s] for p in store.PLATFORMS)] or ["drafted"]
    print(f"{'':<10}" + "".join(f"{s:>11}" for s in shown) + f"{'new':>8}")
    for p in store.PLATFORMS:
        new = sum(g["platform"] == p for g in waiting)
        print(f"{p:<10}" + "".join(f"{table[p][s]:>11}" for s in shown) + f"{new:>8}")
    dismiss = sum(a.get("suggest") == "dismiss" and a["status"] == "drafted" for a in answers)
    print(f"\n'new' = conversations with replies not drafted yet (python inbox/run.py draft)."
          f"{f' {dismiss} drafted entries are suggested for dismissal.' if dismiss else ''}")
    return table


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")
    d = sub.add_parser("draft", help="draft answers to new replies")
    d.add_argument("--limit", type=int, help="at most this many (default: ai.max_drafts_per_run)")
    d.add_argument("--dry-run", action="store_true", help="print the prompt and what would be drafted; write nothing")
    s = sub.add_parser("serve", help="open the review page")
    s.add_argument("--port", type=int)
    s.add_argument("--no-open", action="store_true")
    sub.add_parser("stats", help="answers by platform and status")
    args = ap.parse_args(argv)
    if args.cmd == "draft":
        draft.run(args.limit, args.dry_run)
    elif args.cmd == "serve":
        server.serve(args.port, not args.no_open)
    elif args.cmd == "stats":
        stats()
    else:
        try:
            draft.run()
        except SystemExit as exc:                    # e.g. no API key: still open the page for what's there
            print(f"[!] Drafting stopped: {exc}")
        server.serve()


if __name__ == "__main__":
    main()
