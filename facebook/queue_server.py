"""queue_server.py - the review queue: a local page where you read each lead, edit the drafted message
and either approve it for send.py or send it yourself.

    python queue_server.py [--port 8765] [--no-open]

Opens http://127.0.0.1:8765 in your normal browser (the one you're logged in to Facebook with). For each
lead, either:
    "Approve for sending"     keeps the draft exactly as it is now; python send.py sends it (see send.py)
    "Copy & open Messenger"   copies the draft and opens a chat with the author; paste, read it once more,
                              press Enter, then click "Mark sent"
What you click is written to data/contacted.csv at once, so you can stop and pick up again at any time.

Rules the server enforces, not just the page:
    - outreach.daily_dm_limit DMs per day, counted from contacted.csv across every run
    - one DM per person ever: once an author is marked sent or commented, their other posts are hidden
    - an approved lead can only be unapproved (until send.py has sent it); a held one (a send that may
      have gone out) only changes once send.py checks its chat (or python send.py resolve)
    - outreach.minor_policy for authors the AI thinks are under 18:
          "comment_only" (default)  no DM; the page offers the public comment instead
          "skip"                    hidden altogether
          "allow"                   treated like everyone else
The server only listens on 127.0.0.1, so nothing outside this computer can reach it.
"""
from __future__ import annotations

import argparse
import json
import threading
import webbrowser
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import store

HERE = Path(__file__).resolve().parent
CONFIG = HERE / "config.json"
PAGE = HERE / "queue.html"
WRITE = threading.Lock()


def _cfg() -> dict:
    return json.loads(CONFIG.read_text())        # re-read so edits to the cap apply without a restart


def dm_allowed(lead: dict, cfg: dict) -> bool:
    return not lead["ai"]["likely_minor"] or cfg["outreach"]["minor_policy"] == "allow"


def status_of(lead: dict, cfg: dict, handled: dict, hist: dict) -> str:
    """Where a scored lead stands: todo / low / approved / held / gave_up / already_contacted, or the queue
    action or final send outcome last recorded for it (sent, commented, skipped, not_relevant, recipient)."""
    a, akey, last = lead["ai"], lead.get("author_key"), handled.get(lead["post_key"])
    max_attempts = cfg["outreach"].get("max_attempts_per_recipient", store.MAX_ATTEMPTS)
    if last in store.HELD:
        return "held"
    if last in store.WAITING:
        if not store.skip_reason(akey, max_attempts, hist):
            return "approved"
        return "already_contacted" if akey in store.messaged_authors(hist) else "gave_up"
    if last and last != "unapproved":
        return last
    if akey and akey in store.messaged_authors(hist):
        return "already_contacted"
    if not a["relevant"] or a["score"] < cfg["ai"]["min_score"]:
        return "low"
    return "todo"


def state() -> dict:
    cfg = _cfg()
    handled, hist = store.handled_posts(), store.history()
    policy, min_score = cfg["outreach"]["minor_policy"], cfg["ai"]["min_score"]
    out = []
    for lead in store.load_leads().values():
        a = lead.get("ai")
        if not a:
            continue
        if a["likely_minor"] and policy == "skip":
            continue
        akey = lead.get("author_key")
        status = status_of(lead, cfg, handled, hist)
        sending = status in ("approved", "held", "gave_up")
        out.append({
            **{k: lead.get(k) for k in ("post_key", "author", "author_key", "author_url", "post_url", "group",
                                        "posted_at", "time_label", "text", "sources")},
            "ai": a, "status": status, "dm_allowed": dm_allowed(lead, cfg),
            "dm": lead["approved"]["dm"] if sending and "approved" in lead else lead.get("edited", {}).get("dm", a["dm"]),
            "why": (store.skip_reason(akey, hist=hist) or "") if sending and akey else "",
            "comment": lead.get("edited", {}).get("comment", a["comment"]),
            "messenger_url": store.messenger_url(akey) if akey else "",
        })
    out.sort(key=lambda l: (l["status"] not in ("todo", "approved"), -l["ai"]["score"], l["posted_at"] or ""))
    return {"leads": out, "sent_today": store.sent_today(), "cap": cfg["outreach"]["daily_dm_limit"],
            "minor_policy": policy, "min_score": min_score}


