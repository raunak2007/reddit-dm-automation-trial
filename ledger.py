"""ledger.py - the one record of who has been messaged, shared by run_loop.py and open_queue.py.

contacted.csv gets one row per attempt: username, status, timestamp, reason.
status is a send_dm.Result status ('sent', 'recipient', 'invalid', 'account', 'error'); rows written
before statuses existed say 'failed', which is read as 'error'.

From that history:
    sent_today()      successful sends since local midnight, across every run and both scripts
    skip_reason(u)    why u must not be sent to again (already sent, Reddit won't deliver, bad entry,
                      or too many errors), or None if u should be tried
    send_loop(...)    sends to a list of targets within config.json's cap and delays, and stops as soon
                      as a failure is about the account rather than the recipient

Sends made outside these scripts are pulled in as 'sent' rows too:
    sync_reddit_sent(driver)   everyone in the logged-in account's Reddit "sent" private messages
    import_checkmarks(...)     a queue page's Sent checkboxes, which live in the localStorage of the
                               browser that ticked them (see `python ledger.py --help`)

    import_sent(...)           sent-message pages saved from another browser or account

    python ledger.py import-checkmarks <queue.html> <exported.json | ->
    python ledger.py import-sent <page1.json> [page2.json ...]
"""
from __future__ import annotations

import argparse
import csv
import json
import random
import re
import sys
import time
from datetime import date, datetime
from pathlib import Path

from send_dm import Result, send_compose

HERE = Path(__file__).resolve().parent
LOG = HERE / "contacted.csv"
IMPORTS = HERE / "queue_state" / "checkmark_imports.json"
SENT_URL = "https://old.reddit.com/message/sent/.json?limit=100&raw_json=1"
GET_ITEM_RE = re.compile(r"""localStorage\.getItem\(\s*(?:(\w+)|['"]([^'"]+)['"])\s*\)""")
FIELDS = ["username", "status", "timestamp", "reason"]
FINAL = {"sent": "already sent", "recipient": "Reddit won't deliver to them", "invalid": "bad entry"}
MAX_ATTEMPTS = 3          # 'error' results before a recipient is given up on
MAX_ERRORS_IN_A_ROW = 3   # unexplained failures in a row usually mean the account or the page is the problem


def _rows():
    if not LOG.exists():
        return []
    with open(LOG, newline="") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        r["status"] = "error" if r["status"] == "failed" else r["status"]
    return rows


def _upgrade():
    """Rewrite a contacted.csv from before the 'reason' column so new rows line up with the header."""
    with open(LOG, newline="") as f:
        header = next(csv.reader(f), [])
    if header != FIELDS:
        rows = _rows()
        with open(LOG, "w", newline="") as f:
            w = csv.DictWriter(f, FIELDS, extrasaction="ignore", restval="")
            w.writeheader()
            w.writerows(rows)


def key(username):
    return username.strip().lower().removeprefix("u/")


def record(username, result: Result, when: datetime | None = None):
    if LOG.exists():
        _upgrade()
    new = not LOG.exists()
    with open(LOG, "a", newline="") as f:
        w = csv.DictWriter(f, FIELDS)
        if new:
            w.writeheader()
        w.writerow({"username": username, "status": result.status,
                    "timestamp": (when or datetime.now()).isoformat(timespec="seconds"),
                    "reason": result.reason[:300]})


def _already_sent():
    return {key(r["username"]) for r in _rows() if r["status"] == "sent"}


def _sent_listing(text, where):
    """The 'data' of one page of Reddit's sent-messages JSON, or RuntimeError naming `where`."""
    try:
        return json.loads(text)["data"]
    except (ValueError, KeyError, TypeError):
        raise RuntimeError(f"couldn't read Reddit's sent messages from {where} (logged out, or not that JSON?): "
                           f"{text[:120]!r}") from None


def _record_sent(pages, source):
    """Record every recipient in these sent-message listing pages not already marked sent."""
    have = _already_sent()
    found, added = set(), 0
    for data in pages:
        for child in data["children"]:
            msg = child["data"]
            dest = (msg.get("dest") or "").removeprefix("u/")
            if not dest or dest.startswith("#") or key(dest) in found:     # '#...' = modmail to a subreddit
                continue
            found.add(key(dest))
            if key(dest) not in have:
                when = datetime.fromtimestamp(msg["created_utc"])
                record(dest, Result("sent", f"in {source}: {msg.get('subject', '')!r}"), when)
                have.add(key(dest))
                added += 1
    print(f"[+] {source}: {len(found)} recipients, {added} not in the ledger before")
    return added


