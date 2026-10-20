"""run.py - point it at an Instagram post, and the commenters asking about admissions get a DM.

    python run.py login                  log in to Instagram once in the script's Chrome profile
    python run.py <post or reel link>    read the comments -> pick leads and write DMs -> send them
    python run.py queue                  open the review page (to look, edit drafts, or skip people before a send)
    python run.py send [--by-hand]       approve new leads and send (send.py); --by-hand: you press Send
    python run.py redraft                rewrite every unsent DM (after you change the template or prompt)
    python run.py preview                print the exact instructions Claude gets, and stop

Sending follows outreach.auto_approve (see send.py): with it on, every new lead at or above ai.min_score is sent
its drafted DM. Set it to false to send only what you approve on the review page. With outreach.auto_follow on,
send.py follows each person right before their DM, and outreach.auto_resolve rechecks held sends on each run.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import browser
import comments
import qualify
import queue_server
import send

HERE = Path(__file__).resolve().parent
CONFIG = HERE / "config.json"


def main():
    if len(sys.argv) == 3 and sys.argv[1:] == ["send", "--by-hand"]:
        sys.exit(send.run(json.loads(CONFIG.read_text()), by_hand=True))
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    arg, cfg = sys.argv[1], json.loads(CONFIG.read_text())
    if arg == "send":
        sys.exit(send.run(cfg))
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
    elif "instagram.com" in arg:
        comments.run(cfg, arg)
        qualify.run(cfg)
        sys.exit(send.run(cfg))
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
