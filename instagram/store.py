"""store.py - the commenters found and the record of who has been contacted.

data/leads.json      {"<shortcode>:<username>": lead}: what comments.py read (username, display name, their
                     comment text, the post) plus, once qualify.py has run, an "ai" block (score, flags,
                     drafted DM).
data/contacted.csv   one row per thing you did in the review queue: username, post, status, timestamp, note.
                         sent           you followed and DMed them: never offered again, from any post
                         skipped        not now: hidden for this post only
                         not_relevant   the AI got it wrong: hidden, and kept as a record of misses
"""
from __future__ import annotations

import csv
import json
import os
import tempfile
from datetime import date, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
LEADS = DATA / "leads.json"
CONTACTED = DATA / "contacted.csv"
FIELDS = ["username", "lead_key", "status", "timestamp", "note"]
STATUSES = ("sent", "skipped", "not_relevant")


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


def messaged() -> set[str]:
    return {r["username"] for r in _rows() if r["status"] == "sent"}


def handled() -> dict[str, str]:
    """{lead_key: last status} for every lead you've acted on."""
    return {r["lead_key"]: r["status"] for r in _rows()}


def sent_today() -> int:
    today = date.today().isoformat()
    return len({r["username"] for r in _rows() if r["status"] == "sent" and r["timestamp"].startswith(today)})
