"""test_replies.py - how replies.py tells a reply from our own message, who it checks, and what it does on a block.
No browser: FakeChrome (helpers.py) hands messenger.py's real open_chat a page and its message rows (test_mock_site.py
reads real rows from a mock chat in Chrome).

    cd facebook && ../venv/bin/python -m unittest discover tests -p test_replies.py
"""
from __future__ import annotations

import csv
import unittest
from types import SimpleNamespace
from datetime import datetime, timedelta

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


class FullText(StoreTest):
    def test_the_whole_reply_is_kept_next_to_the_snippet(self):
        long = "Yes please. " + "We are looking at schools in Ohio and need help with essays. " * 100
        store.record_reply("1001", "Person 1", "p:1", long[:replies.SNIPPET], "u", text=long)
        row = store.replies()["1001"]
        self.assertEqual(row["snippet"], long[:replies.SNIPPET])
        self.assertEqual(row["text"], long[:store.REPLY_TEXT_MAX])
        self.assertEqual(len(row["text"]), store.REPLY_TEXT_MAX)

    def test_check_loop_records_every_message_after_our_dm(self):
        l = lead(1)
        store.save_leads({l["post_key"]: l})
        store.record(l["author_key"], l["author"], l["post_key"], "sent", "fake: sent")
        rows = [them("before"), me(l["approved"]["dm"]), them("Thanks!"), them("Is Tuesday ok?")]
        driver = FakeChrome({store.messenger_url(l["author_key"]): {"text": f"{l['author']} {l['approved']['dm']}",
                                                                   "composer": True, "rows": rows}})
        self.patch(messenger, "SETTLE", 0)
        replies.check_loop(driver, replies.to_check(config()), config())
        self.assertEqual(store.replies()[l["author_key"]]["text"], "Thanks! / Is Tuesday ok?")

    def test_an_old_file_without_the_text_column_still_reads_and_is_upgraded(self):
        store.DATA.mkdir(parents=True, exist_ok=True)
        store.REPLIES.write_text("author_key,author,post_key,seen_at,snippet,chat_url\n"
                                 "1001,Person 1,p:1,2026-10-01T10:00:00,\"hi, yes\",https://www.facebook.com/messages/t/1001\n")
        old = store.replies()["1001"]
        self.assertEqual((old["snippet"], old["text"], old["chat_url"]),
                         ("hi, yes", "hi, yes", "https://www.facebook.com/messages/t/1001"))
        self.assertFalse(store.record_reply("1001", "Person 1", "p:1", "again", "x"), "still deduped")
        self.assertTrue(store.record_reply("1002", "Person 2", "p:2", "short", "u2", text="short and long"))
        self.assertTrue(store.REPLIES.read_text().startswith(",".join(store.REPLY_FIELDS)))
        rows = store.replies()
        self.assertEqual((rows["1001"]["text"], rows["1001"]["chat_url"]),
                         ("hi, yes", "https://www.facebook.com/messages/t/1001"))
        self.assertEqual((rows["1002"]["snippet"], rows["1002"]["text"], rows["1002"]["chat_url"]),
                         ("short", "short and long", "u2"))


ANSWER = "Hi Person 1, the first call is free. Tuesday or Wednesday?"


