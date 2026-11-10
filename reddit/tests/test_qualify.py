"""test_qualify.py - scoring and drafting with a fake Claude client: what is sent to it, what is kept, who is left out.
No network: a stand-in `anthropic` module is put in sys.modules for the duration of each test.

    venv/bin/python -m unittest discover reddit/tests
"""
from __future__ import annotations

import sys
import types
import unittest
from types import SimpleNamespace
from unittest import mock

from helpers import LedgerTest, config, post

import qualify
import scrape
import store


class APIError(Exception):
    pass


class AuthenticationError(APIError):
    pass


def fake_anthropic():
    mod = types.ModuleType("anthropic")
    mod.APIError, mod.AuthenticationError = APIError, AuthenticationError
    mod.Anthropic = lambda: (_ for _ in ()).throw(AssertionError("tests must pass their own client"))
    return mod


def answer(**kw):
    a = {"relevant": True, "score": 88, "categories": ["essays"], "author_role": "student", "likely_minor": False,
         "minor_evidence": "says they're a gap-year student", "need": "Help with a Common App essay",
         "reason": "asks for essay help", "subject": "Your Common App essay question",
         "message": "Hi u/student_a, I saw your post in r/ApplyingToCollege..."}
    a.update(kw)
    return a


class FakeClient:
    """client.messages.create(...) answers with a record_lead tool call; remembers every request."""

    def __init__(self, answers=None, fail=()):
        self.answers, self.fail, self.calls = answers or {}, set(fail), []
        self.messages = SimpleNamespace(create=self.create)

    def create(self, **kw):
        self.calls.append(kw)
        text = kw["messages"][0]["content"]
        for key in self.fail:
            if key in text:
                raise APIError(f"overloaded for {key}")
        a = next((v for k, v in self.answers.items() if k in text), answer())
        return SimpleNamespace(content=[SimpleNamespace(type="text", text="..."),
                                        SimpleNamespace(type="tool_use", input=a)])


class Qualify(LedgerTest):
    def setUp(self):
        super().setUp()
        p = mock.patch.dict(sys.modules, {"anthropic": fake_anthropic()})
        p.start()
        self.addCleanup(p.stop)
        self.cfg = config()

    def save(self, *children):
        leads = {}
        for c in children:
            scrape.merge(leads, scrape.parse_post(c, self.cfg), "r/x new")
        store.save_leads(leads)
        return leads

    def test_each_new_post_gets_an_ai_block_with_a_subject_and_message(self):
        self.save(post("a", author="student_a"), post("b", author="parent_b"))
        client = FakeClient({"parent_b": answer(author_role="parent", score=72, subject="Help for your daughter")})
        hits = qualify.run(self.cfg, client=client)
        self.assertEqual(hits, 2)
        leads = store.load_leads()
        self.assertEqual(leads["t3_b"]["ai"]["subject"], "Help for your daughter")
        self.assertTrue(leads["t3_a"]["ai"]["message"].startswith("Hi u/student_a"))
        self.assertEqual(leads["t3_a"]["ai"]["model"], self.cfg["ai"]["model"])
        call = client.calls[0]
        self.assertEqual(call["model"], self.cfg["ai"]["model"])
        self.assertEqual(call["tool_choice"], {"type": "tool", "name": "record_lead"})
        self.assertEqual(call["system"][0]["cache_control"], {"type": "ephemeral"})
        self.assertIn(self.cfg["business"]["booking_link"], call["system"][0]["text"])
        self.assertIn("Subreddit: r/ApplyingToCollege", call["messages"][0]["content"])

    def test_scored_posts_are_not_sent_again_unless_rescore(self):
        self.save(post("a"))
        qualify.run(self.cfg, client=FakeClient())
        again = FakeClient()
        qualify.run(self.cfg, client=again)
        self.assertEqual(again.calls, [])
        qualify.run(self.cfg, rescore=True, client=again)
        self.assertEqual(len(again.calls), 1)

    def test_people_already_messaged_or_held_are_not_scored(self):
        self.save(post("a", author="done_already"), post("b", author="held_one"), post("c", author="fresh"))
        self.mark("done_already", "sent")
        self.mark("held_one", "uncertain")
        client = FakeClient()
        qualify.run(self.cfg, client=client)
        self.assertEqual(len(client.calls), 1)
        self.assertIn("fresh", client.calls[0]["messages"][0]["content"])

    def test_an_api_error_skips_one_post_and_it_is_tried_next_run(self):
        self.save(post("a", author="flaky"), post("b", author="fine"))
        qualify.run(self.cfg, client=FakeClient(fail={"flaky"}))
        leads = store.load_leads()
        self.assertNotIn("ai", leads["t3_a"])
        self.assertIn("ai", leads["t3_b"])
        qualify.run(self.cfg, client=FakeClient())
        self.assertIn("ai", store.load_leads()["t3_a"])

    def test_minors_are_flagged(self):
        self.save(post("a", author="hs_junior"))
        qualify.run(self.cfg, client=FakeClient({"hs_junior": answer(likely_minor=True, minor_evidence="junior")}))
        self.assertTrue(store.load_leads()["t3_a"]["ai"]["likely_minor"])
        self.assertIn("[minor]", self.out.getvalue())

    def test_dry_run_prints_the_prompt_and_calls_nobody(self):
        self.save(post("a"))
        client = FakeClient()
        qualify.run(self.cfg, dry_run=True, client=client)
        self.assertEqual(client.calls, [])
        self.assertIn("record_lead", self.out.getvalue())
        self.assertIn("Title a", self.out.getvalue())
        self.assertNotIn("ai", store.load_leads()["t3_a"])

    def test_max_posts_per_run_takes_the_newest(self):
        from helpers import ago
        self.save(post("old", created=ago(days=5)), post("new", created=ago(hours=1)))
        self.cfg["ai"]["max_posts_per_run"] = 1
        client = FakeClient()
        qualify.run(self.cfg, client=client)
        self.assertIn("Title new", client.calls[0]["messages"][0]["content"])


if __name__ == "__main__":
    unittest.main()
