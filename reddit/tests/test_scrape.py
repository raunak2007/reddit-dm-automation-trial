"""test_scrape.py - reading Reddit's JSON listings: which posts are kept, the lookback window, merging by post id, and
stopping (keeping what was read) when Reddit points at the account. FakeDriver plays reddit.com.

    venv/bin/python -m unittest discover reddit/tests
"""
from __future__ import annotations

import json
import unittest

from helpers import SENDER, FakeDriver, LedgerTest, ago, config, listing, post, root_config

import browser
import scrape
import store
from send_dm import ME_URL, AccountBlocked


def urls(cfg):
    return dict((label, url) for label, url in scrape.listings(cfg))


class ParsePost(unittest.TestCase):
    def test_a_post_becomes_a_lead_shape(self):
        p = scrape.parse_post(post("abc", author="Kid_123", sub="CollegeEssays"), config())
        self.assertEqual(p["post_id"], "t3_abc")
        self.assertEqual(p["author"], "Kid_123")
        self.assertEqual(p["subreddit"], "CollegeEssays")
        self.assertEqual(p["permalink"], "https://www.reddit.com/r/CollegeEssays/comments/abc/title/")
        self.assertIn("essay", p["text"])
        self.assertTrue(p["created_at"])

    def test_posts_not_worth_keeping_are_dropped(self):
        cfg = config()
        dropped = {
            "deleted author": post("a", author="[deleted]"),
            "automoderator": post("b", author="AutoModerator"),
            "this account": post("c", author=SENDER.upper()),
            "stickied": post("d", stickied=True),
            "moderator post": post("e", distinguished="moderator"),
            "removed": post("f", removed_by_category="moderator"),
            "removed text": post("g", selftext="[removed]"),
            "nsfw": post("h", over_18=True),
            "a comment": {"kind": "t1", "data": {"id": "x", "author": "someone"}},
        }
        for why, child in dropped.items():
            self.assertIsNone(scrape.parse_post(child, cfg, SENDER), why)
        self.assertIsNotNone(scrape.parse_post(post("i"), cfg, SENDER))

    def test_skip_authors_from_config(self):
        cfg = config()
        cfg["search"]["skip_authors"] = ["CollegeBot"]
        self.assertIsNone(scrape.parse_post(post("a", author="collegebot"), cfg))


class Listings(unittest.TestCase):
    def test_each_subreddit_gets_its_new_feed_and_a_search_per_keyword(self):
        cfg = config()
        cfg["search"]["subreddits"] = ["r/ApplyingToCollege", "scholarships"]
        cfg["search"]["keywords"] = ["essay help", "financial aid"]
        got = list(scrape.listings(cfg))
        self.assertEqual(len(got), 2 * (1 + 2))
        url = urls(cfg)["r/scholarships search 'financial aid'"]
        self.assertIn("/r/scholarships/search.json?q=financial%20aid", url)
        self.assertIn("restrict_sr=on", url)
        self.assertIn("sort=new", url)
        self.assertIn("t=week", url)
        self.assertTrue(urls(cfg)["r/ApplyingToCollege new"].endswith("/r/ApplyingToCollege/new.json?limit=100&raw_json=1"))

    def test_new_feeds_can_be_turned_off(self):
        cfg = config(); cfg["search"]["read_new_feeds"] = False
        self.assertFalse(any(label.endswith(" new") for label, _ in scrape.listings(cfg)))

    def test_search_window(self):
        self.assertEqual([scrape.search_window(d) for d in (1, 3, 7, 14, 90, 400)],
                         ["day", "week", "week", "month", "year", "all"])


class LookbackAndMerge(LedgerTest):
    def read(self, pages, url="https://old.reddit.com/r/x/new.json?limit=100&raw_json=1", leads=None, budget=50,
             label="r/x new"):
        cfg = config()
        driver = FakeDriver(pages)
        leads = {} if leads is None else leads
        cutoff = ago(days=cfg["search"]["lookback_days"])
        seen, new = scrape.read_listing(driver, cfg, label, url, leads, cutoff, SENDER, {"requests": budget})
        return seen, new, leads, driver

    def test_posts_older_than_the_lookback_are_left_out_and_paging_stops_there(self):
        base = "https://old.reddit.com/r/x/new.json?limit=100&raw_json=1"
        pages = {base: listing(post("a"), post("b", created=ago(days=3)), after="t3_b"),
                 base + "&after=t3_b": listing(post("c", created=ago(days=6)), post("d", created=ago(days=9)),
                                               after="t3_d"),
                 base + "&after=t3_d": listing(post("e", created=ago(days=10)))}
        seen, new, leads, driver = self.read(pages)
        self.assertEqual(sorted(leads), ["t3_a", "t3_b", "t3_c"])
        self.assertEqual((seen, new), (3, 3))
        self.assertNotIn(base + "&after=t3_d", driver.visited, "the page with old posts was the last one read")

    def test_max_pages_and_request_budget(self):
        base = "https://old.reddit.com/r/x/new.json?limit=100&raw_json=1"
        pages = {base: listing(post("a"), after="t3_a"), base + "&after=t3_a": listing(post("b"), after="t3_b"),
                 base + "&after=t3_b": listing(post("c"), after="t3_c")}
        _, _, leads, driver = self.read(pages, budget=2)
        self.assertEqual(sorted(leads), ["t3_a", "t3_b"])
        self.assertEqual(len(driver.visited), 2)

    def test_the_same_post_from_two_listings_is_one_lead_with_both_sources(self):
        url1 = "https://old.reddit.com/r/x/new.json?limit=100&raw_json=1"
        url2 = "https://old.reddit.com/r/x/search.json?q=essay"
        leads = {}
        self.read({url1: listing(post("a"))}, url1, leads, label="r/x new")
        seen, new, leads, _ = self.read({url2: listing(post("a"), post("b"))}, url2, leads, label="r/x search")
        self.assertEqual(new, 1)
        self.assertEqual(leads["t3_a"]["sources"], ["r/x new", "r/x search"])

    def test_an_edited_post_is_scored_again_an_unchanged_one_keeps_its_score(self):
        leads = {}
        scrape.merge(leads, scrape.parse_post(post("a"), config()), "one")
        scrape.merge(leads, scrape.parse_post(post("b"), config()), "one")
        for l in leads.values():
            l["ai"] = {"score": 80}
        scrape.merge(leads, scrape.parse_post(post("a", selftext="edited: also scholarships"), config()), "two")
        scrape.merge(leads, scrape.parse_post(post("b"), config()), "two")
        self.assertNotIn("ai", leads["t3_a"])
        self.assertIn("ai", leads["t3_b"])
        self.assertIn("edited", leads["t3_a"]["text"])


