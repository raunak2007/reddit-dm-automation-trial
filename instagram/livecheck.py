"""livecheck.py - the read-only checks against the real instagram.com, for a weekly scheduler.

    python run.py livecheck      (or python livecheck.py)

Runs tests/test_live.py with IG_LIVE=1 in the Chrome profile from config.json: logged in as
outreach.sender_user_id, the feed and inbox showing no block or challenge, and, if set in config.json's
livecheck section, one profile's Follow button found without pressing it (livecheck.profile) and one chat opened
with its message box checked (livecheck.chat). Those come from config.json, not from IG_LIVE_* variables in your
shell, and IG_LIVE_FOLLOW is never passed on: it runs with IG_LIVE_READONLY=1, so nothing is typed, followed or
pressed (not even "Not Now" on a notifications dialog). It takes send.py's lock, so it never shares the profile
with a sending run.

Exit codes: 0 every check passed, 2 the failures show an account problem (logged out, wrong account, a challenge,
an action block, a locked account), 1 anything else (Chrome didn't start, the page markup changed, a timeout).
Failed checks save screenshots and page listings in debug/live/.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import store

HERE = Path(__file__).resolve().parent
CONFIG = HERE / "config.json"
DEFAULTS = {"profile": "", "chat": "", "timeout_minutes": 10}
# In a failed check's exception line or a skip reason: Instagram refusing the account, not a markup change.
ACCOUNT = re.compile(r"AccountBlocked|not logged in|logged out|refusing this account|different account than "
                     r"outreach\.sender_user_id|no ds_user_id cookie|challenge|checkpoint|security check|"
                     r"rate.?limit|action blocked|\((login|rate_limited|restricted)\)", re.I)
REASON = re.compile(r"^(?:[\w.]+\.)?\w*(?:Error|Exception|Failed|Blocked|Halt|Exit|SkipTest)\b: .*$"
                    r"|skipped '.*'$", re.M)


def settings(cfg: dict) -> dict:
    return {**DEFAULTS, **cfg.get("livecheck", {})}


def environment(cfg: dict, base: dict | None = None) -> dict:
    """The test run's environment: the caller's, minus every IG_LIVE_* variable, plus IG_LIVE=1,
    IG_LIVE_READONLY=1 and the profile / chat from config.json's livecheck section."""
    lc = settings(cfg)
    env = {k: v for k, v in (os.environ if base is None else base).items() if not k.startswith("IG_LIVE")}
    env.update(IG_LIVE="1", IG_LIVE_READONLY="1", PYTHONUNBUFFERED="1")
    for key, name in (("profile", "IG_LIVE_PROFILE"), ("chat", "IG_LIVE_CHAT")):
        value = str(lc.get(key) or "").strip().lower().removeprefix("@")
        if value:
            env[name] = value
    return env


def verdict(returncode: int, output: str) -> int:
    """0 passed, 2 the failures (or a 'not logged in' skip) show an account problem, 1 anything else."""
    reasons = REASON.findall(output)
    skipped_login = any(r.startswith("skipped") and re.search(r"not logged in", r, re.I) for r in reasons)
    if returncode == 0 and not skipped_login:
        return 0
    if skipped_login or any(ACCOUNT.search(r) for r in reasons if not r.startswith("skipped")):
        return 2
    return 1


def command() -> list[str]:
    return [sys.executable, "-m", "unittest", "discover", "-s", str(HERE / "tests"), "-p", "test_live.py", "-v"]


def run(cfg: dict) -> int:
    import apimode
    if apimode.is_api(cfg):                        # config.json "mode": "api": API reads only, no browser
        return apimode.livecheck(cfg)
    lc = settings(cfg)
    with store.lock():
        print(f"[+] live checks (read-only) with profile {cfg['browser']['profile']!r}"
              + (f", profile page @{lc['profile']}" if lc["profile"] else "")
              + (f", chat with @{lc['chat']}" if lc["chat"] else ""))
        try:
            done = subprocess.run(command(), cwd=HERE, env=environment(cfg), capture_output=True, text=True,
                                  timeout=lc["timeout_minutes"] * 60)
        except subprocess.TimeoutExpired:
            print(f"[!] the live checks took longer than livecheck.timeout_minutes ({lc['timeout_minutes']})")
            return 1
    output = (done.stdout or "") + (done.stderr or "")
    print(output.rstrip())
    code = verdict(done.returncode, output)
    print({0: "[done] live checks passed",
           2: "[stopped] the live checks show an account problem (see above); don't run sends until it's fixed",
           1: "[!] live checks failed (not an account problem as far as the output shows); see debug/live/"}[code])
    return code


def main():
    argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter).parse_args()
    sys.exit(run(json.loads(CONFIG.read_text())))


if __name__ == "__main__":
    main()
