"""inbox/store.py - the shared record of answers to people who replied, across Reddit, Facebook and Instagram.

inbox/data/answers.json holds one entry per reply we answer (or decide not to):

    id             "<platform>:<reply_id>", stable, so the same reply is never drafted twice
    platform       "reddit" | "facebook" | "instagram"
    person         the platform's own key in its contacted.csv (Reddit username, Facebook author_key,
                   Instagram username)
    name           display name, for the page
    chat_url       where the conversation is (Messenger / Direct thread URL; Reddit message permalink)
    reply_id       the platform's id for their message (replies.csv: Reddit message_id, Instagram item_id,
                   Facebook author_key + seen_at)
    their_message  the full text of what they wrote
    our_message    the DM we sent them, for context
    draft          what Claude suggested
    text           what will be sent: the draft, or your edit of it
    status         drafted -> approved -> sending -> sent
                   (or dismissed, or failed (not sent; can be approved again), or uncertain (may have gone out))
    reason         why it failed / is uncertain, or a note
    created_at, approved_at, sent_at   ISO timestamps

The page (inbox/server.py) drafts and approves; each platform's `run.py answer` sends its approved entries,
writing 'sending' right before pressing Send (as a crash marker), then 'sent' / 'failed' / 'uncertain'.
Nothing is ever sent from an entry that isn't 'approved'. Every write takes a lock and replaces the file
atomically, so the page and a sending run can't clobber each other.
"""
from __future__ import annotations

import fcntl
import json
import os
import tempfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
ANSWERS = DATA / "answers.json"
LOCK = DATA / "answers.lock"
PLATFORMS = ("reddit", "facebook", "instagram")
STATUSES = ("drafted", "approved", "sending", "sent", "failed", "uncertain", "dismissed")
OPEN = ("drafted", "approved", "failed")             # still waiting for you or for a send
EDITABLE = ("drafted", "approved", "failed")         # the page may change text / status only in these


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def answer_id(platform: str, reply_id: str) -> str:
    return f"{platform}:{reply_id}"


