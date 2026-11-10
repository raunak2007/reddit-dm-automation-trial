"""scrape.py - collect recent Reddit posts from students and parents who may want admissions, essay or scholarship help.

Reads Reddit's own JSON listings with the logged-in Chrome profile (browser.py), for every subreddit in
config.json's search.subreddits:
    - with search.read_new_feeds, the subreddit's newest posts      /r/<sub>/new.json
    - a newest-first search inside it for every search.keywords     /r/<sub>/search.json?q=<kw>&restrict_sr=on
Each listing is paged (100 posts a page) until a page reaches posts older than lookback_days, Reddit has no more,
or max_pages_per_listing is reached; max_requests_per_run caps the whole run. Posts are merged into
data/leads.json by post id, so re-running only adds what's new (and a post edited since is scored again).
Posts by [deleted] or skip_authors accounts, by this account, stickied or moderator posts, removed posts and
(with skip_nsfw) NSFW posts are left out. Nothing is posted, voted on or sent.

Before the first listing it checks which account is logged in (the root config.json's sender_username, when set).
It stops the whole run, keeping what it has, on anything that points at the account: logged out, rate-limited
(429, "you're doing that too much"), a 403 that isn't about one subreddit, a block page or any page that isn't
JSON (browser.fetch_json). A private, banned or missing subreddit only skips that listing. Requests are spaced
browser.min/max_request_delay_seconds apart (3-7 s by default).

    python scrape.py [--limit-requests N]
"""
from __future__ import annotations

import argparse
import sys
import urllib.parse
from datetime import datetime, timedelta

import browser
import store
from send_dm import AccountBlocked

OLD = "https://old.reddit.com"


def search_window(days: int) -> str:
    """Reddit search's t= (the smallest of its windows that covers the lookback)."""
    for limit, t in ((1, "day"), (7, "week"), (31, "month"), (365, "year")):
        if days <= limit:
            return t
    return "all"


def listings(cfg: dict):
    """(label, url) for every listing to read, without the paging parameter."""
    s = cfg["search"]
    t = search_window(s["lookback_days"])
    for sub in s["subreddits"]:
        sub = sub.strip().removeprefix("r/").strip("/")
        if s.get("read_new_feeds", True):
            yield f"r/{sub} new", f"{OLD}/r/{sub}/new.json?limit=100&raw_json=1"
        for kw in s["keywords"]:
            q = urllib.parse.quote(kw)
            yield (f"r/{sub} search {kw!r}",
                   f"{OLD}/r/{sub}/search.json?q={q}&restrict_sr=on&sort=new&t={t}&limit=100&raw_json=1")


def parse_post(child: dict, cfg: dict, sender: str | None = None) -> dict | None:
    """One listing child as the fields a lead keeps, or None if it isn't a post worth keeping."""
    if not isinstance(child, dict) or child.get("kind") != "t3":
        return None
    d = child.get("data") or {}
    author = (d.get("author") or "").strip()
    skip = {a.lower() for a in cfg["search"].get("skip_authors", [])} | {"[deleted]", "automoderator"}
    if sender:
        skip.add(sender.lower())
    if not author or author.lower() in skip or not d.get("id"):
        return None
    body = d.get("selftext") or ""
    if (d.get("stickied") or d.get("distinguished") or d.get("removed_by_category")
            or body.strip() in ("[removed]", "[deleted]")):
        return None
    if d.get("over_18") and cfg["search"].get("skip_nsfw", True):
        return None
    created = float(d.get("created_utc") or 0)
    return {
        "post_id": d.get("name") or f"t3_{d['id']}",
        "author": author,
        "subreddit": d.get("subreddit") or "",
        "title": (d.get("title") or "").strip(),
        "text": body.strip(),
        "flair": d.get("link_flair_text") or "",
        "permalink": store.REDDIT + d["permalink"] if d.get("permalink") else "",
        "created_utc": created,
        "created_at": datetime.fromtimestamp(created).isoformat(timespec="minutes") if created else None,
        "num_comments": d.get("num_comments", 0),
    }


