"""test_sources.py - insights/sources.py: source performance from temp data files, subreddit candidates from a fake
Reddit (no browser, nothing on any site), keyword mining, the report's snippets, a mocked Claude, and stopping
with exit 2 on an account-wide problem. Also a few of insights/run.py's numbers.

    venv/bin/python -m unittest discover insights/tests
"""
from __future__ import annotations

import csv
import io
import json
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import sources  # noqa: E402
import outcomes  # noqa: E402
from send_dm import AccountBlocked  # noqa: E402

TODAY = date.today().isoformat()
NOW = time.time()


def write_csv(path: Path, rows: list[dict]):
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(dict.fromkeys(k for r in rows for k in r))
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fields)
        w.writeheader()
        w.writerows(rows)


def reddit_lead(i, sub, author, score, text, kw="essay help"):
    return f"t3_{sub}{i}", {"post_id": f"t3_{sub}{i}", "author": author, "subreddit": sub, "title": "Question",
                            "text": text, "sources": [f"r/{sub} new", f"r/{sub} search {kw!r}"],
                            "ai": {"relevant": True, "score": score, "categories": ["essays"], "message": "Hi"}}


class Fixture(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        d = self.dir = Path(tmp.name)
        files = {p: {k: d / p / Path(v).name for k, v in f.items()} for p, f in outcomes.FILES.items()}
        files["reddit"]["contacted"] = d / "contacted.csv"
        configs = {p: d / p / "config.json" for p in outcomes.PLATFORMS}
        for p in (mock.patch.object(outcomes, "FILES", files), mock.patch.object(outcomes, "ANSWERS", d / "answers.json"),
                  mock.patch.object(outcomes, "BOOKINGS", d / "bookings.csv"),
                  mock.patch.object(sources, "CONFIGS", configs), mock.patch.object(sources, "REPORTS", d / "reports")):
            p.start()
            self.addCleanup(p.stop)
        self.files = files
        self.write_configs(configs)

        # Reddit: r/ApplyingToCollege 12 DMed, 3 replied (all about early decision deadlines), 1 booked;
        # r/chanceme 10 DMed, none replied; r/scholarships nothing.
        leads, contacted, replies = {}, [], []
        for i in range(12):
            text = ("Early decision deadline is next week and my essay is a mess" if i < 3
                    else "Chances for a state school with a 3.5?")
            k, lead = reddit_lead(i, "ApplyingToCollege", f"a{i}", 85 if i < 3 else 65, text)
            leads[k] = lead
            contacted.append({"username": f"a{i}", "status": "sent", "timestamp": "2026-10-01T10:00:00", "reason": ""})
            if i < 3:
                replies.append({"message_id": f"m{i}", "username": f"a{i}", "received_at": "2026-10-02T10:00:00"})
        for i in range(10):
            k, lead = reddit_lead(i, "chanceme", f"c{i}", 62, "chance me for ivy league schools please", kw="scholarship")
            leads[k] = lead
            contacted.append({"username": f"c{i}", "status": "sent", "timestamp": "2026-10-01T10:00:00", "reason": ""})
        files["reddit"]["leads"].parent.mkdir(parents=True, exist_ok=True)
        files["reddit"]["leads"].write_text(json.dumps(leads))
        write_csv(files["reddit"]["contacted"], contacted)
        write_csv(files["reddit"]["replies"], replies)
        write_csv(d / "bookings.csv", [{"platform": "reddit", "person": "a0", "booked_at": f"{TODAY}T12:00:00"}])

        # Facebook: one configured group (via its feed), one group a keyword search turned up.
        fb = {
            "p1": {"author": "Jane", "author_key": "1001", "group": "", "text": "Need a college essay coach",
                   "sources": ["group feed https://www.facebook.com/groups/12345"], "ai": {"relevant": True, "score": 90}},
            "p2": {"author": "Sam", "author_key": "1002", "group": "Parents of Seniors", "text": "Scholarship advice?",
                   "sources": ["search 'scholarship help'"], "ai": {"relevant": True, "score": 70}},
        }
        files["facebook"]["leads"].parent.mkdir(parents=True, exist_ok=True)
        files["facebook"]["leads"].write_text(json.dumps(fb))
        write_csv(files["facebook"]["contacted"], [{"author_key": "1001", "status": "sent", "timestamp": "2026-10-01T10:00:00"}])
        write_csv(files["facebook"]["replies"], [{"author_key": "1001", "seen_at": "2026-10-02T10:00:00"}])

        # Instagram: commenters on our own post.
        ig = {"abc:kim": {"username": "kim", "post": "https://www.instagram.com/p/abc/", "post_owner": "me",
                          "text": "how do I start my essay", "ai": {"relevant": True, "score": 55}}}
        files["instagram"]["leads"].parent.mkdir(parents=True, exist_ok=True)
        files["instagram"]["leads"].write_text(json.dumps(ig))

    def write_configs(self, configs):
        data = {
            "reddit": {"business": {"services": ["College essay coaching", "Scholarship search"]},
                       "search": {"subreddits": ["ApplyingToCollege", "chanceme", "scholarships"],
                                  "keywords": ["essay help", "scholarship"]},
                       "ai": {"min_score": 60}},
            "facebook": {"search": {"keywords": ["college essay help", "scholarship help"],
                                    "groups": ["https://www.facebook.com/groups/12345/"]},
                         "ai": {"min_score": 60, "model": "claude-test"}},
            "instagram": {"instagram": {"my_username": "me"}, "discover": {"accounts": ["other"]},
                          "ai": {"min_score": 50}},
        }
        for p, path in configs.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data[p]))


