"""digest.py - the daily email: what happened today, what needs you, and on Mondays the weekly headline numbers.

Everything comes from files already on disk, never a site:
    outcomes.py              per-platform today / totals, answers waiting, everyone contacted
    <platform> replies.csv   today's new replies (a short snippet and the link to the chat)
    logs/paused.json         platforms paused by a failed live check
    logs/<date>.log          today's last daily.py run: jobs that stopped on an account problem, failed or were skipped
    insights/reports/*.md    the newest weekly report (Mondays only)

Personal data is kept small: a first name or username per person, snippets cut to 120 characters with email
addresses and phone numbers blanked out. No full names, no message bodies.
"""
from __future__ import annotations

import html
import json
import re
import smtplib
import ssl
from datetime import date
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
LOGS = ROOT / "logs"
REPORTS = ROOT / "insights" / "reports"
SNIPPET_MAX = 120
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+")
PHONE_RE = re.compile(r"\+?\d[\d\s().-]{6,}\d")
LINK_FIELD = {"reddit": "permalink", "facebook": "chat_url", "instagram": "thread_url"}


def _outcomes():
    import sys
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    import outcomes
    return outcomes


def scrub(text: str, limit: int = SNIPPET_MAX) -> str:
    """One line, emails and phone numbers blanked, at most `limit` characters."""
    text = EMAIL_RE.sub("[email]", " ".join((text or "").split()))
    text = PHONE_RE.sub(lambda m: "[phone]" if sum(c.isdigit() for c in m.group()) >= 9 else m.group(), text)
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def display_name(platform: str, person: str, name: str = "") -> str:
    """A first name (Facebook, whose person key is a numeric id) or the username (Reddit, Instagram)."""
    if platform == "facebook":
        first = (name or "").split()
        return first[0] if first and first[0] != person else "(Facebook user)"
    return f"u/{person}" if platform == "reddit" else f"@{person}"


def _link(url: str) -> str:
    return url if (url or "").startswith("https://") else ""


def new_replies(day: str, limit: int = 20) -> list[dict]:
    """Replies received on `day`, oldest first: platform, who, at, snippet, link."""
    oc = _outcomes()
    out = []
    for platform in oc.PLATFORMS:
        names = {p["person"]: p.get("name", "") for p in oc.people(platform)}
        for r in oc._csv(oc.FILES[platform]["replies"]):
            at = r.get(oc.REPLY_TIME[platform], "") or ""
            if not at.startswith(day):
                continue
            person = oc.key(platform, r.get(oc.PERSON[platform]) or "")
            name = names.get(person) or r.get("author") or ""
            out.append({"platform": platform, "who": display_name(platform, person, name), "at": at,
                        "snippet": scrub(r.get("snippet") or r.get("text") or ""),
                        "link": _link(r.get(LINK_FIELD[platform], ""))})
    out.sort(key=lambda r: r["at"])
    return out[:limit]


def paused() -> dict:
    try:
        return json.loads((LOGS / "paused.json").read_text())
    except (OSError, ValueError):
        return {}


def run_problems(day: str) -> dict:
    """From the last daily.py run in logs/<day>.log: jobs that stopped (exit 2), failed (other exit / timeout)
    and were skipped. Each a list of short strings."""
    path = LOGS / f"{day}.log"
    out = {"stopped": [], "failed": [], "skipped": []}
    if not path.exists():
        return out
    text = path.read_text(errors="replace")
    run = text.rsplit("########## daily.py", 1)[-1]
    job = ""
    for line in run.splitlines():
        if not line.startswith("===== "):
            continue
        body = line[6:]
        if body.startswith("skipped "):
            out["skipped"].append(body[8:].strip())
        elif body.startswith("exit "):
            code = body.split()[1]
            if code == "2":
                out["stopped"].append(job)
            elif code != "0":
                out["failed"].append(f"{job} ({'timed out' if code == 'None' else f'exit {code}'})")
        elif re.match(r"\d\d:\d\d:\d\d ", body):
            job = re.sub(r" \(in [^)]*\)$", "", body[9:]).strip()
    return out


