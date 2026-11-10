"""bookings/run.py - record who booked a call, and which DM they came from.

    python bookings/run.py import <export.csv>    bookings exported from any booking tool or form (CSV)
    python bookings/run.py calendly [--days 30]   pull them from Calendly's API (CALENDLY_TOKEN in bookings/.env)
    python bookings/run.py list                   recorded bookings, matched or not
    python bookings/run.py link <booking_id> <platform> <person>    match one by hand
    python bookings/run.py tag <link> <platform> [person]           print a booking link tagged for a platform

Every booking lands in bookings/data/bookings.csv once (by booking_id): booking_id, booked_at, event_start,
name, email, platform, person, how (how it was matched), source. outcomes.py reads it, so booked counts show
in the daily summary and the insights report.

Matching, most certain first, and only when exactly one person fits (else it's kept unmatched for `link`):
  utm      the booking carries utm_source=<platform> (and utm_content=<person>, if the link was tagged per
           person): links from tag(), which each platform's config booking_link should use
  handle   a form answer names their Reddit / Instagram username or Facebook profile
  name     the booker's name equals the display name of exactly one person contacted on that platform
           (or on any platform, if the booking has no utm_source)
Reads only the export / the API; nothing is sent to anyone.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

import outcomes  # noqa: E402

DATA = HERE / "data"
BOOKINGS = DATA / "bookings.csv"
FIELDS = ["booking_id", "booked_at", "event_start", "name", "email", "platform", "person", "how", "source"]
CALENDLY = "https://api.calendly.com"
HANDLE = re.compile(r"(?:reddit\.com/u(?:ser)?/|u/)([A-Za-z0-9_-]{3,20})|instagram\.com/([A-Za-z0-9._]{1,30})|"
                    r"@([A-Za-z0-9._]{2,30})|facebook\.com/(?:profile\.php\?id=)?([A-Za-z0-9.]{5,})", re.I)


# ----------------------------------------------------------------- links
def tag(link: str, platform: str, person: str = "") -> str:
    """The booking link with utm_source=<platform>, utm_medium=dm (and utm_content=<person>), keeping any
    #fragment and existing query. A booking tool that records UTM parameters then says where it came from."""
    parts = urllib.parse.urlsplit(link)
    query = dict(urllib.parse.parse_qsl(parts.query))
    query.update(utm_source=platform, utm_medium="dm")
    if person:
        query["utm_content"] = outcomes.key(platform, person)
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))


# ----------------------------------------------------------------- storage
def load() -> dict:
    if not BOOKINGS.exists():
        return {}
    with open(BOOKINGS, newline="", encoding="utf-8") as f:
        return {r["booking_id"]: r for r in csv.DictReader(f)}


