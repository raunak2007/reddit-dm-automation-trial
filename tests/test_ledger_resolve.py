"""test_ledger_resolve.py - ledger.py settling held Reddit sends from the sent folder on its own. No browser:
a fake reddit_json serves sent-folder pages, and contacted.csv lives in a temp folder.

    venv/bin/python -m unittest discover tests
"""
from __future__ import annotations

import io
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ledger  # noqa: E402
from send_dm import Result  # noqa: E402


def page(*msgs, after=None):
    return {"data": {"after": after, "children": [{"data": m} for m in msgs]}}


def msg(dest, when):
    return {"dest": dest, "subject": "Hello", "created_utc": when.timestamp()}


class HeldSends(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for p in (mock.patch.object(ledger, "LOG", Path(tmp.name) / "contacted.csv"),
                  mock.patch.object(sys, "stdout", io.StringIO())):
            p.start()
            self.addCleanup(p.stop)
        self.hour_ago = datetime.now() - timedelta(hours=1)

    def hold(self, user, when=None, status="uncertain"):
        ledger.record(user, Result(status, "test"), when or self.hour_ago)

    def sync(self, *pages, auto_resolve=True):
        served = iter(pages)
        with mock.patch.object(ledger, "reddit_json", lambda driver, url: next(served)):
            ledger.sync_reddit_sent(None, auto_resolve=auto_resolve)

    def last(self, user):
        return ledger._history()[ledger.key(user)][-1]["status"]

    def test_held_send_in_the_sent_folder_is_settled_as_sent(self):
        self.hold("alice")
        self.sync(page(msg("alice", self.hour_ago + timedelta(seconds=5))))
        self.assertEqual(self.last("alice"), "sent")
        self.assertEqual(ledger.held(), {})

    def test_held_send_missing_from_a_complete_folder_is_cleared(self):
        self.hold("bob")
        self.hold("carol", status="submitting")
        self.sync(page(msg("alice", self.hour_ago)))
        self.assertEqual(self.last("bob"), "cleared")
        self.assertEqual(self.last("carol"), "cleared")
        self.assertIsNone(ledger.skip_reason("bob"), "cleared sends can be retried")

    def test_listing_that_stops_before_the_attempt_settles_nothing(self):
        self.hold("bob", self.hour_ago - timedelta(days=30))
        newest = page(msg("alice", self.hour_ago), after="t4_x")       # more pages Reddit won't list
        self.sync(*[newest] * (ledger.SENT_LISTING_CAP // 100 + 2))
        self.assertEqual(self.last("bob"), "uncertain")

    def test_recent_held_send_waits_for_reddit_to_list_it(self):
        self.hold("bob", datetime.now() - timedelta(minutes=2))
        self.sync(page())
        self.assertEqual(self.last("bob"), "uncertain")

    def test_auto_resolve_off_leaves_it_held(self):
        self.hold("bob")
        self.sync(page(), auto_resolve=False)
        self.assertEqual(self.last("bob"), "uncertain")

    def test_resolve_held_with_nothing_listed_settles_nothing(self):
        self.hold("bob")
        self.assertEqual(ledger.resolve_held(None, now=time.time()), 0)
        self.assertEqual(self.last("bob"), "uncertain")


if __name__ == "__main__":
    unittest.main()
