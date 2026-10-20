"""queue_server.py - the review queue: a local page where you read each lead, edit the drafted message
and send it yourself.

    python queue_server.py [--port 8765] [--no-open]

Opens http://127.0.0.1:8765 in your normal browser (the one you're logged in to Facebook with). For each
lead, "Copy & open Messenger" copies the draft and opens a chat with the author; paste, read it once
more, press Enter, then click "Mark sent". What you click is written to data/contacted.csv at once, so
you can stop and pick up again at any time.

Rules the server enforces, not just the page:
    - outreach.daily_dm_limit DMs per day, counted from contacted.csv across every run
    - one DM per person ever: once an author is marked sent or commented, their other posts are hidden
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


def state() -> dict:
    cfg = _cfg()
    handled, messaged = store.handled_posts(), store.messaged_authors()
    policy, min_score = cfg["outreach"]["minor_policy"], cfg["ai"]["min_score"]
    out = []
    for lead in store.load_leads().values():
        a = lead.get("ai")
        if not a:
            continue
        if a["likely_minor"] and policy == "skip":
            continue
        akey = lead.get("author_key")
        if lead["post_key"] in handled:
            status = handled[lead["post_key"]]
        elif akey and akey in messaged:
            status = "already_contacted"
        elif not a["relevant"] or a["score"] < min_score:
            status = "low"
        else:
            status = "todo"
        out.append({
            **{k: lead.get(k) for k in ("post_key", "author", "author_key", "author_url", "post_url", "group",
                                        "posted_at", "time_label", "text", "sources")},
            "ai": a, "status": status, "dm_allowed": dm_allowed(lead, cfg),
            "dm": lead.get("edited", {}).get("dm", a["dm"]),
            "comment": lead.get("edited", {}).get("comment", a["comment"]),
            "messenger_url": store.messenger_url(akey) if akey else "",
        })
    out.sort(key=lambda l: (l["status"] != "todo", -l["ai"]["score"], l["posted_at"] or ""))
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
        if status == "sent":
            if not dm_allowed(lead, cfg):
                return HTTPStatus.FORBIDDEN, {"error": "author looks under 18; minor_policy doesn't allow a DM"}
            if akey in store.messaged_authors():
                return HTTPStatus.CONFLICT, {"error": "this person was already contacted"}
            if store.sent_today() >= cfg["outreach"]["daily_dm_limit"]:
                return HTTPStatus.CONFLICT, {"error": "daily DM limit reached; continue tomorrow"}
        text = body.get("message") or ""
        if text:
            field = "comment" if status == "commented" else "dm"
            lead.setdefault("edited", {})[field] = text
            store.save_leads(leads)
        store.record(akey, lead.get("author", ""), lead["post_key"], status, note=text[:300])
    return HTTPStatus.OK, {"ok": True, "sent_today": store.sent_today()}


def save_draft(body: dict) -> tuple[HTTPStatus, dict]:
    with WRITE:
        leads = store.load_leads()
        lead = leads.get(body.get("post_key", ""))
        if not lead:
            return HTTPStatus.NOT_FOUND, {"error": "no such lead"}
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
    print(f"[+] Review queue at {url}  ({todo} leads to review, {s['sent_today']}/{s['cap']} DMs sent today)")
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
