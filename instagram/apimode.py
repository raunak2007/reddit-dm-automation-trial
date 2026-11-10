"""apimode.py - the pipeline in API mode (config.json "mode": "api"), through Meta's official Instagram API
(graph.py) instead of the browser. Only for a professional (Business or Creator) account; browser mode stays the
default and doesn't use this file.

    discover   the account's own posts and reels from /me/media within discover.lookback_days (at most 7: private
               replies close 7 days after a comment), then each one's comments from /{media}/comments into
               data/leads.json, the same shape as browser mode plus comment_id (the commenter's newest comment)
               and user_id (their Instagram id). discover.accounts and discover.posts aren't read in API mode.
    send       a private reply to the lead's comment_id (POST /{ig-user-id}/messages {recipient: {comment_id}}),
               through send.py's own loop: the 'submitting' row first, the daily cap and the delays. Nobody is
               followed. Instagram allows one private reply per comment, and contacted.csv still allows one DM per
               person. Instagram saying it was already sent -> 'sent'; the 7-day window closed -> 'expired' (final);
               a rate limit or a dead token -> the run stops with exit code 2.
    replies    /me/conversations: each DMed person's messages after our private reply into data/replies.csv (text,
               item id = the message id, thread_id = the conversation id, user_id = their IGSID) with reply_by,
               their latest message + 24 hours: the messaging window.
    answer     approved inbox answers, as a DM to their IGSID ({recipient: {id}}), only inside that 24-hour window.
    livecheck  no browser: the token works, /me is a professional account matching instagram.my_username, and one
               media, comments and conversations read each work.

Follow-ups are refused in API mode: Meta allows no message to someone who hasn't written back within 24 hours.
"""
from __future__ import annotations

import copy
import random
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import browser
import comments
import graph
import queue_server
import replies
import send
import store
from graph import GraphError, Unanswered, Unreachable
from send_dm import ACCOUNT_WIDE, Result

DEFAULTS = {"graph_version": graph.GRAPH_VERSION, "ig_user_id": "", "max_media_pages": 4}
REPLY_DAYS = 7                    # private replies close 7 days after the comment
WINDOW = timedelta(hours=24)      # the messaging window after their latest message
FOLLOWUP_REFUSAL = ("Follow-ups aren't possible in API mode: Meta's API allows a message only as a private reply "
                    "to a comment or within 24 hours of the person's own latest message, so there is no way to "
                    "message someone who hasn't written back. Nothing was sent.")


def mode(cfg: dict) -> str:
    m = cfg.get("mode") or "browser"
    if m not in ("browser", "api"):
        raise SystemExit(f'config.json "mode" must be "browser" or "api", not {m!r}')
    return m


def is_api(cfg: dict) -> bool:
    return mode(cfg) == "api"


def settings(cfg: dict) -> dict:
    return {**DEFAULTS, **cfg.get("api", {})}


def parse_time(s) -> datetime | None:
    """A Graph API time ("2026-11-08T10:00:00+0000") as naive local time, like the rest of data/."""
    if not s:
        return None
    s = str(s).replace("Z", "+00:00")
    s = re.sub(r"([+-]\d\d)(\d\d)$", r"\1:\2", s)
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt.astimezone().replace(tzinfo=None) if dt.tzinfo else dt


def iso(dt: datetime | None) -> str:
    return dt.isoformat(timespec="seconds") if dt else ""


@dataclass
class Account:
    graph: graph.Graph
    ig_id: str                    # what /{ig-user-id}/messages takes (/me's user_id, else its id)
    username: str
    ids: set = field(default_factory=set)   # every id /me gave: our own messages and comments carry one of them

    def ours(self, who: dict | None) -> bool:
        who = who or {}
        return str(who.get("id") or "") in self.ids or (who.get("username") or "").lower() == self.username


def refusal(exc: Exception) -> str:
    """Why an API failure stops everything: 'token' / 'rate' (exit code 2), else ''."""
    return exc.kind() if isinstance(exc, GraphError) and exc.kind() in ("token", "rate") else ""


