"""insights/sources.py - which places to look for leads are working, and where else to look. Suggestions only.

    python insights/sources.py                  write insights/reports/sources-<date>.md and print where it is
    python insights/sources.py --no-search      offline: no Reddit requests (no new subreddit candidates)
    python insights/sources.py --no-ai          no Claude call (no ranked recommendation at the top)
    python insights/sources.py --min 10         DMs a source needs before "no replies" counts as a finding
    python insights/sources.py --max-requests 24   Reddit requests this run may make at most

The report has:
    Performance        every current source (subreddit, Reddit keyword, Facebook keyword and group, Instagram
                       account and post): posts found, qualified, DMed, replied, booked. From each platform's
                       leads.json and outcomes.py (nothing on any site). Flags sources that bring leads but no
                       replies over --min DMs, ones that bring nothing, and ones whose sample is too small.
    New subreddits     Reddit's own subreddit search (/subreddits/search.json) for a few topics built from the
                       business services and the current keywords, through the logged-in Reddit profile and
                       reddit/browser.py's fetch_json, spaced like the scraper. Each candidate's subscribers and a
                       /r/<sub>/new.json sample (posts per day, share matched by the current keywords). NSFW,
                       quarantined, private, tiny, quiet and already configured ones are left out. It only reads:
                       nothing is joined, followed or posted, and it stops at once (exit 2, keeping what it has)
                       on anything account-wide (logged out, rate-limited, blocked), like scrape.py.
    New keywords       words and phrases common in posts that got replies or high scores but not in
                       search.keywords, for Reddit and Facebook.
    Facebook groups    Facebook isn't touched: phrases to type into Facebook's group search, based on what works,
                       the groups that keyword searches turned up, and the configured groups by performance.
    Config snippets    ready-to-paste JSON for the changes it suggests. No config file is ever changed.
With ANTHROPIC_API_KEY (environment, or any platform's .env) Claude adds a short ranked add / drop / watch list at
the top. daily.py runs this weekly.
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sys
import time
import urllib.parse
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

import outcomes  # noqa: E402
from send_dm import AccountBlocked  # noqa: E402

REPORTS = HERE / "reports"
CONFIGS = {p: ROOT / p / "config.json" for p in outcomes.PLATFORMS}
OLD = "https://old.reddit.com"
HIGH_SCORE = 80                 # a lead at or above this counts as "worked" for keyword mining, replied or not
MIN_SUBSCRIBERS = 2000          # smaller subreddits aren't worth a slot
MIN_POSTS_PER_DAY = 1.0         # quieter ones are "low activity"
MAX_TOPICS = 8
MAX_SAMPLES = 12
NEEDS_YOU = 2
STOP = set("""a an the and or but if of to in on at for with from by about as is are was were be been being am i me my
we our you your he she they them their it its this that these those what which who whom how why when where there here
so do does did doing have has had having can could would should will just not no yes any all some more most very
really also too than then into out up down over again any one get got im ive dont cant wont didnt doesnt its
anyone someone something thing things know think want like make made going go much many way lot even still well
r u s t m d re ve ll please thanks thank hi hello guys question next last week weeks month months year years day days
today tomorrow tonight time mess pretty sure""".split())
INNER_OK = {"of", "for", "my", "to", "and", "on", "in"}     # stopwords allowed inside a phrase ("review my essay")
# Process words in the service descriptions that make poor subreddit-search topics on their own.
SERVICE_FILLER = {"1:1", "search", "applications", "resumes", "outreach", "coaching", "counseling", "list",
                  "strategy", "timelines"}


# ----------------------------------------------------------------- reading what's there
def load_configs() -> dict:
    out = {}
    for p, path in CONFIGS.items():
        try:
            out[p] = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            out[p] = {}
    return out


def norm_sub(name: str) -> str:
    return (name or "").strip().removeprefix("/").removeprefix("r/").strip("/").lower()


def group_id(entry: str) -> str:
    """A Facebook group URL or id as the id (or slug) alone, lowercased."""
    entry = (entry or "").strip().rstrip("/")
    m = re.search(r"facebook\.com/groups/([^/?#]+)", entry)
    return (m.group(1) if m else entry).lower()


def _kw(text: str) -> str | None:
    """The keyword out of a scrape label's {kw!r}."""
    try:
        v = ast.literal_eval(text.strip())
        return v.strip().lower() if isinstance(v, str) else None
    except (ValueError, SyntaxError):
        return None