class AfterOurAnswer(StoreTest):
    """Someone we answered from the inbox is checked again, and each new message of theirs gets its own row."""

    def setUp(self):
        super().setUp()
        self.patch(messenger, "SETTLE", 0)
        self.l = lead(1)
        self.l["approved"]["dm"] = DM
        store.save_leads({self.l["post_key"]: self.l})
        store.record(self.l["author_key"], self.l["author"], self.l["post_key"], "sent", "fake: sent")
        then = (datetime.now() - timedelta(hours=2)).isoformat(timespec="seconds")
        store.DATA.mkdir(parents=True, exist_ok=True)
        with open(store.REPLIES, "w", newline="") as f:     # their first reply, recorded two hours ago
            w = csv.DictWriter(f, store.REPLY_FIELDS)
            w.writeheader()
            w.writerow({"author_key": self.l["author_key"], "author": self.l["author"], "post_key": self.l["post_key"],
                        "seen_at": then, "snippet": "How much is it?", "text": "How much is it?",
                        "chat_url": store.messenger_url(self.l["author_key"])})
        a = self.inbox.add_draft("facebook", f"{self.l['author_key']}:{then}", person=self.l["author_key"],
                                 name=self.l["author"], chat_url=store.messenger_url(self.l["author_key"]),
                                 their_message="How much is it?", our_message=DM, draft=ANSWER)
        self.inbox.update(a["id"], status="sent",
                          sent_at=(datetime.now() - timedelta(hours=1)).isoformat(timespec="seconds"))

    def check(self, rows, only_them=False):
        driver = FakeChrome({store.messenger_url(self.l["author_key"]): {
            "text": f"{self.l['author']} {DM}", "composer": True, "rows": rows}})
        people = [p for p in replies.plan(config()) if not only_them or p[0] == self.l["author_key"]]
        counts = replies.check_loop(driver, people, config())
        self.assertEqual(driver.box.typed, [])
        return counts

    def texts(self):
        return [r["text"] for r in store.reply_rows(self.l["author_key"])]

    def test_a_second_reply_after_our_answer_is_recorded_once(self):
        chat = [me(DM), them("How much is it?"), me(ANSWER), them("Tuesday works!")]
        self.assertEqual(self.check(chat)["new_messages"], 1)
        self.assertEqual(self.texts(), ["How much is it?", "Tuesday works!"])
        self.check(chat)
        self.assertEqual(self.texts(), ["How much is it?", "Tuesday works!"], "not recorded twice")
        self.check(chat + [them("Is 4pm ok?"), them("ok")])
        self.assertEqual(self.texts(), ["How much is it?", "Tuesday works!", "Is 4pm ok?", "ok"])
        seen = [r["seen_at"] for r in store.reply_rows(self.l["author_key"])]
        self.assertEqual(len(set(seen)), len(seen), "seen_at is unique per person: the inbox keys on it")
        self.assertEqual(store.replies()[self.l["author_key"]]["text"], "ok", "replies() gives the latest row")

    def test_nothing_new_means_no_row(self):
        counts = self.check([me(DM), them("How much is it?"), me(ANSWER)])
        self.assertEqual((counts["new_messages"], counts["no_reply"]), (0, 1))
        self.assertEqual(self.texts(), ["How much is it?"])

    def test_our_own_answer_and_later_messages_are_not_counted(self):
        self.check([me(DM), them("How much is it?"), me(ANSWER), me("Also, here is the essay guide")])
        self.assertEqual(self.texts(), ["How much is it?"])

    def test_their_messages_before_our_answer_are_not_new(self):
        self.check([me(DM), them("How much is it?"), them("hello?"), me(ANSWER)])
        self.assertEqual(self.texts(), ["How much is it?"])

    def test_a_chat_without_our_messages_is_left_for_the_next_run(self):
        counts = self.check([them("How much is it?"), them("Tuesday works!")])
        self.assertEqual(counts["unreadable"], 1)
        self.assertEqual(self.texts(), ["How much is it?"])

    def test_waiting_on_them_comes_first_within_the_cap(self):
        other = lead(2)
        store.save_leads({**store.load_leads(), other["post_key"]: other})
        store.record(other["author_key"], other["author"], other["post_key"], "sent", "fake: sent")
        plan = replies.plan(config())
        self.assertEqual([p[0] for p in plan], [self.l["author_key"], other["author_key"]])
        self.assertEqual(len(plan[0]), 6, "a re-check carries since and our texts")
        self.check([me(DM), them("How much is it?"), me(ANSWER), them("Tuesday works!")], only_them=True)
        self.assertEqual([p[0] for p in replies.plan(config())], [self.l["author_key"], other["author_key"]],
                         "answered people still come before never-checked chats")


def load_draft():
    """inbox/draft.py, imported with its own `import store` meaning inbox/store.py (here `store` is facebook's)."""
    import sys
    from inbox import store as inbox_store
    mine = sys.modules["store"]
    sys.modules["store"] = inbox_store
    try:
        from inbox import draft
    finally:
        sys.modules["store"] = mine
    assert draft.store is inbox_store
    return draft


