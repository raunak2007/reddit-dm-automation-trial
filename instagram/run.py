"""run.py - find your recent posts, and the commenters asking about admissions get a DM.

    python run.py                        the daily run: find the recent posts (discover.py), read their comments ->
                                         pick leads and write DMs -> send them. Same as: python run.py auto
    python run.py <post or reel link>    the same for one post you name
    python run.py replies                who wrote back after their DM, inbox and Message Requests (replies.py)
                                         -> data/replies.csv
    python run.py answer                 send the answers you approved in the shared inbox (answer.py)
    python run.py followup               one follow-up to people who never replied (followup.py; off unless
                                         outreach.followup.enabled)
    python run.py livecheck              read-only checks against the real instagram.com (livecheck.py), weekly
    python run.py stats                  posts, leads, DMs sent, held and replied (no browser)
    python run.py posts                  the posts found so far and when each was read (no browser)
    python run.py login                  log in to Instagram once in the script's Chrome profile
    python run.py queue                  open the review page (to look, edit drafts, or skip people before a send)
    python run.py send [--by-hand]       approve new leads and send (send.py); --by-hand: you press Send
    python run.py redraft                rewrite every unsent DM (after you change the template or prompt)
    python run.py preview                print the exact instructions Claude gets, and stop

Sending follows outreach.auto_approve (see send.py): with it on, every new lead at or above ai.min_score is sent
its drafted DM. Set it to false to send only what you approve on the review page. With outreach.auto_follow on,
send.py follows each person right before their DM, and outreach.auto_resolve rechecks held sends on each run.
Which posts a run reads: discover.* in config.json (see discover.py).

Exit codes: 0 done, 2 stopped on an account-wide problem (logged out, a security check, an action block or rate
limit, a locked account, or a streak of failed sends), 1 anything else (a bad link, a missing setting, ...). A
scheduler can alert on 2.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import browser
import comments
import discover
import qualify
import queue_server
import replies
import send
import store

HELP = ("-h", "--help", "help")

HERE = Path(__file__).resolve().parent
CONFIG = HERE / "config.json"


def stats(cfg: dict) -> None:
    hist, leads, posts = store.history(), store.load_leads(), store.load_posts()
    sent = store.sent_at(hist)
    replied = {r["username"] for r in store.load_replies()} & set(sent)
    scored = [l for l in leads.values() if "ai" in l]
    good = [l for l in scored if l["ai"].get("relevant") and l["ai"].get("score", 0) >= cfg["ai"]["min_score"]]
    print(f"posts     {len(posts)} found, {sum(1 for p in posts.values() if p.get('last_read'))} read")
    print(f"leads     {len(leads)} commenters, {len(scored)} scored, {len(good)} at or above ai.min_score")
    print(f"sent      {len(sent)} ({store.sent_today()} today, cap {cfg['outreach']['daily_dm_limit']})")
    print(f"held      {len(store.held())}")
    print(f"follow-up {sum(1 for rows in hist.values() if any(r['status'] == 'followup_sent' for r in rows))} sent"
          f" (outreach.followup.enabled: {cfg['outreach'].get('followup', {}).get('enabled', False)})")
    print(f"replied   {len(replied)}" + (f" ({100 * len(replied) // len(sent)}% of sent)" if sent else "")
          + "  (as of the last python run.py replies)")


def auto(cfg: dict, url: str | None = None) -> int:
    """Read the recent posts (or the one at url), score the new commenters, send. Returns the exit code."""
    if url:
        comments.run(cfg, url)
    else:
        discover.run(cfg)
    qualify.run(cfg)
    return send.run(cfg)


def main():
    args = sys.argv[1:]
    if any(a in HELP for a in args):                 # before anything opens Chrome or reads config.json
        print(__doc__)
        return 0
    if args == ["send", "--by-hand"]:
        return send.run(json.loads(CONFIG.read_text()), by_hand=True)
    if len(args) > 1:
        sys.exit(__doc__)
    arg, cfg = (args[0] if args else "auto"), json.loads(CONFIG.read_text())
    if arg == "auto":
        return auto(cfg)
    if "instagram.com" in arg:
        return auto(cfg, arg)
    if arg == "send":
        return send.run(cfg)
    if arg == "answer":
        import answer
        return answer.run(cfg)
    if arg == "followup":
        import followup
        return followup.run(cfg)
    if arg == "livecheck":
        import livecheck
        return livecheck.run(cfg)
    if arg == "replies":
        replies.run(cfg)
    elif arg == "stats":
        stats(cfg)
    elif arg == "posts":
        discover.print_posts(cfg)
    elif arg == "login":
        driver = browser.make_driver(cfg)
        try:
            browser.ensure_logged_in(driver, cfg)
            print("[+] Logged in; the session is saved for later runs.")
        finally:
            driver.quit()
    elif arg == "queue":
        queue_server.serve()
    elif arg == "redraft":
        qualify.run(cfg, rescore=True)
    elif arg == "preview":
        qualify.run(cfg, dry_run=True)
    else:
        sys.exit(__doc__)
    return 0


def cli() -> int:
    """main(), with an account-wide stop (browser.Halt) printed and turned into exit code 2."""
    try:
        return main()
    except browser.Halt as stop:
        print(f"[stopped] {stop}")
        return 2


if __name__ == "__main__":
    sys.exit(cli())