def tags(platform: str, lead: dict) -> list[tuple[str, str]]:
    """(kind, normalised name) of every source that found this lead."""
    out = set()
    labels = lead.get("sources") or []
    if platform == "reddit":
        if lead.get("subreddit"):
            out.add(("subreddit", norm_sub(lead["subreddit"])))
        for label in labels:
            m = re.match(r"^r/\S+ search (.+)$", label)
            if m and _kw(m.group(1)):
                out.add(("keyword", _kw(m.group(1))))
    elif platform == "facebook":
        for label in labels:
            m = re.match(r"^group search (.+) in (\S+)$", label)
            if m:
                if _kw(m.group(1)):
                    out.add(("keyword", _kw(m.group(1))))
                out.add(("group", group_id(m.group(2))))
                continue
            m = re.match(r"^group feed (\S+)$", label)
            if m:
                out.add(("group", group_id(m.group(1))))
                continue
            m = re.match(r"^search (.+)$", label)
            if m and _kw(m.group(1)):
                out.add(("keyword", _kw(m.group(1))))
        if lead.get("group") and not any(k == "group" for k, _ in out):
            out.add(("group seen", lead["group"].strip()))      # a group a keyword search turned up
    else:
        if lead.get("post_owner"):
            out.add(("account", "@" + lead["post_owner"].strip().lower().removeprefix("@")))
        if lead.get("post"):
            out.add(("post", lead["post"].strip()))
    return sorted(out)


def lead_person(platform: str, lead: dict) -> str:
    who = lead.get("author" if platform == "reddit" else outcomes.PERSON[platform]) or lead.get("author_key") or ""
    return outcomes.key(platform, who)


def leads(platform: str, cfg: dict) -> list[dict]:
    """Every lead in the platform's leads.json as {person, tags, qualified, score, text}."""
    raw = outcomes._json(outcomes.FILES[platform]["leads"], {})
    min_score = (cfg.get("ai") or {}).get("min_score", 60)
    out = []
    for lead in (raw.values() if isinstance(raw, dict) else raw):
        if not isinstance(lead, dict):
            continue
        ai = lead.get("ai") or {}
        score = ai.get("score")
        out.append({"person": lead_person(platform, lead), "tags": tags(platform, lead), "score": score,
                    "qualified": bool(ai) and ai.get("relevant", True) and score is not None and score >= min_score,
                    "texts": [str(lead.get(k) or "") for k in ("title", "text") if lead.get(k)]})
    return out


def configured(platform: str, cfg: dict) -> list[tuple[str, str, str]]:
    """(kind, normalised name, name as written in the config) of every source the config names."""
    s = cfg.get("search") or {}
    if platform == "reddit":
        return ([("subreddit", norm_sub(x), x) for x in s.get("subreddits", [])]
                + [("keyword", x.strip().lower(), x) for x in s.get("keywords", [])])
    if platform == "facebook":
        return ([("keyword", x.strip().lower(), x) for x in s.get("keywords", [])]
                + [("group", group_id(x), x) for x in s.get("groups", [])])
    d = cfg.get("discover") or {}
    mine = (cfg.get("instagram") or {}).get("my_username")
    accounts = ([mine] if mine else []) + list(d.get("accounts", []))
    return ([("account", "@" + a.strip().lower().removeprefix("@"), "@" + a.strip().removeprefix("@")) for a in accounts]
            + [("post", x.strip(), x) for x in d.get("posts", [])])


