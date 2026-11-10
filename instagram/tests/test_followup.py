"""test_followup.py - `python run.py followup` (followup.py): who is due the one follow-up, the inbox re-check right
before each send, drafting (a fake Claude client, and the template fallback), the daily cap counting follow-ups,
and stopping. Fake inbox / thread answers (helpers.FakeAPI) and a fake send function stand in for instagram.com.

    cd instagram/tests && ../../venv/bin/python -m unittest test_followup
"""
from __future__ import annotations

import csv
import sys
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest import mock

from helpers import FakeAPI, StoreTest, config, lead

import browser
import direct
import followup
import queue_server
import run
import send
import store
from send_dm import Result

NOW = datetime.now().replace(microsecond=0)
ME = 100


def us(dt):
    return str(int(dt.timestamp() * 1_000_000))


def thread(tid, user, pk, *items):
    return {"thread_id": tid, "is_group": False, "users": [{"pk": pk, "username": user}], "items": list(items),
            "last_activity_at": us(NOW)}


def item(iid, user_id, at, text="hi"):
    return {"item_id": iid, "user_id": user_id, "timestamp": us(at), "item_type": "text", "text": text}


def cfg(**f):
    c = config(daily_dm_limit=20)
    c["outreach"]["followup"] = {"enabled": True, "after_days": 6, "max_per_run": 5, **f}
    c["ai"].update(enabled=False, model="claude-test")
    c["business"] = {"name": "Aristo", "website": "https://a.example", "booking_link": "https://a.example/#go",
                     "sender_first_name": "Ameer", "offer": "a free call", "services": ["essays"]}
    return c


class Sender:
    def __init__(self, test, answers=None, no_press=()):
        self.test, self.answers, self.no_press, self.calls = test, answers or {}, set(no_press), []

    def __call__(self, driver, url, text, user, name="", *, before_submit=None):
        self.calls.append((url, text, user))
        self.test.events.append(("send", user))
        if user not in self.no_press:
            before_submit()
            self.test.assertEqual(self.test.statuses({"username": user})[-1], "followup_submitting", "recorded before the press")
        answer = self.answers.get(user, Result("sent", "fake: sent"))
        if isinstance(answer, Exception):
            raise answer
        return answer


