"""test_send.py - who is messaged (score, minors, already contacted, one per person), the queue page round-tripping
through the root open_queue.parse_queue, and a send run through the real ledger.prepare / send_loop / report with a
fake browser and a fake sender. Nothing is sent and contacted.csv is a temp file.

    venv/bin/python -m unittest discover reddit/tests
"""
from __future__ import annotations

import json
import unittest

from helpers import SENDER, FakeDriver, LedgerTest, ago, config, post, root_config

import ledger
import scrape
import send
import store
from open_queue import parse_queue
from send_dm import ME_URL, Result, parse_compose_link


def scored(pid, author, score=80, minor=False, created=None, message=None, **ai):
    lead = scrape.parse_post(post(pid, author=author, created=created), config())
    lead.update(found_at="", sources=["r/x new"])
    lead["ai"] = {"relevant": True, "score": score, "likely_minor": minor, "categories": ["essays"],
                  "need": "essay help", "subject": f"Subject for {author}",
                  "message": message if message is not None else f"Hi u/{author}, a tip about your essay.", **ai}
    return lead


class Sender:
    """A send function for ledger.send_loop: answers from a script ({username: Result}, default 'sent'), presses
    (before_submit) for every send that gets as far as Send, and remembers who it was called for."""

    def __init__(self, answers=None):
        self.answers, self.calls = answers or {}, []

    def __call__(self, driver, link, *, sender=None, before_submit=None):
        to = parse_compose_link(link)["to"]
        self.calls.append(to)
        answer = self.answers.get(to, Result("sent", "fake: sent"))
        if answer.status in ("sent", "uncertain"):
            before_submit()
        return answer


class Eligibility(LedgerTest):
    def setUp(self):
        super().setUp()
        self.cfg, self.root = config(), root_config()

    def why(self, lead, **outreach):
        cfg = config(**outreach)
        return send.why_not(lead, cfg, self.root, SENDER)

    def test_a_good_lead_is_sent(self):
        self.assertIsNone(self.why(scored("a", "student_a")))

    def test_score_relevance_and_drafts(self):
        self.assertIn("score 40", self.why(scored("a", "x", score=40)))
        self.assertIn("score", self.why(scored("a", "x", relevant=False)))
        self.assertEqual(self.why(scored("a", "x", message="  ")), "no drafted message")
        lead = scored("a", "x"); del lead["ai"]
        self.assertEqual(self.why(lead), "not scored yet")

    def test_minors_are_skipped_unless_minor_policy_allows(self):
        lead = scored("a", "hs_junior", minor=True)
        self.assertIn("under 18", self.why(lead))
        self.assertIn("under 18", self.why(lead, minor_policy="comment_only"))
        self.assertIsNone(self.why(lead, minor_policy="allow"))

    def test_already_contacted_held_or_given_up(self):
        self.mark("sent_before", "sent")
        self.mark("Held_One", "submitting")
        for _ in range(3):
            self.mark("refuser", "refused")
        self.mark("nobody_here", "recipient")
        self.assertIn("already sent", self.why(scored("a", "SENT_BEFORE")))
        self.assertIn("HELD", self.why(scored("b", "held_one")))
        self.assertIn("gave up", self.why(scored("c", "refuser")))
        self.assertIn("no such user", self.why(scored("d", "nobody_here")))
        self.mark("once_refused", "refused")
        self.assertIsNone(self.why(scored("e", "once_refused")), "one refusal: tried again")

    def test_old_posts_this_account_and_bad_usernames(self):
        self.assertIn("days ago", self.why(scored("a", "x", created=ago(days=8))))
        self.assertEqual(self.why(scored("b", SENDER)), "that's this account")
        lead = scored("c", "x"); lead["author"] = "not a name"
        self.assertIn("not a valid username", self.why(lead))

    def test_one_message_per_person_for_their_best_post_best_score_first(self):
        leads = {l["post_id"]: l for l in (scored("a", "twice", 70), scored("b", "twice", 90), scored("c", "other", 80),
                                           scored("d", "Twice", 95, minor=True), scored("e", "low", 10))}
        out, skipped = send.candidates(leads, self.cfg, self.root, SENDER)
        self.assertEqual([(l["post_id"], user) for l, user, *_ in out], [("t3_b", "twice"), ("t3_c", "other")])
        reasons = {l["post_id"]: why for l, why in skipped}
        self.assertIn("better-scored", reasons["t3_a"])
        self.assertIn("under 18", reasons["t3_d"])

    def test_the_subject_is_outreach_subject_or_the_drafted_one(self):
        lead = scored("a", "x")
        self.assertEqual(send.subject_for(lead, config()), "About your post")
        self.assertEqual(send.subject_for(lead, config(subject="")), "Subject for x")


