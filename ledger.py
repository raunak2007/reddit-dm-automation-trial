"""ledger.py - the one record of who has been messaged, shared by run_loop.py and open_queue.py.

contacted.csv gets one row per event: username, status, timestamp, reason. status is a send_dm.Result
status, or one of the ledger's own:
    submitting   written immediately before Send is clicked; if no outcome row follows (crash, kill,
                 power loss), the attempt is treated as uncertain
    cleared      written by `ledger.py resolve <user> --not-sent` after you've checked an uncertain one
Rows from older versions are read as: 'failed' / 'error' -> 'not_submitted', 'account' -> 'restricted',
and 'recipient' rows that were really refusals ("unable to message ...") -> 'refused'.

From that history:
    sent_today()      recipients sent to (or possibly sent to) since local midnight, across every run
    skip_reason(u)    why u must not be sent to now, or None:
                        already sent / no such user                         -> never again
                        last attempt uncertain (or interrupted mid-submit)  -> held until resolved
                        max_attempts_per_recipient failed/refused attempts  -> given up
    send_loop(...)    sends to eligible targets within the daily cap and delays; stops on anything
                      account-wide (login, rate limit, restriction) and on streaks that point at the
                      account rather than the recipients
    lock()            one sending run at a time

Sends made outside these scripts are pulled in as 'sent' rows too:
    sync_reddit_sent(driver)   everyone in the logged-in account's Reddit sent folder (run at the start of
                               every run; it also settles earlier uncertain sends that did go out)
    import_sent(...)           sent-folder pages saved from another browser or account
    import_checkmarks(...)     a queue page's Sent checkboxes (they live in the original browser's localStorage)

    python ledger.py import-checkmarks <queue.html> <exported.json | ->
    python ledger.py import-sent <page1.json> [page2.json ...]
    python ledger.py held
    python ledger.py resolve <username> --sent | --not-sent
"""
from __future__ import annotations

import argparse
import csv
import fcntl
import json
import os
import random
import re
import sys
import time
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path

from send_dm import (ACCOUNT_WIDE, NO_SUCH_USER, SENT_URL, UNAVAILABLE, AccountBlocked, Result, active_sender,
                     reddit_json, send_compose, verify_sender)

HERE = Path(__file__).resolve().parent
LOG = HERE / "contacted.csv"
STATE = HERE / "queue_state"
IMPORTS = STATE / "checkmark_imports.json"
LOCK = STATE / "send.lock"
GET_ITEM_RE = re.compile(r"""localStorage\.getItem\(\s*(?:(\w+)|['"]([^'"]+)['"])\s*\)""")
FIELDS = ["username", "status", "timestamp", "reason"]
FINAL = {"sent": "already sent", "recipient": "Reddit says no such user"}
HELD = ("uncertain", "submitting")       # the last row being one of these holds the recipient
ATTEMPTS = ("not_submitted", "refused", "uncertain")
MAX_ATTEMPTS = 3            # attempts before a recipient is given up on
MAX_FAILS_IN_A_ROW = 3      # not_submitted in a row: usually the account or the page, not the recipients
MAX_REFUSALS_IN_A_ROW = 3   # refusals from several unrelated recipients in a row
MAX_UNCERTAIN_IN_A_ROW = 2  # sends that can't be confirmed in a row: stop before making it worse
OLD_STATUS = {"failed": "not_submitted", "error": "not_submitted", "account": "restricted"}


def _rows():
    if not LOG.exists():
        return []
    with open(LOG, newline="") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        r["status"] = OLD_STATUS.get(r["status"], r["status"])
        reason = r.get("reason") or ""
        if (r["status"] == "recipient" and re.search(UNAVAILABLE, reason, re.I)
                and not re.search(NO_SUCH_USER, reason, re.I)):
            r["status"] = "refused"
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
    """Append one row and flush it to disk before returning (a 'submitting' row must survive a crash)."""
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
        f.flush()
        os.fsync(f.fileno())


@contextmanager
def lock():
    """Hold an exclusive lock for a whole run so two runs can't send (or double-send) at once."""
    STATE.mkdir(exist_ok=True)
    with open(LOCK, "w") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("Another run_loop.py / open_queue.py is already running; not starting a second one.")
        f.write(f"{datetime.now().isoformat(timespec='seconds')}\n")
        f.flush()
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _history():
    by_user = {}
    for r in _rows():
        by_user.setdefault(key(r["username"]), []).append(r)
    return by_user


def _already_sent():
    return {key(r["username"]) for r in _rows() if r["status"] == "sent"}


