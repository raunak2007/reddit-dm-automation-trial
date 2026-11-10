"""outcomes.py - one view of what happened to every person contacted, on every platform.

    python outcomes.py              per-platform totals: contacted, followed up, replied, booked, reply rate
    python outcomes.py --today      what happened today (what daily.py's summary uses)
    python outcomes.py --json       every person as JSON

It only reads each platform's own files (and the shared inbox and bookings records), never a site:
    reddit     contacted.csv (root ledger), reddit/data/leads.json, replies.csv, followups.csv
    facebook   facebook/data/contacted.csv, leads.json, replies.csv
    instagram  instagram/data/contacted.csv, leads.json, replies.csv
    inbox      inbox/data/answers.json (answers drafted / sent)
    bookings   bookings/data/bookings.csv (bookings.py)
A missing file just means nothing has happened there yet. summary(), the insights report and the bookings
matcher all read people() rather than each platform's files, so a format change is fixed here once.
"""
from __future__ import annotations

import argparse
import csv
import json
from datetime import date, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLATFORMS = ("reddit", "facebook", "instagram")
FILES = {
    "reddit": {"contacted": HERE / "contacted.csv", "leads": HERE / "reddit" / "data" / "leads.json",
               "replies": HERE / "reddit" / "data" / "replies.csv",
               "followups": HERE / "reddit" / "data" / "followups.csv"},
    "facebook": {"contacted": HERE / "facebook" / "data" / "contacted.csv",
                 "leads": HERE / "facebook" / "data" / "leads.json",
                 "replies": HERE / "facebook" / "data" / "replies.csv"},
    "instagram": {"contacted": HERE / "instagram" / "data" / "contacted.csv",
                  "leads": HERE / "instagram" / "data" / "leads.json",
                  "replies": HERE / "instagram" / "data" / "replies.csv"},
}
ANSWERS = HERE / "inbox" / "data" / "answers.json"
BOOKINGS = HERE / "bookings" / "data" / "bookings.csv"
# How each platform names things in its files.
PERSON = {"reddit": "username", "facebook": "author_key", "instagram": "username"}
REPLY_TIME = {"reddit": "received_at", "facebook": "seen_at", "instagram": "replied_at"}
HELD = ("uncertain", "submitting")


def key(platform, person):
    person = (person or "").strip().lower()
    return person.removeprefix("u/") if platform == "reddit" else person.removeprefix("@")


def _csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default
    except ValueError:
        return default


def is_followup(status: str) -> bool:
    """A follow-up that went out. Platforms write it as followup_sent (or the older followed_up)."""
    return status in ("followup_sent", "followed_up")


def _leads_by_person(platform: str) -> dict:
    """{person key: newest lead} from leads.json, with its score, categories, minor flag, source and our DM."""
    out = {}
    leads = _json(FILES[platform]["leads"], {})
    for lead in (leads.values() if isinstance(leads, dict) else leads):
        who = lead.get("author" if platform == "reddit" else PERSON[platform]) or lead.get("author_key") or ""
        if not who:
            continue
        ai = lead.get("ai") or {}
        approved = lead.get("approved") or {}
        out[key(platform, who)] = {
            "name": lead.get("full_name") or lead.get("author") or who,
            "score": ai.get("score"), "categories": ai.get("categories") or [], "likely_minor": bool(ai.get("likely_minor")),
            "need": ai.get("need") or "",
            "source": lead.get("subreddit") or lead.get("group") or lead.get("post") or ", ".join(lead.get("sources") or []),
            "message": approved.get("dm") or ai.get("message") or ai.get("dm") or "",
            "not_relevant": lead.get("status") == "not_relevant" or lead.get("queue_status") == "not_relevant",
        }
    return out


