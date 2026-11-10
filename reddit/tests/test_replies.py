"""test_replies.py - finding replies from people in contacted.csv in a fake inbox: who counts, dedupe by message id,
nothing marked read, contacted.csv left alone, and stopping on account-wide problems.

    venv/bin/python -m unittest discover reddit/tests
"""
from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta

from helpers import SENDER, FakeDriver, LedgerTest, ago, config, listing, message, root_config

import ledger
import replies
import store


class Match(LedgerTest):
    def test_only_people_sent_to_and_only_after_the_first_send(self):
        self.mark("Messaged", "sent", datetime.now() - timedelta(days=1))
        self.mark("only_failed", "not_submitted")
        kids = [message("m1", "messaged"), message("m2", "only_failed"), message("m3", "stranger"),
                message("m4", "Messaged", created=ago(days=3)), message("m5", SENDER)]
        rows = replies.match(kids, store.first_sent(), set(), SENDER)
        self.assertEqual([r["message_id"] for r in rows], ["t4_m1"])
        r = rows[0]
        self.assertEqual(r["username"], "messaged")
        self.assertEqual(r["kind"], "message")
        self.assertEqual(r["permalink"], "https://www.reddit.com/message/messages/m1")
        self.assertEqual(r["snippet"], "thanks!")

    def test_comment_replies_nested_messages_snippets_and_known_ids(self):
        self.mark("a_person", "sent", datetime.now() - timedelta(days=1))
        thread = message("first", "a_person", body="hi")
        thread["data"]["replies"] = {"kind": "Listing", "data": {"children": [
            message("later", "a_person", body="word " * 100, first_message_name="t4_first")]}}
        kids = [thread, message("c1", "a_person", kind="t1")]
        rows = replies.match(kids, store.first_sent(), {"t4_first"})
        self.assertEqual([r["message_id"] for r in rows], ["t4_later", "t1_c1"])
        self.assertEqual(rows[0]["permalink"], "https://www.reddit.com/message/messages/first")
        self.assertLessEqual(len(rows[0]["snippet"]), 160)
        self.assertEqual(rows[1]["kind"], "comment_reply")
        self.assertTrue(rows[1]["permalink"].startswith("https://www.reddit.com/r/ApplyingToCollege/comments/"))


class Run(LedgerTest):
    def setUp(self):
        super().setUp()
        self.mark("lead_one", "sent", datetime.now() - timedelta(days=2))
        self.mark("lead_two", "sent", datetime.now() - timedelta(days=2))

    def inbox(self, *kids, after=None):
        return {replies.INBOX: listing(*kids, after=after)}

    def test_replies_are_recorded_once_and_contacted_csv_is_untouched(self):
        before = ledger.LOG.read_bytes()
        pages = self.inbox(message("m1", "lead_one", body="Yes please, how do I book?"), message("m2", "random"))
        self.assertEqual(replies.run(config(), root_config(), driver=FakeDriver(pages)), 0)
        self.assertEqual(replies.run(config(), root_config(), driver=FakeDriver(pages)), 0)
        rows = store.load_replies()
        self.assertEqual([(r["message_id"], r["username"]) for r in rows], [("t4_m1", "lead_one")])
        self.assertTrue(rows[0]["recorded_at"])
        self.assertEqual(ledger.LOG.read_bytes(), before)
        self.assertIn("1 replies from 1 of the 2 people messaged", self.out.getvalue())

    def test_the_inbox_is_read_without_marking_anything_read(self):
        driver = FakeDriver(self.inbox(message("m1", "lead_one")))
        replies.run(config(), root_config(), driver=driver)
        inbox = [u for u in driver.visited if "/message/inbox" in u]
        self.assertTrue(inbox)
        self.assertTrue(all("mark=false" in u for u in inbox))
        self.assertFalse([u for u in driver.visited if "/api/" in u and "me.json" not in u], "no API writes")

    def test_pages_are_followed(self):
        pages = {replies.INBOX: listing(message("m1", "lead_one"), after="t4_m1"),
                 replies.INBOX + "&after=t4_m1": listing(message("m2", "lead_two"))}
        replies.run(config(), root_config(), driver=FakeDriver(pages))
        self.assertEqual(sorted(r["message_id"] for r in store.load_replies()), ["t4_m1", "t4_m2"])

    def test_a_rate_limit_stops_with_exit_code_2(self):
        pages = {replies.INBOX: json.dumps({"message": "Too Many Requests", "error": 429})}
        self.assertEqual(replies.run(config(), root_config(), driver=FakeDriver(pages)), 2)
        self.assertEqual(store.load_replies(), [])

    def test_wrong_account_stops_with_exit_code_2(self):
        driver = FakeDriver(self.inbox(message("m1", "lead_one")), logged_in_as="someone_else")
        self.assertEqual(replies.run(config(), root_config(), driver=driver), 2)
        self.assertNotIn(replies.INBOX, driver.visited)


if __name__ == "__main__":
    unittest.main()
