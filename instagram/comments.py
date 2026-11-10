"""comments.py - read everyone who commented on one Instagram post or reel.

    python comments.py https://www.instagram.com/p/<code>/

Opens the post in the logged-in Chrome profile and pages through its comments with the same web endpoint
the Instagram page itself uses (so you get usernames, display names and full comment text), falling back
to reading the page if that endpoint doesn't answer. The post's owner, your own account and repeat
commenters are dropped. Results are merged into data/leads.json, tagged with the post. Nothing is
followed, liked or sent. If Instagram logs the profile out, asks for a security check or rate-limits it, it
stops at once with exit code 2. discover.py (python run.py) calls read_post for each recent post.
"""
from __future__ import annotations

import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

import browser
import direct
import store
from send_dm import AccountBlocked

HERE = Path(__file__).resolve().parent
CONFIG = HERE / "config.json"
SHORTCODE = re.compile(r"instagram\.com/(?:[A-Za-z0-9._]+/)?(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)")
ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
APP_ID = "936619743392459"       # Instagram web's public app id, sent by instagram.com on every API call

# Runs in the post's page: one page of comments from the web API, with the session's own cookies.
_FETCH = r"""
const [url, appId, done] = [arguments[0], arguments[1], arguments[arguments.length - 1]];
const csrf = (document.cookie.match(/csrftoken=([^;]+)/) || [])[1] || '';
fetch(url, {credentials: 'include', headers: {'X-IG-App-ID': appId, 'X-CSRFToken': csrf, 'X-Requested-With': 'XMLHttpRequest'}})
  .then(r => r.text().then(t => done({status: r.status, body: t})))
  .catch(e => done({status: 0, body: String(e)}));
"""
# The same for the few read-only endpoints Instagram web asks with a form POST (READ_POSTS), e.g. clips/user/.
_FETCH_FORM = r"""
const [url, appId, form, done] = [arguments[0], arguments[1], arguments[2], arguments[arguments.length - 1]];
const csrf = (document.cookie.match(/csrftoken=([^;]+)/) || [])[1] || '';
fetch(url, {method: 'POST', body: form, credentials: 'include',
            headers: {'X-IG-App-ID': appId, 'X-CSRFToken': csrf, 'X-Requested-With': 'XMLHttpRequest',
                      'Content-Type': 'application/x-www-form-urlencoded'}})
  .then(r => r.text().then(t => done({status: r.status, body: t})))
  .catch(e => done({status: 0, body: String(e)}));
"""
# Endpoints that only read but that Instagram web asks with a POST. api() refuses a POST to anything else, so
# nothing here can like, follow, accept or mark seen.
READ_POSTS = ("clips/user/",)

# Fallback: read the comments shown on the page. Each comment has a link to /<username>/ whose text is
# the username, inside a block that also holds a <time>.
_DOM = r"""
const out = [];
for (const a of document.querySelectorAll('a[href^="/"]')) {
  const m = a.getAttribute('href').match(/^\/([A-Za-z0-9._]+)\/$/);
  if (!m || a.innerText.trim() !== m[1]) continue;
  let box = a;
  for (let i = 0; i < 8 && box && !box.querySelector('time'); i++) box = box.parentElement;
  if (!box) continue;
  const spans = [...box.querySelectorAll('span[dir="auto"], span')].map(s => s.innerText.trim())
    .filter(t => t && t !== m[1] && !/^(reply|see translation|\d+[smhdw]|\d+ likes?|like)$/i.test(t));
  out.push({username: m[1], text: spans.sort((x, y) => y.length - x.length)[0] || ''});
}
return out;
"""
_MORE = r"""
let n = 0;
for (const el of document.querySelectorAll('[aria-label="Load more comments"], [role="button"], button')) {
  const t = (el.getAttribute('aria-label') || el.innerText || '').trim();
  if (/^load more comments$|^view (all )?\d+ (more )?repl|^view more comments/i.test(t)) { el.click(); n++; }
}
const first = document.querySelector('ul li time');
let s = first;
while (s && s.scrollHeight <= s.clientHeight + 5) s = s.parentElement;
if (s && s !== document.documentElement) { s.scrollTop = s.scrollHeight; n++; }
return n;
"""