# ----------------------------------------------------------------- performance
def performance(platform: str, cfg: dict, min_n: int) -> list[dict]:
    """One row per source: kind, source, configured, found, qualified, dmed, replied, booked, flag."""
    ls = leads(platform, cfg)
    ps = {p["person"]: p for p in outcomes.people(platform)}
    rows: dict[tuple, dict] = {}
    for kind, k, shown in configured(platform, cfg):
        rows[(kind, k)] = {"kind": kind, "source": shown, "configured": True, "found": 0, "qualified": 0,
                           "_people": set()}
    for lead in ls:
        for kind, k in lead["tags"]:
            r = rows.setdefault((kind, k), {"kind": kind, "source": k if kind != "subreddit" else f"r/{k}",
                                            "configured": False, "found": 0, "qualified": 0, "_people": set()})
            r["found"] += 1
            r["qualified"] += lead["qualified"]
            if lead["person"]:
                r["_people"].add(lead["person"])
    total_found = len(ls)
    out = []
    for r in rows.values():
        people = [ps[x] for x in r.pop("_people") if x in ps]
        sent = [p for p in people if p["first_sent_at"]]
        r.update(dmed=len(sent), replied=sum(1 for p in sent if p["replied_at"]),
                 booked=sum(1 for p in people if p["booked_at"]))
        if platform == "reddit" and r["kind"] == "subreddit" and r["configured"]:
            r["source"] = "r/" + r["source"].removeprefix("r/")
        r["flag"] = flag(r, min_n, total_found)
        out.append(r)
    kinds = ["subreddit", "keyword", "group", "group seen", "account", "post"]
    return sorted(out, key=lambda r: (kinds.index(r["kind"]), not r["configured"], -r["replied"], -r["qualified"],
                                      -r["found"], r["source"]))


def flag(r: dict, min_n: int, total_found: int) -> str:
    if r["configured"] and r["found"] == 0:
        return "no data yet" if total_found == 0 else "brings nothing"
    if r["dmed"] >= min_n and r["replied"] == 0:
        return f"leads but no replies ({r['dmed']} DMs)"
    if r["found"] >= 2 * min_n and r["qualified"] == 0:
        return f"nothing qualifies ({r['found']} found)"
    if 0 < r["dmed"] < min_n:
        return "small sample"
    return ""


def perf_table(rows: list[dict], kinds: tuple, limit: int = 25) -> str:
    rows = [r for r in rows if r["kind"] in kinds]
    if not rows:
        return "(none)\n"
    lines = ["| source | kind | found | qualified | DMed | replied | booked | note |",
             "|---|---|---:|---:|---:|---:|---:|---|"]
    for r in rows[:limit]:
        name = r["source"] if r["configured"] else f"{r['source']} (not configured)"
        rate = f" ({round(100 * r['replied'] / r['dmed'])}%)" if r["dmed"] else ""
        lines.append(f"| {name[:80]} | {r['kind']} | {r['found']} | {r['qualified']} | {r['dmed']} | "
                     f"{r['replied']}{rate} | {r['booked']} | {r['flag']} |")
    if len(rows) > limit:
        lines.append(f"| ... {len(rows) - limit} more | | | | | | | |")
    return "\n".join(lines) + "\n"


# ----------------------------------------------------------------- keyword mining
def ngrams(*texts: str) -> set[str]:
    """1-3 word phrases in each text (never across texts or sentences), not starting or ending on a stopword."""
    out = set()
    for text in texts:
        for chunk in re.split(r"[.!?\n;:()\[\]]+", (text or "").lower().replace("’", "'")):
            words = [w.replace("'", "") for w in re.findall(r"[a-z][a-z']*", chunk)]
            for n in (1, 2, 3):
                for i in range(len(words) - n + 1):
                    g = words[i:i + n]
                    if g[0] in STOP or g[-1] in STOP or any(len(w) < 2 for w in g):
                        continue
                    if any(w in STOP and w not in INNER_OK for w in g[1:-1]) or (n == 1 and len(g[0]) < 4):
                        continue
                    out.add(" ".join(g))
    return out


def covered(phrase: str, keywords: list[str]) -> bool:
    p = f" {phrase} "
    for kw in keywords:
        k = " " + " ".join(re.findall(r"[a-z0-9]+", kw.lower())) + " "
        if k.strip() and (k in p or p in k):
            return True
    return False