class Performance(Fixture):
    def rows(self, platform):
        perf = sources.performance(platform, sources.load_configs()[platform], 10)
        return {(r["kind"], r["source"]): r for r in perf}

    def test_reddit_subreddits_and_keywords(self):
        r = self.rows("reddit")
        atc = r[("subreddit", "r/ApplyingToCollege")]
        self.assertEqual((atc["found"], atc["qualified"], atc["dmed"], atc["replied"], atc["booked"]), (12, 12, 12, 3, 1))
        self.assertEqual(atc["flag"], "")
        self.assertTrue(r[("subreddit", "r/chanceme")]["flag"].startswith("leads but no replies"))
        self.assertEqual(r[("subreddit", "r/scholarships")]["flag"], "brings nothing")
        self.assertEqual(r[("keyword", "essay help")]["found"], 12)
        self.assertEqual(r[("keyword", "scholarship")]["replied"], 0)

    def test_facebook_groups_and_instagram(self):
        f = self.rows("facebook")
        g = f[("group", "https://www.facebook.com/groups/12345/")]
        self.assertEqual((g["configured"], g["found"], g["dmed"], g["replied"], g["flag"]), (True, 1, 1, 1, "small sample"))
        self.assertFalse(f[("group seen", "Parents of Seniors")]["configured"])
        self.assertEqual(f[("keyword", "scholarship help")]["qualified"], 1)
        self.assertEqual(f[("keyword", "college essay help")]["flag"], "brings nothing")
        i = self.rows("instagram")
        self.assertEqual(i[("account", "@me")]["found"], 1)
        self.assertEqual(i[("account", "@me")]["qualified"], 1)
        self.assertEqual(i[("account", "@other")]["flag"], "brings nothing")
        self.assertIn(("post", "https://www.instagram.com/p/abc/"), i)

    def test_no_data_is_not_flagged_as_nothing(self):
        self.files["reddit"]["leads"].unlink()
        self.assertEqual(self.rows("reddit")[("subreddit", "r/scholarships")]["flag"], "no data yet")


class Keywords(Fixture):
    def test_mines_phrases_from_posts_that_worked(self):
        found = sources.mine_keywords("reddit", sources.load_configs()["reddit"])
        phrases = [f["phrase"] for f in found]
        self.assertTrue(any("early decision" in p for p in phrases), phrases)
        self.assertNotIn("ivy league", " | ".join(phrases), "only in posts that got nothing")
        self.assertFalse(any(sources.covered(p, ["essay help", "scholarship"]) for p in phrases))

    def test_covered(self):
        self.assertTrue(sources.covered("essay help", ["Essay help"]))
        self.assertTrue(sources.covered("scholarship", ["scholarship help"]))
        self.assertFalse(sources.covered("deadline", ["scholarship"]))

    def test_group_phrases(self):
        phrases = [p for p, _ in sources.group_phrases([], [], date(2026, 11, 9))]
        self.assertIn("class of 2027 parents", phrases)
        self.assertIn("class of 2028 parents", phrases)


def sub(name, subscribers=50000, **extra):
    return {"kind": "t5", "data": dict({"display_name": name, "subscribers": subscribers, "over18": False,
                                        "quarantine": False, "subreddit_type": "public", "public_description": name}, **extra)}


def posts(n, span_days, text="help"):
    return {"data": {"children": [{"kind": "t3", "data": {"title": text, "selftext": "",
                                                           "created_utc": NOW - span_days * 86400 * i / max(n - 1, 1)}}
                                  for i in range(n)]}}


class FakeReddit:
    """A fake fetch_json: subreddit search returns a fixed list; /new.json depends on the subreddit."""

    def __init__(self, block_after=None):
        self.urls, self.block_after = [], block_after

    def __call__(self, driver, url):
        self.urls.append(url)
        if self.block_after is not None and len(self.urls) > self.block_after:
            raise AccountBlocked("Reddit is rate-limiting this account", "rate_limited")
        if "/subreddits/search.json" in url:
            return {"data": {"children": [
                sub("ApplyingToCollege"), sub("NSFWsub", over18=True), sub("QuarSub", quarantine=True),
                sub("Tiny", 100), sub("Hidden", subreddit_type="private"), sub("Quiet", 9000), sub("Busy", 80000)]}}
        if "/r/Quiet/" in url:
            return posts(3, 30)
        if "/r/Busy/" in url:
            return posts(100, 2, "essay help please")
        raise AssertionError(url)