def halt_on(exc: Exception, doing: str):
    """Raise browser.Halt (exit 2) for a dead token or a rate limit, SystemExit (exit 1) for anything else."""
    kind = refusal(exc)
    if kind == "token":
        raise browser.Halt(f"the Instagram API token no longer works while {doing}: {exc}. Get a new long-lived "
                           f"token (README, API mode)")
    if kind == "rate":
        raise browser.Halt(f"Instagram is rate-limiting this account's API use while {doing}: {exc}")
    raise SystemExit(f"Instagram's API failed while {doing}: {exc}")


def connect(cfg: dict, now: datetime | None = None, say=print) -> Account:
    """The token (refreshed when due), and /me: it must be a professional account and instagram.my_username (and
    api.ig_user_id, if set). browser.Halt (exit 2) if the token is missing, expired or refused, or the account is
    the wrong one or not professional; SystemExit (exit 1) if Instagram can't be reached."""
    try:
        state = graph.refresh_if_due(graph.current(now), now, say)
    except SystemExit as exc:
        raise browser.Halt(str(exc.code)) from None
    g = graph.Graph(state["token"], settings(cfg)["graph_version"])
    try:
        try:
            me = g.me()
        except GraphError as exc:
            if exc.code != 100:               # 100: a field it doesn't know (account_type): ask without it
                raise
            me = g.get("me", fields="user_id,username,id")
    except (GraphError, Unreachable) as exc:
        if isinstance(exc, GraphError) and exc.kind() == "token":
            raise browser.Halt(f"The Instagram API token doesn't work ({exc}). Get a new long-lived token and put "
                               f"it in instagram/.env as IG_ACCESS_TOKEN (README, API mode).") from None
        halt_on(exc, "reading /me")
    username = (me.get("username") or "").lower()
    kind = str(me.get("account_type") or "").upper()
    if kind not in graph.PROFESSIONAL:
        raise browser.Halt(
            f"@{username or '?'} isn't a professional (Business or Creator) Instagram account "
            f"(account_type: {kind or 'not given'}), so API mode can't use it. Switch it to a professional account "
            f"in Instagram's settings, or set config.json's \"mode\" back to \"browser\".")
    mine = cfg["instagram"]["my_username"].lower().removeprefix("@")
    if username != mine:
        raise browser.Halt(f"The API token is for @{username}, but config.json's instagram.my_username is @{mine}; "
                           f"not using it.")
    ids = {str(v) for v in (me.get("user_id"), me.get("id")) if v}
    ig_id = str(me.get("user_id") or me.get("id") or "")
    expected = str(settings(cfg).get("ig_user_id") or "").strip()
    if expected and expected not in ids:
        raise browser.Halt(f"The API token is for Instagram user {ig_id}, but config.json's api.ig_user_id is "
                           f"{expected}; not using it.")
    say(f"[+] Instagram API: @{username} ({kind.lower()} account, id {ig_id})")
    return Account(g, ig_id, username, ids)


# ----------------------------------------------------------------- discover + comments
def lookback(cfg: dict, say=print) -> int:
    days = cfg.get("discover", {}).get("lookback_days", 7)
    if days > REPLY_DAYS:
        say(f"[=] discover.lookback_days is {days}; API mode reads {REPLY_DAYS} days at most, since Instagram closes "
            f"private replies {REPLY_DAYS} days after a comment")
        return REPLY_DAYS
    return days


def media_posts(acc: Account, cfg: dict, cutoff: datetime | None) -> list[dict]:
    """The account's posts and reels (discover.py's post dicts plus media_id), newest first, back to cutoff."""
    out = []
    for page in acc.graph.media(settings(cfg)["max_media_pages"]):
        for m in page:
            if not m.get("id"):
                continue
            link = m.get("permalink") or ""
            code = comments.SHORTCODE.search(link)
            taken = parse_time(m.get("timestamp"))
            out.append({"shortcode": code.group(1) if code else str(m["id"]), "media_id": str(m["id"]),
                        "url": link or str(m["id"]), "owner": acc.username, "taken_at": iso(taken),
                        "comment_count": m.get("comments_count"), "comments_disabled": False, "pinned": False,
                        "source": f"@{acc.username}",
                        "reel": m.get("media_product_type") == "REELS" or "/reel/" in link})
        dated = [p for p in out if p["taken_at"]]
        if cutoff and dated and datetime.fromisoformat(dated[-1]["taken_at"]) < cutoff:
            break
    return out