def weekly() -> dict | None:
    """The newest insights report's headline line and per-platform rows, or None if there isn't one."""
    reports = sorted(REPORTS.glob("*.md")) if REPORTS.exists() else []
    if not reports:
        return None
    lines = reports[-1].read_text(errors="replace").splitlines()
    headline = next((l.strip() for l in lines if l.strip() and not l.startswith("#")), "")
    platforms, section = [], ""
    for l in lines:
        if l.startswith("#"):
            section = l.lstrip("#").strip().lower()
        elif section == "by platform" and l.startswith("|") and not set(l) <= set("|-: "):
            cells = [c.strip() for c in l.strip("|").split("|")]
            if cells and cells[0] != "group" and len(cells) >= 5:
                platforms.append({"platform": cells[0], "contacted": cells[1], "replied": cells[2],
                                  "reply_rate": cells[3], "booked": cells[4]})
    return {"file": f"insights/reports/{reports[-1].name}", "headline": headline, "platforms": platforms}


def gather(day: str | None = None, include_weekly: bool | None = None, max_replies: int = 20) -> dict:
    """Everything the email shows, as plain data."""
    oc = _outcomes()
    day = day or date.today().isoformat()
    if include_weekly is None:
        include_weekly = date.fromisoformat(day).weekday() == 0
    platforms = []
    for platform in oc.PLATFORMS:
        ps = oc.people(platform)
        platforms.append({"platform": platform, "today": oc.on_day(ps, day), "totals": oc.totals(ps)})
    return {"day": day, "summary": oc.summary(day), "platforms": platforms,
            "replies": new_replies(day, max_replies), "waiting": oc.answers_waiting(),
            "paused": paused(), "problems": run_problems(day),
            "weekly": weekly() if include_weekly else None}


def _attention(d: dict) -> list[str]:
    lines = [f"{name} is PAUSED since {p.get('since', '?')}: {p.get('why', '')}. "
             f"Fix it, or: python daily.py --resume {name}" for name, p in d["paused"].items()]
    lines += [f"stopped on an account problem (logged out, blocked, checkpoint): {j}" for j in d["problems"]["stopped"]]
    lines += [f"failed: {j}" for j in d["problems"]["failed"]]
    lines += [f"skipped: {j}" for j in d["problems"]["skipped"]]
    return lines


def _waiting(d: dict) -> list[str]:
    w = d["waiting"]
    return [s for s in (f"{w['to_review']} answers to review" if w.get("to_review") else "",
                        f"{w['uncertain']} answers uncertain (check the chat)" if w.get("uncertain") else "",
                        f"{w['approved']} approved answers waiting to be sent" if w.get("approved") else "") if s]


def subject(d: dict) -> str:
    head = d["summary"].split("\n", 1)[0]
    flag = " [needs you]" if d["paused"] or d["problems"]["stopped"] else ""
    return f"Outreach {d['day']}: {head}{flag}"


def render_text(d: dict) -> str:
    out = [subject(d), "", d["summary"], "", "Per platform (today | all time):"]
    for p in d["platforms"]:
        t, a = p["today"], p["totals"]
        out.append(f"  {p['platform']:<10} today {t['dms']} DMs, {t['followups']} follow-ups, {t['comments']} comments, "
                   f"{t['replies']} replies, {t['booked']} booked | total {a['contacted']} contacted, {a['replied']} replied "
                   f"({a['reply_rate']}%), {a['answered']} answered, {a['booked']} booked")
    out += ["", f"New replies today ({len(d['replies'])}):"]
    out += [f"  {r['platform']} {r['who']} {r['at'][11:16]}: \"{r['snippet']}\"" + (f"\n    {r['link']}" if r["link"] else "")
            for r in d["replies"]] or ["  (none)"]
    out += ["", "Answers waiting for you:"] + ([f"  {s}" for s in _waiting(d)] or ["  (none)"])
    if _waiting(d):
        out.append("  Review them on the Mac: python inbox/run.py")
    out += ["", "Paused / stopped:"] + ([f"  {s}" for s in _attention(d)] or ["  (nothing)"])
    if d["weekly"]:
        w = d["weekly"]
        out += ["", f"This week ({w['file']}):", f"  {w['headline']}"]
        out += [f"  {p['platform']}: {p['contacted']} contacted, {p['replied']} replied ({p['reply_rate']}), "
                f"{p['booked']} booked" for p in w["platforms"]]
    return "\n".join(out) + "\n"


