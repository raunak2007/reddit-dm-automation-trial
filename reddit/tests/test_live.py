"""test_live.py - read-only checks against the real reddit.com, with the Chrome profile in config.json. Off unless
REDDIT_LIVE=1. Nothing is sent, posted, voted on or marked read: it opens three .json pages through browser.fetch_json
(the way every Reddit read here is done) and checks that what this folder relies on still holds:

    logged in as the root config.json's sender_username          /api/me.json
    the inbox reads as a listing replies.py can parse            /message/inbox/.json?mark=false
    one subreddit's newest posts parse with scrape.parse_post    /r/<first search.subreddits>/new.json
                                                                 (or REDDIT_LIVE_SUB=<name>)

    venv/bin/python reddit/run.py livecheck       (sets REDDIT_LIVE=1; exit 0 passed, 2 account problem, 1 other)
    REDDIT_LIVE=1 venv/bin/python -m unittest discover reddit/tests -p test_live.py -v

Close every other Reddit script first: Chrome won't open the same profile twice. An account problem (logged out,
wrong account, rate-limited, a block page) surfaces as send_dm.AccountBlocked, which run.py livecheck turns into
exit code 2.
"""
from __future__ import annotations

import os
import unittest

import helpers  # noqa: F401  (puts the reddit folder on sys.path)

import browser
import replies
import scrape
import store

driver = cfg = cfg_root = None


class LiveReddit(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        global driver, cfg, cfg_root
        if os.environ.get("REDDIT_LIVE") != "1":       # read here, not at import, so run.py can set it
            raise unittest.SkipTest("set REDDIT_LIVE=1 to check against the real reddit.com")
        cfg, cfg_root = store.load_config(), store.load_root_config()
        driver = browser.make_driver(cfg)
        cls.user = None
        try:
            cls.user = browser.check_login(driver, cfg_root)   # AccountBlocked: logged out / wrong account
        except BaseException:
            driver.quit()
            raise

    @classmethod
    def tearDownClass(cls):
        driver.quit()

    def test_logged_in_as_the_sender(self):
        expected = cfg_root.get("sender_username")
        self.assertTrue(expected, f'the root config.json has no "sender_username" (logged in as u/{self.user})')
        self.assertEqual(self.user.lower(), expected.strip().lower().removeprefix("u/"))

    def test_the_inbox_reads_without_marking_anything(self):
        self.assertIn("mark=false", replies.INBOX)
        browser.pause(cfg, 0.5)
        data = browser.fetch_json(driver, replies.INBOX)
        children = data["data"]["children"]
        self.assertIsInstance(children, list)
        rows = [replies.parse_message(c) for c in replies.flatten(children)]
        print(f"\n    inbox: {len(children)} items, {sum(r is not None for r in rows)} parse as messages")

    def test_one_subreddit_new_feed_parses(self):
        sub = (os.environ.get("REDDIT_LIVE_SUB") or cfg["search"]["subreddits"][0]).strip().removeprefix("r/")
        browser.pause(cfg, 0.5)
        data = browser.fetch_json(driver, f"{scrape.OLD}/r/{sub}/new.json?limit=25&raw_json=1")
        children = data["data"]["children"]
        self.assertTrue(children, f"r/{sub}/new.json listed no posts")
        self.assertTrue(all(c.get("kind") == "t3" for c in children), "not a listing of posts")
        kept = [scrape.parse_post(c, cfg, self.user) for c in children]
        self.assertTrue(all(p["post_id"].startswith("t3_") and p["author"] for p in kept if p))
        print(f"\n    r/{sub}: {len(children)} posts, {sum(p is not None for p in kept)} kept by scrape.parse_post")


if __name__ == "__main__":
    unittest.main()
