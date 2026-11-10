"""replies.py - who wrote back after our DM.

    python run.py replies        (or python replies.py)

Reads the Direct inbox inside the logged-in Chrome profile through the web API the Instagram page itself uses
(direct_v2/inbox/, page by page, with comments.api), then, for anyone whose chat wasn't there, Message Requests
(direct_v2/pending_inbox/, the same way). Those are plain reads: no chat is opened, no request is accepted, so
nothing is marked seen, and nothing is liked or sent. For everyone data/contacted.csv records as 'sent', it finds
their one-to-one chat and keeps each message from them dated after our DM went out: who, when, a short snippet,
the whole message (up to 4000 characters) and the chat's link. Each one is added to data/replies.csv once (by
Instagram's item id), so running it again only adds what's new; rows from Message Requests have how=request.
contacted.csv isn't changed.

Paging stops once every DMed person's chat has been seen, once the chats are older than the earliest DM, or after
replies.max_pages pages (20 chats each, the latest replies.messages_per_chat messages of each), in each of the two.
A creator account's General tab may not be in what the API returns.

If the inbox endpoint doesn't answer, it opens the inbox page instead and reads the chat list (no chat is opened
there either): a chat whose name is the person's display name or username and whose preview isn't "You: ..." is
counted as a reply, with the preview as the snippet and the time worked out from "2h" / "3d". That is rougher: one
reply per chat (the latest), and only the chats the list shows.

Stops at once (exit code 2, browser.Halt) if Instagram logs the profile out, asks for a security check or
rate-limits it.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import browser
import comments
import direct
import store
from send_dm import AccountBlocked

HERE = Path(__file__).resolve().parent
CONFIG = HERE / "config.json"
DEFAULTS = {"max_pages": 10, "messages_per_chat": 20}
SNIPPET = 120
TEXT_MAX = 4000              # the whole message, capped, for data/replies.csv's text column
LABELS = {"like": "like", "media": "photo or video", "raven_media": "disappearing photo or video",
          "voice_media": "voice message", "clip": "reel", "media_share": "shared post", "story_share": "shared story",
          "reel_share": "story reply", "animated_media": "GIF", "link": "link", "action_log": "reaction",
          "xma_media_share": "shared post", "xma_clip": "reel", "generic_xma": "shared item"}

# Fallback: each chat row in the inbox list, as lines of text, and its link if it has one.
_ROWS = r"""
const seen = new Set(), out = [];
for (const el of document.querySelectorAll('a[href*="/direct/t/"], [role="listitem"], [role="button"]')) {
  const lines = (el.innerText || '').split('\n').map(s => s.trim()).filter(Boolean);
  if (lines.length < 2 || lines.length > 6 || seen.has(lines.join('|'))) continue;
  seen.add(lines.join('|'));
  const a = el.matches('a') ? el : el.querySelector('a[href*="/direct/t/"]');
  out.push({lines, href: a ? a.href : ''});
}
return out;
"""


def settings(cfg: dict) -> dict:
    return {**DEFAULTS, **cfg.get("replies", {})}


def when(ts) -> datetime | None:
    """An inbox timestamp (Instagram gives microseconds; seconds or milliseconds are accepted too) as local time."""
    try:
        n = int(ts)
    except (TypeError, ValueError):
        return None
    while n > 10**11:                                # µs -> ms -> s
        n //= 1000
    return datetime.fromtimestamp(n)


def full_text(item: dict) -> str:
    """The whole of one message as they wrote it (line breaks kept), up to TEXT_MAX characters; a label like
    "[voice message]" for a message with no text."""
    kind = item.get("item_type") or ""
    text = item.get("text") or (item.get("link") or {}).get("text") or (item.get("reel_share") or {}).get("text") \
        or (item.get("action_log") or {}).get("description") or ""
    text = str(text).strip() or f"[{LABELS.get(kind, kind or 'message')}]"
    return text if len(text) <= TEXT_MAX else text[:TEXT_MAX - 3] + "..."


def snippet(item: dict) -> str:
    """A short plain-text version of one message."""
    text = " ".join(full_text(item).split())
    return text if len(text) <= SNIPPET else text[:SNIPPET - 3] + "..."


def thread_url(thread: dict) -> str:
    return f"https://www.instagram.com/direct/t/{thread.get('thread_id')}/"


def match(threads: list[dict], sent: dict[str, str], now: datetime | None = None,
          how: str = "api") -> tuple[list[dict], set[str]]:
    """From inbox threads, the messages each DMed person sent after our DM. sent: {username: when our DM went
    out}. Returns (reply rows for data/replies.csv, usernames whose chat was among the threads). Group chats and
    our own messages never count. how: what the rows' how column says ('api', or 'request' for Message Requests)."""
    found_at = (now or datetime.now()).isoformat(timespec="seconds")
    rows, seen = [], set()
    for t in threads:
        users = t.get("users") or []
        if t.get("is_group") or len(users) != 1:
            continue
        them = users[0]
        user = (them.get("username") or "").lower()
        if user not in sent:
            continue
        seen.add(user)
        since, pk = datetime.fromisoformat(sent[user]), str(them.get("pk") or them.get("pk_id") or them.get("id") or "")
        for item in t.get("items") or []:
            at = when(item.get("timestamp"))
            if item.get("is_sent_by_viewer") is True:          # ours, whatever user_id says
                continue
            if not pk or str(item.get("user_id")) != pk or not at or at <= since or not item.get("item_id"):
                continue
            rows.append({"username": user, "item_id": str(item["item_id"]), "replied_at": at.isoformat(timespec="seconds"),
                         "snippet": snippet(item), "text": full_text(item), "thread_url": thread_url(t),
                         "sent_at": sent[user], "found_at": found_at, "how": how})
    rows.sort(key=lambda r: r["replied_at"])
    return rows, seen


