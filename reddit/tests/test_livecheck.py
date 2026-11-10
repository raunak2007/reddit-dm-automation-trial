"""test_livecheck.py - `run.py livecheck` run offline: tests/test_live.py's real checks against a fake browser, and the
exit code it turns their result into (0 passed, 2 an account problem, 1 anything else). Also that test_live.py is
skipped without REDDIT_LIVE=1, and that the check only reads (.json pages, mark=false, no compose or API writes).

    venv/bin/python -m unittest discover reddit/tests
"""
from __future__ import annotations

import io
import json
import os
import unittest
from unittest import mock

from helpers import SENDER, FakeDriver, LedgerTest, listing, message, post

import browser
import replies
import run
import scrape
import store
import test_live

SUB = store.load_config()["search"]["subreddits"][0]
NEW = f"{scrape.OLD}/r/{SUB}/new.json"


class LiveCheck(LedgerTest):
    def setUp(self):
        super().setUp()
        self.patch(browser, "pause", lambda *a, **k: None)
        self.patch(os, "environ", dict(os.environ))
        self.drivers = []

    def check(self, pages=None, **kw):
        base = {replies.INBOX: listing(message("m1", "someone")), NEW: listing(post("a"), post("b", author=SENDER))}

        def make(cfg):
            d = FakeDriver(dict(base, **(pages or {})), **kw)
            self.drivers.append(d)
            return d
        self.patch(browser, "make_driver", make)
        out = io.StringIO()
        code = run.livecheck(stream=out)
        return code, out.getvalue()

    def test_passes_with_exit_0_and_only_reads(self):
        code, out = self.check()
        self.assertEqual(code, 0, out)
        visited = self.drivers[0].visited
        self.assertTrue(all(".json" in u for u in visited), visited)
        self.assertTrue(all("mark=false" in u for u in visited if "/message/inbox" in u))
        self.assertFalse([u for u in visited if "/compose" in u or ("/api/" in u and "me.json" not in u)])
        self.assertTrue(self.drivers[0].quit_called)

    def test_logged_out_or_wrong_account_is_exit_2(self):
        self.assertEqual(self.check({"https://old.reddit.com/api/me.json": json.dumps({"data": {}})})[0], 2)
        self.assertEqual(self.check(logged_in_as="someone_else")[0], 2)

    def test_a_block_page_or_rate_limit_is_exit_2(self):
        self.assertEqual(self.check({replies.INBOX: "<html>whoa there, pardner!</html>"})[0], 2)
        self.assertEqual(self.check({NEW: json.dumps({"error": 429, "message": "Too Many Requests"})})[0], 2)

    def test_anything_else_is_exit_1(self):
        self.assertEqual(self.check({NEW: json.dumps({"kind": "Listing", "data": {"children": []}})})[0], 1)
        self.assertEqual(self.check({NEW: json.dumps({"error": 403, "reason": "private"})})[0], 1)

    def test_no_sender_username_is_exit_1(self):
        self.patch(store, "load_root_config", lambda: {"daily_limit_per_account": 1})
        self.assertEqual(self.check()[0], 1)

    def test_skipped_without_reddit_live(self):
        os.environ.pop("REDDIT_LIVE", None)
        result = unittest.TestResult()
        unittest.defaultTestLoader.loadTestsFromModule(test_live).run(result)
        self.assertEqual((result.testsRun, len(result.skipped), result.errors), (0, 1, []))
        self.assertEqual(run.livecheck_code(result), 1)


if __name__ == "__main__":
    unittest.main()