class QueuePage(LedgerTest):
    def entries(self, *leads, cfg=None):
        out, _ = send.candidates({l["post_id"]: l for l in leads}, cfg or config(), root_config(), SENDER)
        return out

    def test_the_queue_parses_back_exactly_with_no_problems(self):
        tricky = ('Hi u/Kid_1, "quotes" & <angle> brackets,\nnew lines\n\n  and an emoji 🎓 too.\n'
                  'Book here: https://aristoscholars.com/#get-started?a=1&b=2')
        out = self.entries(scored("a", "Kid_1", 90, message=tricky), scored("b", "parent-2", 80),
                           scored("c", "third_one", 70))
        path = send.write_queue(out)
        parsed = parse_queue(path)
        self.assertEqual([e["problem"] for e in parsed], [None, None, None])
        for e, (lead, user, subject, message, link) in zip(parsed, out):
            self.assertEqual((e["to"], e["subject"], e["message"], e["link"]), (user, subject, message, link))
        self.assertEqual(parsed[0]["message"], tricky)

    def test_the_page_has_a_checkmark_key_the_ledger_can_read(self):
        path = send.write_queue(self.entries(scored("a", "x")))
        key = ledger.storage_key(path.read_text())
        self.assertTrue(key.startswith("reddit-queue-sent-"))
        ledger.import_checkmarks(path, json.dumps({"x": "2026-11-09"}))
        self.assertEqual(self.statuses("x"), ["sent"])

    def test_per_lead_subjects_are_what_open_queue_refuses(self):
        out = self.entries(scored("a", "x", 90), scored("b", "y", 80), scored("c", "z", 70), cfg=config(subject=""))
        problems = [e["problem"] for e in parse_queue(send.write_queue(out))]
        self.assertEqual(sum(p is not None for p in problems), 2, "one subject per queue: see outreach.subject")

    def test_an_empty_queue_is_still_a_page(self):
        self.assertEqual(parse_queue(send.write_queue([])), [])


class SendRun(LedgerTest):
    def setUp(self):
        super().setUp()
        store.save_leads({l["post_id"]: l for l in (scored("a", "first", 90), scored("b", "second", 80),
                                                    scored("c", "third", 70), scored("m", "minor", 99, minor=True))})
        self.drivers = []

    def make_driver(self, pages=None, **kw):
        def make(cfg):
            d = FakeDriver(pages, **kw)
            self.drivers.append(d)
            return d
        return make

    def test_eligible_leads_go_through_the_root_sender_and_are_recorded_once(self):
        sender = Sender()
        code = send.run(config(), root_config(), self.make_driver(), sender)
        self.assertEqual(code, 0)
        self.assertEqual(sender.calls, ["first", "second", "third"])
        self.assertEqual(self.statuses("first"), ["submitting", "sent"])
        self.assertEqual(self.statuses("minor"), [])
        self.assertTrue(self.drivers[0].quit_called)
        self.assertTrue(store.QUEUE.exists())

        again = Sender()
        self.assertEqual(send.run(config(), root_config(), self.make_driver(), again), 0)
        self.assertEqual(again.calls, [], "nobody is messaged twice")

    def test_the_daily_cap_is_shared_with_every_reddit_script(self):
        self.mark("someone_from_run_loop", "sent")
        sender = Sender()
        send.run(config(), root_config(daily_limit_per_account=2), self.make_driver(), sender)
        self.assertEqual(sender.calls, ["first"])
        self.assertEqual(ledger.sent_today(), 2)

    def test_an_account_wide_problem_halts_with_exit_code_2(self):
        sender = Sender({"second": Result("rate_limited", "compose: you're doing that too much")})
        self.assertEqual(send.run(config(), root_config(), self.make_driver(), sender), 2)
        self.assertEqual(sender.calls, ["first", "second"])
        self.assertEqual(self.statuses("third"), [])

    def test_logged_out_or_wrong_account_sends_nothing_and_exits_2(self):
        for kw in ({"logged_in_as": "someone_else"}, {"logged_in_as": None}):
            sender = Sender()
            pages = {ME_URL: json.dumps({"data": {}})} if kw["logged_in_as"] is None else None
            self.assertEqual(send.run(config(), root_config(), self.make_driver(pages, **kw), sender), 2)
            self.assertEqual(sender.calls, [])

    def test_without_auto_approve_only_the_queue_is_written(self):
        sender = Sender()
        self.assertEqual(send.run(config(auto_approve=False), root_config(), self.make_driver(), sender), 0)
        self.assertEqual(self.drivers, [], "no browser is opened")
        self.assertEqual(len(parse_queue(store.QUEUE)), 3)

    def test_no_sender_username_stops_before_sending(self):
        sender = Sender()
        code = send.run(config(), root_config(sender_username=""), self.make_driver(), sender)
        self.assertEqual(code, 1)
        self.assertEqual(sender.calls, [])


if __name__ == "__main__":
    unittest.main()