def read_comments(acc: Account, cfg: dict, post: dict, now: datetime) -> dict:
    """One post's commenters from the API into data/leads.json (one lead per commenter, all their comments
    together, comment_id = their newest comment still within 7 days). Raises GraphError / Unreachable."""
    limit, oldest = cfg["instagram"].get("max_comments", 500), now - timedelta(days=REPLY_DAYS)
    rows = []
    for page in acc.graph.comments(post["media_id"]):
        rows += page
        if len(rows) >= limit:
            break
    by_user = {}
    for c in rows[:limit]:
        who = c.get("from") or {}
        user = (who.get("username") or "").lower()
        at = parse_time(c.get("timestamp"))
        if not user or acc.ours(who) or not c.get("id") or (at and at < oldest):
            continue
        u = by_user.setdefault(user, {"username": user, "user_id": str(who.get("id") or ""), "texts": [], "ids": [],
                                      "newest": None, "comment_id": ""})
        u["texts"].append((iso(at), c.get("text") or ""))
        u["ids"].append(str(c["id"]))
        if u["newest"] is None or (at and at > u["newest"]):
            u["newest"], u["comment_id"] = at, str(c["id"])
    leads, new = store.load_leads(), 0
    for user, u in by_user.items():
        text = "\n".join(t for _, t in sorted(u["texts"]))
        key = store.lead_key(post["shortcode"], user)
        if key not in leads:
            new += 1
            leads[key] = {"key": key, "post": post["url"], "shortcode": post["shortcode"], "post_owner": acc.username,
                          "found_at": iso(now), "full_name": "", "is_private": False, "reply_to": None}
        elif leads[key].get("text") != text:
            leads[key].pop("ai", None)                 # new comments from them: score again
        leads[key].update(username=user, user_id=u["user_id"], text=text, comment_id=u["comment_id"],
                          comment_ids=u["ids"], commented_at=iso(u["newest"]), media_id=post["media_id"])
    store.save_leads(leads)
    print(f"[done] {len(rows)} comments from the API; {len(by_user)} commenters within {REPLY_DAYS} days, {new} new")
    return {"shortcode": post["shortcode"], "owner": acc.username, "comments": len(rows), "commenters": len(by_user),
            "new": new, "how": "the Instagram API"}


def discover(cfg: dict, dry_run: bool = False) -> dict:
    """discover.run in API mode. Raises browser.Halt (exit 2) on a dead token, a rate limit or the wrong account."""
    import discover as disc
    now = datetime.now()
    days = lookback(cfg)
    view = copy.deepcopy(cfg)
    view.setdefault("discover", {})["lookback_days"] = days
    d = disc.settings(view)
    if d["accounts"] or d["posts"]:
        print("[=] discover.accounts and discover.posts aren't read in API mode (only this account's own posts)")
    cutoff = now - timedelta(days=days)
    totals = {"read": 0, "skipped": 0, "new": 0, "failed": 0}
    with store.lock():
        acc = connect(cfg)
        try:
            found = media_posts(acc, cfg, cutoff)
        except (GraphError, Unreachable) as exc:
            halt_on(exc, "listing the account's posts")
        state = store.load_posts()
        if not dry_run:
            disc.remember(state, found, now)
            store.save_posts(state)
        todo, skipped = disc.select(found, state, view, now)
        totals["skipped"] = len(skipped)
        for p, why in skipped:
            if not why.startswith("older"):
                print(f"[=] {p['url']}: {why}")
        print(f"[+] {len(todo)} posts to read (of {len(found)} found; lookback {days} days)")
        fails = 0
        for n, p in enumerate(todo, 1):
            print(f"\n[{n}/{len(todo)}] {p['url']} (posted {p.get('taken_at') or 'date unknown'})")
            if dry_run:
                continue
            try:
                got = read_comments(acc, cfg, p, now)
            except (GraphError, Unreachable) as exc:
                if refusal(exc):
                    halt_on(exc, f"reading the comments on {p['url']}")
                fails, totals["failed"] = fails + 1, totals["failed"] + 1
                print(f"    [!] couldn't read it ({exc}); tried again next run")
                if fails >= disc.MAX_FAILS_IN_A_ROW:
                    print(f"[stopped reading] {fails} posts in a row failed")
                    break
                continue
            fails = 0
            disc.mark_read(state, p, got, now)
            store.save_posts(state)
            totals["read"], totals["new"] = totals["read"] + 1, totals["new"] + got["new"]
    if not dry_run:
        print(f"[done] read {totals['read']} posts, {totals['new']} new commenters"
              + (f", {totals['failed']} posts failed" if totals["failed"] else ""))
    return totals