def merge(leads: dict, post: dict, label: str) -> bool:
    """Add or update one post in leads. Returns True if it is new. A post whose title or text changed since it was
    scored loses its "ai" block, so qualify.py scores it again."""
    lead = leads.get(post["post_id"])
    new = lead is None
    if new:
        lead = leads[post["post_id"]] = {"found_at": datetime.now().isoformat(timespec="seconds"), "sources": []}
    elif (lead.get("title"), lead.get("text")) != (post["title"], post["text"]):
        lead.pop("ai", None)
    lead.update(post)
    if label not in lead["sources"]:
        lead["sources"].append(label)
    return new


def read_listing(driver, cfg: dict, label: str, url: str, leads: dict, cutoff: float, sender: str | None,
                 budget: dict, fetch=None) -> tuple[int, int]:
    """Page through one newest-first listing, merging posts newer than cutoff (epoch seconds) into leads.
    budget: {"requests": how many are left this run}. Returns (posts in range, new leads)."""
    fetch = fetch or browser.fetch_json
    seen, new, after = 0, 0, None
    for page in range(cfg["search"]["max_pages_per_listing"]):
        if budget["requests"] <= 0:
            break
        if page:
            browser.pause(cfg)
        budget["requests"] -= 1
        data = fetch(driver, url + (f"&after={after}" if after else ""))
        try:
            listing = data["data"]
            children = listing["children"]
        except (KeyError, TypeError):
            raise browser.PageSkipped(f"{url}: not a listing") from None
        oldest = None
        for child in children:
            post = parse_post(child, cfg, sender)
            created = float(((child or {}).get("data") or {}).get("created_utc") or 0)
            oldest = created if oldest is None else min(oldest, created)
            if post is None or post["created_utc"] < cutoff:
                continue
            seen += 1
            new += merge(leads, post, label)
        after = listing.get("after")
        if not after or not children or (oldest is not None and oldest < cutoff):
            break
    return seen, new


def run(cfg: dict, cfg_root: dict | None = None, limit_requests: int | None = None, driver=None) -> dict:
    """Read every listing. Returns {"new": n, "stopped": why it ended early or '', "halted": True if that was an
    account-wide problem}. driver: an open browser to use (and leave open); otherwise one is opened and closed."""
    cfg_root = store.load_root_config() if cfg_root is None else cfg_root
    cutoff = (datetime.now() - timedelta(days=cfg["search"]["lookback_days"])).timestamp()
    todo = list(listings(cfg))
    if not todo:
        sys.exit("Nothing to read: add search.subreddits to reddit/config.json")
    budget = {"requests": min(limit_requests or 10 ** 9, cfg["search"]["max_requests_per_run"])}
    leads = store.load_leads()
    own = driver is None
    driver = browser.make_driver(cfg) if own else driver
    out = {"new": 0, "stopped": "", "halted": False}
    try:
        sender = browser.check_login(driver, cfg_root)
        print(f"[+] reading as u/{sender}")
        for i, (label, url) in enumerate(todo, 1):
            if budget["requests"] <= 0:
                out["stopped"] = f"used max_requests_per_run ({cfg['search']['max_requests_per_run']})"
                break
            browser.pause(cfg)
            try:
                seen, new = read_listing(driver, cfg, label, url, leads, cutoff, sender, budget)
            except browser.PageSkipped as exc:
                print(f"[=] {label}: skipped ({exc})")
                continue
            finally:
                store.save_leads(leads)
            out["new"] += new
            print(f"[{i}/{len(todo)}] {label}: {seen} posts in range, {new} new")
    except AccountBlocked as exc:
        out["stopped"], out["halted"] = f"account-wide problem ({exc.kind}): {exc}", True
        print(f"[stopped] {out['stopped']}. Kept everything read so far. Look at the account, and wait a day "
              f"before scraping again if Reddit has limited it.")
    except KeyboardInterrupt:
        out["stopped"] = "interrupted"
        print("\n[+] Interrupted; what was read is saved.")
    finally:
        store.save_leads(leads)
        if own:
            driver.quit()
    print(f"[done] {out['new']} new posts; {len(leads)} in data/leads.json")
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit-requests", type=int, help="make at most N requests (for a quick test)")
    args = ap.parse_args()
    sys.exit(2 if run(store.load_config(), limit_requests=args.limit_requests)["halted"] else 0)


if __name__ == "__main__":
    main()
