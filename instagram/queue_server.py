"""queue_server.py - the review page: each commenter, their comment and the drafted DM, ready to send.

    python queue_server.py [--port 8766] [--no-open]

For each lead: "Open profile" to follow them, "Copy DM & open chat" copies the (edited) message and opens
an Instagram chat with them; paste, send, then "Mark sent". What you click goes to data/contacted.csv
straight away.

The server enforces outreach.daily_dm_limit (counted across every run), one DM per person ever (across
all posts), and outreach.minor_policy for commenters who seem to be under 18: "skip" (default) hides
them, "allow" treats them like anyone else. It only listens on 127.0.0.1.
"""
from __future__ import annotations

import argparse
import json
import threading
import webbrowser
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


def state() -> dict:
    cfg = _cfg()
    done, messaged = store.handled(), store.messaged()
    allow_minors, min_score = cfg["outreach"]["minor_policy"] == "allow", cfg["ai"]["min_score"]
    out = []
    for lead in store.load_leads().values():
        a = lead.get("ai")
        if not a or (a["likely_minor"] and not allow_minors):
            continue
        user = lead["username"]
        if lead["key"] in done:
            status = done[lead["key"]]
        elif user.lower() in messaged:
            status = "already_contacted"
        elif not a["relevant"] or a["score"] < min_score:
            status = "low"
        else:
            status = "todo"
        out.append({"key": lead["key"], "username": user, "full_name": lead.get("full_name", ""),
                    "is_private": lead.get("is_private", False), "text": lead.get("text", ""), "post": lead["post"],
                    "ai": a, "status": status, "dm": lead.get("edited_dm", a["dm"]),
                    "profile_url": f"https://www.instagram.com/{user}/", "chat_url": f"https://ig.me/m/{user}"})
    out.sort(key=lambda l: (l["status"] != "todo", -l["ai"]["score"]))
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
        if status == "sent":
            if lead["ai"]["likely_minor"] and cfg["outreach"]["minor_policy"] != "allow":
                return HTTPStatus.FORBIDDEN, {"error": "looks under 18; minor_policy doesn't allow a DM"}
            if lead["username"].lower() in store.messaged():
                return HTTPStatus.CONFLICT, {"error": "already DMed this person"}
            if store.sent_today() >= cfg["outreach"]["daily_dm_limit"]:
                return HTTPStatus.CONFLICT, {"error": "daily DM limit reached; continue tomorrow"}
        if body.get("message"):
            lead["edited_dm"] = body["message"]
            store.save_leads(leads)
        store.record(lead["username"], lead["key"], status, note=body.get("message", "") if status == "sent" else "")
    return HTTPStatus.OK, {"ok": True}


def save_draft(body: dict) -> tuple[HTTPStatus, dict]:
    with WRITE:
        leads = store.load_leads()
        lead = _lead(leads, body)
        if not lead or not isinstance(body.get("dm"), str):
            return HTTPStatus.NOT_FOUND, {"error": "no such lead"}
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
