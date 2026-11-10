"""graph.py - Meta's official Instagram API (Instagram API with Instagram Login, graph.instagram.com) for API mode.

Only for a professional (Business or Creator) account: config.json "mode": "api". Browser mode (the default)
never imports this. Every call and field here follows what the two references in refs/ use (ig-comment-dm's
src/meta.ts and claude-instagram-bot's app.py / catchup.py):

    GET  /me?fields=user_id,username,id,account_type            who the token belongs to
    GET  /me/media?fields=id,caption,permalink,media_type,media_product_type,timestamp,comments_count
    GET  /{media-id}/comments?fields=id,text,timestamp,from&order=reverse_chronological
    GET  /me/conversations?platform=instagram&fields=id,updated_time,messages.limit(N){id,created_time,from,message}
    POST /{ig-user-id}/messages {"recipient": {"comment_id": ...}, "message": {"text": ...}}   a private reply
    POST /{ig-user-id}/messages {"recipient": {"id": <IGSID>}, "message": {"text": ...}}       a DM in the 24h window
    GET  graph.instagram.com/refresh_access_token?grant_type=ig_refresh_token&access_token=...

The token goes in an Authorization: Bearer header (never in a URL we print), except for the refresh call,
which only takes it as a query parameter; nothing here ever prints or logs a token.

A read is retried (429, 5xx, a network error) up to 3 times. A send gets exactly one attempt, as in meta.ts's
sendOnce: the messages endpoint takes no idempotency key, so a send that timed out, got a 5xx or lost its
connection may have gone out (Unanswered), and only a 4xx that Instagram wrote proves it didn't (GraphError).

The token: IG_ACCESS_TOKEN in instagram/.env (a long-lived token, about 60 days). Once it is older than
REFRESH_AFTER_DAYS (30), it is refreshed and the new one is kept in data/api_token.json with its dates (data/ is
gitignored; the file is readable only by you). A new token pasted into .env replaces the stored one. It warns
when the token has under WARN_DAYS (14) left.
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import store

HERE = Path(__file__).resolve().parent
HOST = "https://graph.instagram.com"
GRAPH_VERSION = "v23.0"
SCOPES = ("instagram_business_basic", "instagram_business_manage_comments", "instagram_business_manage_messages")
PROFESSIONAL = ("BUSINESS", "MEDIA_CREATOR", "CREATOR")
RATE_CODES = {4, 17, 32, 613, 80001, 80002, 80006}   # platform and business-use-case rate limits (meta.ts)
TOKEN_CODE = 190                                     # the token no longer works
ALREADY_SENT = 2534025                               # a private reply to this comment was already sent
WINDOW_CLOSED = 2534022                              # the comment's 7-day private-reply window closed (code 10)
PRIVACY = 1545041                                    # blocked by the person's privacy settings (code 200)
UNAVAILABLE = 551                                    # the person isn't available
REFRESH_AFTER_DAYS = 30
MIN_REFRESH_AGE = timedelta(days=1)                  # Meta refuses to refresh a token less than a day old
LIFETIME_DAYS = 60
WARN_DAYS = 14
READ_ATTEMPTS = 3
TIMEOUT = 15


class Unreachable(Exception):
    """No answer from Instagram (network error, timeout)."""


class GraphError(Exception):
    """Instagram answered and refused (a 4xx, or a read that kept failing). kind(): 'token', 'rate', 'transient',
    'already_sent', 'window_closed', 'unavailable' or 'final'."""

    def __init__(self, status: int, body: str):
        err = {}
        try:
            err = (json.loads(body) or {}).get("error") or {}
        except (ValueError, AttributeError):
            pass
        self.status, self.body = status, body[:500]
        self.code = err.get("code") if isinstance(err.get("code"), int) else None
        self.subcode = err.get("error_subcode") if isinstance(err.get("error_subcode"), int) else None
        self.transient = err.get("is_transient") is True
        self.message = str(err.get("message") or body[:200] or f"HTTP {status}")
        super().__init__(f"Instagram API error {status}"
                         + (f" (code {self.code}" + (f", subcode {self.subcode}" if self.subcode else "") + ")"
                            if self.code is not None else "") + f": {self.message}")

    def kind(self) -> str:
        # The order is meta.ts classifyRefusal's: a refusal that proves this comment can't get a reply wins.
        if self.code == TOKEN_CODE:
            return "token"
        if self.subcode == ALREADY_SENT:
            return "already_sent"
        if self.subcode == WINDOW_CLOSED:
            return "window_closed"
        if self.subcode == PRIVACY or self.code == UNAVAILABLE:
            return "unavailable"
        if self.status == 429 or self.code in RATE_CODES:
            return "rate"
        if self.code == 2 or self.transient:
            return "transient"
        return "final"


class Unanswered(Exception):
    """A send whose outcome isn't known: no answer, a 5xx, or a cut-off answer. It may have gone out."""


