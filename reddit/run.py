"""run.py - the whole Reddit pipeline: find posts, score and draft with Claude, send the DMs, watch for replies.

    python reddit/run.py                 scrape -> qualify -> send (the usual daily run; nothing to approve or press)
    python reddit/run.py login           open the Reddit Chrome profile so you can log in once
    python reddit/run.py scrape [--limit-requests N]
    python reddit/run.py qualify [--rescore | --dry-run]
    python reddit/run.py send [--list]   write data/queue.html and send it (send.py); --list: who would go, no browser
    python reddit/run.py replies [--pages N]   record replies from people messaged in data/replies.csv
    python reddit/run.py answer          send the answers approved on the inbox page (answer.py)
    python reddit/run.py followup [--list]   one follow-up to people who never replied (followup.py; off until
                                         followup.enabled is true)
    python reddit/run.py livecheck       read-only check against reddit.com for a weekly scheduler (tests/test_live.py):
                                         exit 0 passed, 2 an account problem (logged out, wrong account, blocked), 1 else
    python reddit/run.py stats           counts from data/leads.json, the root contacted.csv and data/replies.csv

Sending follows outreach.auto_approve (see send.py): with it on, every eligible lead at or above ai.min_score is
sent its drafted message through the root sender (ledger.send_loop), within the root config.json's daily cap.
Set it to false to only write data/queue.html for review and send it with the root open_queue.py.

Exit code 2 when a run halted on an account-wide problem (logged out, rate-limited, restricted, blocked) or a
streak of failures, as ledger.report does, so a scheduler can notify; 0 otherwise. If the scrape halts on one, the
full run still scores what it has (that's Claude, not Reddit) but sends nothing.
"""
from __future__ import annotations

import argparse
import os
import sys
import unittest
from collections import Counter

import store  # first: it puts the repo root on sys.path for ledger / send_dm

import answer  # noqa: E402
import browser  # noqa: E402
import followup  # noqa: E402
import ledger  # noqa: E402
import qualify  # noqa: E402
import replies  # noqa: E402
import scrape  # noqa: E402
import send  # noqa: E402


def login(cfg):
    import undetected_chromedriver  # noqa: F401  (fail early with a clear import error)
    profile = browser.PROFILES / cfg["browser"]["profile"] / "chrome_profile"
    profile.mkdir(parents=True, exist_ok=True)
    driver = browser.make_driver(cfg)
    try:
        driver.get("https://www.reddit.com/login/")
        input("[!] Log in to Reddit in the Chrome window (including any 2FA), then press Enter here... ")
        try:
            print(f"[+] Logged in as u/{browser.check_login(driver, {})}. The session is saved in {profile}.")
        except Exception as exc:
            print(f"[!] Still not logged in: {exc}")
    finally:
        driver.quit()


def livecheck_code(result: unittest.TestResult) -> int:
    """0 if every live check ran and passed, 2 if any stopped on an account problem (send_dm.AccountBlocked:
    logged out, wrong account, rate-limited, a block page), else 1 (skipped, failed, or another error)."""
    trouble = result.errors + result.failures
    if any("AccountBlocked" in tb for _, tb in trouble):
        return 2
    return 0 if result.wasSuccessful() and result.testsRun and not result.skipped else 1


def livecheck(stream=None) -> int:
    """Run tests/test_live.py against reddit.com with REDDIT_LIVE=1 (it only reads .json pages: nothing is sent,
    posted or marked read). One run at a time with the sending scripts (ledger.lock), since they share the profile."""
    os.environ["REDDIT_LIVE"] = "1"
    tests = str(store.HERE / "tests")
    if tests not in sys.path:
        sys.path.insert(0, tests)
    suite = unittest.defaultTestLoader.loadTestsFromName("test_live")
    with ledger.lock():
        result = unittest.TextTestRunner(stream=stream or sys.stderr, verbosity=2).run(suite)
    code = livecheck_code(result)
    print({0: "[done] live check passed", 2: "[stopped] live check: Reddit is refusing this account (see above)",
           1: "[!] live check failed (see above)"}[code])
    return code


def stats(cfg, cfg_root):
    leads = store.load_leads()
    scored = [l for l in leads.values() if "ai" in l]
    good = [l for l in scored if l["ai"]["relevant"] and l["ai"]["score"] >= cfg["ai"]["min_score"]]
    print(f"posts found {len(leads)}, scored {len(scored)}, leads {len(good)} "
          f"({sum(l['ai']['likely_minor'] for l in good)} flagged as possibly under 18)")
    print("by subreddit:", dict(Counter(l.get("subreddit", "?") for l in good).most_common()))
    print("by category:", dict(Counter(c for l in good for c in l["ai"]["categories"]).most_common()))
    sent = store.first_sent()
    authors = {ledger.key(l["author"]) for l in good}
    out, _ = send.candidates(leads, cfg, cfg_root, cfg_root.get("sender_username"))
    got = store.load_replies()
    print(f"lead authors messaged {len(authors & set(sent))} | eligible now {len(out)} | "
          f"sent today {ledger.sent_today()}/{cfg_root['daily_limit_per_account']} (every Reddit script) | "
          f"held {len(ledger.held())}")
    due, _ = followup.candidates(cfg, cfg_root, cfg_root.get("sender_username"))
    print(f"follow-ups sent {sum(r['status'] == 'sent' for r in store.load_followups())} | due now {len(due)} "
          f"({'on' if followup.settings(cfg)['enabled'] else 'off'})")
    print(f"replies {len(got)} from {len({ledger.key(r['username']) for r in got})} people "
          f"(of {len(sent)} messaged in contacted.csv)")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", nargs="?", default="all",
                    choices=["all", "login", "scrape", "qualify", "send", "replies", "answer", "followup",
                             "livecheck", "stats"])
    ap.add_argument("--limit-requests", type=int)
    ap.add_argument("--rescore", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--list", action="store_true", help="send / followup: who would be sent and why anyone isn't")
    ap.add_argument("--pages", type=int, default=3, help="replies: inbox pages of 100 to read")
    args = ap.parse_args()
    cfg, cfg_root = store.load_config(), store.load_root_config()

    if args.cmd == "login":
        login(cfg)
    elif args.cmd == "stats":
        stats(cfg, cfg_root)
    elif args.cmd == "scrape":
        sys.exit(2 if scrape.run(cfg, cfg_root, args.limit_requests)["halted"] else 0)
    elif args.cmd == "qualify":
        qualify.run(cfg, args.rescore, args.dry_run)
    elif args.cmd == "send":
        if args.list:
            send.print_list(cfg, cfg_root)
        else:
            sys.exit(send.run(cfg, cfg_root))
    elif args.cmd == "replies":
        sys.exit(replies.run(cfg, cfg_root, args.pages))
    elif args.cmd == "answer":
        sys.exit(answer.run(cfg, cfg_root))
    elif args.cmd == "followup":
        if args.list:
            followup.print_list(cfg, cfg_root)
        else:
            sys.exit(followup.run(cfg, cfg_root))
    elif args.cmd == "livecheck":
        sys.exit(livecheck())
    else:
        found = scrape.run(cfg, cfg_root, args.limit_requests)
        qualify.run(cfg, args.rescore)
        if found["halted"]:
            print(f"[stopped] not sending: the scrape stopped on {found['stopped']}")
            sys.exit(2)
        sys.exit(send.run(cfg, cfg_root))


if __name__ == "__main__":
    main()
