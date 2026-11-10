"""store.py - the Reddit finder's own files, and what it reads from the root ledger.

data/leads.json     {post_id: lead}. A lead is what scrape.py found (author, subreddit, title, text, permalink,
                    created_at) plus, once qualify.py has run, an "ai" block (score, flags, drafted subject and
                    message).
data/queue.html     the leads send.py would send, in the queue format open_queue.parse_queue reads, so a person
                    can look them over or send them by hand with the root open_queue.py
data/replies.csv    one row per reply from someone in contacted.csv (replies.py): message_id, username, kind,
                    received_at, subject, snippet, text (the full body, up to TEXT_CAP characters), permalink,
                    recorded_at. Files from before the text column are upgraded in place on the next write and
                    read meanwhile with text = snippet.
data/followups.csv  one row per follow-up event (followup.py): username, status (a send_dm.Result status, or
                    'submitting' written right before Send), timestamp, reason, post_id, subject, message. Kept
                    apart from contacted.csv so the ledger's own sent / held / attempts logic never sees a
                    second message to someone it has as sent; followups_today() is what counts them toward the
                    root daily cap.

Who has been messaged is NOT kept here. The root contacted.csv (ledger.py) is the one record for every Reddit
script: run_loop.py, open_queue.py and this folder's send.py all write to it, so the daily cap and
one-DM-per-person-ever hold across all of them. Importing this module puts the repo root on sys.path (after this
folder, so the modules here still come first), so `import ledger` and `import send_dm` work from here.
"""
from __future__ import annotations

import csv
import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
if str(ROOT) not in sys.path:
    sys.path.insert(1, str(ROOT))

import ledger  # noqa: E402

DATA = HERE / "data"
LEADS = DATA / "leads.json"
QUEUE = DATA / "queue.html"
REPLIES = DATA / "replies.csv"
CONFIG = HERE / "config.json"                    # finder settings: search, ai, outreach, browser
ROOT_CONFIG = ROOT / "config.json"               # sender_username, daily cap, delays (shared with run_loop.py)
FOLLOWUPS = DATA / "followups.csv"
REPLY_FIELDS = ["message_id", "username", "kind", "received_at", "subject", "snippet", "text", "permalink",
                "recorded_at"]
FOLLOWUP_FIELDS = ["username", "status", "timestamp", "reason", "post_id", "subject", "message"]
TEXT_CAP = 4000                                  # characters of a reply's body kept in replies.csv
FOLLOWUP_DONE = ("sent", "submitting", "uncertain", "refused", "recipient")   # any of these: never another one
FOLLOWUP_COUNTS = ("sent", "submitting", "uncertain")                         # may have gone out: counts today
REDDIT = "https://www.reddit.com"


def load_config() -> dict:
    return json.loads(CONFIG.read_text())


def load_root_config() -> dict:
    return json.loads(ROOT_CONFIG.read_text())


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


# ----------------------------------------------------------------- the root ledger, read only
def first_sent() -> dict[str, datetime]:
    """{username key: when they were first recorded 'sent'} from the root contacted.csv. Read directly (the
    ledger has no public reader for this); 'sent' rows mean the same in every version of the file."""
    out = {}
    if not ledger.LOG.exists():
        return out
    with open(ledger.LOG, newline="") as f:
        for r in csv.DictReader(f):
            if r.get("status") != "sent" or not r.get("username"):
                continue
            try:
                when = datetime.fromisoformat(r["timestamp"])
            except (TypeError, ValueError):
                when = datetime.min
            k = ledger.key(r["username"])
            if k not in out or when < out[k]:
                out[k] = when
    return out


# ----------------------------------------------------------------- replies
def load_replies() -> list[dict]:
    """Every row of replies.csv. Rows written before the text column have their snippet as text."""
    if not REPLIES.exists():
        return []
    with open(REPLIES, newline="") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        r["text"] = r.get("text") or r.get("snippet") or ""
    return rows


def _upgrade_replies() -> None:
    """Rewrite a replies.csv from before the text column so new rows line up with the header."""
    with open(REPLIES, newline="") as f:
        header = next(csv.reader(f), [])
    if header != REPLY_FIELDS:
        rows = load_replies()
        fd, tmp = tempfile.mkstemp(dir=DATA, suffix=".tmp")
        with os.fdopen(fd, "w", newline="") as f:
            w = csv.DictWriter(f, REPLY_FIELDS, extrasaction="ignore", restval="")
            w.writeheader()
            w.writerows(rows)
        os.replace(tmp, REPLIES)


def reply_by_id(message_id: str) -> dict | None:
    return next((r for r in load_replies() if r["message_id"] == message_id), None)


def add_replies(rows: list[dict]) -> None:
    DATA.mkdir(exist_ok=True)
    if REPLIES.exists():
        _upgrade_replies()
    new = not REPLIES.exists()
    with open(REPLIES, "a", newline="") as f:
        w = csv.DictWriter(f, REPLY_FIELDS, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerows(rows)
        f.flush()
        os.fsync(f.fileno())


# ----------------------------------------------------------------- follow-ups
def load_followups() -> list[dict]:
    if not FOLLOWUPS.exists():
        return []
    with open(FOLLOWUPS, newline="") as f:
        return list(csv.DictReader(f))


def record_followup(username: str, status: str, reason: str = "", **fields) -> None:
    """Append one row and flush it to disk before returning (a 'submitting' row must survive a crash)."""
    DATA.mkdir(exist_ok=True)
    new = not FOLLOWUPS.exists()
    with open(FOLLOWUPS, "a", newline="") as f:
        w = csv.DictWriter(f, FOLLOWUP_FIELDS, extrasaction="ignore", restval="")
        if new:
            w.writeheader()
        w.writerow(dict(fields, username=username, status=status, reason=reason[:300],
                        timestamp=datetime.now().isoformat(timespec="seconds")))
        f.flush()
        os.fsync(f.fileno())


def followed_up() -> dict[str, list[dict]]:
    """{username key: their follow-up rows, oldest first}."""
    out = {}
    for r in load_followups():
        out.setdefault(ledger.key(r["username"]), []).append(r)
    return out


def followups_today() -> int:
    """People sent (or possibly sent) a follow-up since local midnight. Added to ledger.sent_today() wherever
    this folder checks the root daily cap."""
    today = datetime.now().date().isoformat()
    return len({ledger.key(r["username"]) for r in load_followups()
                if r["status"] in FOLLOWUP_COUNTS and r["timestamp"].startswith(today)})


def sent_today() -> int:
    """The root daily cap's count: first DMs (contacted.csv) plus follow-ups (ledger.sent_today reads
    data/followups.csv too, so run_loop.py and open_queue.py share the same count)."""
    return ledger.sent_today()