def mine_keywords(platform: str, cfg: dict, min_docs: int = 3, top: int = 12) -> list[dict]:
    """Phrases in posts whose author replied or that scored >= HIGH_SCORE, more common there than in the platform's
    posts overall, that no current keyword covers. [{phrase, worked, all, lift}], best first."""
    keywords = (cfg.get("search") or {}).get("keywords", [])
    ps = {p["person"]: p for p in outcomes.people(platform)}
    docs = []
    for lead in leads(platform, cfg):
        if not lead["texts"]:
            continue
        p = ps.get(lead["person"]) or {}
        worked = bool(p.get("replied_at")) or (lead["score"] or 0) >= HIGH_SCORE
        docs.append((ngrams(*lead["texts"]), worked))
    pos = [g for g, w in docs if w]
    if not pos:
        return []
    df_pos, df_all = defaultdict(int), defaultdict(int)
    for g, w in docs:
        for x in g:
            df_all[x] += 1
            if w:
                df_pos[x] += 1
    n_pos, n_all = len(pos), len(docs)
    found = []
    for x, c in df_pos.items():
        if c < min_docs or covered(x, keywords):
            continue
        lift = (c / n_pos) / ((df_all[x] + 1) / (n_all + 1))
        if n_all > n_pos and lift < 1.1:
            continue
        found.append({"phrase": x, "worked": c, "all": df_all[x], "lift": round(lift, 2)})
    found.sort(key=lambda f: (-f["worked"] * f["lift"], -len(f["phrase"].split()), f["phrase"]))
    kept = []
    for f in found:                           # drop "essay" when "college essay" says the same thing as often
        if any(f["phrase"] in k["phrase"] and k["worked"] >= f["worked"] for k in kept):
            continue
        kept = [k for k in kept if not (k["phrase"] in f["phrase"] and f["worked"] >= k["worked"])]
        kept.append(f)
        if len(kept) >= top:
            break
    return kept


def mined_table(rows: list[dict]) -> str:
    if not rows:
        return "(not enough posts that got replies or high scores yet)\n"
    lines = ["| phrase | posts that worked | all posts | lift |", "|---|---:|---:|---:|"]
    lines += [f"| {r['phrase']} | {r['worked']} | {r['all']} | {r['lift']} |" for r in rows]
    return "\n".join(lines) + "\n"


def keyword_adds(rows: list[dict], n: int = 3) -> list[str]:
    """The mined phrases worth a search slot: two or more words (one word alone matches too much)."""
    return [r["phrase"] for r in rows if len(r["phrase"].split()) > 1][:n]


# ----------------------------------------------------------------- new subreddits
def topics(cfg: dict, limit: int = MAX_TOPICS) -> list[str]:
    """Subreddit-search phrases from the business services, then the current multi-word keywords."""
    services = []
    for service in (cfg.get("business") or {}).get("services", []):
        service = re.sub(r"\(.*?\)", " ", service.lower())
        for part in re.split(r"/|,| and ", service):
            words = [w for w in re.findall(r"[a-z0-9:]+", part) if w not in SERVICE_FILLER]
            if words:
                services.append(" ".join(words[:3]))
    keywords = [k.lower() for k in (cfg.get("search") or {}).get("keywords", []) if len(k.split()) > 1]
    out = []                                  # two from the services for every one from the keywords
    while services or keywords:
        out += services[:2] + keywords[:1]
        services, keywords = services[2:], keywords[1:]
    seen, unique = set(), []
    for t in out:
        if t not in seen:
            seen.add(t)
            unique.append(t)
    return unique[:limit]


def activity(children: list, keywords: list[str], now: float) -> dict:
    """From one /new.json page: posts per day and the share the current keywords match."""
    posts = [c.get("data") or {} for c in children if isinstance(c, dict) and c.get("kind") == "t3"]
    posts = [d for d in posts if not d.get("stickied")]
    if not posts:
        return {"sampled": 0, "per_day": 0.0, "kw_share": 0.0}
    oldest = min(float(d.get("created_utc") or now) for d in posts)
    days = max((now - oldest) / 86400, 1 / 24)
    kws = [k.lower() for k in keywords]
    hits = sum(1 for d in posts if any(k in f"{d.get('title', '')} {d.get('selftext', '')}".lower() for k in kws))
    return {"sampled": len(posts), "per_day": round(len(posts) / days, 1), "kw_share": round(100 * hits / len(posts))}