def save(rows: dict):
    DATA.mkdir(parents=True, exist_ok=True)
    tmp = BOOKINGS.with_suffix(".tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, FIELDS)
        w.writeheader()
        for r in sorted(rows.values(), key=lambda r: r["booked_at"]):
            w.writerow({k: r.get(k, "") for k in FIELDS})
    os.replace(tmp, BOOKINGS)


# ----------------------------------------------------------------- matching
def _norm(name: str) -> str:
    return re.sub(r"[^a-z ]", "", (name or "").lower()).strip()


def match(b: dict, contacted: list[dict]) -> tuple[str, str, str]:
    """(platform, person, how) for one booking, or ('', '', '') when no single person fits."""
    platform = (b.get("utm_source") or "").lower()
    pool = [p for p in contacted if p["first_sent_at"] and (not platform or p["platform"] == platform)]
    if platform in outcomes.PLATFORMS and b.get("utm_content"):
        who = outcomes.key(platform, b["utm_content"])
        if any(p["person"] == who for p in pool):
            return platform, who, "utm"
    handles = set()
    for m in HANDLE.finditer(" ".join(b.get("answers") or [])):
        handles.add(next(g for g in m.groups() if g).lower())
    hits = [p for p in pool if p["person"] in handles]
    if len({(p["platform"], p["person"]) for p in hits}) == 1:
        return hits[0]["platform"], hits[0]["person"], "handle"
    name = _norm(b.get("name", ""))
    if name and " " in name:                           # a first name alone matches too many people
        hits = [p for p in pool if _norm(p.get("name", "")) == name]
        if len(hits) == 1:
            return hits[0]["platform"], hits[0]["person"], "name"
    return "", "", ""


def record(found: list[dict], source: str) -> tuple[int, int]:
    """Add new bookings (matching each), keep existing ones (a hand `link` is never overwritten). Returns
    (new, matched among the new)."""
    rows, contacted = load(), outcomes.everyone()
    new = matched = 0
    for b in found:
        if b["booking_id"] in rows:
            continue
        platform, person, how = match(b, contacted)
        rows[b["booking_id"]] = {**{k: b.get(k, "") for k in FIELDS}, "platform": platform, "person": person,
                                 "how": how, "source": source}
        new += 1
        matched += bool(person)
    save(rows)
    return new, matched


# ----------------------------------------------------------------- sources
COLUMNS = {   # header words -> field, first match wins; lowercased header contains the word
    "name": ("invitee name", "full name", "name"),
    "email": ("email",),
    "event_start": ("start date", "start time", "event date", "start", "date"),
    "booked_at": ("created", "booked", "scheduled at", "submitted", "timestamp"),
    "utm_source": ("utm_source", "utm source"),
    "utm_content": ("utm_content", "utm content"),
    "booking_id": ("event id", "booking id", "invitee uuid", "id"),
}


def from_csv(path: Path) -> list[dict]:
    """Bookings from any CSV export: columns are recognised by their header words (COLUMNS); every other
    column's value is kept as a form answer, for handle matching. Rows without an id get one from their content."""
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        heads = reader.fieldnames or []
        pick = {}
        for field, words in COLUMNS.items():
            for word in words:
                col = next((h for h in heads if word in h.lower() and h not in pick.values()), None)
                if col:
                    pick[field] = col
                    break
        out = []
        for r in reader:
            b = {field: (r.get(col) or "").strip() for field, col in pick.items()}
            if not b.get("name") and not b.get("email"):
                continue
            b["answers"] = [v for h, v in r.items() if h not in pick.values() and v]
            b["booking_id"] = b.get("booking_id") or "csv-" + hashlib.sha1(
                json.dumps(r, sort_keys=True).encode()).hexdigest()[:12]
            b["booked_at"] = b.get("booked_at") or b.get("event_start") or ""
            out.append(b)
    return out


def _get(url: str, token: str) -> dict:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def from_calendly(token: str, days: int) -> list[dict]:
    """Invitees of events created in the last `days` days, with their UTM tracking and form answers."""
    me = _get(f"{CALENDLY}/users/me", token)["resource"]["uri"]
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    url = f"{CALENDLY}/scheduled_events?" + urllib.parse.urlencode({"user": me, "min_start_time": since, "count": 100})
    out = []
    while url:
        page = _get(url, token)
        for ev in page.get("collection", []):
            invitees = _get(f"{ev['uri']}/invitees?count=100", token).get("collection", [])
            for inv in invitees:
                t = inv.get("tracking") or {}
                out.append({"booking_id": inv["uri"].rsplit("/", 1)[-1], "name": inv.get("name", ""),
                            "email": inv.get("email", ""), "booked_at": inv.get("created_at", ""),
                            "event_start": ev.get("start_time", ""), "utm_source": t.get("utm_source") or "",
                            "utm_content": t.get("utm_content") or "",
                            "answers": [q.get("answer", "") for q in inv.get("questions_and_answers") or []]})
        url = (page.get("pagination") or {}).get("next_page")
    return out


def token() -> str:
    try:
        from dotenv import load_dotenv
        load_dotenv(HERE / ".env")
    except ImportError:
        pass
    t = os.environ.get("CALENDLY_TOKEN", "").strip()
    if not t:
        raise SystemExit("No CALENDLY_TOKEN: put a Calendly personal access token in bookings/.env "
                         "(Calendly > Integrations > API & Webhooks), or export bookings and use: import <file.csv>")
    return t


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("import").add_argument("csv", type=Path)
    sub.add_parser("calendly").add_argument("--days", type=int, default=30)
    sub.add_parser("list")
    ln = sub.add_parser("link")
    ln.add_argument("booking_id"), ln.add_argument("platform", choices=outcomes.PLATFORMS), ln.add_argument("person")
    tg = sub.add_parser("tag")
    tg.add_argument("link"), tg.add_argument("platform", choices=outcomes.PLATFORMS), tg.add_argument("person", nargs="?", default="")
    args = ap.parse_args()

    if args.cmd == "tag":
        print(tag(args.link, args.platform, args.person))
    elif args.cmd in ("import", "calendly"):
        found = from_csv(args.csv) if args.cmd == "import" else from_calendly(token(), args.days)
        new, matched = record(found, args.cmd if args.cmd == "calendly" else args.csv.name)
        unmatched = sum(1 for r in load().values() if not r["person"])
        print(f"[+] {len(found)} bookings read, {new} new, {matched} of them matched to someone contacted"
              + (f"; {unmatched} unmatched in total (python bookings/run.py list, then link)" if unmatched else ""))
    elif args.cmd == "list":
        for r in load().values():
            who = f"{r['platform']}:{r['person']} ({r['how']})" if r["person"] else "UNMATCHED"
            print(f"  {r['booked_at'][:16]:<16} {r['name'][:28]:<28} {who:<40} {r['booking_id']}")
    elif args.cmd == "link":
        rows = load()
        if args.booking_id not in rows:
            raise SystemExit(f"No booking {args.booking_id!r}: python bookings/run.py list")
        rows[args.booking_id].update(platform=args.platform, person=outcomes.key(args.platform, args.person), how="by hand")
        save(rows)
        print(f"[+] {args.booking_id} -> {args.platform}:{args.person}")


if __name__ == "__main__":
    main()
