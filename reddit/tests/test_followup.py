"""test_followup.py - `run.py followup`: who is due one (age of the first DM, replies, answers, minors, at most one
ever), the inbox re-read right before each send, drafting with a fake Claude client (and the template when it
fails), data/followups.csv as the record, and follow-ups counting toward the root daily cap without changing what
the ledger decides. Nothing touches reddit.com, Claude, or the real contacted.csv / answers.json.

    venv/bin/python -m unittest discover reddit/tests
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from datetime import datetime, timedelta
from types import SimpleNamespace

from helpers import SENDER, FakeDriver, LedgerTest, Sender, config, listing, message, root_config
from test_send import scored

import followup
import ledger
import replies
import send
import store
from inbox import store as inbox
from send_dm import Result


class FakeClient:
    """client.messages.create(...) answers with a record_followup tool call; fail=True raises instead."""

    def __init__(self, text="Hi u/{to}, one more idea for your essay: read it aloud. Still happy to talk: link\n- Ameer",
                 fail=False):
        self.text, self.fail, self.calls = text, fail, []
        self.messages = SimpleNamespace(create=self.create)

    def create(self, **kw):
        self.calls.append(kw)
        if self.fail:
            raise RuntimeError("overloaded")
        to = kw["messages"][0]["content"].split("\n")[0].removeprefix("Username: ")
        return SimpleNamespace(content=[SimpleNamespace(type="tool_use", input={"message": self.text.format(to=to)})])


def enabled(**kw):
    cfg = config()
    cfg["followup"] = dict({"enabled": True, "after_days": 6, "max_days": 30, "max_per_run": 5, "inbox_pages": 1},
                           **kw)
    return cfg


class FollowupTest(LedgerTest):
    def setUp(self):
        super().setUp()
        self.drivers = []
        self.leads = {}

    def first_dm(self, user, days_ago=7, minor=False, lead=True):
        if lead:
            l = scored(f"p_{user}", user, minor=minor)
            self.leads[l["post_id"]] = l
            store.save_leads(self.leads)
        self.mark(user, "submitting", datetime.now() - timedelta(days=days_ago))
        self.mark(user, "sent", datetime.now() - timedelta(days=days_ago))

    def due(self, cfg=None, root=None):
        return [p for p, _, _ in followup.candidates(cfg or enabled(), root or root_config(), SENDER)[0]]

    def reasons(self, cfg=None):
        return dict(followup.candidates(cfg or enabled(), root_config(), SENDER)[1])

    def make_driver(self, pages=None, **kw):
        def make(cfg):
            d = FakeDriver(dict({replies.INBOX: listing()}, **(pages or {})), **kw)
            self.drivers.append(d)
            return d
        return make

    def run_followup(self, cfg=None, root=None, sender=None, client=None, pages=None, **kw):
        sender = sender or Sender()
        client = client or FakeClient()
        code = followup.run(cfg or enabled(), root or root_config(), self.make_driver(pages, **kw), sender, client)
        return code, sender, client


class WhoIsDue(FollowupTest):
    def test_only_people_whose_first_dm_is_old_enough_and_not_too_old(self):
        self.first_dm("week_old", 7)
        self.first_dm("too_new", 3)
        self.first_dm("too_old", 45)
        self.assertEqual(self.due(), ["week_old"])
        why = self.reasons()
        self.assertIn("after_days", why["too_new"])
        self.assertIn("max_days", why["too_old"])

    def test_replied_answered_minors_and_run_loop_sends_are_left_alone(self):
        for u in ("replied", "answered", "minor", "fine"):
            self.first_dm(u, 8, minor=(u == "minor"))
        self.first_dm("from_run_loop", 8, lead=False)
        store.add_replies([{"message_id": "t4_r", "username": "Replied", "kind": "message", "snippet": "hi",
                            "text": "hi", "recorded_at": "now"}])
        a = inbox.add_draft("reddit", "t4_x", person="answered", name="answered", chat_url="", their_message="?",
                            our_message="", draft="sure")
        inbox.update(a["id"], status="sent")
        self.assertEqual(self.due(), ["fine"])
        why = self.reasons()
        self.assertIn("replied", why["replied"])
        self.assertIn("answered", why["answered"])
        self.assertIn("under 18", why["minor"])
        self.assertIn("no first message", why["from_run_loop"])

    def test_a_merely_drafted_answer_does_not_count_as_answered(self):
        self.first_dm("someone", 8)
        inbox.add_draft("reddit", "t4_x", person="someone", name="", chat_url="", their_message="", our_message="",
                        draft="d")
        self.assertEqual(self.due(), ["someone"])

    def test_at_most_one_follow_up_ever_and_failures_before_send_retry_then_give_up(self):
        for u in ("sent_one", "held_one", "failed_once", "failed_thrice"):
            self.first_dm(u, 8)
        store.record_followup("sent_one", "sent", "fake")
        store.record_followup("held_one", "submitting", "about to click Send")
        store.record_followup("failed_once", "not_submitted", "timeout")
        for _ in range(3):
            store.record_followup("failed_thrice", "not_submitted", "timeout")
        self.assertEqual(self.due(), ["failed_once"])
        self.assertIn("gave up", self.reasons()["failed_thrice"])


class Sending(FollowupTest):
    def test_off_by_default(self):
        self.first_dm("due_one", 8)
        self.assertFalse(followup.settings(config())["enabled"], "reddit/config.json ships with it off")
        code = followup.run(config(), root_config(), self.make_driver(), Sender(), FakeClient())
        self.assertEqual(code, 0)
        self.assertEqual(self.drivers, [])
        self.assertEqual(store.load_followups(), [])

    def test_a_follow_up_is_drafted_by_claude_sent_and_recorded_only_in_followups_csv(self):
        self.first_dm("due_one", 8)
        before = ledger.LOG.read_bytes()
        code, sender, client = self.run_followup()
        self.assertEqual(code, 0)
        self.assertEqual(sender.calls, ["due_one"])
        link = sender.links[0]
        self.assertTrue(link["message"].startswith("Hi u/due_one, one more idea"))
        self.assertEqual(link["subject"], "re: About your post")
        prompt = client.calls[0]["messages"][0]["content"]
        self.assertIn("Hi u/due_one, a tip about your essay.", prompt, "Claude sees the first message")
        self.assertIn("record_followup", client.calls[0]["tool_choice"]["name"])
        self.assertEqual([r["status"] for r in store.load_followups()], ["submitting", "sent"])
        self.assertEqual(store.load_followups()[-1]["message"], link["message"])
        self.assertEqual(ledger.LOG.read_bytes(), before, "contacted.csv is untouched")
        self.assertEqual(self.statuses("due_one"), ["submitting", "sent"])

        again = self.run_followup()[1]
        self.assertEqual(again.calls, [], "one follow-up, ever")

    def test_the_template_is_used_when_claude_fails(self):
        self.first_dm("due_one", 8)
        _, sender, _ = self.run_followup(client=FakeClient(fail=True))
        msg = sender.links[0]["message"]
        self.assertTrue(msg.startswith("Hi u/due_one, following up once on my message about your post in r/"))
        link = json.loads((Path(__file__).resolve().parent.parent / "config.json").read_text())["business"]["booking_link"]
        self.assertIn(link, msg, "the configured (UTM-tagged) booking link")

    def test_a_reply_found_when_the_inbox_is_read_again_stops_the_follow_up(self):
        self.first_dm("just_replied", 8)
        self.first_dm("quiet_one", 9)
        pages = {replies.INBOX: listing(message("m1", "just_replied", body="Sorry for the slow reply!"))}
        code, sender, _ = self.run_followup(pages=pages)
        self.assertEqual(code, 0)
        self.assertEqual(sender.calls, ["quiet_one"])
        self.assertEqual([r["username"] for r in store.load_replies()], ["just_replied"], "recorded as a reply")
        self.assertTrue(all("mark=false" in u for d in self.drivers for u in d.visited if "/message/inbox" in u))

    def test_follow_ups_count_toward_the_root_daily_cap_everywhere_here(self):
        self.mark("sent_today_by_run_loop", "sent")
        for u in ("due_a", "due_b"):
            self.first_dm(u, 8)
        root = root_config(daily_limit_per_account=2)
        _, sender, _ = self.run_followup(root=root)
        self.assertEqual(sender.calls, ["due_a"])
        self.assertEqual((ledger.sent_today(), store.followups_today(), store.sent_today()), (2, 1, 2),
                         "the root ledger counts today's follow-up, so run_loop.py / open_queue.py share the cap")
        self.assertEqual(ledger.skip_reason("due_a")[:12], "already sent", "the ledger's own view is unchanged")

        fresh = scored("new_post", "brand_new_lead")
        store.save_leads(dict(self.leads, **{fresh["post_id"]: fresh}))
        first = Sender()
        self.assertEqual(send.run(config(), root, self.make_driver(), first), 0)
        self.assertEqual(first.calls, [], "send.py sees today's follow-up in the cap")
        loop = ledger.send_loop(None, [("brand_new_lead", "https://www.reddit.com/message/compose/?to=x&subject=s&message=m")],
                                dict(root, min_delay_seconds=0, max_delay_seconds=0), "me", send=Sender())
        self.assertIn("daily cap", loop["stopped"], "and so does run_loop.py's ledger.send_loop")

    def test_max_per_run(self):
        for i, u in enumerate(("due_a", "due_b", "due_c")):
            self.first_dm(u, 8 + i)
        _, sender, _ = self.run_followup(cfg=enabled(max_per_run=2))
        self.assertEqual(sender.calls, ["due_c", "due_b"], "oldest first DM first")


class Stops(FollowupTest):
    def test_an_account_wide_problem_stops_with_exit_code_2(self):
        self.first_dm("due_a", 9)
        self.first_dm("due_b", 8)
        code, sender, _ = self.run_followup(sender=Sender({"due_a": Result("restricted", "compose: account suspended")}))
        self.assertEqual(code, 2)
        self.assertEqual(sender.calls, ["due_a"])
        self.assertEqual([r["status"] for r in store.load_followups()], ["restricted"])

    def test_a_rate_limit_while_reading_the_inbox_stops_before_sending(self):
        self.first_dm("due_a", 9)
        pages = {replies.INBOX: '{"message": "Too Many Requests", "error": 429}'}
        code, sender, _ = self.run_followup(pages=pages)
        self.assertEqual((code, sender.calls), (2, []))

    def test_logged_out_sends_nothing_and_exits_2(self):
        self.first_dm("due_a", 9)
        code, sender, _ = self.run_followup(logged_in_as="someone_else")
        self.assertEqual((code, sender.calls), (2, []))
        self.assertEqual(store.load_followups(), [])


if __name__ == "__main__":
    unittest.main()