def read_link(cfg: dict, url: str) -> int:
    """`python run.py <link>` in API mode: that post's comments, if it is one of this account's own."""
    code = comments.shortcode(url)
    now = datetime.now()
    with store.lock():
        acc = connect(cfg)
        try:
            post = next((p for p in media_posts(acc, cfg, None) if p["shortcode"] == code), None)
            if not post:
                raise SystemExit(f"{url} isn't one of @{acc.username}'s recent posts; API mode reads only the "
                                 f"account's own posts")
            return read_comments(acc, cfg, post, now)["new"]
        except (GraphError, Unreachable) as exc:
            halt_on(exc, f"reading {url}")


# ----------------------------------------------------------------- send: private replies
def private_reply(acc: Account, lead: dict | None, text: str, before_submit, now: datetime | None = None) -> Result:
    """One private reply to the lead's comment, one attempt. A send_dm.Result like direct.send_message's, plus
    'expired' (the 7-day window closed)."""
    now = now or datetime.now()
    cid = (lead or {}).get("comment_id")
    if not cid:
        return Result("invalid", "api: no comment id on this lead (read its post again in API mode)")
    at = parse_time((lead or {}).get("commented_at"))
    if at and now - at >= timedelta(days=REPLY_DAYS):
        return Result("expired", f"api: comment {cid} is from {at:%Y-%m-%d %H:%M}; Instagram closes private replies "
                                 f"{REPLY_DAYS} days after a comment")
    before_submit()
    try:
        acc.graph.private_reply(acc.ig_id, cid, text)
    except Unanswered as exc:
        return Result("uncertain", f"api: the private reply to comment {cid} may have gone out: {exc}")
    except GraphError as exc:
        kind = exc.kind()
        if kind == "already_sent":
            return Result("sent", f"api: Instagram says a private reply to comment {cid} was already sent")
        if kind == "window_closed" or exc.code == 10:
            return Result("expired", f"api: Instagram says comment {cid}'s 7-day private-reply window closed: {exc}")
        if kind == "rate":
            return Result("rate_limited", f"api: {exc}")
        if kind == "token":
            return Result("login", f"api: the token no longer works: {exc}")
        if kind == "transient":
            return Result("not_submitted", f"api: Instagram's temporary error, nothing sent: {exc}")
        return Result("refused", f"api: {exc}")
    return Result("sent", f"api: private reply to comment {cid}")


def conversations(acc: Account, cfg: dict, wanted: set[str] | None = None,
                  since: datetime | None = None) -> list[dict]:
    """/me/conversations, page by page, until every wanted username's chat is seen, chats are older than since, or
    replies.max_pages pages."""
    r = replies.settings(cfg)
    out, wanted = [], set(wanted or ())
    for page in acc.graph.conversations(r["messages_per_chat"], r["max_pages"]):
        out += page
        wanted -= {other(c, acc)[0] for c in page}
        last = parse_time((page[-1] if page else {}).get("updated_time"))
        if not page or (wanted is not None and not wanted and out) or (since and last and last < since):
            break
    return out


def other(conv: dict, acc: Account) -> tuple[str, str]:
    """(their username, their IGSID) in a conversation: from the first message that isn't ours."""
    for m in (conv.get("messages") or {}).get("data") or []:
        who = m.get("from") or {}
        if who and not acc.ours(who):
            return (who.get("username") or "").lower(), str(who.get("id") or "")
    return "", ""


def theirs(conv: dict, acc: Account) -> list[dict]:
    """Their messages in a conversation, oldest first, each with a parsed 'at'."""
    msgs = []
    for m in (conv.get("messages") or {}).get("data") or []:
        at = parse_time(m.get("created_time"))
        if m.get("id") and at and not acc.ours(m.get("from")):
            msgs.append(dict(m, at=at))
    return sorted(msgs, key=lambda m: m["at"])


