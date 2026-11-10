"""discover.py - find the posts to read without being given a link, so `python run.py` on its own does a whole run.

Which posts: the recent posts and reels of instagram.my_username, plus those of any usernames in
discover.accounts, plus any links in discover.posts. A post is read while it is within discover.lookback_days of
when it was posted, newest first, up to discover.max_posts per account (links in discover.posts aren't capped).
A post whose date isn't known (a link from discover.posts, or one found only on the profile page) is dated from
when this script first saw it. Every run reads those posts again, so new comments are picked up; with
discover.skip_unchanged (on by default), a post whose comment count is the same as at its last read is skipped,
since nobody new can be in it. Comments switched off: skipped.

How it finds them: inside the logged-in Chrome profile, with the session's own cookies, through the web API the
Instagram page uses itself (comments.api): users/web_profile_info/?username=<u> for the account's id and its
latest posts, then feed/user/<id>/ page by page until the posts are older than the lookback. If that doesn't
answer, it opens the profile page and reads the grid's post links instead (no dates there). Nothing is liked,
followed or opened beyond the profile and the posts themselves.

data/posts.json remembers each post found: its link, owner, when it was posted and first seen, its comment count,
when it was last read, how many times and how many commenters it had. A post is only read again while it is
within the lookback.

It stops at once with exit code 2 (browser.Halt) if Instagram logs the profile out, asks for a security check, or
rate-limits or blocks it, the same signals send.py stops on.

    python discover.py          list the posts a run would read now, and why others are skipped (no comments read)
    python run.py posts         what data/posts.json remembers (no browser)
"""
from __future__ import annotations

import json
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
DEFAULTS = {"lookback_days": 7, "max_posts": 6, "accounts": [], "posts": [], "skip_unchanged": True}
MAX_FEED_PAGES = 5           # feed pages (12 posts each) per account: far past any sensible lookback
MAX_FAILS_IN_A_ROW = 3       # posts that failed to read in a row (not Instagram refusing): stop reading

# Fallback: the post and reel links on a profile page's grid, in the order shown (newest first, pinned ones first).
_GRID = r"""
const root = document.querySelector('main, [role="main"]') || document.body, out = [], seen = new Set();
for (const a of root.querySelectorAll('a[href]')) {
  const m = a.getAttribute('href').match(/^(?:https:\/\/www\.instagram\.com)?\/(?:[A-Za-z0-9._]+\/)?(p|reel|tv)\/([A-Za-z0-9_-]+)\/?/);
  if (m && !seen.has(m[2])) { seen.add(m[2]); out.push({kind: m[1], code: m[2]}); }
}
return out;
"""


def settings(cfg: dict) -> dict:
    return {**DEFAULTS, **cfg.get("discover", {})}


def post_url(code: str, reel: bool = False) -> str:
    return f"https://www.instagram.com/{'reel' if reel else 'p'}/{code}/"


def _iso(ts) -> str | None:
    return datetime.fromtimestamp(int(ts)).isoformat(timespec="seconds") if ts else None


def feed_posts(data: dict) -> list[dict]:
    """The posts in one feed/user/<id>/ answer."""
    out = []
    for it in data.get("items") or []:
        code = it.get("code")
        if not code:
            continue
        out.append({"shortcode": code, "url": post_url(code, it.get("product_type") == "clips"),
                    "owner": (it.get("user") or {}).get("username"), "taken_at": _iso(it.get("taken_at")),
                    "comment_count": it.get("comment_count"), "comments_disabled": bool(it.get("comments_disabled")),
                    "pinned": bool(it.get("timeline_pinned_user_ids") or it.get("clips_tab_pinned_user_ids"))})
    return out


def profile_posts(user: dict) -> list[dict]:
    """The latest posts (about 12) in a web_profile_info answer's user, for when the feed doesn't answer."""
    out = []
    for edge in (user.get("edge_owner_to_timeline_media") or {}).get("edges", []):
        n = edge.get("node") or {}
        if not n.get("shortcode"):
            continue
        out.append({"shortcode": n["shortcode"], "url": post_url(n["shortcode"], n.get("product_type") == "clips"),
                    "owner": user.get("username"), "taken_at": _iso(n.get("taken_at_timestamp")),
                    "comment_count": (n.get("edge_media_to_comment") or {}).get("count"),
                    "comments_disabled": bool(n.get("comments_disabled")), "pinned": bool(n.get("pinned_for_users"))})
    return out