class FetchJson(unittest.TestCase):
    URL = "https://old.reddit.com/r/x/new.json?limit=100"

    def fetch(self, text):
        return browser.fetch_json(FakeDriver({self.URL: text}), self.URL)

    def kind(self, text):
        with self.assertRaises(AccountBlocked) as cm:
            self.fetch(text)
        return cm.exception.kind

    def test_listing_comes_back(self):
        self.assertEqual(self.fetch(listing(post("a")))["data"]["children"][0]["data"]["id"], "a")

    def test_account_signals(self):
        self.assertEqual(self.kind(json.dumps({"message": "Too Many Requests", "error": 429})), "rate_limited")
        self.assertEqual(self.kind(json.dumps({"message": "Unauthorized", "error": 401})), "login")
        self.assertEqual(self.kind(json.dumps({"message": "Forbidden", "error": 403})), "restricted")
        self.assertIn(self.kind("whoa there, pardner! your request has been blocked due to a network policy"),
                      ("rate_limited", "restricted"))
        self.assertEqual(self.kind("You're doing that too much. Try again in 5 minutes."), "rate_limited")
        self.assertEqual(self.kind("Your account has been suspended"), "restricted")
        self.assertEqual(self.kind("<html>something else entirely</html>"), "restricted")

    def test_redirect_to_login(self):
        driver = FakeDriver({self.URL: {"redirect": "https://www.reddit.com/login/?dest=x"}})
        with self.assertRaises(AccountBlocked) as cm:
            browser.fetch_json(driver, self.URL)
        self.assertEqual(cm.exception.kind, "login")

    def test_one_subreddit_problems_only_skip_the_page(self):
        for body in ({"reason": "private", "message": "Forbidden", "error": 403},
                     {"reason": "banned", "message": "Not Found", "error": 404},
                     {"message": "Not Found", "error": 404}, {"message": "Internal", "error": 500}):
            with self.assertRaises(browser.PageSkipped):
                self.fetch(json.dumps(body))


class Run(LedgerTest):
    def setUp(self):
        super().setUp()
        self.cfg = config()
        self.cfg["search"].update(subreddits=["A", "B"], keywords=[])
        self.a = urls(self.cfg)["r/A new"]
        self.b = urls(self.cfg)["r/B new"]

    def test_a_rate_limit_stops_the_run_and_keeps_what_was_read(self):
        driver = FakeDriver({self.a: listing(post("a1"), post("a2")),
                             self.b: json.dumps({"message": "Too Many Requests", "error": 429})})
        out = scrape.run(self.cfg, root_config(), driver=driver)
        self.assertTrue(out["halted"])
        self.assertIn("rate_limited", out["stopped"])
        self.assertEqual(sorted(store.load_leads()), ["t3_a1", "t3_a2"])

    def test_a_private_subreddit_is_skipped_and_the_run_goes_on(self):
        cfg = config()
        cfg["search"].update(subreddits=["Private", "B"], keywords=[])
        driver = FakeDriver({urls(cfg)["r/Private new"]: json.dumps({"reason": "private", "error": 403}),
                             self.b: listing(post("b1"))})
        out = scrape.run(cfg, root_config(), driver=driver)
        self.assertFalse(out["halted"])
        self.assertEqual(list(store.load_leads()), ["t3_b1"])

    def test_logged_out_or_the_wrong_account_reads_nothing(self):
        for driver in (FakeDriver({ME_URL: json.dumps({"data": {}})}, logged_in_as=None),
                       FakeDriver({self.a: listing(post("a1"))}, logged_in_as="someone_else")):
            out = scrape.run(self.cfg, root_config(), driver=driver)
            self.assertTrue(out["halted"])
            self.assertIn("login", out["stopped"])
            self.assertEqual(store.load_leads(), {})
            self.assertNotIn(self.a, driver.visited)

    def test_rerunning_only_adds_new_posts(self):
        scrape.run(self.cfg, root_config(), driver=FakeDriver({self.a: listing(post("a1")), self.b: listing()}))
        out = scrape.run(self.cfg, root_config(),
                         driver=FakeDriver({self.a: listing(post("a1"), post("a2")), self.b: listing()}))
        self.assertEqual(out["new"], 1)
        self.assertEqual(sorted(store.load_leads()), ["t3_a1", "t3_a2"])


if __name__ == "__main__":
    unittest.main()
