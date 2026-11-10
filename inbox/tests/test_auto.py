"""Offline tests for auto_approve: the trust gate, every condition that refuses, the daily max, minors, and the
approved_by bookkeeping. Claude is mocked; every data path is a temp dir.
"""
from __future__ import annotations

import copy
import json

from test_inbox import CFG, IG_FIELDS, Base, FakeClient, write_csv

import draft
import server
import store

LINK = CFG["business"]["booking_link"]
GOOD_ANSWER = f"Great, glad to hear it! You can pick a time for the free call here: {LINK} Talk soon."


class AutoBase(Base):
    def setUp(self):
        super().setUp()
        self.cfg = copy.deepcopy(CFG)
        self.cfg["auto_approve"] = {"enabled": True, "intents": ["booking"], "min_reviewed": 2, "max_per_day": 2,
                                    "max_answer_words": 60, "max_message_words": 40}
        self.n = 0

    def review(self, n=2):
        """n entries you approved on the page (through the real handlers)."""
        for _ in range(n):
            self.n += 1
            store.add_entry("reddit", f"r{self.n}", person=f"rev{self.n}", name="x", chat_url="", their_message="hi",
                            our_message="", draft="hello")
            server.approve({"id": f"reddit:r{self.n}"})

    def lead(self, user, minor=False, evidence="talks about her son, so a parent"):
        leads = json.loads(self.paths["instagram"]["leads"].read_text()) if self.paths["instagram"]["leads"].exists() else {}
        ai = {"dm": "Hi! I saw your comment...", "likely_minor": minor, "minor_evidence": evidence}
        leads[f"sc:{user}"] = {"username": user, "full_name": user.title(), "ai": ai, "approved": {"dm": ai["dm"]}}
        self.paths["instagram"]["leads"].write_text(json.dumps(leads))

    def reply(self, user, text, item=None):
        self.n += 1
        rows = draft._csv(self.paths["instagram"]["replies"])
        rows.append({"username": user, "item_id": item or f"i{self.n}", "replied_at": f"2026-11-09T10:{self.n:02d}:00",
                     "snippet": text[:40], "text": text, "thread_url": f"https://ig.me/m/{user}"})
        write_csv(self.paths["instagram"]["replies"], IG_FIELDS + ["text"], rows)

    def scenario(self, text="Yes I'd like the call! How do I book?", answer=GOOD_ANSWER, intent="booking",
                 dismiss=False, minor=False, lead=True, user="jamie.k", review=True):
        if review:
            self.review()
        if lead:
            self.lead(user, minor=minor)
        self.reply(user, text)
        made = self.run_draft(FakeClient(answer=answer, intent=intent, dismiss=dismiss), cfg=self.cfg)
        self.assertEqual(len(made), 1)
        return store.get(made[0]["id"])

    def run_draft(self, cl, cfg=None, **kw):
        import io
        from contextlib import redirect_stdout
        with redirect_stdout(io.StringIO()) as out:
            made = draft.run(cl=cl, cfg=cfg or self.cfg, **kw)
        self.out = out.getvalue()
        return made

    def refused(self, e, words):
        self.assertEqual(e["status"], "drafted")
        self.assertNotEqual(e.get("approved_by"), "auto")
        self.assertIn(words, e.get("auto_skip", ""))


