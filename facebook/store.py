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
                           cleared         a held send checked and found not to have gone out (send.py does
                                           this at the start of each run, or `send.py resolve ... --not-sent`)
                       from comment.py (the public comment, outreach.auto_comment):
                           commented       posted, the same status as the queue's "Mark commented"
                           comment_submitting    written just before Post is pressed (the note is the comment's
                                           text); with no outcome after it, the comment is HELD
                           comment_uncertain     Post was pressed but the comment couldn't be confirmed: HELD
                           comment_not_submitted failed before Post was pressed: retried on a later run
                           comment_closed  the post doesn't take comments from this account: never retried
                           comment_stopped account-wide problem before Post was pressed (note says which): the
                                           run stopped there; retried on a later run, not counted as an attempt
                           comment_cleared a held comment checked and found not to be on the post
                       from followup.py (one follow-up DM to people who never replied, followup.enabled):
                           followup_submitting   written just before Send is pressed (the note is the text);
                                           with no outcome after it, the follow-up may have gone out
                           followup_sent         Messenger took it (or it was already in the chat)
                           followup_uncertain    Send was pressed but it couldn't be confirmed
                           followup_not_submitted failed before Send was pressed: retried on a later run
                           followup_refused      Facebook won't let this account message them now: not retried
                           followup_stopped      account-wide problem before Send was pressed: the run stopped
                       Follow-up rows never change where a lead stands in the queue (handled_posts() leaves them
                       out); any of followup_submitting / _sent / _uncertain / _refused means that person never
                       gets another follow-up, and the first three count toward the daily DM cap.
data/replies.csv       one row per message batch from someone who wrote back (replies.py): the first reply after
                       our DM, then each new message after an answer we sent them. author_key, author, post_key,
                       seen_at (unique per person: the inbox keys replies on author_key:seen_at), snippet, text (everything they wrote after our DM, up to REPLY_TEXT_MAX
                       characters), chat_url. A file from before the text column reads with text = snippet and is
                       rewritten with the column the next time a reply is added.

From that history, as ledger.py does for Reddit:
    sent_today()       authors DMed (or possibly DMed) since local midnight, across every run and by hand,
                       follow-ups included
    commented_today()  posts commented on (or possibly) since local midnight, by comment.py or "Mark commented"
    skip_reason(a)     why author a must not be DMed now: already sent / commented / unavailable -> never;
                       last attempt (DM or comment) uncertain or interrupted -> held; max attempts failed -> given up
    lock()             one browser run (send.py, comment.py, replies.py, answer.py, followup.py, livecheck.py)
                       at a time: they share the Chrome profile

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
from datetime import date, datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
LEADS = DATA / "leads.json"
CONTACTED = DATA / "contacted.csv"
REPLIES = DATA / "replies.csv"
LOCK = DATA / "send.lock"
REPLY_FIELDS = ["author_key", "author", "post_key", "seen_at", "snippet", "text", "chat_url"]
REPLY_TEXT_MAX = 4000
FIELDS = ["author_key", "author", "post_key", "status", "timestamp", "note"]
QUEUE_STATUSES = ("approved", "unapproved", "sent", "commented", "skipped", "not_relevant")
SEND_STATUSES = ("submitting", "uncertain", "refused", "recipient", "not_submitted", "cleared",
                 "login", "rate_limited", "restricted")
COMMENT_STATUSES = ("comment_submitting", "comment_uncertain", "comment_not_submitted", "comment_closed",
                    "comment_stopped", "comment_cleared")
FOLLOWUP_STATUSES = ("followup_submitting", "followup_sent", "followup_uncertain", "followup_not_submitted",
                     "followup_refused", "followup_stopped")
STATUSES = QUEUE_STATUSES + SEND_STATUSES + COMMENT_STATUSES + FOLLOWUP_STATUSES
# Any of these means the person has had their one follow-up (or may have): never another.
FOLLOWUP_DONE = ("followup_submitting", "followup_sent", "followup_uncertain", "followup_refused")
# ...and these, from today, count toward outreach.daily_dm_limit (it may have gone out).
FOLLOWUP_COUNTED = ("followup_submitting", "followup_sent", "followup_uncertain")
FOLLOWUP_ATTEMPTS = ("followup_not_submitted",)
FINAL = {"sent": "already sent", "commented": "already commented on their post",
         "recipient": "Facebook says this person isn't available"}
HELD = ("uncertain", "submitting")       # the last row being one of these holds the author
COMMENT_HELD = ("comment_uncertain", "comment_submitting")     # ...and these hold the author and the post
COMMENT_ATTEMPTS = ("comment_not_submitted", "comment_uncertain")
# A post whose last row is one of these had a comment attempt that didn't go out: it can be tried again.
COMMENT_RETRY = ("comment_not_submitted", "comment_stopped", "comment_cleared")
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