class BeforeWeAnswer(StoreTest):
    """Someone with a recorded reply and no answer sent yet is checked again; a further message gets its own row,
    and the next inbox draft covers the whole conversation."""

    def setUp(self):
        super().setUp()
        self.patch(messenger, "SETTLE", 0)
        self.l = lead(1)
        self.l["approved"]["dm"] = DM
        store.save_leads({self.l["post_key"]: self.l})
        store.record(self.l["author_key"], self.l["author"], self.l["post_key"], "sent", "fake: sent")

    def check(self, rows):
        driver = FakeChrome({store.messenger_url(self.l["author_key"]): {
            "text": f"{self.l['author']} {DM}", "composer": True, "rows": rows}})
        counts = replies.check_loop(driver, replies.plan(config()), config())
        self.assertEqual(driver.box.typed, [])
        return counts

    def texts(self):
        return [r["text"] for r in store.reply_rows(self.l["author_key"])]

    def test_a_further_message_before_our_answer_gets_its_own_row_once(self):
        self.check([me(DM), them("How much is it?")])
        self.check([me(DM), them("How much is it?")])
        self.assertEqual(self.texts(), ["How much is it?"], "nothing new: no row")
        chat = [me(DM), them("How much is it?"), them("Also, do you help with essays?")]
        self.assertEqual(self.check(chat)["new_messages"], 1)
        self.check(chat)
        self.assertEqual(self.texts(), ["How much is it?", "Also, do you help with essays?"])
        seen = [r["seen_at"] for r in store.reply_rows(self.l["author_key"])]
        self.assertEqual(len(set(seen)), 2)

    def test_dismissed_or_uncertain_answers_are_left_alone(self):
        self.check([me(DM), them("How much is it?")])
        a = self.inbox.add_draft("facebook", "x", person=self.l["author_key"], name="", chat_url="",
                                 their_message="", our_message="", draft="d")
        for status, rechecked in (("drafted", True), ("approved", True), ("failed", True), ("dismissed", False),
                                  ("uncertain", False)):
            self.inbox.update(a["id"], status=status)
            self.assertEqual(self.l["author_key"] in [p[0] for p in replies.plan(config())], rechecked, status)

    def test_waiting_drafts_then_answered_then_never_checked(self):
        waiting, answered_, fresh = lead(2), lead(3), lead(4)
        store.save_leads({**store.load_leads(), **{l["post_key"]: l for l in (waiting, answered_, fresh)}})
        for l in (waiting, answered_, fresh):
            store.record(l["author_key"], l["author"], l["post_key"], "sent", "fake: sent")
        for l in (waiting, answered_):
            store.record_reply(l["author_key"], l["author"], l["post_key"], "hi", "u", text="hi")
        a = self.inbox.add_draft("facebook", f"{answered_['author_key']}:x", person=answered_["author_key"],
                                 name="", chat_url="", their_message="hi", our_message="", draft="answer")
        self.inbox.update(a["id"], status="sent")
        order = [p[0] for p in replies.plan(config())]
        self.assertEqual(order[:2], [waiting["author_key"], answered_["author_key"]])
        self.assertEqual(set(order[2:]), {fresh["author_key"], self.l["author_key"]}, "never-checked chats last")

    def test_end_to_end_the_redraft_covers_both_messages(self):
        draft = load_draft()
        self.patch(draft, "PATHS", {**draft.PATHS, "facebook": {"replies": store.REPLIES, "leads": store.LEADS,
                                                                "contacted": store.CONTACTED}})
        asked = []

        def create(**kw):
            asked.append(kw["messages"][0]["content"])
            return SimpleNamespace(content=[SimpleNamespace(type="tool_use", input={
                "intent": "question", "suggest_dismiss": False, "reason": "price", "answer": f"answer {len(asked)}"})])

        cl = SimpleNamespace(messages=SimpleNamespace(create=create))
        cfg = draft.load_config()

        self.check([me(DM), them("How much is it?")])
        first, = draft.run(cl=cl, cfg=cfg)
        self.assertEqual(first["their_message"], "How much is it?")

        self.check([me(DM), them("How much is it?"), them("Also, do you help with essays?")])
        second, = draft.run(cl=cl, cfg=cfg)
        ids = [f"{r['author_key']}:{r['seen_at']}" for r in store.reply_rows(self.l["author_key"])]
        self.assertEqual(second["covers"], ids, "the new draft covers both messages")
        self.assertIn("How much is it?", second["their_message"])
        self.assertIn("Also, do you help with essays?", second["their_message"])
        self.assertIn("Also, do you help with essays?", asked[-1])
        self.assertIn("How much is it?", asked[-1])
        old = self.inbox.get(first["id"])
        self.assertEqual(old["status"], "dismissed", "the untouched first draft is replaced")
        self.assertEqual(draft.run(cl=cl, cfg=cfg), [], "nothing new: nothing drafted")


if __name__ == "__main__":
    unittest.main()