def exclusion(c: dict, have: set[str]) -> str:
    if c["key"] in have:
        return "already configured"
    if c["nsfw"]:
        return "NSFW"
    if c["quarantine"]:
        return "quarantined"
    if c["type"] not in ("public", "restricted"):
        return f"not public ({c['type']})"
    if c["subscribers"] < MIN_SUBSCRIBERS:
        return f"too small ({c['subscribers']} subscribers)"
    return ""


def search_subreddits(cfg: dict, fetch, driver=None, pause=None, max_requests: int = 24,
                      max_topics: int = MAX_TOPICS, max_samples: int = MAX_SAMPLES, page_skipped=(),
                      now: float | None = None) -> dict:
    """Read-only Reddit lookups: subreddit search for each topic, then a /new.json sample of the likeliest
    candidates. fetch(driver, url) -> parsed JSON (browser.fetch_json). Stops at once on AccountBlocked.
    Returns {topics, candidates, excluded, requests, stopped, halted}."""
    now = now or time.time()
    pause = pause or (lambda: None)
    s = cfg.get("search") or {}
    have = {norm_sub(x) for x in s.get("subreddits", [])}
    out = {"topics": topics(cfg, max_topics), "candidates": [], "excluded": [], "requests": 0, "stopped": "",
           "halted": False}
    found: dict[str, dict] = {}

    def get(url):
        if out["requests"] >= max_requests:
            return None
        if out["requests"]:
            pause()
        out["requests"] += 1
        return fetch(driver, url)

    try:
        for t in out["topics"]:
            if out["requests"] >= max_requests:
                out["stopped"] = f"request cap ({max_requests}) reached during topic search"
                break
            try:
                data = get(f"{OLD}/subreddits/search.json?q={urllib.parse.quote(t)}&limit=25&raw_json=1")
            except page_skipped as exc:
                print(f"[=] topic {t!r}: skipped ({exc})")
                continue
            for child in ((data or {}).get("data") or {}).get("children", []):
                d = (child or {}).get("data") or {}
                name = d.get("display_name") or ""
                if (child or {}).get("kind") != "t5" or not name:
                    continue
                c = found.setdefault(norm_sub(name), {
                    "key": norm_sub(name), "name": name, "subscribers": int(d.get("subscribers") or 0),
                    "nsfw": bool(d.get("over18")), "quarantine": bool(d.get("quarantine")),
                    "type": d.get("subreddit_type") or "public",
                    "about": re.sub(r"\s+", " ", d.get("public_description") or "")[:140], "topics": []})
                if t not in c["topics"]:
                    c["topics"].append(t)
        todo = []
        for c in found.values():
            why = exclusion(c, have)
            if why:
                if why != "already configured":
                    out["excluded"].append(dict(c, why=why))
            else:
                todo.append(c)
        todo.sort(key=lambda c: (-len(c["topics"]), -c["subscribers"], c["key"]))
        for i, c in enumerate(todo):
            if i >= max_samples or out["requests"] >= max_requests:
                c["activity"] = None
                out["candidates"].append(c)
                continue
            try:
                data = get(f"{OLD}/r/{c['name']}/new.json?limit=100&raw_json=1")
            except page_skipped as exc:
                reason = "quarantined" if "quarantined" in str(exc) else f"could not read ({exc})"
                out["excluded"].append(dict(c, why=reason))
                continue
            c["activity"] = activity(((data or {}).get("data") or {}).get("children", []), s.get("keywords", []), now)
            if c["activity"]["per_day"] < MIN_POSTS_PER_DAY:
                out["excluded"].append(dict(c, why=f"low activity ({c['activity']['per_day']} posts/day)"))
            else:
                out["candidates"].append(c)
    except AccountBlocked as exc:
        out["stopped"], out["halted"] = f"account-wide problem ({exc.kind}): {exc}", True
        for c in found.values():                         # whatever wasn't reached is still worth a look
            if "activity" not in c and not exclusion(c, have):
                c["activity"] = None
                out["candidates"].append(c)
    out["candidates"].sort(key=lambda c: (c["activity"] is None,
                                          -((c["activity"] or {}).get("per_day", 0) * ((c["activity"] or {}).get("kw_share", 0) + 5)),
                                          -c["subscribers"]))
    return out