def _dm_rows(rows: list[dict]) -> list[dict]:
    return [r for r in rows if r["status"] not in FOLLOWUP_STATUSES]


def messaged_authors(hist: dict | None = None) -> set[str]:
    """Authors never to be offered again: sent, commented, unavailable, or a send or comment that may have gone out."""
    hist = history() if hist is None else hist
    return {a for a, rows in hist.items()
            if any(r["status"] in FINAL for r in rows)
            or (_dm_rows(rows) and _dm_rows(rows)[-1]["status"] in HELD + COMMENT_HELD)}


def _post_rows() -> list[dict]:
    """contacted.csv without the follow-up rows: what decides where each post stands (queue, send.py, comment.py)."""
    return [r for r in _rows() if r["status"] not in FOLLOWUP_STATUSES]


def handled_posts() -> dict[str, str]:
    """{post_key: last status} for every post you've acted on. Follow-ups don't count: a lead that was sent stays
    'sent' after its follow-up."""
    return {r["post_key"]: r["status"] for r in _post_rows()}


def sent_today() -> int:
    """Authors DMed today, each once, across every run and by hand. Still-unresolved uncertain or interrupted
    sends from today count too, since they may have gone out; one resolved --not-sent doesn't. A follow-up sent
    today (or possibly sent: submitting, uncertain) counts as a DM to that author today."""
    today = date.today().isoformat()
    n = 0
    for rows in history().values():
        dms = [r for r in rows if r["status"] not in FOLLOWUP_STATUSES]
        if any(r["status"] == "sent" and r["timestamp"].startswith(today) for r in dms):
            n += 1
        elif dms and dms[-1]["status"] in HELD and dms[-1]["timestamp"].startswith(today):
            n += 1
        elif any(r["status"] in FOLLOWUP_COUNTED and r["timestamp"].startswith(today) for r in rows):
            n += 1
    return n


def commented_today() -> int:
    """Posts commented on today, each once: by comment.py or by "Mark commented" in the queue. A held comment from
    today counts too, since it may be on the post; one checked and cleared doesn't."""
    today, n = date.today().isoformat(), 0
    by_post = {}
    for r in _post_rows():
        by_post.setdefault(r["post_key"], []).append(r)
    for rows in by_post.values():
        if any(r["status"] == "commented" and r["timestamp"].startswith(today) for r in rows):
            n += 1
        elif rows[-1]["status"] in COMMENT_HELD and rows[-1]["timestamp"].startswith(today):
            n += 1
    return n


def held_comments() -> dict[str, dict]:
    """{post_key: last row} for every post whose last comment attempt has an unknown outcome."""
    return {p: r for p, r in {r["post_key"]: r for r in _post_rows()}.items() if r["status"] in COMMENT_HELD}


def resolve_comment(post_key: str, posted: bool, how: str = "by hand") -> None:
    """Settle a held comment: 'commented' (that author is never contacted again), or 'comment_cleared' (eligible
    again). how: who checked the post, for the note."""
    last = held_comments().get(post_key)
    if not last:
        raise SystemExit(f"{post_key} has no held (uncertain) comment to resolve.")
    if posted:
        record(last["author_key"], last["author"], post_key, "commented", f"confirmed {how} after an uncertain attempt")
    else:
        record(last["author_key"], last["author"], post_key, "comment_cleared",
               f"checked {how}: the uncertain comment is not on the post")
    print(f"[+] {last['author'] or post_key} recorded as {'commented' if posted else 'not commented (eligible again)'}")