def via_api(driver, cfg: dict, sent: dict[str, str], box: str = "inbox") -> list[dict] | None:
    """Every thread in the inbox (box "inbox") or Message Requests (box "pending_inbox") that could hold a reply,
    page by page, or None if that endpoint didn't answer. Only reads: nothing is accepted or marked seen. Raises
    AccountBlocked."""
    r = settings(cfg)
    earliest, wanted = min(datetime.fromisoformat(t) for t in sent.values()), set(sent)
    threads, cursor = [], None
    for _ in range(r["max_pages"]):
        q = f"persistentBadging=true&limit=20&thread_message_limit={r['messages_per_chat']}" + \
            (f"&cursor={quote(cursor)}&direction=older" if cursor else "")    # as instagrapi pages older chats
        data = comments.api(driver, f"direct_v2/{box}/?{q}")
        inbox = (data or {}).get("inbox")
        if inbox is None:
            return threads or None
        batch = inbox.get("threads") or []
        threads += batch
        wanted -= {(u.get("username") or "").lower() for t in batch for u in t.get("users") or []}
        cursor, last = inbox.get("oldest_cursor"), when((batch[-1] if batch else {}).get("last_activity_at"))
        if not wanted or inbox.get("has_older") is False or not cursor or (last and last < earliest):
            break
        browser.pause(cfg, 0.4)
    return threads


AGO = re.compile(r"(?:^|·\s*)(\d+)\s*(s|m|h|d|w|y)\b", re.I)
SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800, "y": 31536000}


def ago(lines: list[str]) -> timedelta:
    """How long ago a chat-list row says its last message was ("· 2h", "3d"); zero if it doesn't say."""
    for line in lines:
        m = AGO.search(line)
        if m:
            return timedelta(seconds=int(m.group(1)) * SECONDS[m.group(2).lower()])
    return timedelta(0)


def from_rows(rows: list[dict], sent: dict[str, str], names: dict[str, str], now: datetime) -> tuple[list[dict], set[str]]:
    """The DOM fallback's matching: chat-list rows ({"lines", "href"}) -> reply rows, as match() does. names:
    {username: display name}. A row is theirs if its first line is their display name or username; it is a reply
    if its preview (the next line) isn't ours ("You: ...", "You sent ...")."""
    out, seen = [], set()
    for user, since in sent.items():
        keys = {user.lower(), (names.get(user) or "").strip().lower()} - {""}
        row = next((r for r in rows if r["lines"][0].strip().lower() in keys), None)
        if not row:
            continue
        seen.add(user)
        preview = next((l for l in row["lines"][1:] if not re.match(r"^active\b", l, re.I)), "")
        text = re.sub(r"\s*·\s*\d+\s*[smhdwy]\s*$", "", preview).strip()
        if not text or re.match(r"^you( sent|:)", text, re.I):
            continue
        at = max(now - ago(row["lines"][1:]), datetime.fromisoformat(since) + timedelta(seconds=1))
        out.append({"username": user, "item_id": "page:" + hashlib.sha1(f"{user}|{text}".encode()).hexdigest()[:16],
                    "replied_at": at.isoformat(timespec="seconds"), "snippet": text[:SNIPPET], "text": text[:TEXT_MAX],
                    "thread_url": row["href"] or direct.chat_link(user), "sent_at": since,
                    "found_at": now.isoformat(timespec="seconds"), "how": "page"})
    return out, seen


def read_api(driver, cfg: dict, sent: dict[str, str], now: datetime) -> dict | None:
    """The inbox, then Message Requests for whoever wasn't in it. None if the inbox endpoint didn't answer.
    Returns {"rows", "seen", "threads": {username: (thread, 'api' | 'request')}, "how"}. Raises AccountBlocked."""
    threads = via_api(driver, cfg, sent)
    if threads is None:
        return None
    rows, seen = match(threads, sent, now)
    found = {u: (t, "api") for t in threads for u in [one_to_one(t)] if u in seen}
    missing = {u: at for u, at in sent.items() if u not in seen}
    requests = via_api(driver, cfg, missing, "pending_inbox") if missing else []
    how = f"{len(threads)} chats from Instagram's inbox endpoint"
    if requests is None:
        how += "; Message Requests didn't answer"
    elif missing:
        more, also = match(requests, missing, now, how="request")
        rows, seen = sorted(rows + more, key=lambda r: r["replied_at"]), seen | also
        found.update({u: (t, "request") for t in requests for u in [one_to_one(t)] if u in also})
        how += f" and {len(requests)} from Message Requests"
    return {"rows": rows, "seen": seen, "threads": found, "how": how}