def http(method: str, url: str, *, params: dict | None = None, body: dict | None = None,
         headers: dict | None = None, timeout: float = TIMEOUT) -> tuple[int, str]:
    """One HTTP call: (status, body text). Raises Unreachable if there was no answer. Tests replace this."""
    full = url + ("?" + urlencode(params) if params else "")
    data = json.dumps(body).encode() if body is not None else None
    req = Request(full, data=data, method=method, headers={**(headers or {}),
                                                         **({"Content-Type": "application/json"} if data else {})})
    try:
        with urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except (URLError, TimeoutError, OSError) as exc:
        raise Unreachable(f"couldn't reach graph.instagram.com ({type(exc).__name__})") from None


class Graph:
    def __init__(self, token: str, version: str = GRAPH_VERSION, pause=None):
        self._token, self.version = token, version
        self.pause = pause or (lambda s: time.sleep(s))
        self.base = f"{HOST}/{version}"

    def _auth(self) -> dict:
        return {"Authorization": f"Bearer {self._token}"}

    def _redact(self, text: str) -> str:
        return text.replace(self._token, "***") if self._token else text

    def get(self, path_or_url: str, **params) -> dict:
        """A read, retried on 429 / 5xx / no answer. Raises GraphError (refused) or Unreachable."""
        url = path_or_url if path_or_url.startswith("https://") else f"{self.base}/{path_or_url.lstrip('/')}"
        last = None
        for attempt in range(READ_ATTEMPTS):
            if attempt:
                self.pause(0.5 * 2 ** (attempt - 1) * random.uniform(0.75, 1.25))
            try:
                status, text = http("GET", url, params=params or None, headers=self._auth())
            except Unreachable as exc:
                last = exc
                continue
            text = self._redact(text)
            if status == 429 or status >= 500:
                last = GraphError(status, text)
                continue
            if status >= 400:
                raise GraphError(status, text)
            try:
                return json.loads(text)
            except ValueError:
                raise GraphError(status, f"not JSON: {text[:200]}") from None
        raise last

    def pages(self, path: str, max_pages: int, **params):
        """Each page's data list, following paging.next, at most max_pages pages."""
        data = self.get(path, **params)
        for n in range(max_pages):
            yield data.get("data") or []
            nxt = (data.get("paging") or {}).get("next")
            if not nxt or n + 1 >= max_pages:
                return
            data = self.get(nxt)

    def post_once(self, path: str, body: dict) -> dict:
        """A send: one attempt. Raises GraphError if Instagram refused it (nothing went out), Unanswered if the
        outcome isn't known."""
        try:
            status, text = http("POST", f"{self.base}/{path.lstrip('/')}", body=body, headers=self._auth())
        except Unreachable as exc:
            raise Unanswered(str(exc)) from None
        text = self._redact(text)
        if status >= 500:
            raise Unanswered(f"Instagram answered with a server error ({status})")
        if status >= 400:
            raise GraphError(status, text)
        try:
            return json.loads(text) if text.strip() else {}
        except ValueError:
            return {}

    # ------------------------------------------------------------- the calls
    def me(self) -> dict:
        return self.get("me", fields="user_id,username,id,account_type")

    def media(self, max_pages: int = 4):
        return self.pages("me/media", max_pages, limit=25,
                          fields="id,caption,permalink,media_type,media_product_type,timestamp,comments_count")

    def comments(self, media_id: str, max_pages: int = 20):
        return self.pages(f"{media_id}/comments", max_pages, fields="id,text,timestamp,from",
                          order="reverse_chronological", limit=25)

    def conversations(self, messages: int = 20, max_pages: int = 10):
        return self.pages("me/conversations", max_pages, platform="instagram", limit=25,
                          fields=f"id,updated_time,messages.limit({int(messages)}){{id,created_time,from,message}}")

    def private_reply(self, ig_user_id: str, comment_id: str, text: str) -> dict:
        return self.post_once(f"{ig_user_id}/messages", {"recipient": {"comment_id": comment_id},
                                                         "message": {"text": text}})

    def send_dm(self, ig_user_id: str, igsid: str, text: str) -> dict:
        return self.post_once(f"{ig_user_id}/messages", {"recipient": {"id": igsid}, "message": {"text": text}})