def live_search(cfg: dict, max_requests: int) -> dict:
    """search_subreddits through the logged-in Reddit profile (reddit/browser.py), checking the login first."""
    sys.path.insert(0, str(ROOT / "reddit"))
    import browser                                        # reddit/browser.py (puts reddit/store.py first)
    import store
    driver = browser.make_driver(cfg)
    try:
        try:
            print(f"[+] reading as u/{browser.check_login(driver, store.load_root_config())}")
        except AccountBlocked as exc:
            return {"topics": topics(cfg), "candidates": [], "excluded": [], "requests": 0, "halted": True,
                    "stopped": f"account-wide problem ({exc.kind}): {exc}"}
        return search_subreddits(cfg, browser.fetch_json, driver, pause=lambda: browser.pause(cfg),
                                 max_requests=max_requests, page_skipped=browser.PageSkipped)
    finally:
        driver.quit()


def candidates_section(res: dict | None) -> str:
    out = ["## New subreddit candidates", ""]
    if res is None:
        return "\n".join(out + ["(skipped: --no-search)", ""])
    out.append(f"Searched Reddit for: {', '.join(repr(t) for t in res['topics'])} ({res['requests']} requests).")
    if res["stopped"]:
        out.append(f"\n**Stopped early: {res['stopped']}.**" + (" Look at the account before running again." if res["halted"] else ""))
    out.append("")
    if res["candidates"]:
        out += ["| subreddit | subscribers | posts/day | keyword matches | found by | about |",
                "|---|---:|---:|---:|---|---|"]
        for c in res["candidates"]:
            a = c.get("activity")
            ppd, share = (a["per_day"], f"{a['kw_share']}%") if a else ("not sampled", "")
            out.append(f"| r/{c['name']} | {c['subscribers']:,} | {ppd} | {share} | {', '.join(c['topics'])} | "
                       f"{c['about'].replace('|', '/')} |")
    else:
        out.append("(no new candidates)")
    if res["excluded"]:
        out += ["", "Left out: " + "; ".join(f"r/{c['name']} ({c['why']})" for c in res["excluded"][:30])]
    return "\n".join(out) + "\n"


# ----------------------------------------------------------------- Facebook groups
def grad_years(today: date) -> list[int]:
    first = today.year + (1 if today.month >= 7 else 0)
    return [first, first + 1]


def group_phrases(fb_rows: list[dict], fb_mined: list[dict], today: date | None = None) -> list[tuple[str, str]]:
    """(phrase to type into Facebook's group search, why), from what works."""
    today = today or date.today()
    out = []

    def core(p: str) -> str:
        p = re.sub(r"^(how to|need|need help with|worried about)\s+", "", p.lower().strip())
        return re.sub(r"\s+(help|advice)$", "", p).strip()

    kws = [r for r in fb_rows if r["kind"] == "keyword" and (r["replied"] or r["qualified"])]
    kws.sort(key=lambda r: (-r["replied"], -r["qualified"]))
    for r in kws[:4]:
        c = core(r["source"])
        why = f"keyword {r['source']!r}: {r['qualified']} qualified, {r['replied']} replied"
        out += [(c, why), (f"{c} parents", why)]
    if not kws:                               # no results yet: start from the current keywords
        for r in [r for r in fb_rows if r["kind"] == "keyword" and r["configured"]][:3]:
            out.append((core(r["source"]), f"current keyword {r['source']!r} (no results to go on yet)"))
    for m in fb_mined[:3]:
        out.append((core(m["phrase"]), f"common in Facebook posts that worked ({m['worked']} posts)"))
    for y in grad_years(today):
        out.append((f"class of {y} parents", "parents' groups for students applying now"))
    out.append(("college admissions parents", "general: parents are who books calls"))
    seen, unique = set(), []
    for p, why in out:
        if p and p not in seen:
            seen.add(p)
            unique.append((p, why))
    return unique[:12]


