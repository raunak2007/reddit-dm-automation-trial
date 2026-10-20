"""run.py - point it at an Instagram post, get a review page of commenters with a DM ready for each.

    python run.py login                  log in to Instagram once in the script's Chrome profile
    python run.py <post or reel link>    read the comments -> pick leads and write DMs -> open the review page
    python run.py queue                  reopen the review page
    python run.py send [--auto]          send the DMs you approved (send.py; by hand unless --auto)
    python run.py redraft                rewrite every unsent DM (after you change the template or prompt)
    python run.py preview                print the exact instructions Claude gets, and stop

Nothing here follows anyone, and nothing is sent until you approve it on the review page; `send` is never
part of reading a post.
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
    if len(sys.argv) == 3 and sys.argv[1:] == ["send", "--auto"]:
        sys.exit(send.run(json.loads(CONFIG.read_text()), auto=True))
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
        queue_server.serve()
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