def resolve_held(acc: Account, cfg: dict) -> None:
    """Held private replies: 'sent' if our message is in their conversation; otherwise they stay held (an API
    conversation can't prove a private reply didn't go out). python send.py resolve settles one by hand."""
    held, leads = store.held(), store.load_leads()
    try:
        convs = conversations(acc, cfg, set(held))
    except (GraphError, Unreachable) as exc:
        print(f"[!] couldn't read conversations to check held sends ({exc}); they stay held")
        return
    for user, row in held.items():
        text = ((leads.get(row["lead_key"]) or {}).get("approved") or {}).get("dm", "")
        conv = next((c for c in convs if other(c, acc)[0] == user), None)
        ours = [m.get("message") or "" for m in ((conv or {}).get("messages") or {}).get("data") or []
                if acc.ours(m.get("from"))]
        if text and any(replies_squash(t) == replies_squash(text) for t in ours):
            store.resolve(user, True, how="in the API's conversations")
        else:
            print(f"[=] @{user} is held: the reply isn't in a conversation the API shows. Check it in Instagram, "
                  f"then: python send.py resolve {user} --sent | --not-sent")


def replies_squash(text: str) -> str:
    return re.sub(r"\s+", "", text or "").lower()


def send_run(cfg: dict, by_hand: bool = False) -> int:
    """send.run in API mode. Returns the exit code."""
    if by_hand:
        raise SystemExit("--by-hand is for browser mode; API mode sends private replies through the API")
    out = cfg["outreach"]
    if out.get("auto_approve", True):
        n = queue_server.auto_approve()
        print(f"[+] auto-approved {n} new commenters with their drafted DMs (outreach.auto_approve)")
    leads = send.approved_leads()
    resolving = out.get("auto_resolve", True) and bool(store.held())
    if not leads and not resolving:
        print("[+] Nothing approved to send.")
        return 0
    print(f"[+] {len(leads)} approved commenters (private replies through the Instagram API)")
    view = copy.deepcopy(cfg)
    view["outreach"]["auto_follow"] = False             # never follow in API mode
    with store.lock():
        cap = out["daily_dm_limit"]
        if store.sent_today() >= cap and not resolving:
            raise SystemExit(f"Already sent {store.sent_today()}/{cap} today. Nothing to do until tomorrow.")
        acc = connect(cfg)
        if resolving:
            resolve_held(acc, cfg)
            leads = send.approved_leads()
        by = {(l["username"].lower(), l.get("approved", {}).get("dm", "")): l for l in leads}

        def reply(driver, url, text, user, name="", *, before_submit=None):
            return private_reply(acc, by.get((user.lower(), text)), text, before_submit)
        return send.report(send.send_loop(None, leads, view, send=reply))


# ----------------------------------------------------------------- replies
def match(convs: list[dict], sent: dict[str, str], acc: Account, now: datetime,
          ids: dict[str, str] | None = None) -> tuple[list[dict], set[str]]:
    """Each DMed person's messages after our private reply. ids: {their user id: username} from leads.json, for a
    conversation that doesn't give the username. Returns (rows for data/replies.csv, usernames seen)."""
    rows, seen, ids = [], set(), ids or {}
    for c in convs:
        user, igsid = other(c, acc)
        user = user or ids.get(igsid, "")
        if user not in sent:
            continue
        seen.add(user)
        mine = theirs(c, acc)
        if not mine:
            continue
        reply_by = iso(mine[-1]["at"] + WINDOW)
        since = datetime.fromisoformat(sent[user])
        for m in mine:
            if m["at"] <= since:
                continue
            body = (m.get("message") or "").strip() or "[attachment]"
            text = body if len(body) <= replies.TEXT_MAX else body[:replies.TEXT_MAX - 3] + "..."
            flat = " ".join(body.split())
            rows.append({"username": user, "item_id": str(m["id"]), "replied_at": iso(m["at"]),
                         "snippet": flat if len(flat) <= replies.SNIPPET else flat[:replies.SNIPPET - 3] + "...",
                         "text": text, "thread_url": f"https://ig.me/m/{user}", "thread_id": str(c.get("id") or ""),
                         "user_id": igsid, "reply_by": reply_by, "sent_at": sent[user], "found_at": iso(now),
                         "how": "graph"})
    rows.sort(key=lambda r: r["replied_at"])
    return rows, seen


