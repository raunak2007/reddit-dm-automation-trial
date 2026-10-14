"""open_queue.py - walk a DM queue and open each compose link for a human to send.

By default this does NOT submit anything. It reads a queue HTML file (the format with one
<li class=c> per entry, a reddit.com/message/compose/ link, and a
<input type=checkbox data-u=...> Sent marker), opens the next unsent entry's
compose link in a persistent Chrome profile, and waits for you to press Enter
after you have clicked Send yourself in the browser. It then records the entry
in a local state file so the next run resumes where you left off.

With --send it submits each entry's compose link itself (send_dm.send_compose), honoring
daily_limit_per_account and min/max_delay_seconds from config.json.

Usage:
    python open_queue.py <queue.html> [--profile account_1] [--reset]
    python open_queue.py <queue.html> --list
    python open_queue.py <queue.html> --send [--profile account_1]

Options:
    --profile NAME   Chrome profile under accounts/ to use (default: account_1)
    --reset          Forget local state for this queue and start from entry 1
    --list           Print the parsed queue and exit (no browser)
    --send           Click Send automatically instead of waiting for you
"""
from __future__ import annotations

import argparse
import html as html_mod
import json
import random
import re
import sys
import time
import urllib.parse
from pathlib import Path

import undetected_chromedriver as uc
from chrome_version import chrome_major
from send_dm import send_compose

HERE = Path(__file__).resolve().parent
STATE_DIR = HERE / "queue_state"
CONFIG = HERE / "config.json"

CARD_RE = re.compile(r"<li class=c>(.*?)</li>", re.S)
LINK_RE = re.compile(r'href="(https://www\.reddit\.com/message/compose/\?[^"]+)"')
DATA_U_RE = re.compile(r'data-u="([^"]+)"')


def parse_queue(path: Path):
    raw = path.read_text(encoding="utf-8")
    entries = []
    for i, card in enumerate(CARD_RE.findall(raw), 1):
        link_m = LINK_RE.search(card)
        user_m = DATA_U_RE.search(card)
        if not link_m:
            print(f"[!] entry {i} has no compose link; skipping", file=sys.stderr)
            continue
        link = html_mod.unescape(link_m.group(1))
        q = urllib.parse.parse_qs(urllib.parse.urlparse(link).query)
        entries.append({
            "index": i,
            "to": (user_m.group(1) if user_m else q.get("to", [""])[0]),
            "subject": q.get("subject", [""])[0],
            "message": q.get("message", [""])[0],
            "link": link,
        })
    return entries


def state_path(queue_path: Path) -> Path:
    STATE_DIR.mkdir(exist_ok=True)
    return STATE_DIR / f"{queue_path.stem}.json"


def load_state(queue_path: Path) -> dict:
    p = state_path(queue_path)
    if not p.exists():
        return {"sent": {}, "cursor": 0}
    return json.loads(p.read_text())


def save_state(queue_path: Path, state: dict) -> None:
    state_path(queue_path).write_text(json.dumps(state, indent=2))


def make_driver(profile_name: str):
    profile_dir = HERE / "accounts" / profile_name / "chrome_profile"
    if not profile_dir.is_dir():
        sys.exit(f"Profile folder not found: {profile_dir}\n"
                 f"Run test_connection.py first to log in to account '{profile_name}'.")
    for lock in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        lock_path = profile_dir / lock
        if lock_path.exists():
            lock_path.unlink()
    opts = uc.ChromeOptions()
    opts.add_argument(f"--user-data-dir={profile_dir}")
    opts.add_argument("--no-first-run")
    opts.add_argument("--no-default-browser-check")
    driver = uc.Chrome(options=opts, version_main=chrome_major())
    driver.set_window_size(1400, 900)
    return driver