# ----------------------------------------------------------------- reconciliation
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
            try:
                data = reddit_json(driver, SENT_URL + (f"&after={after}" if after else ""))["data"]
            except (KeyError, TypeError):
                raise RuntimeError(f"Reddit's sent messages at {driver.current_url} weren't a listing") from None
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
    STATE.mkdir(exist_ok=True)
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


def prepare(driver, cfg):
    """Start of every run, before anything is opened or sent: confirm the browser is logged in as
    config.json's sender_username, then pull in this account's sent folder. Exits on any problem.
    Returns the sender's username."""
    expected = cfg.get("sender_username")
    try:
        if not expected:
            name = active_sender(driver)
            raise SystemExit(f'config.json has no "sender_username". This profile is logged in as u/{name}; if that '
                             f'is the account that should send, add  "sender_username": "{name}"  and run again.')
        sender = verify_sender(driver, expected)
        print(f"[+] sending as u/{sender}")
        sync_reddit_sent(driver)
    except AccountBlocked as exc:
        raise SystemExit(f"Not sending ({exc.kind}): {exc}")
    except RuntimeError as exc:
        raise SystemExit(f"Not sending: {exc}")
    return sender


# ----------------------------------------------------------------- decisions
def sent_today():
    """Recipients that got a message today, counting uncertain and interrupted submissions (they may have
    gone out), each recipient once."""
    today = date.today().isoformat()
    users = set()
    for user, rows in _history().items():
        if any(r["status"] in ("sent", "uncertain") and r["timestamp"].startswith(today) for r in rows):
            users.add(user)
        elif rows[-1]["status"] == "submitting" and rows[-1]["timestamp"].startswith(today):
            users.add(user)
    return len(users)


def held():
    """{username: last row} for every recipient whose last attempt's outcome is unknown."""
    out = {}
    for user, rows in _history().items():
        if rows[-1]["status"] in HELD and not any(r["status"] in FINAL for r in rows):
            out[rows[-1]["username"]] = rows[-1]
    return out


def skip_reason(username, max_attempts=MAX_ATTEMPTS):
    mine = _history().get(key(username), [])
    for r in mine:
        if r["status"] in FINAL:
            return f"{FINAL[r['status']]} ({r['timestamp']})"
    if mine and mine[-1]["status"] in HELD:
        what = "was interrupted after Send was clicked" if mine[-1]["status"] == "submitting" else "couldn't be confirmed"
        return (f"HELD: the last attempt ({mine[-1]['timestamp']}) {what} and may have gone out. Check the sent "
                f"folder, then: python ledger.py resolve {username} --sent | --not-sent")
    tries = [r for r in mine if r["status"] in ATTEMPTS]
    if len(tries) >= max_attempts:
        refused = sum(1 for r in tries if r["status"] == "refused")
        return f"gave up after {len(tries)} attempts ({refused} refused by Reddit, cause not stated)"
    return None


def resolve(username, sent: bool):
    if key(username) not in {key(u) for u in held()}:
        raise SystemExit(f"u/{username} has no held (uncertain) attempt to resolve.")
    record(username, Result("sent", "confirmed by hand after an uncertain attempt") if sent
           else Result("cleared", "checked by hand: the uncertain attempt did not go out"))
    print(f"[+] u/{username} recorded as {'sent' if sent else 'not sent (eligible again)'}")


# ----------------------------------------------------------------- the loop
STREAKS = {
    "not_submitted": (MAX_FAILS_IN_A_ROW, "failures before submission in a row with no recipient-specific reason; "
                                          "check the account and the saved step*_failed files"),
    "refused": (MAX_REFUSALS_IN_A_ROW, "different recipients refused in a row. Reddit doesn't say why; refusals "
                                       "across unrelated recipients fit a restriction on this account better than "
                                       "each one's settings, but don't prove it. Before going on, try messaging an "
                                       "account known to accept messages"),
    "uncertain": (MAX_UNCERTAIN_IN_A_ROW, "sends in a row couldn't be confirmed in the sent folder. Check them "
                                          "(python ledger.py held) before sending more"),
}
NOTES = {
    "refused": "Reddit refused without saying why (their settings or a restriction on this account); may retry later",
    "not_submitted": "failed before Send was clicked; will retry on a later run",
    "uncertain": "Send was clicked but the message couldn't be confirmed; HELD until you check it",
    "recipient": "Reddit says there is no such user; won't retry",
    "invalid": "bad entry; not sent",
}


