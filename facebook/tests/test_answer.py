"""test_answer.py - python run.py answer: sending the answers approved on the inbox page (../inbox/store.py, in a
temp dir) into each Messenger chat. No browser: a scripted send function, or FakeChrome (helpers.py) under
messenger.py's real send_message.

    cd facebook && ../venv/bin/python -m unittest discover tests -p test_answer.py
"""
from __future__ import annotations

from helpers import FakeChrome, StoreTest, config

import answer
import messenger
import store
from send_dm import Result


def entry(inbox, n, name=None, status="approved", platform="facebook", chat_url=None):
    name = name or f"Person {n}"
    akey = f"10{n:03d}"
    a = inbox.add_draft(platform, f"{akey}:2026-11-0{n}T10:00:00", person=akey, name=name,
                        chat_url=store.messenger_url(akey) if chat_url is None else chat_url,
                        their_message="How much is a call?", our_message=f"Hi {name}, ...",
                        draft=f"Hi {name}, the first call is free. Book here: https://example.com/{n}")
    if status != "drafted":
        inbox.update(a["id"], status=status)
    return inbox.get(a["id"])


class Sender:
    """A send function for answer_loop: answers from a script ({person: Result or exception}, default 'sent'),
    presses (runs before_submit) unless told not to, and notes the entry's inbox status at the press."""

    def __init__(self, inbox, answers=None, no_press=()):
        self.inbox, self.answers, self.no_press, self.calls, self.at_press = inbox, answers or {}, set(no_press), [], {}

    def __call__(self, driver, url, text, name, *, before_submit=None):
        person = url.rsplit("/", 1)[-1]
        self.calls.append((person, url, text, name))
        if person not in self.no_press:
            before_submit()
            self.at_press[person] = [a["status"] for a in self.inbox.load().values() if a["person"] == person][0]
        answer_ = self.answers.get(person, Result("sent", "fake: sent"))
        if isinstance(answer_, Exception):
            raise answer_
        return answer_


class AnswerTest(StoreTest):
    def setUp(self):
        super().setUp()
        self.patch(messenger, "SETTLE", 0)
        self.patch(messenger, "pause", lambda *a: None)

    def status(self, a):
        return self.inbox.get(a["id"])["status"]


class Sending(AnswerTest):
    def test_sends_approved_facebook_entries_marking_sending_before_the_press(self):
        a, b = entry(self.inbox, 1), entry(self.inbox, 2)
        drafted = entry(self.inbox, 3, status="drafted")
        other = entry(self.inbox, 4, platform="instagram")
        fake = Sender(self.inbox)
        counts = answer.answer_loop(None, self.inbox.approved("facebook"), config(), send_fn=fake)

        self.assertEqual([c[0] for c in fake.calls], [a["person"], b["person"]])
        self.assertEqual(fake.at_press, {a["person"]: "sending", b["person"]: "sending"})
        self.assertEqual((self.status(a), self.status(b)), ("sent", "sent"))
        self.assertTrue(self.inbox.get(a["id"])["sent_at"])
        self.assertEqual((self.status(drafted), self.status(other)), ("drafted", "approved"))
        self.assertEqual(fake.calls[0][2:], (a["text"], a["name"]), "the text and the name to check the chat by")
        self.assertEqual(answer.report(counts), 0)

    def test_falls_back_to_the_persons_messenger_link(self):
        a = entry(self.inbox, 1, chat_url="https://www.reddit.com/message/x")
        fake = Sender(self.inbox)
        answer.answer_loop(None, [a], config(), send_fn=fake)
        self.assertEqual(fake.calls[0][1], store.messenger_url(a["person"]))

    def test_already_in_the_chat_is_sent_and_nothing_is_typed(self):
        a = entry(self.inbox, 1)
        driver = FakeChrome({a["chat_url"]: {"text": f"{a['name']} {a['text']}", "composer": True}})
        counts = answer.answer_loop(driver, [a], config())
        self.assertEqual(self.status(a), "sent")
        self.assertIn("already in the chat", self.inbox.get(a["id"])["reason"])
        self.assertEqual(driver.box.typed, [])
        self.assertEqual(counts["sent"], 1)

    def test_a_chat_that_isnt_with_them_fails_before_typing(self):
        a = entry(self.inbox, 1, name="Jane Doe")
        driver = FakeChrome({a["chat_url"]: {"text": "John Roe", "composer": True}})
        answer.answer_loop(driver, [a], config())
        got = self.inbox.get(a["id"])
        self.assertEqual(got["status"], "failed")
        self.assertIn("isn't shown in this chat", got["reason"])
        self.assertEqual(driver.box.typed, [])

    def test_refused_is_failed_with_the_reason(self):
        a = entry(self.inbox, 1)
        fake = Sender(self.inbox, {a["person"]: Result("refused", "messenger: can't message this person")},
                      no_press={a["person"]})
        answer.answer_loop(None, [a], config(), send_fn=fake)
        got = self.inbox.get(a["id"])
        self.assertEqual((got["status"], got["reason"]), ("failed", "refused: messenger: can't message this person"))

    def test_an_entry_changed_on_the_page_is_not_sent(self):
        a, b = entry(self.inbox, 1), entry(self.inbox, 2)
        entries = self.inbox.approved("facebook")
        self.inbox.update(a["id"], status="dismissed")
        fake = Sender(self.inbox)
        answer.answer_loop(None, entries, config(), send_fn=fake)
        self.assertEqual([c[0] for c in fake.calls], [b["person"]])
        self.assertEqual(self.status(a), "dismissed")

    def test_text_edited_while_typing_is_not_sent_and_stays_approved(self):
        a = entry(self.inbox, 1)

        def edit_then_press(driver, url, text, name, *, before_submit=None):
            self.inbox.update(a["id"], text="a newer version")
            try:
                before_submit()
            except RuntimeError as exc:           # as messenger.submit would: never pressed
                return Result("not_submitted", f"messenger: {exc}")
            raise AssertionError("pressed Send with stale text")

        answer.answer_loop(None, [a], config(), send_fn=edit_then_press)
        got = self.inbox.get(a["id"])
        self.assertEqual((got["status"], got["text"]), ("approved", "a newer version"))


