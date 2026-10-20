"""run.py - the whole pipeline: find posts, score and draft with Claude, send the DMs.

    python run.py login              open the scraper's Chrome profile so you can log in to Facebook once
    python run.py                    scrape -> qualify -> send (the usual daily run; nothing to approve or press)
    python run.py scrape [--limit-pages N]
    python run.py qualify [--rescore | --dry-run]
    python run.py queue              open the review queue (to look, edit drafts, or skip leads before a send)
    python run.py send [--by-hand]   approve new leads and send (send.py); --by-hand: you press Send in each chat
    python run.py stats              counts from data/leads.json and data/contacted.csv

Sending follows outreach.auto_approve (see send.py): with it on, every new lead at or above ai.min_score is sent
its drafted DM. Set it to false to send only what you approve in the queue.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

import browser
import qualify
import queue_server
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


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", nargs="?", default="all", choices=["all", "login", "scrape", "qualify", "queue", "send", "stats"])
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
    else:
        scrape.run(cfg, limit_pages=args.limit_pages)
        qualify.run(cfg, args.rescore)
        raise SystemExit(send.run(cfg))


if __name__ == "__main__":
    main()
