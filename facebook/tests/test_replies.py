"""test_replies.py - how replies.py tells a reply from our own message, who it checks, and what it does on a block.
No browser: FakeChrome (helpers.py) hands messenger.py's real open_chat a page and its message rows (test_mock_site.py
reads real rows from a mock chat in Chrome).

    cd facebook && ../venv/bin/python -m unittest discover tests -p test_replies.py
"""
from __future__ import annotations

import unittest

from helpers import FakeChrome, StoreTest, config, lead

import messenger
import replies
import store

DM = "Hi Person 1, this is message 1. Book a free call: https://example.com/#get-started"


def me(text):
    return {"who": "me", "text": text, "label": "You sent", "side": "me"}


def them(text):
    return {"who": "them", "text": text, "label": "Person 1", "side": "them"}


class FindReply(unittest.TestCase):
    def test_a_reply_after_ours_counts(self):
        self.assertEqual(replies.find_reply([me(DM), them("Yes please, how do I book?")], DM),
                         "Yes please, how do I book?")

    def test_only_our_message_is_not_a_reply(self):
        self.assertIsNone(replies.find_reply([me(DM)], DM))

    def test_their_message_before_our_dm_does_not_count(self):
        self.assertIsNone(replies.find_reply([them("hey, saw you in the group"), me(DM)], DM))

    def test_rows_that_cant_be_told_apart_never_count(self):
        unknown = {"who": "", "text": "Today 3:04 PM", "label": "", "side": ""}
        self.assertIsNone(replies.find_reply([me(DM), unknown], DM))

    def test_our_dm_split_into_a_text_row_and_a_link_row(self):
        rows = [me("Hi Person 1, this is message 1. Book a free call:"), me("https://example.com/#get-started"),
                them("thanks")]
        self.assertEqual(replies.find_reply(rows, DM), "thanks")

    def test_a_photo_from_them_counts(self):
        self.assertEqual(replies.find_reply([me(DM), them("")], DM), "[attachment]")

    def test_our_dm_not_in_the_chat_says_nothing(self):
        with self.assertRaises(LookupError):
            replies.find_reply([them("hello")], DM)
        with self.assertRaises(LookupError):
            replies.find_reply([them(DM)], DM)          # our text, but from them: not our DM


class Watching(StoreTest):
    def setUp(self):
        super().setUp()
        self.patch(messenger, "SETTLE", 0)

    def sent(self, *leads):
        store.save_leads({l["post_key"]: l for l in leads})
        for l in leads:
            store.record(l["author_key"], l["author"], l["post_key"], "sent", "fake: sent")
        return leads

    def chat(self, l, rows):
        return {store.messenger_url(l["author_key"]): {"text": f"{l['author']} {l['approved']['dm']}",
                                                       "composer": True, "rows": rows}}

    def test_records_a_reply_once_and_never_types(self):
        a, b = self.sent(lead(1), lead(2))
        driver = FakeChrome({**self.chat(a, [me(a["approved"]["dm"]), them("Yes! Tuesday works")]),
                             **self.chat(b, [them("old hello"), me(b["approved"]["dm"])])})
        counts = replies.check_loop(driver, replies.to_check(config()), config())
        self.assertEqual((counts["replied"], counts["no_reply"]), (1, 1))
        self.assertEqual(replies.report(counts), 0)
        row = store.replies()[a["author_key"]]
        self.assertEqual((row["author"], row["post_key"], row["snippet"], row["chat_url"]),
                         (a["author"], a["post_key"], "Yes! Tuesday works", store.messenger_url(a["author_key"])))
        self.assertTrue(row["seen_at"])
        self.assertEqual(driver.box.typed, [])

        driver.visited.clear()
        replies.check_loop(driver, replies.to_check(config()), config())
        self.assertEqual(driver.visited, [store.messenger_url(b["author_key"])], "a replied chat isn't opened again")
        self.assertEqual(len(store.replies()), 1)
        store.record_reply(a["author_key"], a["author"], a["post_key"], "again", "x")
        self.assertEqual(store.replies()[a["author_key"]]["snippet"], "Yes! Tuesday works", "deduped")

    def test_only_sent_dms_are_checked(self):
        a, b, c = lead(1), lead(2), lead(3)
        self.sent(a)
        for l, status in ((b, "uncertain"), (c, "commented")):
            store.record(l["author_key"], l["author"], l["post_key"], status, "")
        self.assertEqual([p[0] for p in replies.to_check(config())], [a["author_key"]])

    def test_a_block_stops_the_run_with_exit_2(self):
        a, b = self.sent(lead(1), lead(2))
        pages = {**self.chat(a, []), **self.chat(b, [])}
        pages[store.messenger_url(b["author_key"])]["text"] += " You're Temporarily Blocked"
        driver = FakeChrome(pages)
        counts = replies.check_loop(driver, replies.to_check(config()), config())   # most recent first: b
        self.assertTrue(counts["halted"])
        self.assertEqual(replies.report(counts), 2)
        self.assertNotIn(store.messenger_url(a["author_key"]), driver.visited)

    def test_a_chat_without_our_dm_is_left_for_the_next_run(self):
        a, = self.sent(lead(1))
        driver = FakeChrome(self.chat(a, [them("hi")]))
        counts = replies.check_loop(driver, replies.to_check(config()), config())
        self.assertEqual(counts["unreadable"], 1)
        self.assertEqual(store.replies(), {})

    def test_cap_per_run(self):
        self.sent(*(lead(n) for n in range(1, 6)))
        self.assertEqual(len(replies.to_check(config())), 5)
        opened = []
        self.patch(replies, "check_loop", side_effect=lambda d, people, cfg: opened.extend(people) or
                   {"checked": 0, "replied": 0, "no_reply": 0, "unreadable": 0, "stopped": "", "halted": False})
        self.patch(replies.browser, "make_driver")
        self.patch(replies.send, "prepare")
        self.assertEqual(replies.run(config(replies_per_run=2)), 0)
        self.assertEqual(len(opened), 2)


if __name__ == "__main__":
    unittest.main()
