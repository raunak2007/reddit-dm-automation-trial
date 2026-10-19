"""run_loop.py - send DMs on a loop, respecting config.json limits.

Reads:
    config.json        sender_username, daily_limit_per_account, min/max delays, subject_template,
                       message_template, max_attempts_per_recipient (optional, default 3)
    <queue.html>       required: each entry's compose link, used exactly as written, so every recipient
                       gets the personalized subject and message the queue page shows (entries whose
                       link, checkbox, heading and <pre> body disagree are reported and not sent)
    target_users.csv   only with --from-csv: one username per row (header row "username"), messaged
                       with config.json's generic subject_template / message_template
    contacted.csv      every attempt and its outcome (see ledger.py); shared with open_queue.py

Each run: takes a lock (one run at a time), checks the browser is logged in as sender_username, records
everyone in that account's Reddit sent folder, then for each eligible entry opens the compose link, checks
the prefilled fields, clicks Send once, and records 'sent' only once the message is in the sent folder.
A send that can't be confirmed is recorded 'uncertain' and held until you resolve it (python ledger.py held).
The run stops (exit code 2) on any account-wide problem (login, rate limit, restriction) and on streaks of
failures, refusals or uncertain sends.

Run:
    python run_loop.py <queue.html> [--no-checkmarks]
    python run_loop.py --from-csv      (generic template, not a queue's personalized messages)
"""
import argparse
import csv
import json
import sys
from pathlib import Path

import undetected_chromedriver as uc
from chrome_version import chrome_major

import ledger
from open_queue import parse_queue
from send_dm import DEFAULT_SUBJECT, compose_link

HERE = Path(__file__).resolve().parent
PROFILE = HERE / "accounts" / "account_1" / "chrome_profile"
CONFIG = HERE / "config.json"
TARGETS = HERE / "target_users.csv"


def load_config():
    with open(CONFIG) as f:
        return json.load(f)


def load_targets(cfg, queue=None):
    """[(username, compose_link)]. Queue links are taken exactly as written, so each entry keeps its own
    subject and message; with no queue, CSV targets get a link built from config.json's templates."""
    if queue:
        entries = [e for e in parse_queue(queue) if not e["problem"]]
        print(f"[+] {len(entries)} sendable entries from {queue.name}; each is sent with its own subject and message")
        return [(e["to"], e["link"]) for e in entries]
    print(f"[!] --from-csv: messaging {TARGETS.name} with config.json's generic template, not a queue's messages")
    with open(TARGETS, newline="") as f:
        names = [row["username"].strip() for row in csv.DictReader(f) if row.get("username", "").strip()]
    subject = cfg.get("subject_template", DEFAULT_SUBJECT)
    targets = []
    for u in names:
        try:
            targets.append((u, compose_link(u, cfg["message_template"].format(username=u), subject.format(username=u))))
        except ValueError as exc:
            print(f"[!] u/{u}: {exc}; not sending", file=sys.stderr)
    return targets


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("queue", nargs="?", type=Path, help="queue .html to send from (required unless --from-csv)")
    ap.add_argument("--from-csv", action="store_true",
                    help="send to target_users.csv with config.json's generic template instead of a queue")
    ap.add_argument("--no-checkmarks", action="store_true",
                    help="send even though the queue page's Sent checkmarks haven't been imported")
    args = ap.parse_args()
    if bool(args.queue) == args.from_csv:
        ap.error("give a queue .html (its personalized messages are sent), or --from-csv for the generic "
                 "template, not both")
    if args.queue and not args.queue.is_file():
        raise SystemExit(f"Queue file not found: {args.queue}")
    if args.queue and not args.no_checkmarks and not ledger.checkmarks_imported(args.queue):
        raise SystemExit(ledger.checkmark_help(args.queue))
    if args.queue and ledger.checkmarks_note(args.queue):
        print(ledger.checkmarks_note(args.queue))
    if not PROFILE.is_dir():
        raise SystemExit(f"Profile folder not found: {PROFILE}\nLog in once with test_connection.py first.")

    cfg = load_config()
    targets = load_targets(cfg, args.queue)
    if not targets:
        raise SystemExit("No sendable targets found.")
    cap = cfg["daily_limit_per_account"]

    with ledger.lock():
        if ledger.sent_today() >= cap:
            raise SystemExit(f"Already sent {ledger.sent_today()}/{cap} today. Nothing to do until tomorrow.")
        options = uc.ChromeOptions()
        options.add_argument(f"--user-data-dir={PROFILE}")
        options.add_argument("--no-first-run")
        options.add_argument("--no-default-browser-check")
        driver = uc.Chrome(options=options, version_main=chrome_major())
        try:
            driver.set_window_size(1400, 900)
            sender = ledger.prepare(driver, cfg)
            counts = ledger.send_loop(driver, targets, cfg, sender)
        finally:
            driver.quit()
    sys.exit(ledger.report(counts))


if __name__ == "__main__":
    main()
