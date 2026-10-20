"""browser.py - open Chrome with the Facebook session the scraper and send.py use.

By default each profile lives in accounts/<profile>/chrome_profile and you log in to it once
(python run.py login). To read with the Chrome profile you already use day to day instead, set
config.json's browser.user_data_dir (e.g. "~/Library/Application Support/Google/Chrome") and quit
Chrome first: Chrome won't let two processes share a profile.

Importing it also puts the repo root on sys.path, so the facebook scripts share the Reddit scripts'
chrome_version.py and send_dm.py's page helpers instead of keeping copies.
"""
from __future__ import annotations

import random
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(1, str(HERE.parent))             # after this folder, so its own modules still come first

import undetected_chromedriver as uc              # noqa: E402
from chrome_version import chrome_major           # noqa: E402

LOGGED_OUT = ("log in to facebook", "log into facebook", "create new account", "forgot password?")


def make_driver(cfg: dict):
    b = cfg["browser"]
    if b.get("user_data_dir"):
        profile_dir = Path(b["user_data_dir"]).expanduser()
        if not profile_dir.is_dir():
            sys.exit(f"browser.user_data_dir doesn't exist: {profile_dir}")
    else:
        profile_dir = HERE / "accounts" / b["profile"] / "chrome_profile"
        profile_dir.mkdir(parents=True, exist_ok=True)
    for lock in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        (profile_dir / lock).unlink(missing_ok=True)
    opts = uc.ChromeOptions()
    opts.add_argument(f"--user-data-dir={profile_dir}")
    opts.add_argument("--no-first-run")
    opts.add_argument("--no-default-browser-check")
    opts.add_argument("--disable-notifications")
    driver = uc.Chrome(options=opts, version_main=chrome_major())
    driver.set_window_size(1300, 950)
    return driver


def pause(cfg: dict, scale: float = 1.0):
    b = cfg["browser"]
    time.sleep(random.uniform(b["min_page_delay_seconds"], b["max_page_delay_seconds"]) * scale)


def logged_in(driver) -> bool:
    text = driver.execute_script("return (document.body && document.body.innerText || '').slice(0, 3000)").lower()
    return "/login" not in driver.current_url and not any(s in text for s in LOGGED_OUT)


def ensure_logged_in(driver, cfg: dict, interactive: bool = True):
    """Open facebook.com; if it shows the login page, wait for you to log in in the window."""
    driver.get("https://www.facebook.com/")
    pause(cfg, 0.6)
    if logged_in(driver):
        return
    if not interactive:
        sys.exit("Not logged in to Facebook in this profile. Run: python run.py login")
    print("[!] Not logged in. Log in to Facebook in the Chrome window (including any 2FA / checkpoint).")
    input("    Press Enter here once your News Feed is showing... ")
    driver.get("https://www.facebook.com/")
    pause(cfg, 0.6)
    if not logged_in(driver):
        sys.exit("Still not logged in; stopping.")
