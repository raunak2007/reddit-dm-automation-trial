"""run_loop.py - send DMs on a loop, respecting config.json limits.

Reads:
    config.json        daily_limit_per_account, min/max delays, subject_template, message_template
    <queue.html>       if given: each entry's own recipient, subject and personalized message
    target_users.csv   if no queue is given: one username per row (header row "username"),
                       messaged with config.json's subject_template / message_template
    contacted.csv      every attempt and its outcome (see ledger.py); shared with open_queue.py

Already-sent and undeliverable recipients are skipped; other failures are retried on later runs.
"Already sent" includes everyone in the account's Reddit sent messages (checked at the start of every
run; no sending if that check fails) and a queue page's imported Sent checkmarks (see ledger.py).
The run stops (exit code 2) as soon as Reddit refuses the account itself.

Run:
    python run_loop.py <queue.html> [--no-checkmarks]
    python run_loop.py
"""
import argparse
import csv
import json
import random
import sys
import time
from pathlib import Path

import undetected_chromedriver as uc
from chrome_version import chrome_major

import ledger
from open_queue import parse_queue
from send_dm import COMPOSE_URL, DEFAULT_SUBJECT, Result, compose_link

HERE = Path(__file__).resolve().parent
PROFILE = HERE / "accounts" / "account_1" / "chrome_profile"
CONFIG = HERE / "config.json"
TARGETS = HERE / "target_users.csv"


def load_config():
    with open(CONFIG) as f:
        return json.load(f)


def load_targets(cfg, queue=None):
    """[(username, subject, message)] from the queue file, or from target_users.csv + config templates."""
    if queue:
        return [(e["to"], e["subject"], e["message"]) for e in parse_queue(queue)]
    with open(TARGETS, newline="") as f:
        names = [row["username"].strip() for row in csv.DictReader(f) if row.get("username", "").strip()]
    subject = cfg.get("subject_template", DEFAULT_SUBJECT)
    return [(u, subject.format(username=u), cfg["message_template"].format(username=u)) for u in names]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("queue", nargs="?", type=Path, help="queue .html to send from (default: target_users.csv)")
    ap.add_argument("--no-checkmarks", action="store_true",
                    help="send even though the queue page's Sent checkmarks haven't been imported")
    args = ap.parse_args()
    if args.queue and not args.queue.is_file():
        raise SystemExit(f"Queue file not found: {args.queue}")
    if args.queue and not args.no_checkmarks and not ledger.checkmarks_imported(args.queue):
        raise SystemExit(ledger.checkmark_help(args.queue))
    if not PROFILE.is_dir():
        raise SystemExit(f"Profile folder not found: {PROFILE}\nLog in once with test_connection.py first.")

    cfg = load_config()
    targets = load_targets(cfg, args.queue)
    if not targets:
        raise SystemExit("No targets found.")
    links = []
    for username, subject, message in targets:
        try:
            links.append((username, compose_link(username, message, subject)))
        except ValueError as exc:
            if not ledger.skip_reason(username):
                print(f"[!] u/{username}: {exc}")
                ledger.record(username, Result("invalid", str(exc)))
    cap = cfg["daily_limit_per_account"]
    if ledger.sent_today() >= cap:
        raise SystemExit(f"Already sent {ledger.sent_today()}/{cap} today. Nothing to do until tomorrow.")

    options = uc.ChromeOptions()
    options.add_argument(f"--user-data-dir={PROFILE}")
    options.add_argument("--no-first-run")
    options.add_argument("--no-default-browser-check")
    driver = uc.Chrome(options=options, version_main=chrome_major())

    try:
        driver.set_window_size(1400, 900)
        driver.get(COMPOSE_URL)
        time.sleep(random.uniform(4, 7))
        if "login" in driver.current_url:
            raise SystemExit("Not logged in. Run test_connection.py and log in manually first.")
        try:
            ledger.sync_reddit_sent(driver)
        except RuntimeError as exc:
            raise SystemExit(f"Not sending: {exc}")

        counts = ledger.send_loop(driver, links, cfg)
    finally:
        driver.quit()
    sys.exit(ledger.report(counts))


if __name__ == "__main__":
    main()
