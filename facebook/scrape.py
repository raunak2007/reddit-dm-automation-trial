"""scrape.py - collect recent Facebook posts that may be about admissions, scholarships or internships.

Reads, with the logged-in profile from browser.py:
    - Facebook's post search for every config.json search.keywords entry, filtered to recent posts
    - the newest-first feed of every search.groups entry (group URLs or ids; you must be a member)
    - with search.search_inside_groups, each group's own search for every keyword

Each page is scrolled until it stops loading new posts, max_scrolls_per_page is reached, or
stop_after_old_posts posts in a row are older than lookback_days. Posts are merged into
data/leads.json by post id, so re-running only adds what's new. Nothing is posted or sent.

It stops the whole run (keeping what it has) if Facebook shows a checkpoint, a "temporarily blocked"
notice or the login page: carrying on through those is how accounts get restricted.

    python scrape.py [--keywords-only | --groups-only] [--limit-pages N]
"""
from __future__ import annotations

import argparse
import base64
import json
import re
import sys
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path

import browser
import store

HERE = Path(__file__).resolve().parent
CONFIG = HERE / "config.json"
BLOCKED = re.compile(r"you.re temporarily blocked|temporarily restricted|you can.t use this feature|"
                     r"we suspended your account|confirm your identity|checkpoint", re.I)
_compact = dict(separators=(",", ":"))
RECENT_FILTER = base64.b64encode(json.dumps(
    {"recent_posts:0": json.dumps({"name": "recent_posts", "args": ""}, **_compact)}, **_compact).encode()).decode()
DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

# Reads the posts on the page. Feed posts are the children of [role=feed] (group feeds) or
# top-level [role=article]s (search); [role=article]s nested inside a post are its comments.
_EXTRACT = r"""
const PERMALINK = /\/(posts|permalink)\/|story_fbid=|\/groups\/[^/]+\/posts\/|multi_permalinks=/;
const PROFILE = /\/groups\/[^/]+\/user\/\d+|profile\.php\?id=\d+|facebook\.com\/(?!groups\/|events\/|watch|hashtag|photo|share|reel)[A-Za-z0-9.]{3,}\/?(\?|$)|\/people\//;
const out = [];
const feedPosts = [...document.querySelectorAll('[role="feed"] > div')].filter(d => !d.parentElement.closest('[role="article"]'));
const roots = feedPosts.concat([...document.querySelectorAll('[role="article"]')].filter(a =>
  !a.parentElement.closest('[role="article"]') && !feedPosts.some(f => f.contains(a))));
for (const post of roots) {
  // "See more" expands truncated text in place
  for (const b of post.querySelectorAll('[role="button"]')) {
    if (/^see more$/i.test(b.innerText.trim())) { try { b.click(); } catch (e) {} }
  }
  const comments = [...post.querySelectorAll('[role="article"]')].filter(c => c !== post && c.parentElement.closest('[role="article"]'));
  const inComment = el => comments.some(c => c.contains(el));
  const msg = post.querySelector('[data-ad-preview="message"], [data-ad-comet-preview="message"], [data-ad-rendering-role="story_message"]');
  let text = msg ? msg.innerText : '';
  if (!text) {
    const parts = [...post.querySelectorAll('div[dir="auto"]')].filter(d => !inComment(d)).map(d => d.innerText.trim());
    text = [...new Set(parts)].filter(t => t.length > 20).join('\n');
  }
  const links = [...post.querySelectorAll('a[href]')].filter(a => !inComment(a));
  const nameEl = post.querySelector('[data-ad-rendering-role="profile_name"] a, h2 a, h3 a, h4 a, strong a');
  const author = nameEl ? nameEl.innerText.trim() : '';
  const authorHref = nameEl && PROFILE.test(nameEl.href) ? nameEl.href
                   : (links.find(a => PROFILE.test(a.href) && a.innerText.trim() === author) || {}).href || '';
  const perma = links.find(a => PERMALINK.test(a.href));
  const groupA = links.find(a => /\/groups\/[^/]+\/?(\?|$)/.test(a.href) && a.innerText.trim().length > 2);
  const timeEl = perma ? (perma.querySelector('[aria-label]') || perma) : null;
  out.push({
    author, author_href: authorHref,
    post_url: perma ? perma.href.split('&__cft__')[0].split('?__cft__')[0] : '',
    time_label: timeEl ? (timeEl.getAttribute('aria-label') || timeEl.innerText || '').trim() : '',
    group: groupA ? groupA.innerText.trim() : '',
    text: text.trim(),
  });
}
return out;
"""