def replies_run(cfg: dict) -> dict:
    """replies.run in API mode."""
    sent, now = store.sent_at(), datetime.now()
    if not sent:
        print("[+] Nobody is recorded as sent in data/contacted.csv yet; no replies to look for.")
        return {"dmed": 0, "replied": 0, "new": 0, "missing": 0, "how": ""}
    ids = {l["user_id"]: l["username"].lower() for l in store.load_leads().values() if l.get("user_id")}
    with store.lock():
        acc = connect(cfg)
        try:
            convs = conversations(acc, cfg, set(sent), min(datetime.fromisoformat(t) for t in sent.values()))
        except (GraphError, Unreachable) as exc:
            halt_on(exc, "reading conversations")
    rows, seen = match(convs, sent, acc, now, ids)
    fresh = store.add_replies(rows)
    replied = {r["username"] for r in store.load_replies()} & set(sent)
    for r in fresh:
        print(f"  @{r['username']:<28} {r['replied_at'][:16]}  {r['snippet']!r}  (answer by {r['reply_by'][:16]})")
    missing = len(set(sent) - seen)
    how = f"{len(convs)} conversations from the Instagram API"
    print(f"[done] read {how}; {len(replied)} of {len(sent)} people DMed have replied; {len(fresh)} new "
          f"{'message' if len(fresh) == 1 else 'messages'} saved to data/replies.csv")
    if missing:
        print(f"[=] {missing} DMed people have no conversation the API showed (no reply yet, or older than the "
              f"pages read)")
    return {"dmed": len(sent), "replied": len(replied), "new": len(fresh), "missing": missing, "how": how}


# ----------------------------------------------------------------- answer
def deadlines(acc: Account | None, convs: list[dict], people: list[str]) -> dict[str, tuple[str, datetime | None]]:
    """{username: (their IGSID, when the 24-hour window closes)}: from the live conversations, else replies.csv."""
    out = {}
    for c in convs:
        user, igsid = other(c, acc)
        mine = theirs(c, acc)
        if user in people and igsid and mine:
            out[user] = (igsid, mine[-1]["at"] + WINDOW)
    for r in store.load_replies():
        user = r["username"].lower()
        if user in people and user not in out and r.get("user_id"):
            by = [datetime.fromisoformat(x["reply_by"]) for x in store.load_replies()
                  if x["username"].lower() == user and x.get("reply_by")]
            out[user] = (r["user_id"], max(by) if by else None)
    return out