def mark_sent(queue_path: Path, state: dict, entry: dict) -> None:
    state["sent"][entry["to"]] = {
        "index": entry["index"],
        "sent_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    state["cursor"] += 1
    save_state(queue_path, state)


def send_all(driver, queue_path: Path, entries: list, state: dict) -> None:
    """--send: submit each unsent entry's compose link, within config.json's cap and delays.
    Starts from the top every run, so entries that failed last time are retried."""
    cfg = json.loads(CONFIG.read_text())
    cap = cfg["daily_limit_per_account"]
    today = time.strftime("%Y-%m-%d")
    sent_today = sum(1 for v in state["sent"].values() if v["sent_at"].startswith(today))
    failed = 0
    state["cursor"] = 0
    while state["cursor"] < len(entries):
        entry = entries[state["cursor"]]
        if entry["to"] in state["sent"]:
            state["cursor"] += 1
            continue
        if sent_today >= cap:
            print(f"[+] Hit daily cap ({cap}). Stopping; the next run resumes here.")
            break
        print(f"\n[{state['cursor'] + 1}/{len(entries)}] u/{entry['to']} ({sent_today + 1}/{cap} today)")
        try:
            ok = send_compose(driver, entry["link"])
        except ValueError as exc:
            print(f"    [!] bad entry: {exc}")
            ok = False
        if ok:
            mark_sent(queue_path, state, entry)
            sent_today += 1
        else:
            failed += 1
            print("    [!] not sent; leaving it unsent and moving on")
            state["cursor"] += 1
            save_state(queue_path, state)
        if state["cursor"] < len(entries) and sent_today < cap:
            delay = random.uniform(cfg["min_delay_seconds"], cfg["max_delay_seconds"])
            print(f"[+] Waiting {delay:.0f}s before the next one...")
            time.sleep(delay)
    if failed:
        print(f"[!] {failed} entries failed; the next --send run retries them.")


def run(queue_path: Path, profile: str, reset: bool, list_only: bool, auto_send: bool):
    entries = parse_queue(queue_path)
    if not entries:
        sys.exit("No entries found in that queue file.")
    print(f"[+] Parsed {len(entries)} entries from {queue_path.name}")

    if list_only:
        for e in entries:
            print(f"  {e['index']:>3}. u/{e['to']:<32} subject={e['subject']!r} "
                  f"body={len(e['message'])} chars")
        return

    state = {"sent": {}, "cursor": 0} if reset else load_state(queue_path)
    driver = make_driver(profile)

    try:
        if auto_send:
            send_all(driver, queue_path, entries, state)
        while not auto_send and state["cursor"] < len(entries):
            entry = entries[state["cursor"]]
            username = entry["to"]

            if username in state["sent"]:
                state["cursor"] += 1
                continue

            print(f"\n[{state['cursor'] + 1}/{len(entries)}] u/{username}")
            print(f"    subject: {entry['subject']!r}")
            print(f"    body:    {len(entry['message'])} chars")

            driver.get(entry["link"])
            time.sleep(1.5)

            print("    → Reddit compose tab is open with the message prefilled.")
            print("    → Review it. Click Send yourself if you want to send it.")
            print("    → Then come back here.")
            choice = input(
                "    [s]ent  [k]skip  [o]pen-in-browser-again  [q]uit  > "
            ).strip().lower()

            if choice == "q":
                print("[+] Stopping. Cursor saved.")
                break
            if choice == "o":
                driver.get(entry["link"])
                time.sleep(1.0)
                continue
            if choice == "k":
                print("    skipped")
                state["cursor"] += 1
                save_state(queue_path, state)
                continue

            # default: treat anything else as "sent"
            mark_sent(queue_path, state, entry)

        remaining = len(entries) - len(state["sent"])
        print(f"\n[done] {len(state['sent'])} marked sent, {remaining} remaining.")
        print(f"       state: {state_path(queue_path)}")
        input("Press Enter to close Chrome... ")
    except KeyboardInterrupt:
        print("\n[+] Interrupted. Progress saved.")
    finally:
        driver.quit()


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("queue", type=Path, help="path to the queue .html")
    ap.add_argument("--profile", default="account_1")
    ap.add_argument("--reset", action="store_true")
    ap.add_argument("--list", action="store_true", dest="list_only")
    ap.add_argument("--send", action="store_true", dest="auto_send")
    args = ap.parse_args()
    if not args.queue.is_file():
        sys.exit(f"Queue file not found: {args.queue}")
    run(args.queue.resolve(), args.profile, args.reset, args.list_only, args.auto_send)


if __name__ == "__main__":
    main()