def from_api(driver, cfg: dict, username: str, cutoff: datetime) -> list[dict] | None:
    """An account's posts back to `cutoff` (plus up to a page past it) from the web API, or None if it didn't
    answer. Raises AccountBlocked."""
    info = comments.api(driver, f"users/web_profile_info/?username={quote(username)}")
    user = ((info or {}).get("data") or {}).get("user")
    if not user or not user.get("id"):
        return None
    posts, max_id = [], None
    for _ in range(MAX_FEED_PAGES):
        data = comments.api(driver, f"feed/user/{user['id']}/?count=12" + (f"&max_id={quote(max_id)}" if max_id else ""))
        if not data:
            break
        page = feed_posts(data)
        posts += page
        max_id = data.get("next_max_id")
        unpinned = [p for p in page if not p["pinned"] and p["taken_at"]]
        if not data.get("more_available") or not max_id or not page or \
                (unpinned and datetime.fromisoformat(unpinned[-1]["taken_at"]) < cutoff):
            break
        browser.pause(cfg, 0.4)
    for p in posts:
        p["owner"] = p["owner"] or user.get("username") or username
    return posts or profile_posts(user)


def from_page(driver, cfg: dict, username: str) -> list[dict]:
    """The post links on the account's profile grid (no dates or comment counts). Raises AccountBlocked."""
    driver.get(f"{direct.IG}/{username}/")
    browser.pause(cfg, 0.6)
    direct.check_account(driver)
    return [{"shortcode": g["code"], "url": post_url(g["code"], g["kind"] == "reel"), "owner": username,
             "taken_at": None, "comment_count": None, "comments_disabled": False, "pinned": False}
            for g in driver.execute_script(_GRID)]


def find(driver, cfg: dict, cutoff: datetime) -> list[dict]:
    """Every candidate post: the accounts' recent posts (API, else the profile page), then discover.posts.
    Each is tagged with its source ('@<account>' or 'watch'). Raises AccountBlocked."""
    d, found = settings(cfg), []
    accounts = [cfg["instagram"]["my_username"]] + [a for a in d["accounts"] if a]
    for account in dict.fromkeys(a.lower().removeprefix("@") for a in accounts):
        posts = from_api(driver, cfg, account, cutoff)
        how = "the API"
        if posts is None:
            print(f"[!] Instagram's API didn't answer for @{account}; reading the profile page instead (no dates)")
            posts, how = from_page(driver, cfg, account), "the profile page"
        print(f"[+] @{account}: {len(posts)} recent posts from {how}")
        found += [dict(p, source=f"@{account}") for p in posts]
        browser.pause(cfg, 0.4)
    for link in d["posts"]:
        m = comments.SHORTCODE.search(link or "")
        if not m:
            print(f"[!] discover.posts: not a post or reel link, ignored: {link}")
            continue
        found.append({"shortcode": m.group(1), "url": link, "owner": None, "taken_at": None, "comment_count": None,
                      "comments_disabled": False, "pinned": False, "source": "watch"})
    return found


def remember(state: dict, posts: list[dict], now: datetime) -> None:
    """Add what was just learnt about each post to data/posts.json's state (first_seen is kept from the first time)."""
    for p in posts:
        s = state.setdefault(p["shortcode"], {"first_seen": now.isoformat(timespec="seconds"), "reads": 0,
                                              "last_read": None})
        s.update({k: p.get(k) for k in ("url", "owner", "taken_at", "source") if p.get(k) or k not in s})


def dated(p: dict, state: dict, now: datetime) -> datetime:
    """When a post counts as from: when it was posted, else when it was first seen."""
    when = p.get("taken_at") or state.get(p["shortcode"], {}).get("first_seen")
    return datetime.fromisoformat(when) if when else now


def select(posts: list[dict], state: dict, cfg: dict, now: datetime) -> tuple[list[dict], list[tuple[dict, str]]]:
    """Which of the found posts to read now: (to read, newest first; [(post, why not)])."""
    d = settings(cfg)
    cutoff = now - timedelta(days=d["lookback_days"])
    first = {}
    for p in posts:
        first.setdefault(p["shortcode"], p)                  # found twice (an account and a link): the first wins
    unique = list(first.values())
    unique.sort(key=lambda p: dated(p, state, now), reverse=True)
    read, skip, per_account = [], [], {}
    for p in unique:
        s, when = state.get(p["shortcode"], {}), dated(p, state, now)
        if when < cutoff:
            skip.append((p, f"older than discover.lookback_days ({when:%Y-%m-%d})"))
        elif p.get("comments_disabled"):
            skip.append((p, "comments are switched off"))
        elif p["source"] != "watch" and per_account.get(p["source"], 0) >= d["max_posts"]:
            skip.append((p, f"past discover.max_posts ({d['max_posts']}) for {p['source']}"))
        elif d["skip_unchanged"] and s.get("last_read") and p.get("comment_count") is not None \
                and s.get("comment_count") == p["comment_count"]:
            per_account[p["source"]] = per_account.get(p["source"], 0) + 1
            skip.append((p, f"no new comments since its last read ({s['last_read']}, {p['comment_count']} comments)"))
        else:
            per_account[p["source"]] = per_account.get(p["source"], 0) + 1
            read.append(p)
    return read, skip