def one_to_one(thread: dict) -> str:
    """The other person's username in a one-to-one chat, else ''."""
    users = thread.get("users") or []
    return "" if thread.get("is_group") or len(users) != 1 else (users[0].get("username") or "").lower()


def thread(driver, cfg: dict, thread_id: str) -> dict | None:
    """One chat's latest messages from the API (direct_v2/threads/<id>/), or None if it didn't answer. Reads only:
    the chat isn't opened or marked seen. Raises AccountBlocked."""
    data = comments.api(driver, f"direct_v2/threads/{quote(str(thread_id))}/?limit={settings(cfg)['messages_per_chat']}")
    t = (data or {}).get("thread")
    return t if isinstance(t, dict) and t.get("thread_id") else None


def pending_users(driver, cfg: dict, people: list[str]) -> set[str] | None:
    """Which of people have their chat in Message Requests right now, or None if that endpoint didn't answer.
    Reads only: nothing is accepted. Raises AccountBlocked."""
    if not people:
        return set()
    requests = via_api(driver, cfg, {p.lower(): "1970-01-01T00:00:00" for p in people}, "pending_inbox")
    if requests is None:
        return None
    return {one_to_one(t) for t in requests} & {p.lower() for p in people}


def via_page(driver, cfg: dict) -> list[dict]:
    """The chat list on the inbox page, scrolled a few times. Raises AccountBlocked."""
    driver.get(f"{direct.IG}/direct/inbox/")
    browser.pause(cfg, 0.6)
    direct.dismiss_dialogs(driver)
    direct.check_account(driver)
    rows = []
    for _ in range(5):
        rows = driver.execute_script(_ROWS)
        driver.execute_script("for (const el of document.querySelectorAll('*')) "
                              "if (el.scrollHeight > el.clientHeight + 50 && el.querySelector('[role=\"button\"], a'))"
                              " el.scrollTop = el.scrollHeight;")
        browser.pause(cfg, 0.3)
    return driver.execute_script(_ROWS) or rows


def run(cfg: dict) -> dict:
    """Check the inbox for replies from everyone DMed and add new ones to data/replies.csv. Raises browser.Halt
    (exit code 2) if Instagram refuses the account. Returns {"dmed", "replied", "new", "missing", "how"}."""
    import apimode
    if apimode.is_api(cfg):                        # config.json "mode": "api": conversations via the API
        return apimode.replies_run(cfg)
    sent, now = store.sent_at(), datetime.now()
    if not sent:
        print("[+] Nobody is recorded as sent in data/contacted.csv yet; no replies to look for.")
        return {"dmed": 0, "replied": 0, "new": 0, "missing": 0, "how": ""}
    with store.lock():
        driver = browser.make_driver(cfg)
        try:
            browser.ensure_logged_in(driver, cfg, interactive=sys.stdin.isatty())
            got = read_api(driver, cfg, sent, now)
            if got is not None:
                rows, seen, how = got["rows"], got["seen"], got["how"]
            else:
                print("[!] The inbox endpoint didn't answer; reading the inbox page instead (latest message per chat, "
                      "times approximate)")
                names = {l["username"].lower(): l.get("full_name", "") for l in store.load_leads().values()}
                page = via_page(driver, cfg)
                rows, seen = from_rows(page, sent, names, now)
                how = f"{len(page)} chats on the inbox page"
        except AccountBlocked as exc:
            raise browser.Halt(f"account-wide problem while reading the inbox ({exc.kind}): {exc}")
        finally:
            driver.quit()
    fresh = store.add_replies(rows)
    replied = {r["username"] for r in store.load_replies()} & set(sent)
    for r in fresh:
        print(f"  @{r['username']:<28} {r['replied_at'][:16]}  {r['snippet']!r}\n  {'':<29} {r['thread_url']}"
              + ("  (in Message Requests, not accepted)" if r.get("how") == "request" else ""))
    missing = len(set(sent) - seen)
    print(f"[done] read {how}; {len(replied)} of {len(sent)} people DMed have replied; {len(fresh)} new "
          f"{'message' if len(fresh) == 1 else 'messages'} saved to data/replies.csv")
    if missing:
        print(f"[=] {missing} DMed people's chats weren't in what was read (older than the pages read, in "
              f"General, or deleted)")
    return {"dmed": len(sent), "replied": len(replied), "new": len(fresh), "missing": missing, "how": how}


if __name__ == "__main__":
    import argparse
    argparse.ArgumentParser(description="Read the Direct inbox and record replies from people DMed into "
                                        "data/replies.csv (Message Requests too). Reads only; nothing is sent, "
                                        "accepted or marked seen.").parse_args()
    try:
        run(json.loads(CONFIG.read_text()))
    except browser.Halt as stop:
        print(f"[stopped] {stop}")
        sys.exit(2)