def groups_section(fb_rows: list[dict], fb_mined: list[dict]) -> str:
    out = ["## Facebook groups", "",
           "Facebook isn't searched for this. Type these into Facebook's search and pick **Groups**; join the active "
           "ones you'd genuinely take part in, then add their URLs to facebook/config.json `search.groups`.", ""]
    out += [f"- `{p}` ({why})" for p, why in group_phrases(fb_rows, fb_mined)]
    seen = [r for r in fb_rows if r["kind"] == "group seen" and r["qualified"]]
    if seen:
        out += ["", "Groups that keyword searches already found qualified leads in (not configured):", "",
                perf_table(seen, ("group seen",), 15)]
    out += ["", "Configured groups by performance:", "",
            perf_table([r for r in fb_rows if r["kind"] == "group" and r["configured"]], ("group",))]
    return "\n".join(out)


# ----------------------------------------------------------------- what to change
def plan(perf: dict, mined: dict, search: dict | None, configs: dict) -> list[dict]:
    """The config changes the data supports: [{file, path, current, add, drop}] (only ones that change something)."""
    def drops(platform, kind):
        return [r["source"] for r in perf[platform] if r["kind"] == kind and r["configured"]
                and r["flag"].startswith(("leads but no replies", "nothing qualifies", "brings nothing"))]

    rs, fs = configs["reddit"].get("search") or {}, configs["facebook"].get("search") or {}
    ig = configs["instagram"].get("discover") or {}
    adds_sub = [c["name"] for c in (search or {}).get("candidates", []) if c.get("activity")][:3]
    drop_sub = {d.removeprefix("r/").lower() for d in drops("reddit", "subreddit")}
    items = [
        ("reddit/config.json", ["search", "subreddits"], rs.get("subreddits", []), adds_sub,
         [x for x in rs.get("subreddits", []) if norm_sub(x) in drop_sub]),
        ("reddit/config.json", ["search", "keywords"], rs.get("keywords", []),
         keyword_adds(mined.get("reddit", [])), drops("reddit", "keyword")),
        ("facebook/config.json", ["search", "keywords"], fs.get("keywords", []),
         keyword_adds(mined.get("facebook", [])), drops("facebook", "keyword")),
        ("facebook/config.json", ["search", "groups"], fs.get("groups", []), [], drops("facebook", "group")),
        ("instagram/config.json", ["discover", "accounts"], ig.get("accounts", []), [],
         [a for a in ig.get("accounts", []) if "@" + a.lower().removeprefix("@") in
          {d.lower() for d in drops("instagram", "account")}]),
    ]
    return [{"file": f, "path": p, "current": cur, "add": a, "drop": d} for f, p, cur, a, d in items if a or d]


def snippets_section(changes: list[dict]) -> str:
    out = ["## Config snippets (nothing has been changed)", ""]
    if not changes:
        return "\n".join(out + ["(nothing to change yet)", ""])
    out.append("Each replaces the whole list at that key. Leave out any add or drop you don't agree with.\n")
    for ch in changes:
        drop = set(ch["drop"])
        new = [x for x in ch["current"] if x not in drop] + [x for x in ch["add"] if x not in ch["current"]]
        snippet = {ch["path"][0]: {ch["path"][1]: new}}
        bits = ([f"add {', '.join(ch['add'])}"] if ch["add"] else []) + ([f"drop {', '.join(ch['drop'])}"] if ch["drop"] else [])
        out += [f"**{ch['file']}** `{'.'.join(ch['path'])}`: {'; '.join(bits)}", "",
                "```json", json.dumps(snippet, indent=2, ensure_ascii=False), "```", ""]
    return "\n".join(out)


# ----------------------------------------------------------------- Claude
def load_env():
    for env in [ROOT / p / ".env" for p in (*outcomes.PLATFORMS, "inbox")]:
        if env.exists():
            try:
                from dotenv import load_dotenv
                load_dotenv(env)
            except ImportError:
                break