# ----------------------------------------------------------------- replies
def _reply_rows() -> tuple[list[str], list[dict]]:
    """(the header data/replies.csv has, its rows). Rows from a file without the text column get text = snippet."""
    if not REPLIES.exists():
        return [], []
    with open(REPLIES, newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        header = list(reader.fieldnames or [])
    for r in rows:
        if not r.get("text"):
            r["text"] = r.get("snippet") or ""
    return header, rows


def replies() -> dict[str, dict]:
    """{author_key: their latest row in data/replies.csv}: everyone seen writing back after our DM."""
    return {r["author_key"]: r for r in _reply_rows()[1]}


def reply_rows(author_key: str | None = None) -> list[dict]:
    """Every row of data/replies.csv, oldest first (only author_key's, if given)."""
    return [r for r in _reply_rows()[1] if author_key is None or r["author_key"] == author_key]


def _upgrade_replies(header: list[str], rows: list[dict]) -> None:
    """Rewrite a replies.csv from before the text column with it (old rows get text = snippet)."""
    if not header or header == REPLY_FIELDS:
        return
    fd, tmp = tempfile.mkstemp(dir=DATA, suffix=".tmp")
    with os.fdopen(fd, "w", newline="") as f:
        w = csv.DictWriter(f, REPLY_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows({k: r.get(k) or "" for k in REPLY_FIELDS} for r in rows)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, REPLIES)


def _append_reply(row: dict) -> None:
    new = not REPLIES.exists()
    with open(REPLIES, "a", newline="") as f:
        w = csv.DictWriter(f, REPLY_FIELDS)
        if new:
            w.writeheader()
        w.writerow(row)
        f.flush()
        os.fsync(f.fileno())


def add_reply(author_key: str, author: str, post_key: str, text: str, chat_url: str) -> dict:
    """Append one more message from someone already in data/replies.csv (replies.py's re-check after we answered).
    seen_at is unique per person (bumped a second past their latest row if needed), since the inbox keys Facebook
    replies on author_key:seen_at. Returns the row written. Deduping is the caller's job."""
    header, rows = _reply_rows()
    DATA.mkdir(exist_ok=True)
    _upgrade_replies(header, rows)
    at = datetime.now().replace(microsecond=0)
    taken = [r["seen_at"] for r in rows if r["author_key"] == author_key and r.get("seen_at")]
    if taken:
        latest = max(datetime.fromisoformat(t) for t in taken)
        if at <= latest:
            at = latest + timedelta(seconds=1)
    row = {"author_key": author_key, "author": author, "post_key": post_key, "seen_at": at.isoformat(),
           "snippet": text[:160], "text": text[:REPLY_TEXT_MAX], "chat_url": chat_url}
    _append_reply(row)
    return row


def record_reply(author_key: str, author: str, post_key: str, snippet: str, chat_url: str, text: str = "") -> bool:
    """Add one person to data/replies.csv unless they're already in it. Returns True if a row was written.
    text: everything they wrote after our DM (kept up to REPLY_TEXT_MAX characters); snippet is the short version.
    A file from before the text column is rewritten with it first (old rows get text = snippet)."""
    header, rows = _reply_rows()
    if any(r["author_key"] == author_key for r in rows):
        return False
    DATA.mkdir(exist_ok=True)
    _upgrade_replies(header, rows)
    _append_reply({"author_key": author_key, "author": author, "post_key": post_key,
                   "seen_at": datetime.now().isoformat(timespec="seconds"), "snippet": snippet[:200],
                   "text": (text or snippet)[:REPLY_TEXT_MAX], "chat_url": chat_url})
    return True


# ----------------------------------------------------------------- follow-ups
def followups() -> dict[str, list[dict]]:
    """{author_key: their follow-up rows, oldest first}."""
    out = {}
    for r in _rows():
        if r["status"] in FOLLOWUP_STATUSES:
            out.setdefault(r["author_key"], []).append(r)
    return out


def held() -> dict[str, dict]:
    """{author_key: last row} for every author whose last send's outcome is unknown."""
    out = {}
    for a, rows in history().items():
        dms = _dm_rows(rows)
        if dms and dms[-1]["status"] in HELD and not any(r["status"] in FINAL for r in dms):
            out[a] = dms[-1]
    return out


def skip_reason(author_key: str, max_attempts: int = MAX_ATTEMPTS, hist: dict | None = None) -> str | None:
    mine = _dm_rows((history() if hist is None else hist).get(author_key, []))
    for r in mine:
        if r["status"] in FINAL:
            return f"{FINAL[r['status']]} ({r['timestamp']})"
    if mine and mine[-1]["status"] in HELD:
        what = "was interrupted after Send was pressed" if mine[-1]["status"] == "submitting" else "couldn't be confirmed"
        return (f"HELD: the last attempt ({mine[-1]['timestamp']}) {what} and may have gone out. The next send.py "
                f"run checks the chat (or by hand: python send.py resolve {author_key} --sent | --not-sent)")
    if mine and mine[-1]["status"] in COMMENT_HELD:
        return (f"HELD: a public comment on their post ({mine[-1]['timestamp']}) may have been posted. The next "
                f"comment.py run checks the post (or by hand: python comment.py resolve {mine[-1]['post_key']} "
                f"--commented | --not-commented)")
    tries = [r for r in mine if r["status"] in ATTEMPTS]
    if len(tries) >= max_attempts:
        refused = sum(1 for r in tries if r["status"] == "refused")
        return f"gave up after {len(tries)} attempts ({refused} refused by Facebook, cause not stated)"
    return None


def resolve(author_key: str, sent: bool, how: str = "by hand") -> None:
    """Settle a held send: 'sent', or 'cleared' (eligible again). how: who checked the chat, for the note."""
    last = held().get(author_key)
    if not last:
        raise SystemExit(f"{author_key} has no held (uncertain) send to resolve.")
    if sent:
        record(author_key, last["author"], last["post_key"], "sent", f"confirmed {how} after an uncertain attempt")
    else:
        record(author_key, last["author"], last["post_key"], "cleared",
               f"checked {how}: the uncertain attempt did not go out")
    print(f"[+] {last['author'] or author_key} recorded as {'sent' if sent else 'not sent (eligible again)'}")