def shortcode(url: str) -> str:
    m = SHORTCODE.search(url)
    if not m:
        sys.exit(f"That doesn't look like an Instagram post or reel link: {url}")
    return m.group(1)


def media_id(driver, code: str) -> str:
    """The numeric id the API wants: from the page's app-link meta tag, else decoded from the shortcode."""
    found = driver.execute_script(
        "const m = document.querySelector('meta[property=\"al:ios:url\"]');"
        "return m ? (m.content.match(/id=(\\d+)/) || [])[1] : null;")
    if found:
        return found
    n = 0
    for ch in code[:11]:
        n = n * 64 + ALPHABET.index(ch)
    return str(n)


def owner(driver) -> str | None:
    """The post owner's username, from the page's og:url / header link."""
    return driver.execute_script(r"""
      const m = (document.querySelector('meta[property="og:url"]') || {}).content || '';
      const fromUrl = (m.match(/instagram\.com\/([A-Za-z0-9._]+)\/(?:p|reel)\//) || [])[1];
      if (fromUrl) return fromUrl;
      const h = document.querySelector('header a[href^="/"][role="link"]');
      return h ? h.getAttribute('href').replaceAll('/', '') : null;""")


def api(driver, path: str, form: dict | None = None) -> dict | None:
    """GET https://www.instagram.com/api/v1/<path> from inside the logged-in page (the page must be on
    instagram.com), with the session's own cookies. The parsed JSON, or None if the endpoint didn't answer
    usefully. Raises send_dm.AccountBlocked if Instagram is refusing the account (logged out, a challenge or
    checkpoint, rate-limited or action-blocked), the same signal send.py stops on. Only ever reads: with
    form, it POSTs that form instead, the way Instagram web asks a few read-only endpoints (READ_POSTS, e.g.
    clips/user/); a POST anywhere else raises ValueError, so nothing is liked, followed or marked seen."""
    url = f"https://www.instagram.com/api/v1/{path}"
    if form is None:
        r = driver.execute_async_script(_FETCH, url, APP_ID)
    else:
        if not any(path.startswith(p) for p in READ_POSTS):
            raise ValueError(f"comments.api only POSTs to read-only endpoints ({', '.join(READ_POSTS)}), not {path}")
        r = driver.execute_async_script(_FETCH_FORM, url, APP_ID, urlencode(form))
    status, head = r["status"], r["body"][:600].lower()
    if status in (401, 403) or (status != 200 and "login" in head[:300]) or '"require_login":true' in head:
        raise AccountBlocked("Instagram says this profile isn't logged in (run: python run.py login)", "login")
    if "checkpoint_required" in head or "challenge_required" in head:
        raise AccountBlocked("Instagram wants a security check (challenge) on this account; open it in the "
                             "browser and clear it before running again", "restricted")
    if status == 429 or "please wait a few minutes" in head or "feedback_required" in head:
        raise AccountBlocked("Instagram is rate-limiting this account; stop for today", "rate_limited")
    if status != 200:
        return None
    try:
        return json.loads(r["body"])
    except ValueError:
        return None


def _row(c: dict, parent: str | None = None) -> dict:
    u = c.get("user") or {}
    return {"username": u.get("username", ""), "full_name": u.get("full_name", ""), "user_id": str(u.get("pk", "")),
            "is_private": bool(u.get("is_private")), "text": c.get("text", ""), "comment_id": str(c.get("pk", "")),
            "reply_to": parent, "commented_at": datetime.fromtimestamp(c["created_at"]).isoformat(timespec="minutes")
            if c.get("created_at") else None}


def via_api(driver, mid: str, cfg: dict) -> list[dict] | None:
    ig = cfg["instagram"]
    rows, cursor = [], None
    while len(rows) < ig["max_comments"]:
        q = "can_support_threading=true&permalink_enabled=false" + (f"&min_id={cursor}" if cursor else "")
        data = api(driver, f"media/{mid}/comments/?{q}")
        if data is None:
            return rows or None
        for c in data.get("comments", []):
            rows.append(_row(c))
            if ig["include_replies"]:
                for r in c.get("preview_child_comments", []):
                    rows.append(_row(r, str(c.get("pk"))))
                if c.get("child_comment_count", 0) > len(c.get("preview_child_comments", [])):
                    rows += replies(driver, mid, str(c["pk"]))
        cursor = data.get("next_min_id")
        if not cursor or not data.get("comments"):
            break
        browser.pause(cfg, 0.4)
    return rows