def send_loop(driver, targets, cfg, sender, send=send_compose):
    """targets: [(username, compose_link)], links used exactly as given. sender: the verified account name.
    Returns a count per status for this run, plus 'stopped' (why it ended early, '' if it got through the
    list) and 'halted' (True if it stopped for anything other than the daily cap)."""
    cap = cfg["daily_limit_per_account"]
    max_attempts = cfg.get("max_attempts_per_recipient", MAX_ATTEMPTS)
    counts = {s: 0 for s in ("sent", "recipient", "refused", "invalid", "not_submitted", "uncertain", "skipped")}
    counts.update(stopped="", halted=False)
    streak = {"status": None, "n": 0}
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
        submitted = []

        def before_submit():
            record(username, Result("submitting", "about to click Send"))
            submitted.append(True)

        try:
            result = send(driver, link, sender=sender, before_submit=before_submit)
        except Exception as exc:                          # a bug or a dead browser, not a Reddit answer
            result = Result("uncertain" if submitted else "not_submitted", f"{type(exc).__name__}: {exc}")
        if result.status != "invalid" or submitted:       # invalid entries never reached Reddit: nothing to keep
            record(username, result)

        if result.status in ACCOUNT_WIDE:
            counts["stopped"], counts["halted"] = f"account-wide problem ({result.status}): {result.reason}", True
            break
        counts[result.status] += 1
        if result.status != "sent":
            print(f"    [!] {NOTES.get(result.status, result.status)}: {result.reason}")
        streak = ({"status": result.status, "n": streak["n"] + 1} if result.status == streak["status"]
                  else {"status": result.status, "n": 1})
        limit, why = STREAKS.get(result.status, (None, ""))
        if limit and streak["n"] >= limit:
            counts["stopped"], counts["halted"] = f"{streak['n']} {why}", True
            break

        if result.status != "invalid" and i + 1 < len(targets) and sent_today() < cap:
            delay = random.uniform(cfg["min_delay_seconds"], cfg["max_delay_seconds"])
            print(f"[+] Waiting {delay:.0f}s before the next one...")
            time.sleep(delay)
    return counts


def report(counts):
    """Print a run summary; returns the exit code (2 if the run halted on an account or streak problem)."""
    print(f"\n[done] confirmed sent {counts['sent']}, uncertain {counts['uncertain']}, refused {counts['refused']}, "
          f"no such user {counts['recipient']}, failed before submitting {counts['not_submitted']}, "
          f"bad entries {counts['invalid']}, skipped {counts['skipped']}; {sent_today()} sent today in total")
    if counts["stopped"]:
        print(f"[stopped] {counts['stopped']}")
    waiting = held()
    if waiting:
        print(f"[!] {len(waiting)} recipient(s) HELD with an uncertain outcome: python ledger.py held")
    return 2 if counts["halted"] else 0


# ----------------------------------------------------------------- command line
def main():
    ap = argparse.ArgumentParser(description="Manage contacted.csv: import outside sends, review held attempts.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    imp = sub.add_parser("import-checkmarks", help="record the users checked off in a queue page as sent")
    imp.add_argument("queue", type=Path, help="the queue .html the checkmarks belong to")
    imp.add_argument("exported", help="file holding the exported localStorage JSON, or - for stdin")
    sent = sub.add_parser("import-sent", help="record everyone in saved Reddit sent-message pages as sent",
                          description="In the browser logged in as the other account, open " + SENT_URL +
                          " and save the page (Cmd+S) as page1.json; if it ends with an \"after\" value, "
                          "open the same URL plus &after=<that value> for page2.json, and so on.")
    sent.add_argument("pages", nargs="+", type=Path)
    sub.add_parser("held", help="list recipients whose last attempt's outcome is unknown")
    res = sub.add_parser("resolve", help="settle a held recipient after checking the sent folder yourself")
    res.add_argument("username")
    which = res.add_mutually_exclusive_group(required=True)
    which.add_argument("--sent", action="store_true", help="it did go out: never message them again")
    which.add_argument("--not-sent", action="store_true", help="it didn't go out: eligible again")
    args = ap.parse_args()

    if args.cmd == "import-sent":
        try:
            import_sent(args.pages)
        except RuntimeError as exc:
            sys.exit(str(exc))
    elif args.cmd == "held":
        waiting = held()
        for user, r in waiting.items():
            print(f"u/{user}  {r['status']}  {r['timestamp']}  {r['reason']}")
        print(f"{len(waiting)} held. Check each in the sent folder (https://www.reddit.com/message/sent/), then "
              f"python ledger.py resolve <user> --sent | --not-sent" if waiting else "Nothing held.")
    elif args.cmd == "resolve":
        resolve(args.username, args.sent)
    else:
        if not storage_key(args.queue.read_text(encoding="utf-8")):
            sys.exit(f"{args.queue.name} has no localStorage Sent checkboxes.")
        text = sys.stdin.read() if args.exported == "-" else Path(args.exported).read_text()
        import_checkmarks(args.queue, text.strip())


if __name__ == "__main__":
    main()
