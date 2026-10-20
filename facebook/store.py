"""store.py - the scraped leads and the record of who has been contacted.

data/leads.json        {post_key: lead}. A lead is what scrape.py found (author, text, links, posted_at)
                       plus, once qualify.py has run, an "ai" block (score, flags, drafted DM and comment).
data/contacted.csv     one row per thing you did in the review queue: author_key, author, post_key, status,
                       timestamp, note. status is one of:
                           sent            you sent the DM: this author is never offered again
                           commented       you posted the public comment instead
                           skipped         not now: this post is hidden, a later post by them can still show
                           not_relevant    the AI got it wrong: hidden, and kept as a record of misses

Everything is rebuilt from these two files on every run, so nothing resets between runs and the daily cap
counts every DM sent today, whichever run it was in.
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import tempfile
import urllib.parse
from datetime import date, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
LEADS = DATA / "leads.json"
CONTACTED = DATA / "contacted.csv"
FIELDS = ["author_key", "author", "post_key", "status", "timestamp", "note"]
STATUSES = ("sent", "commented", "skipped", "not_relevant")
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


def messaged_authors() -> set[str]:
    return {r["author_key"] for r in _rows() if r["status"] in ("sent", "commented")}


def handled_posts() -> dict[str, str]:
    """{post_key: last status} for every post you've acted on."""
    return {r["post_key"]: r["status"] for r in _rows()}


def sent_today() -> int:
    today = date.today().isoformat()
    return len({r["author_key"] for r in _rows() if r["status"] == "sent" and r["timestamp"].startswith(today)})