def sync_reddit_sent(driver, max_pages=10):
    """Record everyone the logged-in account has sent a private message to (Reddit's sent folder,
    newest 100 x max_pages; Reddit lists at most ~1000) and isn't already marked sent here.
    Raises RuntimeError if the folder can't be read, so callers can refuse to send blind."""
    def pages():
        after = None
        for _ in range(max_pages):
            driver.get(SENT_URL + (f"&after={after}" if after else ""))
            data = _sent_listing(driver.execute_script("return document.body ? document.body.innerText : ''"),
                                 driver.current_url)
            yield data
            after = data.get("after")
            if not after:
                break
    return _record_sent(pages(), "this account's Reddit sent messages")


def import_sent(paths):
    """Record recipients from sent-message JSON pages saved from any browser/account (see main())."""
    return _record_sent([_sent_listing(Path(p).read_text(encoding="utf-8"), p) for p in paths],
                        "imported Reddit sent messages")


def storage_key(queue_html: str):
    """The localStorage key a queue page keeps its Sent checkboxes under, or None."""
    m = GET_ITEM_RE.search(queue_html)
    if not m:
        return None
    if m.group(2):                                    # getItem('the-key')
        return m.group(2)
    var = re.search(rf"""\b(?:const|let|var)\s+{m.group(1)}\s*=\s*['"]([^'"]+)['"]""", queue_html)
    return var and var.group(1)


def checkmarks_imported(queue_path: Path):
    k = storage_key(queue_path.read_text(encoding="utf-8"))
    if not k:
        return True                                   # page has no checkboxes to import
    return IMPORTS.exists() and k in json.loads(IMPORTS.read_text())


def import_checkmarks(queue_path: Path, exported: str):
    """exported: the JSON the queue page stores, {"username": "YYYY-MM-DD", ...}. Records each
    checked user as sent on that date (noon), unless already marked sent."""
    k = storage_key(queue_path.read_text(encoding="utf-8"))
    if not k:
        raise SystemExit(f"{queue_path.name} has no localStorage Sent checkboxes to import.")
    marks = json.loads(exported or "{}")
    if not isinstance(marks, dict):
        raise SystemExit("Expected a JSON object like {\"username\": \"2026-10-13\"}.")
    have = _already_sent()
    added = 0
    for username, day in marks.items():
        if key(username) in have:
            continue
        try:
            when = datetime.fromisoformat(f"{day}T12:00:00")
        except (TypeError, ValueError):
            when = datetime.now()
        record(username, Result("sent", f"checked off in {queue_path.name}"), when)
        have.add(key(username))
        added += 1
    IMPORTS.parent.mkdir(exist_ok=True)
    done = json.loads(IMPORTS.read_text()) if IMPORTS.exists() else {}
    done[k] = {"queue": queue_path.name, "imported_at": datetime.now().isoformat(timespec="seconds"),
               "checked": len(marks)}
    IMPORTS.write_text(json.dumps(done, indent=2))
    print(f"[+] {len(marks)} checkmarks in {k!r}; {added} not in the ledger before")


def checkmark_help(queue_path: Path):
    k = storage_key(queue_path.read_text(encoding="utf-8"))
    return (f"{queue_path.name} keeps its Sent checkmarks in the localStorage of the browser they were ticked in,\n"
            f"which this Chrome profile can't see. Import them once before sending:\n"
            f"  1. Open {queue_path.name} in the ORIGINAL browser, open the console (Cmd+Opt+J / Cmd+Opt+C), run:\n"
            f"       copy(localStorage.getItem('{k}') || '{{}}')\n"
            f"  2. pbpaste | python ledger.py import-checkmarks \"{queue_path}\" -\n"
            f"(Or pass --no-checkmarks to send without them; Reddit's sent folder is still checked.)")


def sent_today():
    today = date.today().isoformat()
    return sum(1 for r in _rows() if r["status"] == "sent" and r["timestamp"].startswith(today))