def render_html(d: dict) -> str:
    e = html.escape
    td = 'style="padding:4px 8px;border-bottom:1px solid #ddd;text-align:right"'
    th = 'style="padding:4px 8px;border-bottom:2px solid #999;text-align:right"'
    parts = ['<div style="font-family:-apple-system,Helvetica,Arial,sans-serif;font-size:14px;color:#222;max-width:680px">',
             f"<h2 style=\"margin:0 0 4px\">Outreach {e(d['day'])}</h2>",
             "<p style=\"margin:0 0 12px\">" + "<br>".join(e(l) for l in d["summary"].splitlines()) + "</p>"]
    attention = _attention(d)
    if attention:
        parts.append('<div style="background:#fdecea;border-left:4px solid #c0392b;padding:8px 12px;margin:0 0 12px">'
                     "<b>Needs you</b><br>" + "<br>".join(e(a) for a in attention) + "</div>")
    cols = ("DMs", "follow-ups", "comments", "replies", "booked", "contacted", "replied", "rate", "answered", "booked")
    parts.append('<table style="border-collapse:collapse;margin:0 0 12px"><tr><th style="text-align:left;padding:4px 8px">'
                 '</th><th colspan="5" ' + th + ">today</th><th colspan=\"5\" " + th + ">all time</th></tr><tr><th></th>"
                 + "".join(f"<th {th}>{c}</th>" for c in cols) + "</tr>")
    for p in d["platforms"]:
        t, a = p["today"], p["totals"]
        vals = (t["dms"], t["followups"], t["comments"], t["replies"], t["booked"],
                a["contacted"], a["replied"], f"{a['reply_rate']}%", a["answered"], a["booked"])
        parts.append(f'<tr><td style="padding:4px 8px;border-bottom:1px solid #ddd">{e(p["platform"])}</td>'
                     + "".join(f"<td {td}>{e(str(v))}</td>" for v in vals) + "</tr>")
    parts.append("</table>")
    parts.append(f"<h3 style=\"margin:12px 0 4px\">New replies today ({len(d['replies'])})</h3>")
    if d["replies"]:
        parts.append("<ul style=\"margin:0;padding-left:18px\">")
        for r in d["replies"]:
            who = e(r["who"]) if not r["link"] else f'<a href="{e(r["link"])}">{e(r["who"])}</a>'
            parts.append(f"<li>{e(r['platform'])} {who} {e(r['at'][11:16])}: <i>{e(r['snippet'])}</i></li>")
        parts.append("</ul>")
    else:
        parts.append("<p style=\"margin:0\">(none)</p>")
    waiting = _waiting(d)
    parts.append("<h3 style=\"margin:12px 0 4px\">Answers waiting for you</h3><p style=\"margin:0\">"
                 + ("<br>".join(e(s) for s in waiting) + "<br><span style=\"color:#666\">Review them on the Mac: "
                    "python inbox/run.py</span>" if waiting else "(none)") + "</p>")
    if not attention:
        parts.append("<h3 style=\"margin:12px 0 4px\">Paused / stopped</h3><p style=\"margin:0\">(nothing)</p>")
    if d["weekly"]:
        w = d["weekly"]
        parts.append(f"<h3 style=\"margin:12px 0 4px\">This week</h3><p style=\"margin:0\">{e(w['headline'])}</p>")
        if w["platforms"]:
            parts.append("<ul style=\"margin:4px 0 0;padding-left:18px\">" + "".join(
                f"<li>{e(p['platform'])}: {e(p['contacted'])} contacted, {e(p['replied'])} replied "
                f"({e(p['reply_rate'])}), {e(p['booked'])} booked</li>" for p in w["platforms"]) + "</ul>")
        parts.append(f"<p style=\"color:#666;margin:4px 0 0\">Full report: {e(w['file'])}</p>")
    parts.append("</div>")
    return "\n".join(parts)


def message(d: dict, cfg: dict) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = subject(d)
    msg["From"] = cfg["from"]
    msg["To"] = ", ".join(cfg["to"])
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=(cfg["from"].rsplit("@", 1)[-1] or None))
    msg.set_content(render_text(d))
    msg.add_alternative(render_html(d), subtype="html")
    return msg


def send(msg: EmailMessage, cfg: dict, password: str):
    """Send over SMTP with STARTTLS (Gmail: smtp.gmail.com:587 with an app password)."""
    with smtplib.SMTP(cfg.get("smtp_host", "smtp.gmail.com"), int(cfg.get("smtp_port", 587)), timeout=60) as s:
        s.ehlo()
        s.starttls(context=ssl.create_default_context())
        s.ehlo()
        s.login(cfg.get("username") or cfg["from"], password)
        s.send_message(msg)