class AutoApproveTest(AutoBase):
    def test_approves_a_simple_booking_answer(self):
        e = self.scenario()
        self.assertEqual((e["status"], e["approved_by"]), ("approved", "auto"))
        self.assertTrue(e["approved_at"] and e["auto_approved_at"])
        self.assertIn("utm_source=instagram", e["text"])            # the tagged link still counts as the link
        self.assertEqual([a["id"] for a in store.approved("instagram")], [e["id"]])
        self.assertIn("AUTO-APPROVED", self.out)

    def test_off_by_default(self):
        self.assertFalse(CFG["auto_approve"]["enabled"])
        self.review()
        self.lead("jamie.k")
        self.reply("jamie.k", "Yes I'd like the call! How do I book?")
        [e] = self.run_draft(FakeClient(answer=GOOD_ANSWER, intent="booking"), cfg=CFG)
        self.assertEqual(store.get(e["id"])["status"], "drafted")
        self.assertNotIn("auto_skip", store.get(e["id"]))

    def test_gate_needs_reviews_by_you(self):
        e = self.scenario(review=False)
        self.refused(e, "reviewed 0 of the 2")
        self.assertIn("[auto-approve] inactive", self.out)

    def test_auto_approvals_dont_count_toward_the_gate(self):
        self.review()
        self.assertEqual(store.reviewed_count(), 2)
        self.lead("jamie.k")
        self.reply("jamie.k", "yes please, send me the link")
        self.run_draft(FakeClient(answer=GOOD_ANSWER, intent="booking"))
        self.assertEqual(store.reviewed_count(), 2)

    def test_page_actions_count_as_reviewed(self):
        for rid in ("a", "b", "c", "d"):
            store.add_entry("facebook", rid, person=rid, name=rid, chat_url="", their_message="", our_message="",
                            draft="d")
        server.edit({"id": "facebook:a", "text": "mine"})
        server.dismiss({"id": "facebook:b"})
        server.approve({"id": "facebook:c", "text": "ok"})
        self.assertEqual(store.reviewed_count(), 3)
        self.assertEqual(store.get("facebook:c")["approved_by"], "you")
        self.assertEqual(store.get("facebook:b")["dismissed_by"], "you")
        self.assertTrue(store.get("facebook:a")["edited_by_you"])
        # an entry approved before approved_by existed counts as yours
        store.update("facebook:d", status="approved")
        self.assertEqual(store.reviewed_count(), 4)

    def test_unapprove_an_auto_approval(self):
        e = self.scenario()
        server.unapprove({"id": e["id"]})
        e = store.get(e["id"])
        self.assertEqual((e["status"], e["approved_by"]), ("drafted", ""))
        self.assertEqual(store.auto_approved_today(), 1)           # still counts toward today's max
        store.update(e["id"], status="approved", approved_by="auto")
        store.update(e["id"], status="sending")
        with self.assertRaises(ValueError):
            server.unapprove({"id": e["id"]})

    # --- each condition refusing
    def test_intent_not_allowed(self):
        self.refused(self.scenario(intent="question"), "intent 'question'")

    def test_decline_suggested(self):
        self.refused(self.scenario(dismiss=True), "decline")

    def test_minor(self):
        self.refused(self.scenario(minor=True), "under 18")

    def test_minor_unknown_counts_as_minor(self):
        self.refused(self.scenario(lead=False), "can't tell")

    def test_minor_not_checked_counts_as_unknown(self):
        self.review()
        self.lead("sam", minor=False, evidence="not checked")
        self.reply("sam", "yes, how do I book?")
        [e] = self.run_draft(FakeClient(answer=GOOD_ANSWER, intent="booking"))
        self.refused(store.get(e["id"]), "can't tell")

    def test_answer_without_link(self):
        self.refused(self.scenario(answer="Great! Ameer will be in touch soon."), "booking link")

    def test_answer_asks_a_question(self):
        self.refused(self.scenario(answer=f"Great! Book here: {LINK} What grade is your son in?"), "asks a question")

    def test_answer_too_long(self):
        self.refused(self.scenario(answer=GOOD_ANSWER + " word" * 60), "longer than 60 words")

    def test_their_message_too_long(self):
        self.refused(self.scenario(text="yes I'd like the call " + "and more " * 30), "their message is longer")

    def test_their_message_asks_something_else(self):
        self.refused(self.scenario(text="Sure, I'd like the call. How much does it cost?"), "besides booking")

    def test_not_their_first_answer(self):
        self.review()
        store.add_entry("instagram", "old", person="jamie.k", name="Jamie", chat_url="", their_message="hi",
                        our_message="", draft="hello")
        store.update("instagram:old", status="sent")
        e = self.scenario(review=False)
        self.refused(e, "already answered")

    def test_daily_max(self):
        self.review()
        for u in ("ann", "ben", "cat"):
            self.lead(u)
            self.reply(u, "yes, send me the link please")
        self.run_draft(FakeClient(answer=GOOD_ANSWER, intent="booking"))
        by = sorted(a.get("approved_by", "") for a in store.load().values() if a["platform"] == "instagram")
        self.assertEqual(by.count("auto"), 2)
        skipped = [a for a in store.load().values() if a["platform"] == "instagram" and a["status"] == "drafted"]
        self.assertEqual(len(skipped), 1)
        self.assertIn("max_per_day", skipped[0]["auto_skip"])

    def test_booking_link_matching(self):
        self.assertTrue(draft.has_booking_link(f"here: {LINK}.", LINK))
        self.assertTrue(draft.has_booking_link(draft.tagged(CFG, "reddit", "u/x", f"go {LINK}"), LINK))
        self.assertFalse(draft.has_booking_link("go https://aristoscholars.com/", LINK))
        self.assertFalse(draft.has_booking_link("no link", LINK))


if __name__ == "__main__":
    import unittest
    unittest.main()
