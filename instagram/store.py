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
                         cleared        a held send checked and found not to have gone out (send.py does this
                                        at the start of each run, or `send.py resolve ... --not-sent`)
                     from followup.py (one follow-up, ever, to someone who got our DM and never replied; these
                     rows' lead_key is "followup:<username>", so the review page's view of the lead is unchanged,
                     and the first DM's held / sent logic below ignores them):
                         followup_submitting   written just before Send is pressed on the follow-up
                         followup_sent         Instagram took the follow-up
                         followup_uncertain    Send was pressed but it couldn't be confirmed (never retried)
                         followup_failed       not sent (failed before Send, or skipped by a check): may retry

data/posts.json      {"<shortcode>": {"url", "owner", "taken_at", "comment_count", "first_seen", "last_read",
                     "reads", "commenters", "source"}}: the posts discover.py found and when each was last read.
data/replies.csv     one row per message someone sent back after our DM (replies.py): username, item_id,
                     replied_at, snippet, text (the whole message, up to 4000 characters), thread_url, sent_at,
                     found_at, how ('api' = the inbox, 'request' = Message Requests, 'page' = the inbox page's
                     chat list). One row per item_id. Files from before `text` existed are still read (their
                     text is the snippet) and get the column the next time a reply is added.

From that history, as ledger.py does for Reddit:
    sent_today()       DMs sent (or possibly sent) since local midnight, across every run and by hand: each
                       person's first DM, plus their follow-up if that went out today
    skip_reason(u)     why u must not be DMed now: already sent / no such account -> never; last attempt
                       uncertain or interrupted -> held; max attempts failed -> given up
    lock()             one run at a time on the Chrome profile (send.py, and run.py reading posts or replies)
    sent_at()          {username: when our DM went out}, for everyone recorded 'sent'
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
POSTS = DATA / "posts.json"
REPLIES = DATA / "replies.csv"
REPLY_FIELDS = ["username", "item_id", "replied_at", "snippet", "text", "thread_url", "sent_at", "found_at", "how"]
FIELDS = ["username", "lead_key", "status", "timestamp", "note"]
QUEUE_STATUSES = ("approved", "unapproved", "sent", "skipped", "not_relevant")
SEND_STATUSES = ("submitting", "uncertain", "refused", "recipient", "not_submitted", "cleared",
                 "login", "rate_limited", "restricted")
FOLLOWUP_STATUSES = ("followup_submitting", "followup_sent", "followup_uncertain", "followup_failed")
FOLLOWUP_DONE = ("followup_submitting", "followup_sent", "followup_uncertain")   # it went, or may have: never again
STATUSES = QUEUE_STATUSES + SEND_STATUSES + FOLLOWUP_STATUSES
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


def _save_json(path: Path, value) -> None:
    """Write via a temp file so a crash mid-write can't leave half a file."""
    DATA.mkdir(exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=DATA, suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump(value, f, indent=1, ensure_ascii=False)
    os.replace(tmp, path)


def save_leads(leads: dict) -> None:
    _save_json(LEADS, leads)


def load_posts() -> dict:
    return json.loads(POSTS.read_text()) if POSTS.exists() else {}


def save_posts(posts: dict) -> None:
    _save_json(POSTS, posts)


def _reply_header() -> list[str]:
    with open(REPLIES, newline="") as f:
        return next(csv.reader(f), [])


def load_replies() -> list[dict]:
    """Every row of data/replies.csv, each with every REPLY_FIELDS key (an older file without `text` gives the
    snippet as the text)."""
    if not REPLIES.exists():
        return []
    with open(REPLIES, newline="") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        for k in REPLY_FIELDS:
            if r.get(k) is None:
                r[k] = (r.get("snippet") or "") if k == "text" else ""
    return rows


def add_replies(rows: list[dict]) -> list[dict]:
    """Append the replies whose item_id isn't in data/replies.csv yet; returns the ones added. A file written
    before a column existed is rewritten with the current header first (atomically), so columns never shift."""
    old = load_replies()
    seen = {r["item_id"] for r in old}
    fresh = []
    for r in rows:
        if r["item_id"] not in seen:
            seen.add(r["item_id"])
            fresh.append(r)
    if fresh:
        DATA.mkdir(exist_ok=True)
        if REPLIES.exists() and _reply_header() != REPLY_FIELDS:
            fd, tmp = tempfile.mkstemp(dir=DATA, suffix=".tmp")
            with os.fdopen(fd, "w", newline="") as f:
                w = csv.DictWriter(f, REPLY_FIELDS)
                w.writeheader()
                w.writerows({k: r.get(k, "") for k in REPLY_FIELDS} for r in old)
            os.replace(tmp, REPLIES)
        new = not REPLIES.exists()
        with open(REPLIES, "a", newline="") as f:
            w = csv.DictWriter(f, REPLY_FIELDS)
            if new:
                w.writeheader()
            w.writerows({k: r.get(k, "") for k in REPLY_FIELDS} for r in fresh)
    return fresh


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
    """Hold an exclusive lock for a whole send.py run so two runs can't send (or double-send) at once. run.py also
    holds it while it reads posts or the inbox, since those use the same Chrome profile."""
    DATA.mkdir(exist_ok=True)
    with open(LOCK, "w") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("Another send.py (or run.py reading posts or replies) is already using this Instagram "
                             "profile; not starting a second one.")
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


def is_followup(row: dict) -> bool:
    return row["status"] in FOLLOWUP_STATUSES


def dm_rows(rows: list[dict]) -> list[dict]:
    """A person's rows about the first DM: everything but follow-up rows (followup.py)."""
    return [r for r in rows if not is_followup(r)]


def followup_rows(rows: list[dict]) -> list[dict]:
    return [r for r in rows if is_followup(r)]


def followup_key(username: str) -> str:
    """The lead_key of a follow-up row: never a real lead's key, so handled() and the review page don't see it."""
    return f"followup:{username.lower()}"


def messaged(hist: dict | None = None) -> set[str]:
    """People never to be offered again: sent, no such account, or a send that may have gone out."""
    hist = history() if hist is None else hist
    out = set()
    for u, rows in hist.items():
        mine = dm_rows(rows)
        if mine and (any(r["status"] in FINAL for r in mine) or mine[-1]["status"] in HELD):
            out.add(u)
    return out


def sent_at(hist: dict | None = None) -> dict[str, str]:
    """{username: timestamp of their first 'sent' row}: when our DM went out (or was marked sent by hand)."""
    hist = history() if hist is None else hist
    return {u: next(r["timestamp"] for r in rows if r["status"] == "sent")
            for u, rows in hist.items() if any(r["status"] == "sent" for r in rows)}


def handled() -> dict[str, str]:
    """{lead_key: last status} for every lead you've acted on."""
    return {r["lead_key"]: r["status"] for r in _rows()}


def sent_today() -> int:
    """DMs sent today across every run and by hand: each person's first DM once, plus their follow-up
    (followup.py) if it went out today. Still-unresolved uncertain or interrupted sends from today count too,
    since they may have gone out; one resolved --not-sent (or a follow-up that failed before Send) doesn't."""
    today = date.today().isoformat()
    n = 0
    for rows in history().values():
        mine, extra = dm_rows(rows), followup_rows(rows)
        if any(r["status"] == "sent" and r["timestamp"].startswith(today) for r in mine):
            n += 1
        elif mine and mine[-1]["status"] in HELD and mine[-1]["timestamp"].startswith(today):
            n += 1
        if any(r["status"] in FOLLOWUP_DONE and r["timestamp"].startswith(today) for r in extra) \
                and extra[-1]["status"] != "followup_failed":
            n += 1
    return n


def held() -> dict[str, dict]:
    """{username: last row} for everyone whose last send's outcome is unknown (follow-up rows aside)."""
    out = {}
    for u, rows in history().items():
        mine = dm_rows(rows)
        if mine and mine[-1]["status"] in HELD and not any(r["status"] in FINAL for r in mine):
            out[u] = mine[-1]
    return out


def skip_reason(username: str, max_attempts: int = MAX_ATTEMPTS, hist: dict | None = None) -> str | None:
    mine = dm_rows((history() if hist is None else hist).get(username.lower(), []))
    for r in mine:
        if r["status"] in FINAL:
            return f"{FINAL[r['status']]} ({r['timestamp']})"
    if mine and mine[-1]["status"] in HELD:
        what = "was interrupted after Send was pressed" if mine[-1]["status"] == "submitting" else "couldn't be confirmed"
        return (f"HELD: the last attempt ({mine[-1]['timestamp']}) {what} and may have gone out. The next send.py "
                f"run checks the chat (or by hand: python send.py resolve {username.lower()} --sent | --not-sent)")
    tries = [r for r in mine if r["status"] in ATTEMPTS]
    if len(tries) >= max_attempts:
        refused = sum(1 for r in tries if r["status"] == "refused")
        return f"gave up after {len(tries)} attempts ({refused} refused by Instagram, cause not stated)"
    return None


def resolve(username: str, sent: bool, how: str = "by hand") -> None:
    """Settle a held send: 'sent', or 'cleared' (eligible again). how: who checked the chat, for the note."""
    username = username.lower().removeprefix("@")
    last = held().get(username)
    if not last:
        raise SystemExit(f"@{username} has no held (uncertain) send to resolve.")
    if sent:
        record(username, last["lead_key"], "sent", f"confirmed {how} after an uncertain attempt")
    else:
        record(username, last["lead_key"], "cleared", f"checked {how}: the uncertain attempt did not go out")
    print(f"[+] @{username} recorded as {'sent' if sent else 'not sent (eligible again)'}")
