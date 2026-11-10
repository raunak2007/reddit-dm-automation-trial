"""test_answer.py - `run.py answer`: approved entries in a temp copy of inbox/data/answers.json are sent with a fake
browser and a fake sender, and their status follows the send (sending before Send, then sent / failed / uncertain).
Nothing touches reddit.com, the real answers.json or the real contacted.csv.

    venv/bin/python -m unittest discover reddit/tests
"""
from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta

from helpers import SENDER, FakeDriver, LedgerTest, Sender, config, root_config

import answer
import ledger
import store
from inbox import store as inbox
from send_dm import ME_URL, Result


def reply(mid, user, kind="message", subject="re: About your post", text="Yes please, how do I book?"):
    return {"message_id": mid, "username": user, "kind": kind, "received_at": "", "subject": subject,
            "snippet": text[:160], "text": text, "permalink": f"https://www.reddit.com/message/messages/{mid}",
            "recorded_at": "now"}


class AnswerTest(LedgerTest):
    def setUp(self):
        super().setUp()
        self.drivers = []
        for user in ("lead_one", "lead_two", "lead_three"):
            self.mark(user, "sent", datetime.now() - timedelta(days=2))
        store.add_replies([reply("t4_a", "lead_one"), reply("t4_b", "lead_two", subject="Your essay question"),
                           reply("t1_c", "lead_three", kind="comment_reply", subject="comment reply")])

    def entry(self, reply_id, person, text=None, approve=True):
        a = inbox.add_draft("reddit", reply_id, person=person, name=person, chat_url="", their_message="hi",
                            our_message="first DM", draft=text or f"Happy to help, u/{person}! - Ameer")
        if approve:
            inbox.update(a["id"], status="approved")
        return a["id"]

    def make_driver(self, pages=None, **kw):
        def make(cfg):
            d = FakeDriver(pages, **kw)
            self.drivers.append(d)
            return d
        return make

    def run_answer(self, sender=None, cfg=None, root=None, **kw):
        sender = sender or Sender()
        code = answer.run(cfg or config(), root or root_config(), self.make_driver(**kw), sender)
        return code, sender

    def status(self, aid):
        return inbox.get(aid)["status"]