class FollowupTest(StoreTest):
    def setUp(self):
        super().setUp()
        self.patch(browser, "pause", lambda *a: None)
        self.prepare = self.patch(send, "prepare", return_value=str(ME))
        self.patch(direct, "follow", side_effect=AssertionError("followed someone for a follow-up"))
        self.events, self.threads, self.recheck = [], {}, {}
        self.fake = FakeAPI({"direct_v2/inbox/": lambda url: {"inbox": {"threads": list(self.threads.values()),
                                                                        "has_older": False}},
                             "direct_v2/pending_inbox/": {"inbox": {"threads": [], "has_older": False}},
                             "direct_v2/threads/": self.thread_answer})
        self.patch(browser, "make_driver", lambda c: self.fake)
        self.leads, self.pk = {}, {}

    def thread_answer(self, url):
        tid = url.split("direct_v2/threads/")[1].split("/")[0]
        self.events.append(("recheck", tid))
        t = self.recheck.get(tid) or next((t for t in self.threads.values() if t["thread_id"] == tid), None)
        return {"thread": t} if t else (404, "{}")

    def dmed(self, user, days_ago, *, minor=False, chat=True, extra_rows=()):
        """A lead DMed `days_ago` days ago, with their chat (our DM only) in the inbox."""
        n = len(self.leads) + 1
        l = lead(n, user)
        l["ai"].update(likely_minor=minor, first_name=user.split(".")[0], need="essay help")
        self.leads[l["key"]] = l
        store.save_leads(self.leads)
        at = (NOW - timedelta(days=days_ago)).isoformat(timespec="seconds")
        rows = [(user, l["key"], "approved", at, l["approved"]["dm"]), (user, l["key"], "sent", at, "")]
        rows += [(user, store.followup_key(user), s, at, "") for s in extra_rows]
        self.write(rows)
        self.pk[user] = 1000 + n
        if chat:
            self.threads[user] = thread(f"t{n}", user, self.pk[user],
                                        item(f"ours{n}", ME, NOW - timedelta(days=days_ago)))
        return l

    def write(self, rows):
        store.DATA.mkdir(exist_ok=True)
        new = not store.CONTACTED.exists()
        with open(store.CONTACTED, "a", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(store.FIELDS)
            w.writerows(rows)

    def go(self, sender=None, c=None, draft_fn=None):
        return followup.run(c or cfg(), send_fn=sender or Sender(self),
                            draft_fn=draft_fn or (lambda cand: (f"bump {cand['username']}", "template")))


class WhoIsDue(FollowupTest):
    def test_only_people_dmed_long_enough_ago_with_no_reply_answer_or_follow_up(self):
        self.dmed("due.one", 7)
        self.dmed("too.recent", 3)
        self.dmed("replied", 8)
        self.dmed("answered", 8)
        self.dmed("minor", 8, minor=True)
        self.dmed("done", 9, extra_rows=["followup_sent"])
        self.dmed("maybe", 9, extra_rows=["followup_uncertain"])
        self.dmed("failed.once", 10, extra_rows=["followup_failed"])
        store.add_replies([{"username": "replied", "item_id": "r1", "replied_at": NOW.isoformat()}])
        e = self.answers.add_draft("instagram", "x1", person="answered", name="", chat_url="", their_message="?",
                                   our_message="", draft="ok")
        self.answers.update(e["id"], status="sent")
        todo, skip = followup.candidates(cfg())
        self.assertEqual([c["username"] for c in todo], ["failed.once", "due.one"], "oldest DM first")
        why = dict(skip)
        self.assertIn("after_days", why["too.recent"])
        self.assertIn("replied", why["replied"])
        self.assertIn("answer", why["answered"])
        self.assertIn("under 18", why["minor"])
        self.assertIn("already followed up", why["done"])
        self.assertIn("already followed up", why["maybe"])

    def test_after_days_is_configurable(self):
        self.dmed("a", 3)
        self.assertEqual([c["username"] for c in followup.candidates(cfg(after_days=2))[0]], ["a"])

    def test_minor_policy_allow(self):
        self.dmed("minor", 8, minor=True)
        c = cfg()
        c["outreach"]["minor_policy"] = "allow"
        self.assertEqual(len(followup.candidates(c)[0]), 1)

    def test_off_by_default(self):
        self.dmed("a", 8)
        c = cfg()
        del c["outreach"]["followup"]
        self.patch(browser, "make_driver", side_effect=AssertionError("opened Chrome"))
        self.assertEqual(followup.run(c, send_fn=Sender(self)), 0)
        self.assertIn("off", self.out.getvalue())


class Sends(FollowupTest):
    def test_sends_one_follow_up_and_records_it_apart_from_the_first_dm(self):
        a = self.dmed("jane.doe", 7)
        dm = Sender(self)
        self.assertEqual(self.go(dm), 0)
        self.assertEqual(dm.calls, [(direct.chat_link("jane.doe"), "bump jane.doe", "jane.doe")])
        self.assertEqual(self.statuses(a), ["approved", "sent", "followup_submitting", "followup_sent"])
        last = store.history()["jane.doe"][-1]
        self.assertEqual((last["lead_key"], last["note"]), ("followup:jane.doe", "bump jane.doe"))
        self.assertEqual(store.handled()[a["key"]], "sent", "the lead itself still reads 'sent'")
        self.assertEqual(queue_server.status_of(a, cfg(), store.handled(), store.history()), "sent",
                         "the review page shows them as DMed, as before")
        self.assertEqual(store.held(), {})
        self.assertIn("already sent", store.skip_reason("jane.doe"))
        self.assertIn("jane.doe", store.messaged())
        self.assertEqual(store.sent_today(), 1, "the follow-up counts today (the first DM was days ago)")

        dm2 = Sender(self)
        self.go(dm2)
        self.assertEqual(dm2.calls, [], "at most one follow-up, ever")

    def test_rechecks_the_thread_right_before_each_send_and_skips_a_new_reply(self):
        self.dmed("a.one", 8)
        b = self.dmed("b.two", 7)
        tid = self.threads["b.two"]["thread_id"]
        self.recheck[tid] = thread(tid, "b.two", self.pk["b.two"], item("late", self.pk["b.two"], NOW, "just saw this!"))
        dm = Sender(self)
        self.go(dm)
        self.assertEqual([c[2] for c in dm.calls], ["a.one"])
        self.assertEqual(self.events, [("recheck", self.threads["a.one"]["thread_id"]), ("send", "a.one"),
                                       ("recheck", tid)])
        self.assertEqual([r["item_id"] for r in store.load_replies()], ["late"], "the reply is saved")
        self.assertNotIn("followup_submitting", self.statuses(b))

    def test_a_reply_found_in_the_inbox_read_is_skipped(self):
        self.dmed("a.one", 8)
        self.threads["a.one"]["items"].append(item("r", self.pk["a.one"], NOW - timedelta(days=1), "thanks"))
        dm = Sender(self)
        self.go(dm)
        self.assertEqual(dm.calls, [])
        self.assertEqual(store.load_replies()[0]["username"], "a.one")

    def test_a_chat_that_cant_be_checked_is_skipped(self):
        self.dmed("no.chat", 8, chat=False)
        self.dmed("no.recheck", 8)
        self.fake.answers["direct_v2/threads/"] = (500, "")
        dm = Sender(self)
        self.go(dm)
        self.assertEqual(dm.calls, [])
        self.assertIn("can't be ruled out", self.out.getvalue())
        self.assertIn("couldn't read their chat again", self.out.getvalue())

    def test_inbox_silent_sends_nothing(self):
        self.dmed("a", 8)
        self.fake.answers["direct_v2/inbox/"] = (404, "{}")
        dm = Sender(self)
        self.assertEqual(self.go(dm), 0)
        self.assertEqual(dm.calls, [])

    def test_never_marks_anything_seen_or_posts(self):
        self.dmed("a", 8)
        self.go()
        self.assertEqual(self.fake.posted, [])
        for url in self.fake.fetched:
            self.assertRegex(url, r"direct_v2/(inbox|pending_inbox|threads/[^/]+)/\?")
            self.assertNotIn("seen", url)

    def test_max_per_run(self):
        for n in range(4):
            self.dmed(f"p{n}", 8 + n)
        dm = Sender(self)
        self.go(dm, cfg(max_per_run=2))
        self.assertEqual(len(dm.calls), 2)


class DailyCap(FollowupTest):
    def test_follow_ups_count_toward_the_daily_limit(self):
        for n in range(3):
            self.dmed(f"p{n}", 8)
        today = NOW.isoformat(timespec="seconds")
        self.write([("fresh.one", "P:fresh.one", "sent", today, "")])          # one first DM today
        dm = Sender(self)
        c = cfg()
        c["outreach"]["daily_dm_limit"] = 2
        self.go(dm, c)
        self.assertEqual(len(dm.calls), 1)
        self.assertEqual(store.sent_today(), 2)
        self.assertIn("daily cap", self.out.getvalue())

    def test_already_at_the_cap_opens_no_browser(self):
        self.dmed("a", 8)
        self.write([("fresh.one", "P:fresh.one", "sent", NOW.isoformat(timespec="seconds"), "")])
        c = cfg()
        c["outreach"]["daily_dm_limit"] = 1
        self.patch(browser, "make_driver", side_effect=AssertionError("opened Chrome"))
        self.assertEqual(followup.run(c, send_fn=Sender(self)), 0)

    def test_send_py_sees_follow_ups_in_its_cap(self):
        self.dmed("a", 8)
        self.go()
        new = self.approve(lead(50, "new.lead"))
        c = config(daily_dm_limit=1, auto_follow=False)
        counts = send.send_loop(None, new, c, send=lambda *a, **k: Result("sent", "fake"))
        self.assertEqual(counts["sent"], 0)
        self.assertIn("daily cap", counts["stopped"])

    def test_a_follow_up_that_failed_before_send_doesnt_count(self):
        a = self.dmed("a", 8)
        dm = Sender(self, {"a": Result("not_submitted", "direct: no box")}, no_press={"a"})
        self.go(dm)
        self.assertEqual(self.statuses(a)[-1], "followup_failed")
        self.assertEqual(store.sent_today(), 0)
        self.assertEqual([c["username"] for c in followup.candidates(cfg())[0]], ["a"], "may be tried again")


class Stops(FollowupTest):
    def test_account_wide_before_the_press_exits_2_and_records_no_attempt(self):
        a = self.dmed("a", 9)
        self.dmed("b", 8)
        dm = Sender(self, {"a": Result("rate_limited", "direct: action blocked")}, no_press={"a"})
        self.assertEqual(self.go(dm), 2)
        self.assertEqual([c[2] for c in dm.calls], ["a"])
        self.assertEqual(self.statuses(a), ["approved", "sent"])

    def test_account_wide_after_the_press_is_uncertain_and_never_retried(self):
        a = self.dmed("a", 9)
        dm = Sender(self, {"a": Result("login", "logged out")})
        self.assertEqual(self.go(dm), 2)
        self.assertEqual(self.statuses(a)[-1], "followup_uncertain")
        self.assertIn("already followed up", dict(followup.candidates(cfg())[1])["a"])
        self.assertEqual(store.held(), {}, "not held: the first DM's hold logic ignores follow-ups")

    def test_a_block_while_rechecking_exits_2(self):
        self.dmed("a", 9)
        self.fake.answers["direct_v2/threads/"] = (400, '{"message":"feedback_required"}')
        dm = Sender(self)
        self.assertEqual(self.go(dm), 2)
        self.assertEqual(dm.calls, [])

    def test_a_challenge_on_the_inbox_read_halts(self):
        self.dmed("a", 9)
        self.fake.answers["direct_v2/inbox/"] = (400, '{"message":"challenge_required"}')
        with self.assertRaises(browser.Halt):
            self.go()

    def test_two_uncertain_in_a_row_stop(self):
        for n in range(3):
            self.dmed(f"p{n}", 8 + n)
        dm = Sender(self, {f"p{n}": Result("uncertain", "not confirmed") for n in range(3)})
        self.assertEqual(self.go(dm), 2)
        self.assertEqual(len(dm.calls), 2)


class Drafting(FollowupTest):
    def candidate(self):
        l = self.dmed("jess.k", 8)
        return {"username": "jess.k", "sent_at": "2026-11-01T10:00:00", "lead": l}

    def test_claude_drafts_it_from_the_first_dm_with_the_followup_prompt(self):
        c = self.candidate()
        client = mock.Mock()
        client.messages.create.return_value = SimpleNamespace(content=[SimpleNamespace(
            type="tool_use", input={"dm": "hey jess! just bumping this in case it got buried"})])
        ai = cfg()
        ai["ai"]["enabled"] = True
        text, how = followup.draft(ai, c, client=client)
        self.assertEqual((text, how), ("hey jess! just bumping this in case it got buried", "ai"))
        kw = client.messages.create.call_args.kwargs
        self.assertIn("ONE short follow-up", kw["system"][0]["text"])
        self.assertIn("https://a.example/#go", kw["system"][0]["text"])
        self.assertIn(c["lead"]["approved"]["dm"], kw["messages"][0]["content"], "the first DM is referenced")
        self.assertEqual(kw["model"], "claude-test")

    def test_uses_qualify_pys_client(self):
        c = self.candidate()
        ai = cfg()
        ai["ai"]["enabled"] = True
        made = self.patch(followup.qualify, "client", side_effect=RuntimeError("no ANTHROPIC_API_KEY"))
        text, how = followup.draft(ai, c)
        made.assert_called_once()
        self.assertEqual(how, "template")

    def test_template_fallback(self):
        c = self.candidate()
        text, how = followup.draft(cfg(), c)
        self.assertEqual(how, "template")
        self.assertEqual(text, "hey jess! just bumping my msg in case it got buried - still happy to help with "
                               "essay help if you want: https://a.example/#go")
        c["lead"]["ai"].update(first_name="", need="none")
        c["lead"]["full_name"] = ""
        self.assertTrue(followup.draft(cfg(), c)[0].startswith("hey! just bumping"))
        self.assertIn("college stuff", followup.draft(cfg(), c)[0])

    def test_an_unusable_claude_draft_falls_back(self):
        c = self.candidate()
        client = mock.Mock()
        client.messages.create.return_value = SimpleNamespace(content=[SimpleNamespace(
            type="tool_use", input={"dm": "hey {first_name}!"})])
        ai = cfg()
        ai["ai"]["enabled"] = True
        self.assertEqual(followup.draft(ai, c, client=client)[1], "template")


class RunPy(StoreTest):
    def test_run_py_followup_and_its_exit_code(self):
        called = self.patch(followup, "run", return_value=2)
        self.patch(run, "CONFIG", self.tmp / "config.json")
        (self.tmp / "config.json").write_text("{}")
        with mock.patch.object(sys, "argv", ["run.py", "followup"]):
            self.assertEqual(run.cli(), 2)
        called.assert_called_once()


if __name__ == "__main__":
    unittest.main()
