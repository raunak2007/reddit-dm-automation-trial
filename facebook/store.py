"""store.py - the scraped leads and the record of who has been contacted (this folder's ledger.py).

data/leads.json        {post_key: lead}. A lead is what scrape.py found (author, text, links, posted_at)
                       plus, once qualify.py has run, an "ai" block (score, flags, drafted DM and comment), and
                       once you approve it in the review queue, an "approved" block (the exact DM send.py sends).
data/contacted.csv     one row per event: author_key, author, post_key, status, timestamp, note. status is one of:
                       from the review queue:
                           approved        you approved the DM: send.py will send it
                           unapproved      you took the approval back: the post is back in "To review"
                           sent            you sent the DM yourself: this author is never offered again
                           commented       you posted the public comment instead
                           skipped         not now: this post is hidden, a later post by them can still show
                           not_relevant    the AI got it wrong: hidden, and kept as a record of misses
                       from send.py (the statuses of send_dm.Result, as on master):
                           sent            confirmed: the message is in the chat after reloading it
                           submitting      written just before Send is pressed; with no outcome after it (crash,
                                           kill), the attempt is treated as uncertain
                           uncertain       Send was pressed but the message couldn't be confirmed: HELD
                           refused         Facebook won't let this account message them (cause not stated)
                           recipient       Facebook says the person isn't available: never retried
                           not_submitted   failed before Send was pressed: retried on a later run
                           login / rate_limited / restricted   account-wide: the run stopped there
                           cleared         `send.py resolve <author_key> --not-sent` after you checked a held one

From that history, as ledger.py does for Reddit:
    sent_today()       authors DMed (or possibly DMed) since local midnight, across every run and by hand
    skip_reason(a)     why author a must not be DMed now: already sent / commented / unavailable -> never;
                       last attempt uncertain or interrupted -> held; max attempts failed -> given up
    lock()             one send.py run at a time

Everything is rebuilt from these two files on every run, so nothing resets between runs.
"""
from __future__ import annotations

import csv
import fcntl
import hashlib
import json
import os
import re
import tempfile
import urllib.parse
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
LEADS = DATA / "leads.json"
CONTACTED = DATA / "contacted.csv"
LOCK = DATA / "send.lock"
FIELDS = ["author_key", "author", "post_key", "status", "timestamp", "note"]
QUEUE_STATUSES = ("approved", "unapproved", "sent", "commented", "skipped", "not_relevant")
SEND_STATUSES = ("submitting", "uncertain", "refused", "recipient", "not_submitted", "cleared",
                 "login", "rate_limited", "restricted")
STATUSES = QUEUE_STATUSES + SEND_STATUSES
FINAL = {"sent": "already sent", "commented": "already commented on their post",
         "recipient": "Facebook says this person isn't available"}
HELD = ("uncertain", "submitting")       # the last row being one of these holds the author
ATTEMPTS = ("not_submitted", "refused", "uncertain")
# A post whose last row is one of these is approved and hasn't gone out yet: send.py picks it up.
WAITING = ("approved", "cleared", "not_submitted", "refused", "login", "rate_limited", "restricted")
MAX_ATTEMPTS = 3                         # failed attempts before an author is given up on
FB = "https://www.facebook.com"
RESERVED = {"groups", "events", "watch", "marketplace", "photo", "photo.php", "story.php", "permalink.php",
            "search", "hashtag", "messages", "notifications", "pages", "reel", "share", "login", "home.php"}


# ----------------------------------------------------------------- identity
def author_key(href: str | None) -> str | None:
    """A stable id for a profile link: the numeric id when the link has one, else the username.
    /groups/<g>/user/<id>/, /profile.php?id=<id>, /people/<name>/<id>/ and /<username> all work."""
    if not href:
        return None
    u = urllib.parse.urlparse(urllib.parse.urljoin(FB, href))
    if "facebook.com" not in u.netloc:
        return None
    path = [p for p in u.path.split("/") if p]
    q = urllib.parse.parse_qs(u.query)
    if path[:1] == ["profile.php"] and q.get("id"):
        return q["id"][0]
    if len(path) >= 4 and path[0] == "groups" and path[2] == "user":
        return path[3]
    if len(path) >= 3 and path[0] == "people" and path[2].isdigit():
        return path[2]
    if len(path) == 1 and re.fullmatch(r"[A-Za-z0-9.]{3,}", path[0]) and path[0] not in RESERVED:
        return path[0].lower()
    return None


def profile_url(key: str) -> str:
    return f"{FB}/profile.php?id={key}" if key.isdigit() else f"{FB}/{key}"


