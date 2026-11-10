"""browser.py - open the Reddit Chrome profile the root scripts use, and read Reddit's .json pages through it.

The profile is accounts/<browser.profile>/chrome_profile at the repo root (account_1 by default), the same one
run_loop.py and open_queue.py send with, launched the same way (undetected_chromedriver, with the installed
Chrome's major version from chrome_version.py). Log in to it once with `python reddit/run.py login` (or the
root test_connection.py). Chrome won't let two processes share a profile, so don't scrape, send or read
replies while another of these scripts has it open.

fetch_json() is how every Reddit read here is done. It sorts what comes back into:
    the JSON itself                                  -> returned
    Reddit's JSON error for this one page            -> PageSkipped (a private, banned or missing subreddit;
                                                        a 5xx): the run moves on to the next page
    anything that points at the account              -> AccountBlocked, from send_dm: 'login' (logged out,
                                                        sent to /login, 401), 'rate_limited' (429, "doing
                                                        that too much") or 'restricted' (a 403 with no
                                                        subreddit reason, a block page, captcha, or any page
                                                        that isn't JSON at all)
Callers stop the whole run on AccountBlocked and keep what they have: carrying on through those is how
accounts get restricted.
"""
from __future__ import annotations

import json
import random
import re
import sys
import time

import store  # noqa: F401  (puts the repo root on sys.path)
from send_dm import ACCOUNT_WIDE, AccountBlocked, active_sender, classify, verify_sender  # noqa: E402

PROFILES = store.ROOT / "accounts"
# What Reddit's network-level block and "slow down" pages say. They are HTML, not JSON.
BLOCK_PAGE = (r"whoa there,? pardner|you.ve been blocked by network security|blocked by network security|"
              r"our cdn was unable to reach|too many requests|prove your humanity|are you a human")
# 403 reasons that are about one subreddit, not this account.
SUBREDDIT_REASONS = {"private", "banned", "quarantined", "gold_only", "gated", "employees_only", "premium_only"}
_TEXT = "var p = document.querySelector('body > pre'); return p ? p.innerText : (document.body ? document.body.innerText : '')"


class PageSkipped(RuntimeError):
    """Reddit answered this one page with an error that says nothing about the account."""


def make_driver(cfg: dict):
    import undetected_chromedriver as uc
    from chrome_version import chrome_major

    profile_dir = PROFILES / cfg["browser"]["profile"] / "chrome_profile"
    if not profile_dir.is_dir():
        sys.exit(f"Profile folder not found: {profile_dir}\nLog in once with: python reddit/run.py login")
    for lock in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        (profile_dir / lock).unlink(missing_ok=True)
    opts = uc.ChromeOptions()
    opts.add_argument(f"--user-data-dir={profile_dir}")
    opts.add_argument("--no-first-run")
    opts.add_argument("--no-default-browser-check")
    driver = uc.Chrome(options=opts, version_main=chrome_major())
    driver.set_window_size(1400, 900)
    return driver


def pause(cfg: dict, scale: float = 1.0):
    b = cfg["browser"]
    time.sleep(random.uniform(b["min_request_delay_seconds"], b["max_request_delay_seconds"]) * scale)


def json_error(data, url: str):
    """Raise for Reddit's JSON error bodies ({"error": 429, "message": ...}); return data otherwise."""
    if not isinstance(data, dict) or "error" not in data or "data" in data:
        return data
    code, reason = data.get("error"), str(data.get("reason") or "").lower()
    said = f"{code} {data.get('message', '')} {reason}".strip()
    if code == 429:
        raise AccountBlocked(f"Reddit is rate-limiting this account ({said}) at {url}", "rate_limited")
    if code == 401:
        raise AccountBlocked(f"Reddit says this profile isn't logged in ({said}) at {url}", "login")
    if code == 403 and reason in SUBREDDIT_REASONS:
        raise PageSkipped(f"{url}: subreddit is {reason}")
    if code == 403:
        raise AccountBlocked(f"Reddit refused this account ({said}) at {url}", "restricted")
    raise PageSkipped(f"{url}: Reddit answered {said}")


def fetch_json(driver, url: str):
    """Open one of Reddit's .json URLs in the logged-in browser and parse it (see the module docstring)."""
    driver.get(url)
    current = driver.current_url or ""
    if re.search(r"reddit\.com/(login|register|account/login)", current):
        raise AccountBlocked(f"not logged in: {url} redirected to {current}", "login")
    text = driver.execute_script(_TEXT) or ""
    try:
        data = json.loads(text)
    except ValueError:
        head = re.sub(r"\s+", " ", text)[:300]
        kind = classify(RuntimeError(head))          # send_dm's wording for login / rate limit / restriction
        if kind in ACCOUNT_WIDE:
            raise AccountBlocked(f"Reddit is refusing this account at {url}: {head[:160]!r}", kind)
        if re.search(BLOCK_PAGE, head, re.I):
            kind = "rate_limited" if re.search(r"too many|pardner", head, re.I) else "restricted"
            raise AccountBlocked(f"Reddit showed a block page at {url}: {head[:160]!r}", kind)
        raise AccountBlocked(f"{url} did not return JSON (a block or login page?): {head[:160]!r}", "restricted")
    return json_error(data, url)


def check_login(driver, cfg_root: dict) -> str:
    """The logged-in username. With the root config.json's sender_username set, it must be that account.
    AccountBlocked('login') otherwise."""
    expected = cfg_root.get("sender_username")
    return verify_sender(driver, expected) if expected else active_sender(driver)
