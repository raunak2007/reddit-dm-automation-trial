"""run.py - the whole pipeline: find posts, score and draft with Claude, send the DMs.

    python run.py login              open the scraper's Chrome profile so you can log in to Facebook once
    python run.py                    scrape -> qualify -> send (-> comment, with outreach.auto_comment) (the usual daily run)
    python run.py scrape [--limit-pages N]
    python run.py qualify [--rescore | --dry-run]
    python run.py queue              open the review queue (to look, edit drafts, or skip leads before a send)
    python run.py send [--by-hand]   approve new leads and send (send.py); --by-hand: you press Send in each chat
    python run.py comment            post the drafted public comment on leads that won't get a DM (comment.py)
    python run.py replies            look in each DMed chat for a reply and list them in data/replies.csv (replies.py)
    python run.py stats              counts from data/leads.json and data/contacted.csv

Sending follows outreach.auto_approve (see send.py): with it on, every new lead at or above ai.min_score is sent
its drafted DM. Set it to false to send only what you approve in the queue.

Exit codes: 2 when a send or comment run halted on an account-wide problem or a streak of failures (so a scheduler
can tell you), 1 when a step stopped before starting (not logged in, wrong account, chat PIN, ...), else 0. In the
full run, a send step that stops before starting doesn't keep the comment step from running; one that halts does.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

import browser
import comment
import qualify
import queue_server
import replies
import scrape
import send
import store

HERE = Path(__file__).resolve().parent
CONFIG = HERE / "config.json"


def login(cfg):
    driver = browser.make_driver(cfg)
    try:
        browser.ensure_logged_in(driver, cfg, interactive=True)
        print("[+] Logged in. The session is saved in this profile for later runs.")
    finally:
        driver.quit()


def stats(cfg):
    leads = store.load_leads()
    scored = [l for l in leads.values() if "ai" in l]
    good = [l for l in scored if l["ai"]["relevant"] and l["ai"]["score"] >= cfg["ai"]["min_score"]]
    print(f"posts found {len(leads)}, scored {len(scored)}, leads {len(good)} "
          f"({sum(l['ai']['likely_minor'] for l in good)} flagged as possibly under 18)")
    print("by category:", dict(Counter(c for l in good for c in l["ai"]["categories"]).most_common()))
    print("actions:", dict(Counter(store.handled_posts().values())), f"| DMs sent today {store.sent_today()}",
          f"| held {len(store.held())}")
    print(f"comments today {store.commented_today()}/{cfg['outreach'].get('daily_comment_limit', comment.DAILY_LIMIT)}"
          f" | held comments {len(store.held_comments())} | replies {len(store.replies())}")


def step(fn, *args):
    """Run one step and return its exit code; a browser.Halt (account-wide stop before starting) is printed and
    counts as 2, any other SystemExit with a message (it stopped before starting) is printed and counts as 1."""
    try:
        return fn(*args) or 0
    except browser.Halt as stop:
        print(f"[stopped] {stop}")
        return 2
    except SystemExit as exc:
        if isinstance(exc.code, int) or exc.code is None:
            return exc.code or 0
        print(f"[stopped] {exc.code}")
        return 1


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", nargs="?", default="all", choices=["all", "login", "scrape", "qualify", "queue", "send", "comment",
                                                          "replies", "stats"])
    ap.add_argument("--limit-pages", type=int)
    ap.add_argument("--rescore", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--by-hand", action="store_true", help="send: open each chat and wait for you to press Send")
    args = ap.parse_args()
    cfg = json.loads(CONFIG.read_text())

    if args.cmd == "login":
        login(cfg)
    elif args.cmd == "stats":
        stats(cfg)
    elif args.cmd == "scrape":
        scrape.run(cfg, limit_pages=args.limit_pages)
    elif args.cmd == "qualify":
        qualify.run(cfg, args.rescore, args.dry_run)
    elif args.cmd == "queue":
        queue_server.serve()
    elif args.cmd == "send":
        raise SystemExit(send.run(cfg, args.by_hand))
    elif args.cmd == "comment":
        raise SystemExit(comment.run(cfg))
    elif args.cmd == "replies":
        raise SystemExit(replies.run(cfg))
    else:
        scrape.run(cfg, limit_pages=args.limit_pages)
        qualify.run(cfg, args.rescore)
        code = step(send.run, cfg)
        if code != 2 and cfg["outreach"].get("auto_comment", False):
            code = max(code, step(comment.run, cfg))
        raise SystemExit(code)


if __name__ == "__main__":
    browser.halting(main)