def mark(body: dict) -> tuple[HTTPStatus, dict]:
    cfg = _cfg()
    status = body.get("status")
    if status not in store.STATUSES:
        return HTTPStatus.BAD_REQUEST, {"error": f"unknown status {status!r}"}
    with WRITE:
        leads = store.load_leads()
        lead = leads.get(body.get("post_key", ""))
        if not lead or "ai" not in lead:
            return HTTPStatus.NOT_FOUND, {"error": "no such lead"}
        akey = lead.get("author_key") or f"post:{lead['post_key']}"
        if status not in store.QUEUE_STATUSES:
            return HTTPStatus.BAD_REQUEST, {"error": f"{status!r} is recorded by send.py, not the queue"}
        now = status_of(lead, cfg, store.handled_posts(), store.history())
        if now == "held":
            return HTTPStatus.CONFLICT, {"error": "a send to them may have gone out; the next send run checks the chat (or run "
                                                  f"python send.py resolve {akey} --sent or --not-sent)"}
        if status == "unapproved" and now not in ("approved", "gave_up"):
            return HTTPStatus.CONFLICT, {"error": "only an approved lead can be unapproved"}
        if status != "unapproved" and now in ("approved", "gave_up"):
            return HTTPStatus.CONFLICT, {"error": "this lead is approved for send.py; unapprove it first"}
        text = body.get("message") or ""
        if status in ("sent", "approved"):
            if not dm_allowed(lead, cfg):
                return HTTPStatus.FORBIDDEN, {"error": "author looks under 18; minor_policy doesn't allow a DM"}
            if akey in store.messaged_authors():
                return HTTPStatus.CONFLICT, {"error": "this person was already contacted"}
        if status == "sent" and store.sent_today() >= cfg["outreach"]["daily_dm_limit"]:
            return HTTPStatus.CONFLICT, {"error": "daily DM limit reached; continue tomorrow"}
        if status == "approved":
            if not lead.get("author_key"):
                return HTTPStatus.BAD_REQUEST, {"error": "no profile link to message them at; send it from the post"}
            if not text.strip():
                return HTTPStatus.BAD_REQUEST, {"error": "the message is empty"}
            lead["approved"] = {"dm": text, "at": datetime.now().isoformat(timespec="seconds")}
        elif status == "unapproved":
            lead.pop("approved", None)
        if text:
            field = "comment" if status == "commented" else "dm"
            lead.setdefault("edited", {})[field] = text
        store.save_leads(leads)
        store.record(akey, lead.get("author", ""), lead["post_key"], status, note=text[:300])
    return HTTPStatus.OK, {"ok": True, "sent_today": store.sent_today()}


def auto_approve() -> int:
    """Approve every lead waiting in "To review" that may be DMed, with its draft as it stands, as if you'd clicked
    "Approve for sending" on each (outreach.auto_approve). Leads you skipped, unapproved or marked are left alone.
    Returns how many were approved."""
    cfg = _cfg()
    with WRITE:
        leads, handled, hist = store.load_leads(), store.handled_posts(), store.history()
        at, picked = datetime.now().isoformat(timespec="seconds"), []
        for lead in leads.values():
            if "ai" not in lead or handled.get(lead["post_key"]) == "unapproved":
                continue
            text = lead.get("edited", {}).get("dm", lead["ai"]["dm"])
            if (status_of(lead, cfg, handled, hist) != "todo" or not dm_allowed(lead, cfg)
                    or not lead.get("author_key") or not text.strip()):
                continue
            lead["approved"] = {"dm": text, "at": at, "auto": True}
            picked.append((lead, text))
        store.save_leads(leads)                   # before the rows, so every 'approved' row has its text
        for lead, text in picked:
            store.record(lead["author_key"], lead.get("author", ""), lead["post_key"], "approved",
                         note=f"auto-approved: {text}"[:300])
    return len(picked)


def save_draft(body: dict) -> tuple[HTTPStatus, dict]:
    with WRITE:
        leads = store.load_leads()
        lead = leads.get(body.get("post_key", ""))
        if not lead:
            return HTTPStatus.NOT_FOUND, {"error": "no such lead"}
        if "approved" in lead and isinstance(body.get("dm"), str):
            return HTTPStatus.CONFLICT, {"error": "this DM is approved for sending; unapprove it to edit it"}
        edited = lead.setdefault("edited", {})
        for field in ("dm", "comment"):
            if isinstance(body.get(field), str):
                edited[field] = body[field]
        store.save_leads(leads)
    return HTTPStatus.OK, {"ok": True}


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, payload, ctype="application/json"):
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send(HTTPStatus.OK, PAGE.read_bytes(), "text/html; charset=utf-8")
        elif self.path == "/api/state":
            self._send(HTTPStatus.OK, state())
        else:
            self._send(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self):
        if self.headers.get("Origin") not in (None, f"http://{self.headers.get('Host')}"):
            return self._send(HTTPStatus.FORBIDDEN, {"error": "cross-origin request"})
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        except ValueError:
            return self._send(HTTPStatus.BAD_REQUEST, {"error": "bad JSON"})
        routes = {"/api/mark": mark, "/api/draft": save_draft}
        if self.path not in routes:
            return self._send(HTTPStatus.NOT_FOUND, {"error": "not found"})
        self._send(*routes[self.path](body))

    def log_message(self, fmt, *args):            # keep the terminal for our own messages
        pass


def serve(port: int | None = None, open_browser=True):
    port = port or _cfg()["outreach"]["queue_port"]
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}/"
    s = state()
    todo = sum(l["status"] == "todo" for l in s["leads"])
    approved = sum(l["status"] == "approved" for l in s["leads"])
    print(f"[+] Review queue at {url}  ({todo} leads to review, {approved} approved for send.py, "
          f"{s['sent_today']}/{s['cap']} DMs sent today)")
    print("    Ctrl+C to stop. Everything you mark is already saved.")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[+] Stopped.")
    finally:
        server.server_close()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int)
    ap.add_argument("--no-open", action="store_true")
    args = ap.parse_args()
    serve(args.port, not args.no_open)


if __name__ == "__main__":
    main()
