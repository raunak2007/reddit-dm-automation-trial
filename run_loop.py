"""run_loop.py - send DMs from target_users.csv on a loop, respecting config.json limits.

Reads:
    config.json        daily_limit_per_account, min/max delays, message_template
    target_users.csv   one username per row (header row "username")
    contacted.csv      written here; tracks who has already been messaged

Run:
    python run_loop.py
"""
import csv
import json
import os
import random
import time
from datetime import date, datetime
from pathlib import Path

import undetected_chromedriver as uc

from send_dm import send_dm

HERE = Path(__file__).resolve().parent
PROFILE = HERE / "accounts" / "account_1" / "chrome_profile"
CONFIG = HERE / "config.json"
TARGETS = HERE / "target_users.csv"
CONTACTED = HERE / "contacted.csv"


def load_config():
    with open(CONFIG) as f:
        return json.load(f)


def load_targets():
    with open(TARGETS, newline="") as f:
        return [row["username"].strip() for row in csv.DictReader(f) if row.get("username", "").strip()]


def load_contacted():
    if not CONTACTED.exists():
        return set()
    with open(CONTACTED, newline="") as f:
        return {row["username"] for row in csv.DictReader(f)}


def log_contacted(username, status):
    new = not CONTACTED.exists()
    with open(CONTACTED, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["username", "status", "timestamp"])
        w.writerow([username, status, datetime.now().isoformat()])


def main():
    if not PROFILE.is_dir():
        raise SystemExit(f"Profile folder not found: {PROFILE}\nLog in once with test_connection.py first.")

    cfg = load_config()
    targets = load_targets()
    contacted = load_contacted()
    cap = cfg["daily_limit_per_account"]
    sent = 0

    options = uc.ChromeOptions()
    options.add_argument(f"--user-data-dir={PROFILE}")
    options.add_argument("--no-first-run")
    options.add_argument("--no-default-browser-check")
    driver = uc.Chrome(options=options, version_main=154)

    try:
        driver.set_window_size(1400, 900)
        driver.get("https://www.reddit.com/chat/")
        time.sleep(random.uniform(4, 7))
        if "login" in driver.current_url:
            raise SystemExit("Not logged in. Run test_connection.py and log in manually first.")

        for username in targets:
            if sent >= cap:
                print(f"[+] Hit daily cap ({cap}). Stopping.")
                break
            if username in contacted:
                print(f"[=] Skipping {username} (already contacted)")
                continue

            message = cfg["message_template"].format(username=username)
            print(f"[+] Sending to {username} ({sent + 1}/{cap})")
            ok = False
            try:
                ok = send_dm(driver, username, message)
            except Exception as exc:
                print(f"[!] {username} raised {type(exc).__name__}: {exc}")

            log_contacted(username, "sent" if ok else "failed")
            contacted.add(username)
            if ok:
                sent += 1

            delay = random.uniform(cfg["min_delay_seconds"], cfg["max_delay_seconds"])
            print(f"[+] Waiting {delay:.0f}s before next send...")
            time.sleep(delay)

        print(f"[done] Sent {sent} messages this run.")
    finally:
        driver.quit()


if __name__ == "__main__":
    main()
