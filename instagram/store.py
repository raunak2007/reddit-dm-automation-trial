"""store.py - the commenters found and the record of who has been contacted (this folder's ledger.py).

data/leads.json      {"<shortcode>:<username>": lead}: what comments.py read (username, display name, their
                     comment text, the post) plus, once qualify.py has run, an "ai" block (score, flags,
                     drafted DM), and once you approve it on the review page, an "approved" block (the exact
                     DM send.py sends).
data/contacted.csv   one row per event: username, lead_key, status, timestamp, note. status is one of:
                     from the review page:
                         approved       you approved the DM: send.py will send it
                         unapproved     you took the approval back: the lead is back in "To send"
                         sent           you followed and DMed them yourself: never offered again, from any post
                         skipped        not now: hidden for this post only
                         not_relevant   the AI got it wrong: hidden, and kept as a record of misses
                     from send.py (the statuses of send_dm.Result, as on master):
                         sent           confirmed: the message is in the chat after reloading it
                         submitting     written just before Send is pressed; with no outcome after it (crash,
                                        kill), the attempt is treated as uncertain
                         uncertain      Send was pressed but the message couldn't be confirmed: HELD
                         refused        Instagram won't let this account message them (cause not stated)
                         recipient      Instagram says there is no such account: never retried
                         not_submitted  failed before Send was pressed: retried on a later run
                         login / rate_limited / restricted   account-wide: the run stopped there
                         cleared        `send.py resolve <username> --not-sent` after you checked a held one

From that history, as ledger.py does for Reddit:
    sent_today()       people DMed (or possibly DMed) since local midnight, across every run and by hand
    skip_reason(u)     why u must not be DMed now: already sent / no such account -> never; last attempt
                       uncertain or interrupted -> held; max attempts failed -> given up
    lock()             one send.py run at a time
"""
from __future__ import annotations

import csv
import fcntl
import json
import os
import tempfile
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
LEADS = DATA / "leads.json"
CONTACTED = DATA / "contacted.csv"
LOCK = DATA / "send.lock"
FIELDS = ["username", "lead_key", "status", "timestamp", "note"]
QUEUE_STATUSES = ("approved", "unapproved", "sent", "skipped", "not_relevant")
SEND_STATUSES = ("submitting", "uncertain", "refused", "recipient", "not_submitted", "cleared",
                 "login", "rate_limited", "restricted")
STATUSES = QUEUE_STATUSES + SEND_STATUSES
FINAL = {"sent": "already sent", "recipient": "Instagram says there is no such account"}
HELD = ("uncertain", "submitting")       # the last row being one of these holds the person
ATTEMPTS = ("not_submitted", "refused", "uncertain")
# A lead whose last row is one of these is approved and hasn't gone out yet: send.py picks it up.
WAITING = ("approved", "cleared", "not_submitted", "refused", "login", "rate_limited", "restricted")
MAX_ATTEMPTS = 3                         # failed attempts before someone is given up on


def lead_key(shortcode: str, username: str) -> str:
    return f"{shortcode}:{username.lower()}"


def load_leads() -> dict:
    return json.loads(LEADS.read_text()) if LEADS.exists() else {}


def save_leads(leads: dict) -> None:
    """Write via a temp file so a crash mid-write can't leave half a leads.json."""
    DATA.mkdir(exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=DATA, suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump(leads, f, indent=1, ensure_ascii=False)
    os.replace(tmp, LEADS)


def _rows() -> list[dict]:
    if not CONTACTED.exists():
        return []
    with open(CONTACTED, newline="") as f:
        return list(csv.DictReader(f))


def record(username: str, key: str, status: str, note: str = "") -> None:
    """Append one row and flush it to disk before returning (a 'submitting' row must survive a crash)."""
    if status not in STATUSES:
        raise ValueError(f"unknown status {status!r}")
    DATA.mkdir(exist_ok=True)
    new = not CONTACTED.exists()
    with open(CONTACTED, "a", newline="") as f:
        w = csv.DictWriter(f, FIELDS)
        if new:
            w.writeheader()
        w.writerow({"username": username.lower(), "lead_key": key, "status": status,
                    "timestamp": datetime.now().isoformat(timespec="seconds"), "note": note[:300]})
        f.flush()
        os.fsync(f.fileno())


@contextmanager
def lock():
    """Hold an exclusive lock for a whole send.py run so two runs can't send (or double-send) at once."""
    DATA.mkdir(exist_ok=True)
    with open(LOCK, "w") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("Another send.py is already running; not starting a second one.")
        f.write(f"{datetime.now().isoformat(timespec='seconds')}\n")
        f.flush()
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def history() -> dict[str, list[dict]]:
    """{username: their rows, oldest first}."""
    by_user = {}
    for r in _rows():
        by_user.setdefault(r["username"], []).append(r)
    return by_user


def messaged(hist: dict | None = None) -> set[str]:
    """People never to be offered again: sent, no such account, or a send that may have gone out."""
    hist = history() if hist is None else hist
    return {u for u, rows in hist.items() if any(r["status"] in FINAL for r in rows) or rows[-1]["status"] in HELD}


def handled() -> dict[str, str]:
    """{lead_key: last status} for every lead you've acted on."""
    return {r["lead_key"]: r["status"] for r in _rows()}


def sent_today() -> int:
    """People DMed today, each once, across every run and by hand. Still-unresolved uncertain or interrupted
    sends from today count too, since they may have gone out; one resolved --not-sent doesn't."""
    today = date.today().isoformat()
    n = 0
    for rows in history().values():
        if any(r["status"] == "sent" and r["timestamp"].startswith(today) for r in rows):
            n += 1
        elif rows[-1]["status"] in HELD and rows[-1]["timestamp"].startswith(today):
            n += 1
    return n


def held() -> dict[str, dict]:
    """{username: last row} for everyone whose last send's outcome is unknown."""
    return {u: rows[-1] for u, rows in history().items()
            if rows[-1]["status"] in HELD and not any(r["status"] in FINAL for r in rows)}


def skip_reason(username: str, max_attempts: int = MAX_ATTEMPTS, hist: dict | None = None) -> str | None:
    mine = (history() if hist is None else hist).get(username.lower(), [])
    for r in mine:
        if r["status"] in FINAL:
            return f"{FINAL[r['status']]} ({r['timestamp']})"
    if mine and mine[-1]["status"] in HELD:
        what = "was interrupted after Send was pressed" if mine[-1]["status"] == "submitting" else "couldn't be confirmed"
        return (f"HELD: the last attempt ({mine[-1]['timestamp']}) {what} and may have gone out. Check the chat, "
                f"then: python send.py resolve {username.lower()} --sent | --not-sent")
    tries = [r for r in mine if r["status"] in ATTEMPTS]
    if len(tries) >= max_attempts:
        refused = sum(1 for r in tries if r["status"] == "refused")
        return f"gave up after {len(tries)} attempts ({refused} refused by Instagram, cause not stated)"
    return None


def resolve(username: str, sent: bool) -> None:
    username = username.lower().removeprefix("@")
    last = held().get(username)
    if not last:
        raise SystemExit(f"@{username} has no held (uncertain) send to resolve.")
    if sent:
        record(username, last["lead_key"], "sent", "confirmed by hand after an uncertain attempt")
    else:
        record(username, last["lead_key"], "cleared", "checked by hand: the uncertain attempt did not go out")
    print(f"[+] @{username} recorded as {'sent' if sent else 'not sent (eligible again)'}")