class Candidates(Fixture):
    def search(self, fetch, **kw):
        return sources.search_subreddits(sources.load_configs()["reddit"], fetch, max_topics=1, now=NOW, **kw)

    def test_filters(self):
        pauses = []
        res = self.search(FakeReddit(), pause=lambda: pauses.append(1))
        self.assertEqual([c["name"] for c in res["candidates"]], ["Busy"])
        busy = res["candidates"][0]
        self.assertEqual(busy["activity"]["kw_share"], 100)
        self.assertGreater(busy["activity"]["per_day"], 40)
        why = {c["name"]: c["why"] for c in res["excluded"]}
        self.assertEqual(why["NSFWsub"], "NSFW")
        self.assertEqual(why["QuarSub"], "quarantined")
        self.assertTrue(why["Tiny"].startswith("too small"))
        self.assertTrue(why["Hidden"].startswith("not public"))
        self.assertTrue(why["Quiet"].startswith("low activity"))
        self.assertNotIn("ApplyingToCollege", why, "already configured: left out entirely")
        self.assertEqual(res["requests"], 3)
        self.assertEqual(len(pauses), 2, "a pause between requests, not before the first")
        self.assertFalse(res["halted"])

    def test_request_cap(self):
        fake = FakeReddit()
        res = self.search(fake, max_requests=2)
        self.assertEqual(len(fake.urls), 2)
        self.assertEqual([(c["name"], c["activity"]) for c in res["candidates"]][-1][1], None)

    def test_account_blocked_stops_at_once(self):
        fake = FakeReddit(block_after=1)
        res = self.search(fake)
        self.assertTrue(res["halted"])
        self.assertEqual(len(fake.urls), 2, "no request after the account-wide signal")


class Main(Fixture):
    def run_main(self, argv, **kw):
        with redirect_stdout(io.StringIO()):
            code = sources.main(argv, **kw)
        return code, (self.dir / "reports" / f"sources-{TODAY}.md").read_text()

    def test_no_search_makes_no_requests(self):
        fake = mock.Mock(side_effect=AssertionError("no requests with --no-search"))
        with mock.patch.object(sources, "live_search", side_effect=AssertionError("no browser")):
            code, report = self.run_main(["--no-search", "--no-ai"], reddit={"fetch": fake})
        self.assertEqual(code, 0)
        fake.assert_not_called()
        self.assertIn("(skipped: --no-search)", report)
        self.assertIn("r/chanceme", report)
        self.assertNotIn("## Recommendation", report)

    def test_account_blocked_exits_2_and_keeps_the_report(self):
        code, report = self.run_main(["--no-ai"], reddit={"fetch": FakeReddit(block_after=0)})
        self.assertEqual(code, 2)
        self.assertIn("Stopped early: account-wide problem (rate_limited)", report)

    def test_full_report_with_mocked_claude(self):
        client = mock.Mock()
        client.messages.create.return_value = SimpleNamespace(
            content=[SimpleNamespace(type="text", text="1. **drop** r/chanceme: 10 DMs, 0 replies")])
        code, report = self.run_main([], reddit={"fetch": FakeReddit()}, client=client)
        self.assertEqual(code, 0)
        self.assertLess(report.index("## Recommendation"), report.index("## Performance"))
        self.assertIn("**drop** r/chanceme", report)
        kwargs = client.messages.create.call_args.kwargs
        self.assertEqual(kwargs["model"], "claude-test")
        self.assertIn("r/chanceme", kwargs["messages"][0]["content"])
        self.assertIn("| r/Busy |", report)
        # The snippets: valid JSON, adding the candidate and dropping the source with no replies.
        blocks = [json.loads(b.split("```")[0]) for b in report.split("```json\n")[1:]]
        subs = next(b["search"]["subreddits"] for b in blocks if "subreddits" in b.get("search", {}))
        self.assertIn("Busy", subs)
        self.assertNotIn("chanceme", subs)
        self.assertIn("ApplyingToCollege", subs)
        # and nothing was written to any config
        self.assertEqual(json.loads(sources.CONFIGS["reddit"].read_text())["search"]["subreddits"],
                         ["ApplyingToCollege", "chanceme", "scholarships"])

    def test_no_ai_never_calls_claude(self):
        client = mock.Mock()
        self.run_main(["--no-search", "--no-ai"], client=client)
        client.messages.create.assert_not_called()


if __name__ == "__main__":
    unittest.main()