@contextmanager
def _locked():
    DATA.mkdir(parents=True, exist_ok=True)
    with open(LOCK, "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _read() -> dict:
    if not ANSWERS.exists():
        return {}
    return {a["id"]: a for a in json.loads(ANSWERS.read_text(encoding="utf-8") or "[]")}


def _write(answers: dict):
    DATA.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=DATA, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(sorted(answers.values(), key=lambda a: a["created_at"]), f, indent=1, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, ANSWERS)


def load() -> dict:
    """{id: entry}, oldest first."""
    with _locked():
        return _read()


def get(aid: str) -> dict | None:
    return load().get(aid)


def add_draft(platform: str, reply_id: str, *, person: str, name: str, chat_url: str, their_message: str,
              our_message: str, draft: str) -> dict | None:
    """Record a new draft. Returns it, or None if this reply already has an entry (any status)."""
    if platform not in PLATFORMS:
        raise ValueError(f"unknown platform {platform!r}")
    aid = answer_id(platform, reply_id)
    with _locked():
        answers = _read()
        if aid in answers:
            return None
        answers[aid] = {"id": aid, "platform": platform, "person": person, "name": name, "chat_url": chat_url,
                        "reply_id": str(reply_id), "their_message": their_message, "our_message": our_message,
                        "draft": draft, "text": draft, "status": "drafted", "reason": "",
                        "created_at": now(), "approved_at": "", "sent_at": ""}
        _write(answers)
        return answers[aid]


def update(aid: str, *, expect: tuple = (), **fields) -> dict:
    """Change an entry. expect: statuses it must currently have (else ValueError, nothing written).
    status changes stamp approved_at / sent_at."""
    with _locked():
        answers = _read()
        if aid not in answers:
            raise KeyError(aid)
        a = answers[aid]
        if expect and a["status"] not in expect:
            raise ValueError(f"{aid} is {a['status']!r}, not {' / '.join(expect)}")
        if "status" in fields:
            if fields["status"] not in STATUSES:
                raise ValueError(f"unknown status {fields['status']!r}")
            if fields["status"] == "approved":
                fields.setdefault("approved_at", now())
            if fields["status"] == "sent":
                fields.setdefault("sent_at", now())
        a.update(fields)
        _write(answers)
        return a


def approved(platform: str) -> list[dict]:
    """Entries ready to send for one platform, oldest approval first."""
    return sorted((a for a in load().values() if a["platform"] == platform and a["status"] == "approved"),
                  key=lambda a: a["approved_at"])


def stuck(platform: str) -> list[dict]:
    """Entries left 'sending' (a run died after marking it): turn them 'uncertain' so nothing resends blind."""
    out = []
    for a in load().values():
        if a["platform"] == platform and a["status"] == "sending":
            out.append(update(a["id"], status="uncertain", reason="a run stopped after it started sending this"))
    return out


def answered(platform: str, person: str) -> bool:
    """True if we've sent (or may have sent) this person an answer: follow-ups must leave them alone."""
    return any(a["platform"] == platform and a["person"].lower() == person.lower()
               and a["status"] in ("sending", "sent", "uncertain") for a in load().values())


# ----------------------------------------------------------------- helpers for inbox/draft.py and the page
# Entries written by inbox/draft.py carry a few extra fields besides the ones above (the senders ignore them):
#     covers       every reply_id this entry answers (several messages from one person, grouped; the newest is
#                  reply_id), so none of them is drafted again
#     suggest      "dismiss" when they declined or asked not to be contacted: the page suggests dismissing it
#                  (status stays 'drafted' until you choose, so answered() doesn't count it)
#     intent       what Claude (or the decline check) read the message as: question / interested / declined / stop /
#                  other
#     note         Claude's one-line reading of the message (reason stays for the senders' failed / uncertain notes)
#     received_at  when their newest message came
#     kind, subject, post_key   platform details (Reddit: message / comment_reply ...; Facebook: the post we DMed about)
CORE = ("id", "platform", "person", "name", "chat_url", "reply_id", "their_message", "our_message", "draft", "text",
        "status", "reason", "created_at", "approved_at", "sent_at")


def add_entry(platform: str, reply_id: str, *, person: str, name: str, chat_url: str, their_message: str,
              our_message: str, draft: str, **extra) -> dict | None:
    """add_draft plus extra fields (covers, suggest, ...), written in one locked step. extra can't override the
    fields add_draft sets. Returns the entry, or None if this reply (or any reply in covers) already has one."""
    if platform not in PLATFORMS:
        raise ValueError(f"unknown platform {platform!r}")
    bad = set(extra) & set(CORE)
    if bad:
        raise ValueError(f"can't set {', '.join(sorted(bad))} through extra fields")
    aid = answer_id(platform, reply_id)
    covers = [str(c) for c in extra.get("covers") or [reply_id]]
    if str(reply_id) not in covers:
        covers.append(str(reply_id))
    extra["covers"] = covers
    with _locked():
        answers = _read()
        taken = _covered(answers, platform)
        if aid in answers or str(reply_id) in taken:
            return None
        answers[aid] = {"id": aid, "platform": platform, "person": person, "name": name, "chat_url": chat_url,
                        "reply_id": str(reply_id), "their_message": their_message, "our_message": our_message,
                        "draft": draft, "text": draft, "status": "drafted", "reason": "",
                        "created_at": now(), "approved_at": "", "sent_at": "", **extra}
        _write(answers)
        return answers[aid]


def _covered(answers: dict, platform: str) -> set[str]:
    out = set()
    for a in answers.values():
        if a["platform"] == platform:
            out.add(str(a["reply_id"]))
            out.update(str(c) for c in a.get("covers") or ())
    return out


def covered(platform: str) -> set[str]:
    """Every reply id (from that platform's replies.csv) that already has an entry, grouped ones included."""
    return _covered(load(), platform)


def of_person(platform: str, person: str) -> list[dict]:
    """This person's entries, oldest first."""
    return [a for a in load().values() if a["platform"] == platform and a["person"].lower() == person.lower()]