def recommend(report: str, configs: dict, client=None) -> str:
    """Claude's short ranked add / drop / watch list, or why there is none."""
    head = "## Recommendation (Claude; nothing has been changed)\n\n"
    if client is None:
        load_env()
        if not os.environ.get("ANTHROPIC_API_KEY"):
            return head + "(skipped: no ANTHROPIC_API_KEY)\n"
        import anthropic
        client = anthropic.Anthropic()
    model = (configs.get("facebook", {}).get("ai") or {}).get("model") or "claude-sonnet-5-5"
    system = ("You review where a small business (a college-admissions counseling service that DMs students and "
              "parents who asked for help online) looks for leads: subreddits, search keywords, Facebook groups, "
              "Instagram posts. From the report given, write a short ranked list of at most 8 items, each 'add', "
              "'drop' or 'watch', naming the source and giving the reason from the numbers. Say plainly when a "
              "sample is too small to conclude anything (prefer 'watch' then). Never suggest anything deceptive, "
              "spammy, aimed at minors, or that raises messaging volume; prefer fewer, better sources. Markdown "
              "list only, no preamble.")
    try:
        resp = client.messages.create(model=model, max_tokens=1500, system=system,
                                      messages=[{"role": "user", "content": report}])
    except Exception as exc:                                     # an API or network error: the rest still stands
        return head + f"(skipped: Claude API error: {exc})\n"
    text = "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text")
    return head + (text.strip() or "(no answer)") + "\n"


# ----------------------------------------------------------------- the report
def build(configs: dict, min_n: int, search: dict | None) -> str:
    perf = {p: performance(p, configs[p], min_n) for p in outcomes.PLATFORMS}
    mined = {p: mine_keywords(p, configs[p]) for p in ("reddit", "facebook")}
    out = ["## Performance", "",
           f"Found = posts (Instagram: commenters) each source turned up; a post found by several searches counts "
           f"for each. Qualified = scored at or above the platform's ai.min_score. \"No replies\" needs "
           f"{min_n}+ DMs to count.", ""]
    names = {"reddit": ("Reddit", ("subreddit", "keyword")), "facebook": ("Facebook", ("keyword", "group")),
             "instagram": ("Instagram", ("account", "post"))}
    for p, (title, kinds) in names.items():
        out += [f"### {title}", "", perf_table(perf[p], kinds)]
    flagged = [f"- {p}: {r['source']} ({r['kind']}): {r['flag']}" for p in outcomes.PLATFORMS for r in perf[p]
               if r["configured"] and r["flag"] and r["flag"] not in ("small sample", "no data yet")]
    out += ["### Flagged", "", "\n".join(flagged) or "(nothing flagged)", ""]
    out.append(candidates_section(search))
    out += ["## New keyword candidates", "",
            f"Phrases in posts whose author replied or that scored {HIGH_SCORE}+, more common there than overall, "
            "that no current keyword covers.", "",
            "### Reddit (search.keywords)", "", mined_table(mined["reddit"]),
            "### Facebook (search.keywords)", "", mined_table(mined["facebook"])]
    out.append(groups_section(perf["facebook"], mined["facebook"]))
    out.append(snippets_section(plan(perf, mined, search, configs)))
    return "\n".join(out)


def main(argv=None, reddit=None, client=None) -> int:
    """reddit: {"fetch", "driver", "pause"} to search with instead of the real profile (tests)."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--no-search", action="store_true", help="no Reddit requests (offline)")
    ap.add_argument("--no-ai", action="store_true", help="no Claude recommendation")
    ap.add_argument("--min", type=int, default=10, help="DMs a source needs before 'no replies' is flagged")
    ap.add_argument("--max-requests", type=int, default=24, help="most Reddit requests this run")
    args = ap.parse_args(argv)
    configs = load_configs()
    search = None
    if not args.no_search:
        if reddit is not None:
            search = search_subreddits(configs["reddit"], reddit["fetch"], reddit.get("driver"), reddit.get("pause"),
                                       max_requests=args.max_requests, page_skipped=reddit.get("page_skipped", ()))
        else:
            search = live_search(configs["reddit"], args.max_requests)
        if search["halted"]:
            print(f"[stopped] {search['stopped']}. Kept what was read; look at the account before running again.")
    body = build(configs, args.min, search)
    report = f"# Lead sources, {date.today().isoformat()}\n\nSuggestions only: nothing has been joined, followed, " \
             f"posted or changed in any config.\n\n"
    if not args.no_ai:
        report += recommend(body, configs, client) + "\n"
    report += body
    REPORTS.mkdir(parents=True, exist_ok=True)
    path = REPORTS / f"sources-{date.today().isoformat()}.md"
    path.write_text(report, encoding="utf-8")
    print(f"[+] {path}")
    return NEEDS_YOU if search and search["halted"] else 0


if __name__ == "__main__":
    sys.exit(main())
