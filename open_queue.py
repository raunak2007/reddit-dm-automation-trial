"""open_queue.py - walk a DM queue and open each compose link for a human to send.

By default this does NOT submit anything. It reads a queue HTML file (the format with one
<li class=c> per entry, a reddit.com/message/compose/ link, and a
<input type=checkbox data-u=...> Sent marker), opens the next unsent entry's
compose link in a persistent Chrome profile, and waits for you to press Enter
after you have clicked Send yourself in the browser. Entries you mark sent are
recorded in contacted.csv (ledger.py, shared with run_loop.py), and your place
in the queue is kept in queue_state/<queue>.json.

With --send it submits each entry's compose link itself, via the same loop as
run_loop.py: config.json's daily cap and delays, skipping anyone already sent or
undeliverable, and stopping (exit code 2) if Reddit refuses the account.

Before either mode opens anything, everyone in the account's Reddit sent messages is recorded as
sent (no sending if that check fails), and the queue page's own Sent checkmarks must have been
imported once with `python ledger.py import-checkmarks` (or pass --no-checkmarks).

Usage:
    python open_queue.py <queue.html> [--profile account_1] [--reset]
    python open_queue.py <queue.html> --list
    python open_queue.py <queue.html> --send [--profile account_1]

Options:
    --profile NAME   Chrome profile under accounts/ to use (default: account_1)
    --reset          Start from entry 1 again (who was sent stays in contacted.csv)
    --list           Print the parsed queue and exit (no browser)
    --send           Click Send automatically instead of waiting for you
    --no-checkmarks  Go ahead without importing the queue page's Sent checkmarks
"""
from __future__ import annotations

import argparse
import html as html_mod
import json
import re
import sys
import time
import urllib.parse
from pathlib import Path

import undetected_chromedriver as uc
from chrome_version import chrome_major

import ledger
from send_dm import Result, compose_link

HERE = Path(__file__).resolve().parent
STATE_DIR = HERE / "queue_state"
CONFIG = HERE / "config.json"

CARD_RE = re.compile(r"<li class=c>(.*?)</li>", re.S)
LINK_RE = re.compile(r'href="(https://www\.reddit\.com/message/compose/\?[^"]+)"')
DATA_U_RE = re.compile(r'data-u="([^"]+)"')
PRE_RE = re.compile(r"<pre[^>]*>(.*?)</pre>", re.S)


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
        to = user_m.group(1) if user_m else q.get("to", [""])[0]
        subject = q.get("subject", [""])[0]
        message = q.get("message", [""])[0]
        pre_m = PRE_RE.search(card)
        if pre_m:
            body = html_mod.unescape(pre_m.group(1)).strip()
            if " ".join(body.split()) != " ".join(message.split()):
                print(f"[!] entry {i}: compose link body differs from the <pre> body; "
                      f"using the <pre> body", file=sys.stderr)
                message = body
                link = compose_link(to, message, subject)
        entries.append({
            "index": i,
            "to": to,
            "subject": subject,
            "message": message,
            "link": link,
        })
    return entries


def state_path(queue_path: Path) -> Path:
    STATE_DIR.mkdir(exist_ok=True)
    return STATE_DIR / f"{queue_path.stem}.json"


def load_state(queue_path: Path) -> dict:
    p = state_path(queue_path)
    if not p.exists():
        return {"cursor": 0}
    return {"cursor": json.loads(p.read_text()).get("cursor", 0)}


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


def send_all(driver, entries: list) -> int:
    """--send: ledger.send_loop over the queue's compose links. Returns the exit code."""
    cfg = json.loads(CONFIG.read_text())
    return ledger.report(ledger.send_loop(driver, [(e["to"], e["link"]) for e in entries], cfg))


def run(queue_path: Path, profile: str, reset: bool, list_only: bool, auto_send: bool, no_checkmarks: bool):
    entries = parse_queue(queue_path)
    if not entries:
        sys.exit("No entries found in that queue file.")
    print(f"[+] Parsed {len(entries)} entries from {queue_path.name}")

    if list_only:
        for e in entries:
            why = ledger.skip_reason(e["to"])
            print(f"  {e['index']:>3}. u/{e['to']:<32} subject={e['subject']!r} "
                  f"body={len(e['message'])} chars" + (f"  [skip: {why}]" if why else ""))
        if not ledger.checkmarks_imported(queue_path):
            print("\n[!] " + ledger.checkmark_help(queue_path))
        return

    if not no_checkmarks and not ledger.checkmarks_imported(queue_path):
        sys.exit(ledger.checkmark_help(queue_path))
    state = {"cursor": 0} if reset else load_state(queue_path)
    driver = make_driver(profile)
    code = 0

    try:
        try:
            ledger.sync_reddit_sent(driver)
        except RuntimeError as exc:
            sys.exit(f"Not opening anything: {exc}")
        if auto_send:
            code = send_all(driver, entries)
        while not auto_send and state["cursor"] < len(entries):
            entry = entries[state["cursor"]]
            username = entry["to"]

            why = ledger.skip_reason(username)
            if why:
                print(f"[=] skipping u/{username}: {why}")
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
            ledger.record(username, Result("sent", "marked sent by hand"))
            state["cursor"] += 1
            save_state(queue_path, state)

        if not auto_send:
            done = sum(1 for e in entries if ledger.skip_reason(e["to"]))
            print(f"\n[done] {done} of {len(entries)} entries sent or not sendable; "
                  f"{ledger.sent_today()} sent today in total.")
            print(f"       state: {state_path(queue_path)}")
            input("Press Enter to close Chrome... ")
    except KeyboardInterrupt:
        print("\n[+] Interrupted. Progress saved.")
    finally:
        driver.quit()
    return code


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("queue", type=Path, help="path to the queue .html")
    ap.add_argument("--profile", default="account_1")
    ap.add_argument("--reset", action="store_true")
    ap.add_argument("--list", action="store_true", dest="list_only")
    ap.add_argument("--send", action="store_true", dest="auto_send")
    ap.add_argument("--no-checkmarks", action="store_true")
    args = ap.parse_args()
    if not args.queue.is_file():
        sys.exit(f"Queue file not found: {args.queue}")
    sys.exit(run(args.queue.resolve(), args.profile, args.reset, args.list_only, args.auto_send,
                 args.no_checkmarks))


if __name__ == "__main__":
    main()
