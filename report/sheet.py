"""sheet.py - copy everyone contacted and the per-day numbers into a Google Sheet.

    People   one row per person contacted (outcomes.everyone()), rewritten in full every sync
    Daily    one row per day per platform, upserted by (date, platform): rows already there for other days are kept

It signs in as a Google service account (google-auth, imported only when a real sync runs) and calls the
Sheets REST API v4 through google-auth's AuthorizedSession, which is a requests.Session. The tabs are created
if the spreadsheet doesn't have them yet.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path
from urllib.parse import quote

from digest import _outcomes, display_name

API = "https://sheets.googleapis.com/v4/spreadsheets"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
PEOPLE_HEADER = ["platform", "person", "name", "first DM", "follow-up", "replied at", "replies", "answered", "booked",
                 "score", "source", "last status"]
DAILY_HEADER = ["date", "platform", "DMs", "follow-ups", "comments", "replies", "booked",
                "contacted to date", "replied to date", "reply rate to date %"]


class SheetError(RuntimeError):
    pass


def people_rows() -> list[list]:
    rows = []
    for p in _outcomes().everyone():
        if not (p["first_sent_at"] or p.get("followup_at") or p.get("commented_at")):
            continue                                   # a held send that never went out isn't someone contacted
        rows.append([p["platform"], p["person"], display_name(p["platform"], p["person"], p.get("name", "")),
                     p["first_sent_at"], p["followup_at"], p["replied_at"], p["replies"], p["answered_at"],
                     p["booked_at"], "" if p.get("score") is None else p["score"], p.get("source") or "",
                     p["last_status"]])
    rows.sort(key=lambda r: (r[3] or r[4], r[0], r[1]))
    return rows


def daily_rows(today: str | None = None) -> list[list]:
    """A row for every (day, platform) with any activity, and one for today for each platform."""
    oc = _outcomes()
    today = today or date.today().isoformat()
    rows = []
    for platform in oc.PLATFORMS:
        ps = oc.people(platform)
        days = {today}
        for p in ps:
            for k in ("first_sent_at", "followup_at", "replied_at", "booked_at", "commented_at"):
                if (p.get(k) or "")[:10]:
                    days.add(p[k][:10])
        for day in sorted(d for d in days if d <= today):
            d = oc.on_day(ps, day)
            sent = [p for p in ps if p["first_sent_at"] and p["first_sent_at"][:10] <= day]
            replied = sum(1 for p in sent if p["replied_at"] and p["replied_at"][:10] <= day)
            rows.append([day, platform, d["dms"], d["followups"], d["comments"], d["replies"], d["booked"],
                         len(sent), replied, round(100 * replied / len(sent), 1) if sent else 0.0])
    return rows


def upsert(existing: list[list], new: list[list]) -> list[list]:
    """Daily rows: `new` replaces rows with the same (date, platform), the rest are kept, sorted by date, platform."""
    merged = {}
    for r in existing:
        if len(r) >= 2 and r[0] and r[0] != DAILY_HEADER[0]:
            merged[(str(r[0]), str(r[1]))] = list(r)
    for r in new:
        merged[(r[0], r[1])] = r
    return [merged[k] for k in sorted(merged)]


class Sheets:
    """The few Sheets API calls this needs, over any requests-like session (AuthorizedSession, or a fake)."""

    def __init__(self, http, spreadsheet_id: str):
        self.http, self.base = http, f"{API}/{spreadsheet_id}"

    def _call(self, method, url, **kw):
        r = self.http.request(method, url, timeout=60, **kw)
        if r.status_code >= 400:
            hint = " (is the sheet shared with the service account's email?)" if r.status_code in (403, 404) else ""
            raise SheetError(f"{method} {url.split('?')[0]} -> {r.status_code}{hint}: {r.text[:300]}")
        return r.json() if r.content else {}

    def tabs(self) -> list[str]:
        meta = self._call("GET", f"{self.base}?fields=sheets.properties.title")
        return [s["properties"]["title"] for s in meta.get("sheets", [])]

    def ensure_tabs(self, names):
        missing = [n for n in names if n not in self.tabs()]
        if missing:
            self._call("POST", f"{self.base}:batchUpdate",
                       json={"requests": [{"addSheet": {"properties": {"title": n}}} for n in missing]})

    def read(self, tab) -> list[list]:
        return self._call("GET", f"{self.base}/values/{quote(tab, safe='')}").get("values", [])

    def clear(self, tab):
        self._call("POST", f"{self.base}/values/{quote(tab, safe='')}:clear", json={})

    def write(self, tab, rows):
        self._call("PUT", f"{self.base}/values/{quote(tab + '!A1', safe='')}?valueInputOption=RAW",
                   json={"range": f"{tab}!A1", "majorDimension": "ROWS", "values": rows})


def session(credentials_file: Path):
    """An AuthorizedSession for the service account. google-auth is imported here, only when needed."""
    try:
        from google.oauth2 import service_account
        from google.auth.transport.requests import AuthorizedSession
    except ImportError:
        raise SheetError("google-auth isn't installed: venv/bin/pip install -r report/requirements.txt") from None
    if not credentials_file.exists():
        raise SheetError(f"no service-account key at {credentials_file} (see report/README.md)")
    creds = service_account.Credentials.from_service_account_file(str(credentials_file), scopes=SCOPES)
    return AuthorizedSession(creds)


def sync(sheets: Sheets, people: list[list], daily: list[list]) -> dict:
    """Overwrite People, upsert Daily. Returns the row counts written."""
    sheets.ensure_tabs(["People", "Daily"])
    sheets.clear("People")
    sheets.write("People", [PEOPLE_HEADER, *people])
    merged = upsert(sheets.read("Daily"), daily)
    sheets.clear("Daily")
    sheets.write("Daily", [DAILY_HEADER, *merged])
    return {"people": len(people), "daily": len(merged)}