# ----------------------------------------------------------------- the token
def token_file() -> Path:
    return store.DATA / "api_token.json"


def _fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()[:16]


def env_token() -> str:
    """IG_ACCESS_TOKEN from the environment or instagram/.env (read without printing it)."""
    if os.environ.get("IG_ACCESS_TOKEN"):
        return os.environ["IG_ACCESS_TOKEN"].strip()
    env = HERE / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            key, _, value = line.partition("=")
            if key.strip() == "IG_ACCESS_TOKEN":
                return value.strip().strip('"').strip("'")
    return ""


def _save(state: dict) -> None:
    store.DATA.mkdir(exist_ok=True)
    path = token_file()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1))
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _load() -> dict:
    try:
        return json.loads(token_file().read_text())
    except (OSError, ValueError):
        return {}


def current(now: datetime | None = None) -> dict:
    """{"token", "obtained_at", "expires_at", "source"}: the stored token, or .env's if it is new (a token first
    seen in .env is dated from then, expiring LIFETIME_DAYS later). SystemExit if there is none."""
    now = now or datetime.now()
    env, state = env_token(), _load()
    if env and state.get("source") != _fingerprint(env):
        state = {"token": env, "obtained_at": now.isoformat(timespec="seconds"),
                 "expires_at": (now + timedelta(days=LIFETIME_DAYS)).isoformat(timespec="seconds"),
                 "source": _fingerprint(env), "refreshed": False}
        _save(state)
    if not state.get("token"):
        raise SystemExit("API mode needs a long-lived Instagram token: put IG_ACCESS_TOKEN=... in instagram/.env "
                         "(see README, API mode)")
    return state


def refresh_if_due(state: dict, now: datetime | None = None, say=print) -> dict:
    """Refresh the token once it is REFRESH_AFTER_DAYS old (and at least a day old), keeping the new one and its
    dates in data/api_token.json. A failed refresh is only a warning while the token still works. Warns when it
    has under WARN_DAYS left; SystemExit once it has expired."""
    now = now or datetime.now()
    obtained, expires = datetime.fromisoformat(state["obtained_at"]), datetime.fromisoformat(state["expires_at"])
    if now - obtained >= timedelta(days=REFRESH_AFTER_DAYS) and now - obtained >= MIN_REFRESH_AGE:
        try:
            status, text = http("GET", f"{HOST}/refresh_access_token",
                                params={"grant_type": "ig_refresh_token", "access_token": state["token"]})
            data = json.loads(text) if status == 200 else {}
        except (Unreachable, ValueError):
            status, data = 0, {}
        if data.get("access_token") and isinstance(data.get("expires_in"), (int, float)):
            state = dict(state, token=data["access_token"], obtained_at=now.isoformat(timespec="seconds"),
                         expires_at=(now + timedelta(seconds=data["expires_in"])).isoformat(timespec="seconds"),
                         refreshed=True)
            _save(state)
            expires = datetime.fromisoformat(state["expires_at"])
            say(f"[+] refreshed the Instagram API token; it now expires {expires:%Y-%m-%d}")
        else:
            say(f"[!] couldn't refresh the Instagram API token (HTTP {status}); it still works until "
                f"{expires:%Y-%m-%d}")
    if now >= expires:
        raise SystemExit(f"The Instagram API token expired on {expires:%Y-%m-%d}. Get a new long-lived token and put "
                         f"it in instagram/.env as IG_ACCESS_TOKEN (README, API mode).")
    if expires - now < timedelta(days=WARN_DAYS):
        say(f"[!] the Instagram API token expires on {expires:%Y-%m-%d} ({(expires - now).days} days): get a new "
            f"long-lived token soon (README, API mode)")
    return state