class Stops(AnswerTest):
    def test_a_block_before_send_halts_and_leaves_it_approved(self):
        a, b = entry(self.inbox, 1), entry(self.inbox, 2)
        fake = Sender(self.inbox, {a["person"]: Result("rate_limited", "messenger: You're Temporarily Blocked")},
                      no_press={a["person"]})
        counts = answer.answer_loop(None, [a, b], config(), send_fn=fake)
        self.assertTrue(counts["halted"])
        self.assertEqual(answer.report(counts), 2)
        self.assertEqual([c[0] for c in fake.calls], [a["person"]], "nothing is tried after the block")
        got = self.inbox.get(a["id"])
        self.assertEqual(got["status"], "approved")
        self.assertIn("Temporarily Blocked", got["reason"])
        self.assertEqual(self.status(b), "approved")

    def test_a_pin_in_the_chat_halts_before_anything_is_typed(self):
        a = entry(self.inbox, 1, name="Jane Doe")
        driver = FakeChrome({a["chat_url"]: {"text": "Jane Doe Enter your PIN to restore your chat history",
                                             "composer": True}})
        counts = answer.answer_loop(driver, [a], config())
        self.assertTrue(counts["halted"])
        self.assertEqual(self.status(a), "approved")
        self.assertEqual(driver.box.typed, [])

    def test_a_block_after_the_press_is_uncertain_and_halts(self):
        a, b = entry(self.inbox, 1), entry(self.inbox, 2)
        fake = Sender(self.inbox, {a["person"]: Result("rate_limited", "messenger: blocked after Send")})
        counts = answer.answer_loop(None, [a, b], config(), send_fn=fake)
        self.assertTrue(counts["halted"])
        self.assertEqual(self.status(a), "uncertain", "it may have gone out: never resent blind")
        self.assertEqual(self.status(b), "approved")

    def test_a_crash_after_the_press_is_uncertain(self):
        a = entry(self.inbox, 1)
        fake = Sender(self.inbox, {a["person"]: OSError("chrome died")})
        answer.answer_loop(None, [a], config(), send_fn=fake)
        got = self.inbox.get(a["id"])
        self.assertEqual(got["status"], "uncertain")
        self.assertIn("chrome died", got["reason"])

    def test_two_uncertain_in_a_row_halt(self):
        es = [entry(self.inbox, n) for n in (1, 2, 3)]
        fake = Sender(self.inbox, {e["person"]: Result("uncertain", "fake") for e in es})
        counts = answer.answer_loop(None, es, config(), send_fn=fake)
        self.assertTrue(counts["halted"])
        self.assertEqual(len(fake.calls), 2)


class Run(AnswerTest):
    def setUp(self):
        super().setUp()
        self.driver = self.patch(answer.browser, "make_driver")
        self.prepare = self.patch(answer.send, "prepare")
        self.sent = []
        self.patch(answer, "answer_loop", side_effect=lambda d, es, cfg: self.sent.extend(es) or answer.new_counts())

    def test_stuck_entries_become_uncertain_and_are_not_resent(self):
        a, b = entry(self.inbox, 1), entry(self.inbox, 2)
        self.inbox.update(a["id"], status="sending")       # a run died after marking it
        self.assertEqual(answer.run(config()), 0)
        self.assertEqual(self.status(a), "uncertain")
        self.assertEqual([e["id"] for e in self.sent], [b["id"]])

    def test_cap_per_run(self):
        for n in range(1, 6):
            entry(self.inbox, n)
        cfg = config()
        cfg["answers"] = {"max_per_run": 2}
        answer.run(cfg)
        self.assertEqual(len(self.sent), 2)
        self.sent.clear()
        answer.run(config())
        self.assertEqual(len(self.sent), 5, "default 10 a run")

    def test_nothing_approved_opens_no_browser(self):
        entry(self.inbox, 1, status="drafted")
        self.assertEqual(answer.run(config()), 0)
        self.driver.assert_not_called()

    def test_takes_the_same_lock_as_send(self):
        entry(self.inbox, 1)
        with store.lock():
            with self.assertRaises(SystemExit):
                answer.run(config())
        self.driver.assert_not_called()


if __name__ == "__main__":
    import unittest
    unittest.main()