class Sending(AnswerTest):
    def test_approved_answers_go_to_people_already_contacted_and_contacted_csv_is_untouched(self):
        a = self.entry("t4_a", "lead_one", "Hi u/lead_one,\nhere's how to book: link\n- Ameer")
        drafted = self.entry("t4_b", "lead_two", approve=False)
        before = ledger.LOG.read_bytes()
        code, sender = self.run_answer()
        self.assertEqual(code, 0)
        self.assertEqual(sender.calls, ["lead_one"], "only approved entries; ledger.skip_reason isn't consulted")
        self.assertEqual(sender.links[0]["message"], "Hi u/lead_one,\nhere's how to book: link\n- Ameer")
        self.assertEqual(sender.links[0]["subject"], "re: About your post")
        got = inbox.get(a)
        self.assertEqual(got["status"], "sent")
        self.assertTrue(got["sent_at"])
        self.assertEqual(self.status(drafted), "drafted")
        self.assertEqual(ledger.LOG.read_bytes(), before, "answers aren't recorded in contacted.csv")
        self.assertTrue(self.drivers[0].quit_called)

        again = self.run_answer()[1]
        self.assertEqual(again.calls, [], "a sent answer is never sent again")

    def test_subjects_come_from_replies_csv_or_the_config_default(self):
        self.entry("t4_b", "lead_two")
        self.entry("t1_c", "lead_three")
        self.entry("t4_unknown", "lead_one")
        cfg = config()
        cfg["answers"] = {"subject": "re: our chat"}
        _, sender = self.run_answer(cfg=cfg)
        self.assertEqual([l["subject"] for l in sender.links], ["re: Your essay question", "re: our chat", "re: our chat"])

    def test_the_entrys_own_kind_and_subject_are_used_when_replies_csv_lacks_the_reply(self):
        inbox.add_entry("reddit", "t4_grouped", person="lead_one", name="", chat_url="", their_message="",
                        our_message="", draft="d", kind="message", subject="Question about essays")
        inbox.update("reddit:t4_grouped", status="approved")
        self.assertEqual(self.run_answer()[1].links[0]["subject"], "re: Question about essays")

    def test_sending_is_written_right_before_send_is_clicked(self):
        a = self.entry("t4_a", "lead_one")
        seen = []

        def send(driver, link, *, sender=None, before_submit=None):
            seen.append(self.status(a))
            before_submit()
            seen.append(self.status(a))
            return Result("sent", "fake")

        answer.run(config(), root_config(), self.make_driver(), send)
        self.assertEqual(seen, ["approved", "sending"])
        self.assertEqual(self.status(a), "sent")

    def test_outcomes_map_to_sent_failed_and_uncertain_with_the_reason(self):
        ids = [self.entry("t4_a", "lead_one"), self.entry("t4_b", "lead_two"), self.entry("t1_c", "lead_three")]
        sender = Sender({"lead_two": Result("refused", "compose: unable to message this account"),
                         "lead_three": Result("uncertain", "compose: Send was clicked; timed out")})
        self.assertEqual(self.run_answer(sender)[0], 0)
        self.assertEqual([self.status(a) for a in ids], ["sent", "failed", "uncertain"])
        self.assertIn("unable to message", inbox.get(ids[1])["reason"])
        self.assertIn("timed out", inbox.get(ids[2])["reason"])

    def test_a_crash_after_send_is_uncertain_before_send_is_failed(self):
        a, b = self.entry("t4_a", "lead_one"), self.entry("t4_b", "lead_two")

        def send(driver, link, *, sender=None, before_submit=None):
            if "lead_one" in link:
                before_submit()
            raise RuntimeError("chrome went away")

        answer.run(config(), root_config(), self.make_driver(), send)
        self.assertEqual((self.status(a), self.status(b)), ("uncertain", "failed"))

    def test_an_entry_changed_on_the_page_after_the_run_started_is_not_sent(self):
        a = self.entry("t4_a", "lead_one", "old text")

        def send(driver, link, *, sender=None, before_submit=None):
            inbox.update(a, text="edited on the page")
            try:
                before_submit()
            except RuntimeError:
                return Result("not_submitted", "compose: step3_send: not clicking Send")
            return Result("sent", "should not happen")

        answer.run(config(), root_config(), self.make_driver(), send)
        self.assertEqual((self.status(a), inbox.get(a)["text"]), ("approved", "edited on the page"),
                         "left for the next run, with the new text")

    def test_left_sending_by_a_dead_run_becomes_uncertain_and_is_not_resent(self):
        a = self.entry("t4_a", "lead_one")
        inbox.update(a, status="sending")
        code, sender = self.run_answer()
        self.assertEqual((code, sender.calls), (0, []))
        self.assertEqual(self.status(a), "uncertain")
        self.assertEqual(self.drivers, [], "nothing approved: no browser")

    def test_max_per_run_oldest_approval_first(self):
        ids = [self.entry(f"t4_{i}", u) for i, u in enumerate(("lead_three", "lead_one", "lead_two"))]
        cfg = config()
        cfg["answers"] = {"max_per_run": 2}
        _, sender = self.run_answer(cfg=cfg)
        self.assertEqual(sender.calls, ["lead_three", "lead_one"])
        self.assertEqual(self.status(ids[2]), "approved")

    def test_bad_entries_fail_without_opening_compose(self):
        a = self.entry("t4_a", "not a name")
        b = self.entry("t4_b", SENDER)
        code, sender = self.run_answer()
        self.assertEqual((code, sender.calls), (0, []))
        self.assertEqual((self.status(a), self.status(b)), ("failed", "failed"))
        self.assertIn("this account", inbox.get(b)["reason"])


class Stops(AnswerTest):
    def test_an_account_wide_problem_stops_with_exit_code_2(self):
        a, b = self.entry("t4_a", "lead_one"), self.entry("t4_b", "lead_two")
        sender = Sender({"lead_one": Result("rate_limited", "compose: you're doing that too much")})
        code, sender = self.run_answer(sender)
        self.assertEqual(code, 2)
        self.assertEqual(sender.calls, ["lead_one"])
        self.assertEqual((self.status(a), self.status(b)), ("failed", "approved"))

    def test_logged_out_or_wrong_account_sends_nothing_and_exits_2(self):
        a = self.entry("t4_a", "lead_one")
        for kw in ({"logged_in_as": "someone_else"}, {"logged_in_as": None, "pages": {ME_URL: json.dumps({"data": {}})}}):
            code, sender = self.run_answer(**kw)
            self.assertEqual((code, sender.calls), (2, []))
            self.assertEqual(self.status(a), "approved")

    def test_a_streak_of_failures_before_send_stops_with_exit_code_2(self):
        for i, u in enumerate(("lead_one", "lead_two", "lead_three", "lead_four")):
            self.entry(f"t4_{i}", u)
        sender = Sender({u: Result("not_submitted", "compose: step1: timeout")
                         for u in ("lead_one", "lead_two", "lead_three", "lead_four")})
        code, sender = self.run_answer(sender)
        self.assertEqual(code, 2)
        self.assertEqual(len(sender.calls), ledger.MAX_FAILS_IN_A_ROW)


if __name__ == "__main__":
    unittest.main()