UNITS = {"m": "minutes", "min": "minutes", "mins": "minutes", "minute": "minutes", "minutes": "minutes",
         "h": "hours", "hr": "hours", "hrs": "hours", "hour": "hours", "hours": "hours",
         "d": "days", "day": "days", "days": "days", "w": "weeks", "wk": "weeks", "week": "weeks", "weeks": "weeks",
         "y": "years", "yr": "years", "year": "years", "years": "years"}


def parse_time(label: str, now: datetime | None = None) -> datetime | None:
    """Facebook's timestamp text ("3h", "2 days", "Yesterday at 4:05 PM", "October 3 at 5:00 PM",
    "Oct 3, 2025") as a datetime, or None when it can't be read (Facebook sometimes scrambles it)."""
    now = now or datetime.now()
    s = re.sub(r"\s+", " ", label or "").strip().lower()
    if not s:
        return None
    if s.startswith("just now") or s == "now":
        return now
    m = re.match(r"^(\d+)\s*([a-z]+)\b", s)
    if m and m.group(2) in UNITS:
        n, unit = int(m.group(1)), UNITS[m.group(2)]
        return now - (timedelta(days=365 * n) if unit == "years" else timedelta(**{unit: n}))
    if s.startswith("yesterday"):
        return now - timedelta(days=1)
    s = re.sub(r"\s+at\s+.*$", "", s).replace(",", "")
    if s in DAYS:                             # "Tuesday": within the last week
        return now - timedelta(days=(now.weekday() - DAYS.index(s)) % 7 or 7)
    for fmt in ("%B %d %Y", "%b %d %Y", "%B %d", "%b %d", "%d %B %Y", "%d %B"):
        try:
            d = datetime.strptime(s, fmt)
        except ValueError:
            continue
        if "%Y" not in fmt:
            d = d.replace(year=now.year)
            if d > now:
                d = d.replace(year=now.year - 1)
        return d
    return None


def group_base(entry: str) -> str:
    entry = entry.strip().rstrip("/")
    if entry.startswith("http"):
        m = re.search(r"facebook\.com/groups/([^/?#]+)", entry)
        if not m:
            raise SystemExit(f"Not a Facebook group URL: {entry}")
        entry = m.group(1)
    return f"{store.FB}/groups/{entry}"


def pages(cfg: dict, keywords_only=False, groups_only=False):
    """(label, url) for every page to read."""
    s = cfg["search"]
    if not groups_only:
        for kw in s["keywords"]:
            q = urllib.parse.quote(kw)
            yield f"search {kw!r}", f"{store.FB}/search/posts/?q={q}&filters={RECENT_FILTER}"
    if not keywords_only:
        for g in s["groups"]:
            base = group_base(g)
            yield f"group feed {base}", f"{base}/?sorting_setting=CHRONOLOGICAL"
            if s.get("search_inside_groups"):
                for kw in s["keywords"]:
                    yield f"group search {kw!r} in {base}", f"{base}/search/?q={urllib.parse.quote(kw)}"


def check_blocked(driver):
    text = driver.execute_script("return (document.body && document.body.innerText || '').slice(0, 4000)")
    if "checkpoint" in driver.current_url or BLOCKED.search(text or ""):
        raise RuntimeError(f"Facebook is showing a block / checkpoint page at {driver.current_url}")
    if not browser.logged_in(driver):
        raise RuntimeError("Facebook logged this profile out")


