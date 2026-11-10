"""server.py - the inbox page: read each reply, edit Claude's draft answer, and approve or dismiss it.

    python inbox/run.py serve [--port 8767] [--no-open]

Opens http://127.0.0.1:8767 (server.port in config.json). Tabs:
    To review    drafted and failed answers: edit the text (saved as you type), then Approve, Dismiss or Re-draft
    Approved     waiting for the platform's sender (and ones it is sending right now); Unapprove puts one back
    Sent         sent
    Dismissed / uncertain   dismissed (Restore puts one back), and sends that may have gone out: check the chat,
                            then "Mark sent" or "Mark not sent (back to approved)"
Nothing is sent from here. Approved answers go out on the next daily run, or with python <platform>/run.py answer.

The server enforces the rules, not just the page: text and status change only for store.EDITABLE entries (through
store.update(expect=...)), an uncertain entry can only be settled, a dismissed one only restored, and nothing
touches an entry that is sending or sent. It listens on 127.0.0.1 only and refuses cross-origin requests.
"""
from __future__ import annotations

import argparse
import json
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import draft
import store

HERE = Path(__file__).resolve().parent
PAGE = HERE / "page.html"
MAX_TEXT = 10000
NOTE = ("Nothing is sent from this page. Approved answers go out on the next daily run, or now with "
        "python reddit/run.py answer, python facebook/run.py answer or python instagram/run.py answer.")


def _port() -> int:
    try:
        return int(draft.load_config().get("server", {}).get("port", 8767))
    except (OSError, ValueError):
        return 8767


def state() -> dict:
    entries = sorted(store.load().values(), key=lambda a: a.get("received_at") or a["created_at"], reverse=True)
    counts = {s: sum(a["status"] == s for a in entries) for s in store.STATUSES}
    return {"entries": entries, "counts": counts, "note": NOTE, "editable": list(store.EDITABLE)}


def _id(body: dict) -> str:
    aid = body.get("id")
    if not isinstance(aid, str) or not aid:
        raise LookupError("missing id")
    return aid


def _text(body: dict) -> str:
    t = body.get("text")
    if not isinstance(t, str):
        raise TypeError("text must be a string")
    if len(t) > MAX_TEXT:
        raise TypeError(f"text is longer than {MAX_TEXT} characters")
    return t


def edit(body):
    return store.update(_id(body), expect=store.EDITABLE, text=_text(body))


def approve(body):
    aid = _id(body)
    text = _text(body) if "text" in body else (store.get(aid) or {}).get("text", "")
    if not text.strip():
        raise TypeError("the answer is empty; write one, or dismiss it")
    return store.update(aid, expect=store.EDITABLE, status="approved", text=text, reason="")


def unapprove(body):
    return store.update(_id(body), expect=("approved",), status="drafted", approved_at="")


def dismiss(body):
    return store.update(_id(body), expect=store.EDITABLE, status="dismissed", reason="dismissed on the page")


def restore(body):
    return store.update(_id(body), expect=("dismissed",), status="drafted", reason="")


def resolve(body):
    """An uncertain send, checked by hand: sent, or not sent (back to approved, so the sender tries again)."""
    sent = body.get("sent")
    if not isinstance(sent, bool):
        raise TypeError("sent must be true or false")
    if sent:
        return store.update(_id(body), expect=("uncertain",), status="sent", reason="confirmed sent on the page")
    return store.update(_id(body), expect=("uncertain",), status="approved", approved_at=store.now(),
                        reason="checked on the page: it did not go out")


def redraft(body):
    hint = body.get("hint") if isinstance(body.get("hint"), str) else ""
    return draft.redraft(_id(body), hint=hint)


ROUTES = {"/api/edit": edit, "/api/approve": approve, "/api/unapprove": unapprove, "/api/dismiss": dismiss,
          "/api/restore": restore, "/api/resolve": resolve, "/api/redraft": redraft}


def handle(path: str, body: dict) -> tuple[HTTPStatus, dict]:
    fn = ROUTES.get(path)
    if not fn:
        return HTTPStatus.NOT_FOUND, {"error": "not found"}
    try:
        return HTTPStatus.OK, {"ok": True, "entry": fn(body)}
    except (KeyError, LookupError):
        return HTTPStatus.NOT_FOUND, {"error": "no such answer"}
    except ValueError as exc:                      # wrong status (store.update's expect), or Claude came back empty
        return HTTPStatus.CONFLICT, {"error": str(exc)}
    except TypeError as exc:
        return HTTPStatus.BAD_REQUEST, {"error": str(exc)}
    except SystemExit as exc:
        return HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)}
    except Exception as exc:                       # noqa: BLE001 - e.g. the Claude API failing on Re-draft
        return HTTPStatus.BAD_GATEWAY, {"error": f"{type(exc).__name__}: {exc}"}


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, payload, ctype="application/json"):
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _local_host(self) -> bool:
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        return host in ("127.0.0.1", "localhost")       # refuses DNS-rebinding hosts

    def do_GET(self):
        if not self._local_host():
            return self._send(HTTPStatus.FORBIDDEN, {"error": "bad host"})
        if self.path in ("/", "/index.html"):
            self._send(HTTPStatus.OK, PAGE.read_bytes(), "text/html; charset=utf-8")
        elif self.path == "/api/state":
            self._send(HTTPStatus.OK, state())
        else:
            self._send(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self):
        if not self._local_host():
            return self._send(HTTPStatus.FORBIDDEN, {"error": "bad host"})
        if self.headers.get("Origin") not in (None, f"http://{self.headers.get('Host')}"):
            return self._send(HTTPStatus.FORBIDDEN, {"error": "cross-origin request"})
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        except ValueError:
            return self._send(HTTPStatus.BAD_REQUEST, {"error": "bad JSON"})
        if not isinstance(body, dict):
            return self._send(HTTPStatus.BAD_REQUEST, {"error": "expected a JSON object"})
        self._send(*handle(self.path, body))

    def log_message(self, fmt, *args):
        pass


def make_server(port: int) -> ThreadingHTTPServer:
    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def serve(port: int | None = None, open_browser: bool = True):
    port = port or _port()
    server = make_server(port)
    url = f"http://127.0.0.1:{port}/"
    c = state()["counts"]
    print(f"[+] Inbox at {url}  ({c['drafted'] + c['failed']} to review, {c['approved']} approved, "
          f"{c['uncertain']} uncertain)")
    print(f"    {NOTE}\n    Ctrl+C to stop. Everything you do is saved as you go.")
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