def replies(driver, mid: str, cid: str) -> list[dict]:
    out, cursor = [], None
    for _ in range(10):
        data = api(driver, f"media/{mid}/comments/{cid}/child_comments/" + (f"?max_id={cursor}" if cursor else ""))
        if not data:
            break
        out += [_row(c, cid) for c in data.get("child_comments", [])]
        cursor = data.get("next_max_child_cursor")
        if not cursor or not data.get("has_more_tail_child_comments"):
            break
        time.sleep(1.5)
    return out


def via_page(driver, cfg: dict) -> list[dict]:
    idle, count = 0, 0
    for _ in range(60):
        rows = driver.execute_script(_DOM)
        idle = idle + 1 if len(rows) == count else 0
        count = len(rows)
        if idle >= 3 or count >= cfg["instagram"]["max_comments"]:
            break
        driver.execute_script(_MORE)
        browser.pause(cfg, 0.4)
    return [dict(r, full_name="", user_id="", is_private=False, comment_id="", reply_to=None, commented_at=None)
            for r in driver.execute_script(_DOM)]


def read_post(driver, cfg: dict, url: str) -> dict:
    """Read one post's commenters with a driver that is already logged in, and merge them into data/leads.json.
    Raises AccountBlocked (stop everything) if Instagram refuses the account. Returns what it found:
    {"shortcode", "owner", "comments", "commenters", "new", "how"}."""
    code = shortcode(url)
    driver.get(f"https://www.instagram.com/p/{code}/")
    browser.pause(cfg)
    direct.check_account(driver)
    post_owner = owner(driver)
    rows = via_api(driver, media_id(driver, code), cfg)
    how = "Instagram's comments endpoint"
    if rows is None:
        print("[!] The comments endpoint didn't answer; reading the page instead (slower, no display names)")
        rows, how = via_page(driver, cfg), "the page"

    skip = {(post_owner or "").lower(), cfg["instagram"]["my_username"].lower(), ""}
    by_user = {}                                       # one lead per commenter, all their comments together
    for r in rows:
        u = r["username"].lower()
        if u in skip:
            continue
        if u in by_user:
            by_user[u]["text"] += "\n" + r["text"]
        else:
            by_user[u] = dict(r)
    leads, new = store.load_leads(), 0
    for u, r in by_user.items():
        key = store.lead_key(code, u)
        if key not in leads:
            new += 1
            leads[key] = {"key": key, "post": url, "shortcode": code, "post_owner": post_owner,
                          "found_at": datetime.now().isoformat(timespec="seconds")}
        elif leads[key].get("text") != r["text"]:
            leads[key].pop("ai", None)                 # new comments from them: score again
        leads[key].update(r)
    store.save_leads(leads)
    print(f"[done] {len(rows)} comments from {how}; {len(by_user)} commenters, {new} new (post by @{post_owner})")
    return {"shortcode": code, "owner": post_owner, "comments": len(rows), "commenters": len(by_user), "new": new,
            "how": how}


def run(cfg: dict, url: str) -> int:
    """Read one post in its own browser session. Exits with code 2 (browser.Halt) if Instagram refuses the
    account. Returns how many new commenters it found."""
    shortcode(url)                                     # a bad link stops before Chrome opens
    import apimode
    if apimode.is_api(cfg):
        return apimode.read_link(cfg, url)
    driver = browser.make_driver(cfg)
    try:
        browser.ensure_logged_in(driver, cfg, interactive=sys.stdin.isatty())
        return read_post(driver, cfg, url)["new"]
    except AccountBlocked as exc:
        raise browser.Halt(f"{exc} ({exc.kind})")
    finally:
        driver.quit()


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Read everyone who commented on one post or reel into data/leads.json. "
                                             "Reads only; nothing is followed, liked or sent.")
    ap.add_argument("url", help="the post or reel link")
    args = ap.parse_args()                         # before Chrome opens, so --help only prints help
    try:
        run(json.loads(CONFIG.read_text()), args.url)
    except browser.Halt as stop:
        print(f"[stopped] {stop}")
        sys.exit(2)