def skip_reason(username, max_attempts=MAX_ATTEMPTS):
    mine = [r for r in _rows() if key(r["username"]) == key(username)]
    for r in mine:
        if r["status"] in FINAL:
            return f"{FINAL[r['status']]} ({r['timestamp']})"
    errors = sum(1 for r in mine if r["status"] == "error")    # 'account' rows weren't their fault
    if errors >= max_attempts:
        return f"gave up after {errors} failed attempts"
    return None


def send_loop(driver, targets, cfg, send=send_compose):
    """targets: [(username, compose_link)]. Returns a count per status for this run, plus 'stopped':
    why the loop ended early ('' if it got through the list), and 'halted': True if that reason was a
    problem with the account rather than the daily cap."""
    cap = cfg["daily_limit_per_account"]
    max_attempts = cfg.get("max_attempts_per_recipient", MAX_ATTEMPTS)
    counts = {"sent": 0, "recipient": 0, "invalid": 0, "error": 0, "skipped": 0, "stopped": "", "halted": False}
    errors_in_a_row = 0
    for i, (username, link) in enumerate(targets):
        done_today = sent_today()
        if done_today >= cap:
            counts["stopped"] = f"hit the daily cap ({done_today}/{cap} sent today)"
            break
        why = skip_reason(username, max_attempts)
        if why:
            print(f"[=] skipping u/{username}: {why}")
            counts["skipped"] += 1
            continue

        print(f"\n[+] u/{username} ({done_today + 1}/{cap} today)")
        try:
            result = send(driver, link)
        except Exception as exc:                          # a bug or a dead browser, not a Reddit answer
            result = Result("error", f"{type(exc).__name__}: {exc}")
        record(username, result)

        if result.status == "account":
            counts["stopped"], counts["halted"] = f"Reddit is refusing this account: {result.reason}", True
            break
        counts[result.status] += 1
        if result.status == "sent":
            errors_in_a_row = 0
        elif result.status == "error":
            errors_in_a_row += 1
            print(f"    [!] failed (will retry on a later run): {result.reason}")
            if errors_in_a_row >= MAX_ERRORS_IN_A_ROW:
                counts["stopped"] = (f"{errors_in_a_row} failures in a row with no recipient-specific reason; "
                                     "check the account and the saved step*_failed files")
                counts["halted"] = True
                break
        else:
            print(f"    [!] not sendable ({result.status}), won't retry: {result.reason}")

        if result.status != "invalid" and i + 1 < len(targets) and sent_today() < cap:    # invalid never reached Reddit
            delay = random.uniform(cfg["min_delay_seconds"], cfg["max_delay_seconds"])
            print(f"[+] Waiting {delay:.0f}s before the next one...")
            time.sleep(delay)
    return counts


def report(counts):
    """Print a run summary; returns the exit code (2 if the loop stopped because of the account)."""
    print(f"\n[done] sent {counts['sent']}, unreachable {counts['recipient']}, bad entries {counts['invalid']}, "
          f"errors {counts['error']}, skipped {counts['skipped']}; {sent_today()} sent today in total")
    if counts["stopped"]:
        print(f"[stopped] {counts['stopped']}")
    return 2 if counts["halted"] else 0


def main():
    ap = argparse.ArgumentParser(description="Record sends made outside these scripts in contacted.csv.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    imp = sub.add_parser("import-checkmarks", help="record the users checked off in a queue page as sent")
    imp.add_argument("queue", type=Path, help="the queue .html the checkmarks belong to")
    imp.add_argument("exported", help="file holding the exported localStorage JSON, or - for stdin")
    sent = sub.add_parser("import-sent", help="record everyone in saved Reddit sent-message pages as sent",
                          description="In the browser logged in as the other account, open " + SENT_URL +
                          " and save the page (Cmd+S) as page1.json; if it ends with an \"after\" value, "
                          "open the same URL plus &after=<that value> for page2.json, and so on.")
    sent.add_argument("pages", nargs="+", type=Path)
    args = ap.parse_args()
    if args.cmd == "import-sent":
        try:
            import_sent(args.pages)
        except RuntimeError as exc:
            sys.exit(str(exc))
        return
    if not storage_key(args.queue.read_text(encoding="utf-8")):
        sys.exit(f"{args.queue.name} has no localStorage Sent checkboxes.")
    text = sys.stdin.read() if args.exported == "-" else Path(args.exported).read_text()
    import_checkmarks(args.queue, text.strip())


if __name__ == "__main__":
    main()