def people(platform: str) -> list[dict]:
    """Everyone this platform has a send record for, one dict each:
    platform, person, name, first_sent_at, followup_at, replied_at, replies, last_status, held, score, categories,
    likely_minor, need, source, message, answered_at, booked_at."""
    f = FILES[platform]
    leads = _leads_by_person(platform)
    rows: dict[str, dict] = {}
    for r in _csv(f["contacted"]):
        who = key(platform, r.get(PERSON[platform]) or r.get("username") or "")
        if not who:
            continue
        p = rows.setdefault(who, {"platform": platform, "person": who, "name": r.get("author") or who,
                                  "first_sent_at": "", "followup_at": "", "replied_at": "", "replies": 0,
                                  "last_status": "", "held": False, "answered_at": "", "booked_at": "",
                                  "commented_at": ""})
        status, at = r.get("status", ""), r.get("timestamp", "")
        if status == "sent" and (not p["first_sent_at"] or at < p["first_sent_at"]):
            p["first_sent_at"] = at
        if is_followup(status) and not p["followup_at"]:
            p["followup_at"] = at
        if status == "commented" and not p["commented_at"]:
            p["commented_at"] = at
        p["last_status"] = status
    for r in _csv(f.get("followups", Path("/nonexistent"))):     # Reddit keeps follow-ups out of the ledger
        who = key(platform, r.get("username") or "")
        if who in rows and r.get("status") in ("sent", "followup_sent") and not rows[who]["followup_at"]:
            rows[who]["followup_at"] = r.get("timestamp") or r.get("sent_at") or ""
    for r in _csv(f["replies"]):
        who = key(platform, r.get(PERSON[platform]) or "")
        if who in rows:
            at = r.get(REPLY_TIME[platform], "")
            rows[who]["replies"] += 1
            if not rows[who]["replied_at"] or at < rows[who]["replied_at"]:
                rows[who]["replied_at"] = at
    for a in _json(ANSWERS, []):
        who = key(platform, a.get("person", ""))
        if a.get("platform") == platform and who in rows and a.get("status") == "sent":
            rows[who]["answered_at"] = rows[who]["answered_at"] or a.get("sent_at", "")
    for b in _csv(BOOKINGS):
        who = key(platform, b.get("person", ""))
        if b.get("platform") == platform and who in rows:
            rows[who]["booked_at"] = rows[who]["booked_at"] or b.get("booked_at", "")
    for who, p in rows.items():
        p["held"] = p["last_status"] in HELD and not p["first_sent_at"]
        p.update({k: v for k, v in leads.get(who, {}).items() if k != "name" or p["name"] == who})
        p.setdefault("score", None)
        p.setdefault("message", "")
    return list(rows.values())


def everyone() -> list[dict]:
    return [p for platform in PLATFORMS for p in people(platform)]


def totals(ps: list[dict]) -> dict:
    sent = [p for p in ps if p["first_sent_at"]]
    replied = [p for p in sent if p["replied_at"]]
    return {"contacted": len(sent), "followed_up": sum(1 for p in sent if p["followup_at"]),
            "replied": len(replied), "answered": sum(1 for p in sent if p["answered_at"]),
            "booked": sum(1 for p in ps if p["booked_at"]),
            "commented": sum(1 for p in ps if p.get("commented_at")),
            "held": sum(1 for p in ps if p["held"]),
            "reply_rate": round(100 * len(replied) / len(sent), 1) if sent else 0.0}


def on_day(ps: list[dict], day: str) -> dict:
    """What happened on `day` (YYYY-MM-DD): DMs, follow-ups, comments, new replies, bookings."""
    return {"dms": sum(1 for p in ps if p["first_sent_at"].startswith(day)),
            "followups": sum(1 for p in ps if p["followup_at"].startswith(day)),
            "comments": sum(1 for p in ps if (p.get("commented_at") or "").startswith(day)),
            "replies": sum(1 for p in ps if p["replied_at"].startswith(day)),
            "booked": sum(1 for p in ps if p["booked_at"].startswith(day)),
            "held": sum(1 for p in ps if p["held"])}


def answers_waiting() -> dict:
    """Inbox entries waiting for you (drafted / failed / uncertain) and approved ones waiting to be sent."""
    a = _json(ANSWERS, [])
    return {"to_review": sum(1 for x in a if x.get("status") in ("drafted", "failed")),
            "approved": sum(1 for x in a if x.get("status") == "approved"),
            "uncertain": sum(1 for x in a if x.get("status") == "uncertain")}


def summary(day: str | None = None) -> str:
    """A few lines for a notification or the log: today per platform, then what needs you."""
    day = day or date.today().isoformat()
    lines, total = [], {"dms": 0, "followups": 0, "comments": 0, "replies": 0, "booked": 0}
    for platform in PLATFORMS:
        ps = people(platform)
        d = on_day(ps, day)
        for k in total:
            total[k] += d[k]
        bits = [f"{d['dms']} DMs"] + [f"{d[k]} {k}" for k in ("followups", "comments", "replies", "booked") if d[k]]
        if d["held"]:
            bits.append(f"{d['held']} held")
        lines.append(f"{platform}: " + ", ".join(bits))
    w = answers_waiting()
    waiting = [f"{w['to_review']} answers to review" if w["to_review"] else "",
               f"{w['uncertain']} answers uncertain" if w["uncertain"] else ""]
    head = (f"{total['dms']} DMs, {total['followups']} follow-ups, {total['replies']} new replies"
            + (f", {total['booked']} booked" if total["booked"] else ""))
    return "\n".join([head, *lines, *(x for x in waiting if x)])


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--today", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    if args.json:
        print(json.dumps(everyone(), indent=1, ensure_ascii=False))
    elif args.today:
        print(summary())
    else:
        for platform in PLATFORMS:
            t = totals(people(platform))
            print(f"{platform:<10} contacted {t['contacted']:>4}  followed up {t['followed_up']:>4}  replied "
                  f"{t['replied']:>4} ({t['reply_rate']}%)  answered {t['answered']:>3}  booked {t['booked']:>3}  "
                  f"commented {t['commented']:>3}  held {t['held']}")
        print(summary().split("\n", 1)[0] + " today")


if __name__ == "__main__":
    main()