def mark_read(state: dict, p: dict, got: dict, now: datetime) -> None:
    s = state[p["shortcode"]]
    s.update(last_read=now.isoformat(timespec="seconds"), reads=s.get("reads", 0) + 1, commenters=got["commenters"],
             comment_count=p.get("comment_count") if p.get("comment_count") is not None else got["comments"])
    if got.get("owner") and not s.get("owner"):
        s["owner"] = got["owner"]


def run(cfg: dict, dry_run: bool = False) -> dict:
    """Find the recent posts and read each one's commenters into data/leads.json (comments.read_post). Raises
    browser.Halt (exit code 2) if Instagram refuses the account. Returns {"read", "skipped", "new", "failed"}."""
    now = datetime.now()
    cutoff = now - timedelta(days=settings(cfg)["lookback_days"])
    totals = {"read": 0, "skipped": 0, "new": 0, "failed": 0}
    with store.lock():
        driver = browser.make_driver(cfg)
        try:
            browser.ensure_logged_in(driver, cfg, interactive=sys.stdin.isatty())
            direct.check_account(driver)
            state = store.load_posts()
            found = find(driver, cfg, cutoff)
            if not dry_run:
                remember(state, found, now)
                store.save_posts(state)
            todo, skipped = select(found, state, cfg, now)
            totals["skipped"] = len(skipped)
            for p, why in skipped:
                if not why.startswith("older"):
                    print(f"[=] {p['url']}: {why}")
            print(f"[+] {len(todo)} posts to read (of {len({p['shortcode'] for p in found})} found; "
                  f"lookback {settings(cfg)['lookback_days']} days)")
            fails = 0
            for n, p in enumerate(todo, 1):
                print(f"\n[{n}/{len(todo)}] {p['url']} ({p['source']}, posted {p.get('taken_at') or 'date unknown'})")
                if dry_run:
                    continue
                try:
                    got = comments.read_post(driver, cfg, p["url"])
                except AccountBlocked:
                    raise
                except Exception as exc:                  # a dead page or a bug, not Instagram refusing us
                    fails, totals["failed"] = fails + 1, totals["failed"] + 1
                    print(f"    [!] couldn't read it ({type(exc).__name__}: {exc}); tried again next run")
                    if fails >= MAX_FAILS_IN_A_ROW:
                        print(f"[stopped reading] {fails} posts in a row failed; check the browser and the post links")
                        break
                    continue
                fails = 0
                mark_read(state, p, got, now)
                store.save_posts(state)
                totals["read"], totals["new"] = totals["read"] + 1, totals["new"] + got["new"]
                if n < len(todo):
                    browser.pause(cfg)
        except AccountBlocked as exc:
            raise browser.Halt(f"account-wide problem while reading posts ({exc.kind}): {exc}")
        finally:
            driver.quit()
    if not dry_run:
        print(f"[done] read {totals['read']} posts, {totals['new']} new commenters"
              + (f", {totals['failed']} posts failed" if totals["failed"] else ""))
    return totals


def print_posts(cfg: dict) -> None:
    """What data/posts.json remembers, newest first, and which are still within the lookback."""
    state, now = store.load_posts(), datetime.now()
    cutoff = now - timedelta(days=settings(cfg)["lookback_days"])
    rows = sorted(state.items(), key=lambda kv: dated({"shortcode": kv[0], **kv[1]}, state, now), reverse=True)
    for code, s in rows:
        live = dated({"shortcode": code, **s}, state, now) >= cutoff
        print(f"  {'*' if live else ' '} {s.get('url', code):<48} {s.get('source', ''):<18} posted "
              f"{(s.get('taken_at') or '?')[:16]:<16}  read {s.get('reads', 0)}x, last {(s.get('last_read') or 'never')[:16]}, "
              f"{s.get('commenters', '?')} commenters")
    print(f"{len(rows)} posts remembered; * = within discover.lookback_days ({settings(cfg)['lookback_days']}), "
          f"read again on each run")


if __name__ == "__main__":
    import argparse
    argparse.ArgumentParser(description="Dry run: open Chrome and list which recent posts the daily run would read "
                                        "(and why others are skipped). Reads only; nothing is sent.").parse_args()
    try:
        run(json.loads(CONFIG.read_text()), dry_run=True)
    except browser.Halt as stop:
        print(f"[stopped] {stop}")
        sys.exit(2)