def messenger_url(key: str) -> str:
    return f"{FB}/messages/t/{key}"


def post_key(post_url: str | None, author: str | None, text: str) -> str:
    """The permalink's post id when there is one, else a hash of author + text (search results sometimes
    hide the permalink until hovered)."""
    if post_url:
        u = urllib.parse.urlparse(post_url)
        q = urllib.parse.parse_qs(u.query)
        for name in ("story_fbid", "fbid", "multi_permalinks"):
            if q.get(name):
                return f"p:{q[name][0]}"
        m = re.search(r"/(?:posts|permalink)/([A-Za-z0-9]+)", u.path)
        if m:
            return f"p:{m.group(1)}"
    return "h:" + hashlib.sha1(f"{author}|{text[:400]}".encode()).hexdigest()[:16]


# ----------------------------------------------------------------- leads
def load_leads() -> dict:
    return json.loads(LEADS.read_text()) if LEADS.exists() else {}


def save_leads(leads: dict) -> None:
    """Write via a temp file so a crash mid-write can't leave half a leads.json."""
    DATA.mkdir(exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=DATA, suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump(leads, f, indent=1, ensure_ascii=False)
    os.replace(tmp, LEADS)


# ----------------------------------------------------------------- contacted
def _rows() -> list[dict]:
    if not CONTACTED.exists():
        return []
    with open(CONTACTED, newline="") as f:
        return list(csv.DictReader(f))


def record(author_key: str, author: str, post_key: str, status: str, note: str = "") -> None:
    """Append one row and flush it to disk before returning (a 'submitting' row must survive a crash)."""
    if status not in STATUSES:
        raise ValueError(f"unknown status {status!r}")
    DATA.mkdir(exist_ok=True)
    new = not CONTACTED.exists()
    with open(CONTACTED, "a", newline="") as f:
        w = csv.DictWriter(f, FIELDS)
        if new:
            w.writeheader()
        w.writerow({"author_key": author_key, "author": author, "post_key": post_key, "status": status,
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
    """{author_key: their rows, oldest first}."""
    by_author = {}
    for r in _rows():
        by_author.setdefault(r["author_key"], []).append(r)
    return by_author


def messaged_authors(hist: dict | None = None) -> set[str]:
    """Authors never to be offered again: sent, commented, unavailable, or a send that may have gone out."""
    hist = history() if hist is None else hist
    return {a for a, rows in hist.items() if any(r["status"] in FINAL for r in rows) or rows[-1]["status"] in HELD}


def handled_posts() -> dict[str, str]:
    """{post_key: last status} for every post you've acted on."""
    return {r["post_key"]: r["status"] for r in _rows()}


def sent_today() -> int:
    """Authors DMed today, each once, across every run and by hand. Still-unresolved uncertain or interrupted
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
    """{author_key: last row} for every author whose last send's outcome is unknown."""
    return {a: rows[-1] for a, rows in history().items()
            if rows[-1]["status"] in HELD and not any(r["status"] in FINAL for r in rows)}


def skip_reason(author_key: str, max_attempts: int = MAX_ATTEMPTS, hist: dict | None = None) -> str | None:
    mine = (history() if hist is None else hist).get(author_key, [])
    for r in mine:
        if r["status"] in FINAL:
            return f"{FINAL[r['status']]} ({r['timestamp']})"
    if mine and mine[-1]["status"] in HELD:
        what = "was interrupted after Send was pressed" if mine[-1]["status"] == "submitting" else "couldn't be confirmed"
        return (f"HELD: the last attempt ({mine[-1]['timestamp']}) {what} and may have gone out. Check the chat, "
                f"then: python send.py resolve {author_key} --sent | --not-sent")
    tries = [r for r in mine if r["status"] in ATTEMPTS]
    if len(tries) >= max_attempts:
        refused = sum(1 for r in tries if r["status"] == "refused")
        return f"gave up after {len(tries)} attempts ({refused} refused by Facebook, cause not stated)"
    return None


def resolve(author_key: str, sent: bool) -> None:
    last = held().get(author_key)
    if not last:
        raise SystemExit(f"{author_key} has no held (uncertain) send to resolve.")
    if sent:
        record(author_key, last["author"], last["post_key"], "sent", "confirmed by hand after an uncertain attempt")
    else:
        record(author_key, last["author"], last["post_key"], "cleared",
               "checked by hand: the uncertain attempt did not go out")
    print(f"[+] {last['author'] or author_key} recorded as {'sent' if sent else 'not sent (eligible again)'}")