def read_page(driver, cfg: dict, label: str, url: str, leads: dict, cutoff: datetime) -> tuple[int, int]:
    """Scroll one page, merging posts into leads. Returns (posts seen, new leads)."""
    s = cfg["search"]
    driver.get(url)
    browser.pause(cfg)
    check_blocked(driver)
    seen, new, old_in_a_row, idle = set(), 0, 0, 0
    for _ in range(s["max_scrolls_per_page"]):
        before = len(seen)
        for p in driver.execute_script(_EXTRACT):
            if len(p["text"]) < 25:
                continue
            key = store.post_key(p["post_url"], p["author"], p["text"])
            if key in seen:
                continue
            seen.add(key)
            posted = parse_time(p["time_label"])
            if posted and posted < cutoff:
                old_in_a_row += 1
                continue
            old_in_a_row = 0
            if not posted and not s.get("keep_undated_posts", True):
                continue
            akey = store.author_key(p["author_href"])
            lead = leads.get(key)
            if lead is None:
                new += 1
                lead = leads[key] = {"post_key": key, "found_at": datetime.now().isoformat(timespec="seconds"),
                                     "sources": []}
            lead.update(author=p["author"], author_key=akey or lead.get("author_key"),
                        author_url=store.profile_url(akey) if akey else lead.get("author_url", ""),
                        post_url=p["post_url"] or lead.get("post_url", ""), group=p["group"] or lead.get("group", ""),
                        time_label=p["time_label"], posted_at=posted.isoformat(timespec="minutes") if posted else None)
            if len(p["text"]) >= len(lead.get("text", "")):
                if lead.get("text") and lead["text"] != p["text"]:
                    lead.pop("ai", None)                # text grew ("See more"): score it again
                lead["text"] = p["text"]
            if label not in lead["sources"]:
                lead["sources"].append(label)
        if old_in_a_row >= s["stop_after_old_posts"]:
            break
        idle = idle + 1 if len(seen) == before else 0
        if idle >= 3:
            break
        driver.execute_script("window.scrollBy(0, window.innerHeight * (0.8 + Math.random() * 0.6))")
        browser.pause(cfg, 0.45)
        check_blocked(driver)
    return len(seen), new


def run(cfg: dict, keywords_only=False, groups_only=False, limit_pages: int | None = None) -> int:
    cutoff = datetime.now() - timedelta(days=cfg["search"]["lookback_days"])
    todo = list(pages(cfg, keywords_only, groups_only))[:limit_pages]
    if not todo:
        sys.exit("Nothing to read: add search.keywords or search.groups to config.json")
    leads = store.load_leads()
    driver = browser.make_driver(cfg)
    total_new = 0
    try:
        browser.ensure_logged_in(driver, cfg, interactive=True)
        for i, (label, url) in enumerate(todo, 1):
            try:
                seen, new = read_page(driver, cfg, label, url, leads, cutoff)
            except RuntimeError as exc:
                print(f"[stopped] {exc}. Kept everything read so far. Look at the window, and wait a day "
                      f"before scraping again if Facebook has limited the account.")
                break
            except Exception as exc:                  # one odd page shouldn't lose the whole run
                print(f"[!] {label}: {type(exc).__name__}: {exc}")
                continue
            finally:
                store.save_leads(leads)
            total_new += new
            print(f"[{i}/{len(todo)}] {label}: {seen} posts in range, {new} new")
            browser.pause(cfg)
    except KeyboardInterrupt:
        print("\n[+] Interrupted; what was read is saved.")
    finally:
        store.save_leads(leads)
        driver.quit()
    print(f"[done] {total_new} new posts; {len(leads)} in data/leads.json")
    return total_new


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    which = ap.add_mutually_exclusive_group()
    which.add_argument("--keywords-only", action="store_true")
    which.add_argument("--groups-only", action="store_true")
    ap.add_argument("--limit-pages", type=int, help="read only the first N pages (for a quick test)")
    args = ap.parse_args()
    run(json.loads(CONFIG.read_text()), args.keywords_only, args.groups_only, args.limit_pages)


if __name__ == "__main__":
    main()