def answer_run(cfg: dict, now_fn=datetime.now) -> int:
    """answer.run in API mode: each approved Instagram answer as a DM to their IGSID inside the 24-hour window.
    Returns the exit code."""
    import answer as ans
    a, out = ans.settings(cfg), cfg["outreach"]
    with store.lock():
        for e in ans.answers.stuck(ans.PLATFORM):
            print(f"[!] @{e['person']}: a run stopped while sending this answer; now 'uncertain' (look at the chat)")
        todo = ans.answers.approved(ans.PLATFORM)
        if not todo:
            print("[+] No approved Instagram answers to send.")
            return 0
        print(f"[+] {len(todo)} approved answers; at most {a['max_per_run']} this run (answers.max_per_run)")
        acc = connect(cfg)
        people = [e["person"].lower() for e in todo]
        try:
            convs = conversations(acc, cfg, set(people))
        except (GraphError, Unreachable) as exc:
            if refusal(exc):
                halt_on(exc, "reading conversations")
            print(f"[!] couldn't read conversations ({exc}); going by data/replies.csv")
            convs = []
        window = deadlines(acc, convs, people)
        counts = {"sent": 0, "failed": 0, "uncertain": 0, "skipped": 0}
        stopped, halted, streak, tried = "", False, {"status": None, "n": 0}, 0
        for entry in todo:
            if tried >= a["max_per_run"]:
                stopped = f"answers.max_per_run ({a['max_per_run']}) reached; the rest go next run"
                break
            entry = ans.answers.get(entry["id"])
            if not entry or entry["status"] != "approved":
                counts["skipped"] += 1
                continue
            person, text, aid = entry["person"].lower(), (entry.get("text") or "").strip(), entry["id"]
            igsid, closes = window.get(person, ("", None))
            why = ("no text to send" if not text else
                   "their Instagram id isn't known (run python run.py replies in API mode first)" if not igsid else
                   "it can't be told when their 24-hour window closes" if not closes else
                   f"the 24-hour window closed on {closes:%Y-%m-%d %H:%M}" if now_fn() >= closes else "")
            print(f"\n[+] answering @{person}" + (f" (window closes {closes:%Y-%m-%d %H:%M})" if closes else ""))
            if why:
                print(f"    [!] {why}")
                ans.mark(aid, "failed", why, ("approved",))
                counts["failed"] += 1
                continue
            if tried:
                delay = random.uniform(out["min_delay_seconds"], out["max_delay_seconds"])
                print(f"[+] Waiting {delay:.0f}s before the next one...")
                time.sleep(delay)
            tried += 1
            if not ans.mark(aid, "sending", "", ("approved",)):
                counts["skipped"] += 1
                continue
            try:
                acc.graph.send_dm(acc.ig_id, igsid, entry["text"])
                status, reason = "sent", "api: sent to their IGSID"
            except Unanswered as exc:
                status, reason = "uncertain", f"api: may have gone out: {exc}"
            except GraphError as exc:
                status, reason = "failed", f"api: Instagram refused it, nothing was sent: {exc}"
                if refusal(exc):
                    ans.mark(aid, status, reason, ("sending",))
                    counts["failed"] += 1
                    stopped, halted = f"account-wide problem ({exc.kind()}): {exc}", True
                    break
            ans.mark(aid, status, reason, ("sending",))
            counts[status] += 1
            print("    sent" if status == "sent" else f"    [!] {status}: {reason}")
            if status == "sent":
                streak = {"status": None, "n": 0}
                continue
            streak = {"status": status, "n": streak["n"] + 1 if streak["status"] == status else 1}
            if streak["n"] >= (ans.MAX_FAILED_IN_A_ROW if status == "failed" else ans.MAX_UNCERTAIN_IN_A_ROW):
                stopped, halted = f"{streak['n']} answers in a row {status}", True
                break
    print(f"\n[done] answers sent {counts['sent']}, failed {counts['failed']}, uncertain {counts['uncertain']}, "
          f"skipped {counts['skipped']}")
    if stopped:
        print(f"[stopped] {stopped}")
    return 2 if halted else 0


# ----------------------------------------------------------------- livecheck
def livecheck(cfg: dict) -> int:
    """Read-only checks through the API (no browser): 0 passed, 2 an account problem (token, permissions, wrong or
    non-professional account), 1 anything else."""
    try:
        acc = connect(cfg)
    except browser.Halt as stop:
        print(f"[stopped] {stop}")
        return 2
    except SystemExit as exc:
        print(f"[!] {exc.code}")
        return 1
    code = 0

    def check(what, fn):
        nonlocal code
        try:
            got = fn()
            print(f"[ok] {what}")
            return got
        except GraphError as exc:
            account = exc.kind() == "token" or exc.code in (3, 10, 200) or exc.status == 403
            print(f"[{'stopped' if account else '!'}] {what}: {exc}"
                  + (" (the token lacks a permission: it needs " + ", ".join(graph.SCOPES) + ")" if account else ""))
            code = max(code, 2 if account else 1)
        except Unreachable as exc:
            print(f"[!] {what}: {exc}")
            code = max(code, 1)
        return None

    media = check("read the account's media (instagram_business_basic)", lambda: next(acc.graph.media(1), []))
    if media:
        check(f"read the comments on {media[0].get('permalink') or media[0]['id']} "
              f"(instagram_business_manage_comments)", lambda: next(acc.graph.comments(media[0]["id"], 1), []))
    elif media is not None:
        print("[=] no media to read comments from; the comments permission wasn't checked")
    check("read conversations (instagram_business_manage_messages)", lambda: next(acc.graph.conversations(1, 1), []))
    print({0: "[done] API checks passed", 2: "[stopped] the API checks show an account problem",
           1: "[!] API checks failed (not an account problem as far as Instagram said)"}[code])
    return code
