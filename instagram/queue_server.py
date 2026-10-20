"""queue_server.py - the review page: each commenter, their comment and the drafted DM, ready to send.

    python queue_server.py [--port 8766] [--no-open]

For each lead, "Open profile" to follow them, then either:
    "Approve for sending"    keeps the DM exactly as it is now; python send.py sends it (see send.py)
    "Copy DM & open chat"    copies the (edited) message and opens a chat with them; paste, send, "Mark sent"
What you click goes to data/contacted.csv straight away.

The server enforces outreach.daily_dm_limit (counted across every run), one DM per person ever (across
all posts), and outreach.minor_policy for commenters who seem to be under 18: "skip" (default) hides
them, "allow" treats them like anyone else. An approved lead can only be unapproved (until send.py has sent
it); a held one (a send that may have gone out) only changes with python send.py resolve. It only listens
on 127.0.0.1.
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
    return json.loads(CONFIG.read_text())        # re-read so a changed cap applies without a restart


def status_of(lead: dict, cfg: dict, done: dict, hist: dict) -> str:
    """Where a scored lead stands: todo / low / approved / held / gave_up / already_contacted, or the review
    action or final send outcome last recorded for it (sent, skipped, not_relevant, recipient)."""
    a, user, last = lead["ai"], lead["username"].lower(), done.get(lead["key"])
    max_attempts = cfg["outreach"].get("max_attempts_per_recipient", store.MAX_ATTEMPTS)
    if last in store.HELD:
        return "held"
    if last in store.WAITING:
        if not store.skip_reason(user, max_attempts, hist):
            return "approved"
        return "already_contacted" if user in store.messaged(hist) else "gave_up"
    if last and last != "unapproved":
        return last
    if user in store.messaged(hist):
        return "already_contacted"
    if not a["relevant"] or a["score"] < cfg["ai"]["min_score"]:
        return "low"
    return "todo"


def state() -> dict:
    cfg = _cfg()
    done, hist = store.handled(), store.history()
    allow_minors = cfg["outreach"]["minor_policy"] == "allow"
    out = []
    for lead in store.load_leads().values():
        a = lead.get("ai")
        if not a or (a["likely_minor"] and not allow_minors):
            continue
        user = lead["username"]
        status = status_of(lead, cfg, done, hist)
        sending = status in ("approved", "held", "gave_up")
        out.append({"key": lead["key"], "username": user, "full_name": lead.get("full_name", ""),
                    "is_private": lead.get("is_private", False), "text": lead.get("text", ""), "post": lead["post"],
                    "ai": a, "status": status,
                    "dm": lead["approved"]["dm"] if sending and "approved" in lead else lead.get("edited_dm", a["dm"]),
                    "why": (store.skip_reason(user, hist=hist) or "") if sending else "",
                    "profile_url": f"https://www.instagram.com/{user}/", "chat_url": f"https://ig.me/m/{user}"})
    out.sort(key=lambda l: (l["status"] not in ("todo", "approved"), -l["ai"]["score"]))
    return {"leads": out, "sent_today": store.sent_today(), "cap": cfg["outreach"]["daily_dm_limit"]}


def _lead(leads: dict, body: dict) -> dict | None:
    lead = leads.get(body.get("key", ""))
    return lead if lead and "ai" in lead else None


def mark(body: dict) -> tuple[HTTPStatus, dict]:
    cfg = _cfg()
    status = body.get("status")
    if status not in store.STATUSES:
        return HTTPStatus.BAD_REQUEST, {"error": f"unknown status {status!r}"}
    with WRITE:
        leads = store.load_leads()
        lead = _lead(leads, body)
        if not lead:
            return HTTPStatus.NOT_FOUND, {"error": "no such lead"}
        if status not in store.QUEUE_STATUSES:
            return HTTPStatus.BAD_REQUEST, {"error": f"{status!r} is recorded by send.py, not the review page"}
        user = lead["username"].lower()
        now = status_of(lead, cfg, store.handled(), store.history())
        if now == "held":
            return HTTPStatus.CONFLICT, {"error": "a send to them may have gone out; check the chat, then run "
                                                  f"python send.py resolve {user} --sent or --not-sent"}
        if status == "unapproved" and now not in ("approved", "gave_up"):
            return HTTPStatus.CONFLICT, {"error": "only an approved lead can be unapproved"}
        if status != "unapproved" and now in ("approved", "gave_up"):
            return HTTPStatus.CONFLICT, {"error": "this lead is approved for send.py; unapprove it first"}
        text = body.get("message") or ""
        if status in ("sent", "approved"):
            if lead["ai"]["likely_minor"] and cfg["outreach"]["minor_policy"] != "allow":
                return HTTPStatus.FORBIDDEN, {"error": "looks under 18; minor_policy doesn't allow a DM"}
            if user in store.messaged():
                return HTTPStatus.CONFLICT, {"error": "already DMed this person"}
        if status == "sent" and store.sent_today() >= cfg["outreach"]["daily_dm_limit"]:
            return HTTPStatus.CONFLICT, {"error": "daily DM limit reached; continue tomorrow"}
        if status == "approved":
            if not text.strip():
                return HTTPStatus.BAD_REQUEST, {"error": "the message is empty"}
            lead["approved"] = {"dm": text, "at": datetime.now().isoformat(timespec="seconds")}
        elif status == "unapproved":
            lead.pop("approved", None)
        if text:
            lead["edited_dm"] = text
        store.save_leads(leads)
        store.record(lead["username"], lead["key"], status, note=text if status in ("sent", "approved") else "")
    return HTTPStatus.OK, {"ok": True}


def save_draft(body: dict) -> tuple[HTTPStatus, dict]:
    with WRITE:
        leads = store.load_leads()
        lead = _lead(leads, body)
        if not lead or not isinstance(body.get("dm"), str):
            return HTTPStatus.NOT_FOUND, {"error": "no such lead"}
        if "approved" in lead:
            return HTTPStatus.CONFLICT, {"error": "this DM is approved for sending; unapprove it to edit it"}
        lead["edited_dm"] = body["dm"]
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

    def log_message(self, fmt, *args):
        pass


def serve(port: int | None = None, open_browser=True):
    port = port or _cfg()["outreach"]["queue_port"]
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}/"
    s = state()
    print(f"[+] Review page at {url}  ({sum(l['status'] == 'todo' for l in s['leads'])} to review, "
          f"{sum(l['status'] == 'approved' for l in s['leads'])} approved for send.py, "
          f"{s['sent_today']}/{s['cap']} DMs sent today). Ctrl+C to stop.")
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
