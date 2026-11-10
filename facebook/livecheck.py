"""livecheck.py - the read-only checks against the real facebook.com (tests/test_live.py), for a weekly scheduler.

    python livecheck.py        (python run.py livecheck)

Runs tests/test_live.py with FB_LIVE=1 in a separate Python, with the optional checks taken from config.json's
livecheck block instead of environment variables (an empty value leaves that check out):
    chat         author_key of someone you've messaged before   } open that chat, check its message box and
    name         their name as Messenger shows it               } print who wrote each message
    post         a post URL                                     } open that post and check comment.py finds
    post_author  its author's name                              } exactly its own comment box
    post_text    the first words of the post                    }
Nothing is typed, pressed, sent or recorded: test_live.py only opens pages and reads them (tests/test_livecheck.py
checks that). It takes the same lock as send.py, since it opens the same Chrome profile.

Exit codes: 0 when every check passed, 2 when a failure shows an account problem (logged out, wrong account, chat
PIN, checkpoint, block), 1 for anything else (the markup changed, nothing could be checked, ...). Failed checks
save their screenshots and page listings in debug/live/.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import browser  # noqa: F401  (for browser.halting)
import store

HERE = Path(__file__).resolve().parent
CONFIG = HERE / "config.json"
ENV = {"chat": "FB_LIVE_CHAT", "name": "FB_LIVE_NAME", "post": "FB_LIVE_POST", "post_author": "FB_LIVE_POST_AUTHOR",
       "post_text": "FB_LIVE_POST_TEXT"}
# What test_live.py (and messenger.py / browser.py under it) print when the account itself is the problem.
ACCOUNT = re.compile(r"AccountBlocked|not logged in|still not logged in|log ?in to facebook|chat PIN|checkpoint|"
                     r"temporarily (blocked|restricted)|refusing this account|different account than|no c_user cookie",
                     re.I)


def environment(cfg):
    """os.environ with FB_LIVE=1, FB_LIVE_STRICT=1 (being logged out fails instead of skipping) and the FB_LIVE_*
    settings from cfg's livecheck block only (stray ones from the shell are dropped)."""
    env = {k: v for k, v in os.environ.items() if k not in ENV.values()}
    env.update(FB_LIVE="1", FB_LIVE_STRICT="1")
    for key, var in ENV.items():
        value = str(cfg.get("livecheck", {}).get(key) or "").strip()
        if value:
            env[var] = value
    return env


def verdict(returncode, output):
    """(exit code, why) from the unittest run's exit code and output."""
    ran = re.search(r"^Ran (\d+) tests?", output, re.M)
    skipped = re.search(r"skipped=(\d+)", output)
    if returncode == 0:
        if not ran or (skipped and int(skipped.group(1)) >= int(ran.group(1))):
            return 1, "nothing was checked (every check skipped)"
        return 0, "every check passed"
    failures = output[output.find("=" * 20):] if "=" * 20 in output else output
    m = ACCOUNT.search(failures)
    if m:
        return 2, f"account problem: {m.group(0)!r} (see the output above)"
    return 1, "a check failed (see the output above and debug/live/)"


def run(cfg, runner=subprocess.run):
    cmd = [sys.executable, "-m", "unittest", "tests/test_live.py", "-v"]
    with store.lock():
        print("[+] running the read-only live checks (tests/test_live.py)")
        done = runner(cmd, cwd=HERE, env=environment(cfg), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                      text=True)
    print(done.stdout)
    code, why = verdict(done.returncode, done.stdout or "")
    print(f"[{'done' if code == 0 else 'stopped'}] {why}")
    return code


def main():
    sys.exit(run(json.loads(CONFIG.read_text())))


if __name__ == "__main__":
    browser.halting(main)
