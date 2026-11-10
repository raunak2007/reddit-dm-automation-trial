"""store.py - the Reddit finder's own files, and what it reads from the root ledger.

data/leads.json     {post_id: lead}. A lead is what scrape.py found (author, subreddit, title, text, permalink,
                    created_at) plus, once qualify.py has run, an "ai" block (score, flags, drafted subject and
                    message).
data/queue.html     the leads send.py would send, in the queue format open_queue.parse_queue reads, so a person
                    can look them over or send them by hand with the root open_queue.py
data/replies.csv    one row per reply from someone in contacted.csv (replies.py): message_id, username, kind,
                    received_at, subject, snippet, permalink, recorded_at

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
REPLY_FIELDS = ["message_id", "username", "kind", "received_at", "subject", "snippet", "permalink", "recorded_at"]
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
    if not REPLIES.exists():
        return []
    with open(REPLIES, newline="") as f:
        return list(csv.DictReader(f))


def add_replies(rows: list[dict]) -> None:
    DATA.mkdir(exist_ok=True)
    new = not REPLIES.exists()
    with open(REPLIES, "a", newline="") as f:
        w = csv.DictWriter(f, REPLY_FIELDS, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerows(rows)
        f.flush()
        os.fsync(f.fileno())
