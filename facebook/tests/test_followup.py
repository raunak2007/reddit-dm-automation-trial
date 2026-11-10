"""test_followup.py - python run.py followup: who gets the one follow-up, the chat re-read right before it, the daily
DM cap, the stops, and that follow-up rows leave send.py's and the queue's statuses alone. No browser.

    cd facebook && ../venv/bin/python -m unittest discover tests -p test_followup.py
"""
from __future__ import annotations

import csv
from datetime import datetime, timedelta
from types import SimpleNamespace

from helpers import FakeChrome, StoreTest, config, lead

import followup
import messenger
import qualify
import queue_server
import send
import store
from send_dm import AccountBlocked, Result

BUSINESS = {"name": "Aristo", "website": "https://example.com", "booking_link": "https://example.com/#book",
            "sender_first_name": "Ameer", "offer": "a free call", "services": ["essays"]}


def cfg(**fu):
    c = config()
    c["outreach"]["followup"] = {"enabled": True, "after_days": 6, "max_per_run": 5, "use_ai": False, **fu}
    c["business"], c["ai"]["model"] = BUSINESS, "claude-test"
    return c


def me(text):
    return {"who": "me", "text": text, "label": "You sent", "side": "me"}


def them(text):
    return {"who": "them", "text": text, "label": "Person", "side": "them"}


def row(l, status, days_ago=0.0, note=""):
    """A contacted.csv row from days_ago (store.record only writes 'now')."""
    store.DATA.mkdir(parents=True, exist_ok=True)
    new = not store.CONTACTED.exists()
    with open(store.CONTACTED, "a", newline="") as f:
        w = csv.DictWriter(f, store.FIELDS)
        if new:
            w.writeheader()
        w.writerow({"author_key": l["author_key"], "author": l["author"], "post_key": l["post_key"], "status": status,
                    "timestamp": (datetime.now() - timedelta(days=days_ago)).isoformat(timespec="seconds"),
                    "note": note})


class Sender:
    """Presses (before_submit) and answers from a script ({author_key: Result or exception}, default 'sent')."""

    def __init__(self, answers=None):
        self.answers, self.calls = answers or {}, []

    def __call__(self, driver, url, text, name, *, before_submit=None):
        akey = url.rsplit("/", 1)[-1]
        self.calls.append((akey, text))
        before_submit()
        answer = self.answers.get(akey, Result("sent", "fake: sent"))
        if isinstance(answer, Exception):
            raise answer
        return answer


class FollowupTest(StoreTest):
    def setUp(self):
        super().setUp()
        self.patch(messenger, "SETTLE", 0)
        self.patch(messenger, "pause", lambda *a: None)
        self.chats, self.names = {}, {}

    def dmed(self, *leads, days_ago=7):
        store.save_leads({**store.load_leads(), **{l["post_key"]: l for l in leads}})
        for l in leads:
            row(l, "approved", days_ago + 0.01)
            row(l, "sent", days_ago)
            self.chats[l["author_key"]] = [me(l["approved"]["dm"])]
            self.names[l["author"]] = l["author_key"]
        return leads

    def open_chat(self, driver, url, name):
        akey = url.rsplit("/", 1)[-1]
        said = self.chats.get(akey)
        if isinstance(said, Exception):
            raise said

    def read(self, driver, name):
        return self.chats[self.names[name]]

    def loop(self, c=None, sender=None, people=None):
        c = c or cfg()
        sender = sender or Sender()
        counts = followup.followup_loop(None, followup.candidates(c) if people is None else people, c,
                                        send_fn=sender, open_chat=self.open_chat, read=self.read,
                                        draft=lambda l, dm, days: f"Hi {l['author']}, following up on my note.")
        return counts, sender

    def fu_statuses(self, l):
        return [r["status"] for r in store.followups().get(l["author_key"], [])]


class WhoGetsOne(FollowupTest):
    def test_only_people_dmed_long_enough_ago_with_no_reply_answer_or_followup(self):
        ok, recent, replied, answered, minor, done, retry = (lead(n) for n in range(1, 8))
        self.dmed(ok, replied, answered, done, retry)
        self.dmed(recent, days_ago=3)
        minor["ai"]["likely_minor"] = True
        self.dmed(minor)
        store.record_reply(replied["author_key"], replied["author"], replied["post_key"], "hi", "u")
        a = self.inbox.add_draft("facebook", "x", person=answered["author_key"], name="", chat_url="",
                                 their_message="?", our_message="", draft="ok")
        self.inbox.update(a["id"], status="sent")
        row(done, "followup_sent", 1)
        row(retry, "followup_not_submitted", 1)
        got = [p[0] for p in followup.candidates(cfg())]
        self.assertEqual(sorted(got), sorted([ok["author_key"], retry["author_key"]]))

        reasons = {l["author_key"]: followup.recheck(l["author_key"], cfg())
                   for l in (recent, replied, answered, minor, done)}
        self.assertIn("days ago", reasons[recent["author_key"]])
        self.assertEqual(reasons[replied["author_key"]], "they replied")
        self.assertEqual(reasons[answered["author_key"]], "answered from the inbox")
        self.assertIn("under 18", reasons[minor["author_key"]])
        self.assertIn("already followed up", reasons[done["author_key"]])

    def test_minors_never_get_one_even_with_minor_policy_allow(self):
        l = lead(1)
        l["ai"]["likely_minor"] = True
        self.dmed(l)
        c = cfg()
        c["outreach"]["minor_policy"] = "allow"
        self.assertEqual(followup.candidates(c), [])

    def test_never_dmed_held_or_unavailable_get_none(self):
        held, gone, approved = lead(1), lead(2), lead(3)
        store.save_leads({l["post_key"]: l for l in (held, gone, approved)})
        row(held, "uncertain", 9)
        row(gone, "sent", 9)
        row(gone, "recipient", 8)
        row(approved, "approved", 9)
        self.assertEqual(followup.candidates(cfg()), [])

    def test_off_by_default_and_off_opens_no_browser(self):
        self.dmed(lead(1))
        driver = self.patch(followup.browser, "make_driver")
        c = cfg()
        del c["outreach"]["followup"]
        self.assertFalse(followup.settings(c)["enabled"])
        self.assertEqual(followup.settings(c)["after_days"], 6)
        self.assertEqual(followup.run(c), 0)
        driver.assert_not_called()


class Sending(FollowupTest):
    def test_one_followup_ever_with_submitting_before_the_press(self):
        a, b = self.dmed(lead(1), lead(2))
        counts, sender = self.loop()
        self.assertEqual(counts["sent"], 2)
        self.assertEqual(self.fu_statuses(a), ["followup_submitting", "followup_sent"])
        self.assertEqual(store.followups()[a["author_key"]][0]["note"], f"Hi {a['author']}, following up on my note.")
        self.assertEqual(followup.candidates(cfg()), [], "at most one per person, ever")

        counts, sender = self.loop(people=[(a["author_key"], a["author"], a["post_key"], a["approved"]["dm"], 7, a)])
        self.assertEqual(sender.calls, [], "rechecked right before: already followed up")

    def test_they_wrote_after_our_dm_is_a_reply_not_a_followup(self):
        a, b = self.dmed(lead(1), lead(2))
        self.chats[a["author_key"]] += [them("Sorry for the delay!"), them("Can we talk Friday?")]
        counts, sender = self.loop()
        self.assertEqual([c[0] for c in sender.calls], [b["author_key"]])
        self.assertEqual(counts["replied"], 1)
        r = store.replies()[a["author_key"]]
        self.assertEqual(r["text"], "Sorry for the delay! / Can we talk Friday?")
        self.assertEqual(self.fu_statuses(a), [])

    def test_their_messages_before_our_dm_dont_stop_it(self):
        a, = self.dmed(lead(1))
        self.chats[a["author_key"]].insert(0, them("hello from before"))
        counts, sender = self.loop()
        self.assertEqual(len(sender.calls), 1)

    def test_we_wrote_after_our_dm_is_skipped(self):
        a, = self.dmed(lead(1))
        self.chats[a["author_key"]].append(me("Also, here's the essay guide I mentioned"))
        counts, sender = self.loop()
        self.assertEqual(sender.calls, [])
        self.assertEqual(counts["skipped"], 1)

    def test_our_dm_not_in_the_chat_is_left_for_the_next_run(self):
        a, = self.dmed(lead(1))
        self.chats[a["author_key"]] = [them("hi")]
        counts, sender = self.loop()
        self.assertEqual((sender.calls, counts["unreadable"]), ([], 1))
        self.assertEqual(self.fu_statuses(a), [])
        self.assertEqual(len(followup.candidates(cfg())), 1)

    def test_failure_before_send_is_retried_and_refusal_is_final(self):
        a, b = self.dmed(lead(1), lead(2))

        class NoPress(Sender):
            def __call__(s, driver, url, text, name, *, before_submit=None):
                akey = url.rsplit("/", 1)[-1]
                s.calls.append((akey, text))
                return {a["author_key"]: Result("not_submitted", "box went away"),
                        b["author_key"]: Result("refused", "can't message this person")}[akey]

        self.loop(sender=NoPress())
        self.assertEqual(self.fu_statuses(a), ["followup_not_submitted"])
        self.assertEqual(self.fu_statuses(b), ["followup_refused"])
        self.assertEqual([p[0] for p in followup.candidates(cfg())], [a["author_key"]])

    def test_the_real_send_path_on_a_fake_chat(self):
        a, = self.dmed(lead(1, "Jane Doe"))
        text = "Hi Jane Doe, following up on my note."
        url = store.messenger_url(a["author_key"])
        driver = FakeChrome({url: {"text": f"Jane Doe {a['approved']['dm']} {text}", "composer": True,
                                   "rows": [me(a["approved"]["dm"])]}})
        c = cfg()
        counts = followup.followup_loop(driver, followup.candidates(c), c,
                                        draft=lambda l, dm, days: text)
        self.assertEqual(self.fu_statuses(a), ["followup_sent"], "already in the chat: recorded, not sent again")
        self.assertEqual(driver.box.typed, [])
        self.assertEqual(counts["sent"], 1)


class CapAndStatuses(FollowupTest):
    def test_followups_count_toward_the_daily_dm_cap(self):
        a, b, c_ = self.dmed(lead(1), lead(2), lead(3))
        self.assertEqual(store.sent_today(), 0)
        c = cfg()
        c["outreach"]["daily_dm_limit"] = 2
        counts, sender = self.loop(c)
        self.assertEqual(len(sender.calls), 2)
        self.assertFalse(counts["halted"])
        self.assertIn("daily cap", counts["stopped"])
        self.assertEqual(store.sent_today(), 2)

        n = lead(4)
        self.approve(n)
        got = send.send_loop(None, send.approved_leads(), c, send=lambda *a, **k: Result("sent", "x"))
        self.assertIn("daily cap", got["stopped"], "send.py sees today's follow-ups too")

    def test_a_held_followup_counts_today(self):
        a, = self.dmed(lead(1))
        row(a, "followup_submitting", 0)
        self.assertEqual(store.sent_today(), 1)

    def test_followup_rows_leave_the_lead_sent_everywhere(self):
        a, = self.dmed(lead(1))
        a["ai"].update(categories=[], author_role="student", reason="", need="", minor_evidence="")
        store.save_leads({a["post_key"]: a})
        row(a, "followup_submitting", 0)
        row(a, "followup_uncertain", 0)
        c = cfg()
        self.assertEqual(store.handled_posts()[a["post_key"]], "sent")
        self.assertEqual(queue_server.status_of(a, c, store.handled_posts(), store.history()), "sent")
        self.assertEqual(store.held(), {}, "a follow-up never makes the first DM held")
        self.assertIn(a["author_key"], store.messaged_authors())
        self.assertIn("already sent", store.skip_reason(a["author_key"]))
        self.patch(queue_server, "_cfg", lambda: c)
        card = queue_server.state()["leads"][0]
        self.assertEqual((card["status"], card["followup"]["status"]), ("sent", "followup_uncertain"))

    def test_a_held_first_dm_stays_held_after_a_followup_row(self):
        a = lead(1)
        store.save_leads({a["post_key"]: a})
        row(a, "submitting", 1)
        row(a, "followup_not_submitted", 0)        # can't happen today, but must not unhold it
        self.assertIn(a["author_key"], store.held())
        self.assertIn("HELD", store.skip_reason(a["author_key"]))


class Stops(FollowupTest):
    def test_a_block_when_opening_the_chat_halts_before_typing(self):
        a, b = self.dmed(lead(1), lead(2))
        self.chats[a["author_key"]] = AccountBlocked("You're Temporarily Blocked", "rate_limited")
        counts, sender = self.loop()
        self.assertTrue(counts["halted"])
        self.assertEqual(followup.report(counts), 2)
        self.assertEqual(sender.calls, [])
        self.assertEqual(self.fu_statuses(a), ["followup_stopped"])
        self.assertEqual(len(followup.candidates(cfg())), 2, "a stop before Send is retried later")

    def test_a_pin_in_a_real_chat_halts(self):
        a, = self.dmed(lead(1, "Jane Doe"))
        url = store.messenger_url(a["author_key"])
        driver = FakeChrome({url: {"text": "Jane Doe Enter your PIN to restore your chat history", "composer": True}})
        counts = followup.followup_loop(driver, followup.candidates(cfg()), cfg(), draft=lambda *x: "hi")
        self.assertTrue(counts["halted"])
        self.assertEqual(driver.box.typed, [])

    def test_a_block_after_the_press_is_uncertain_halts_and_is_never_repeated(self):
        a, b = self.dmed(lead(1), lead(2))
        counts, sender = self.loop(sender=Sender({a["author_key"]: Result("rate_limited", "blocked after Send")}))
        self.assertTrue(counts["halted"])
        self.assertEqual(len(sender.calls), 1)
        self.assertEqual(self.fu_statuses(a), ["followup_submitting", "followup_uncertain"])
        self.assertEqual([p[0] for p in followup.candidates(cfg())], [b["author_key"]])

    def test_run_takes_the_lock_and_prepares(self):
        self.dmed(lead(1))
        self.patch(followup.browser, "make_driver")
        prepare = self.patch(followup.send, "prepare", side_effect=followup.browser.Halt("chat PIN"))
        with self.assertRaises(SystemExit) as stop:
            followup.run(cfg())
        self.assertEqual(stop.exception.code, 2)
        prepare.assert_called_once()
        with store.lock():
            with self.assertRaises(SystemExit):
                followup.run(cfg())


class Drafting(FollowupTest):
    def test_template_when_ai_is_off(self):
        l = lead(1)
        l["ai"]["first_name"] = "Jane"
        text = followup.Drafter(cfg(use_ai=False))(l, "Hi Jane, ...", 7)
        self.assertTrue(text.startswith("Hi Jane, following up"))
        self.assertIn(BUSINESS["booking_link"], text)

    def fake_client(self, message=None, error=None):
        def create(**kw):
            self.request = kw
            if error:
                raise error
            return SimpleNamespace(content=[SimpleNamespace(type="tool_use", input={"message": message})])
        return SimpleNamespace(messages=SimpleNamespace(create=create))

    def test_claude_drafts_it_from_the_followup_prompt_and_the_first_dm(self):
        self.patch(qualify, "client", lambda: self.fake_client("Hi Jane, one more tip: ..."))
        l = lead(1)
        text = followup.Drafter(cfg(use_ai=True))(l, "Hi Person 1, the first DM", 7)
        self.assertEqual(text, "Hi Jane, one more tip: ...")
        self.assertEqual(self.request["model"], "claude-test")
        self.assertIn("follow-up", self.request["system"][0]["text"])
        self.assertIn(BUSINESS["booking_link"], self.request["system"][0]["text"])
        self.assertIn("Hi Person 1, the first DM", self.request["messages"][0]["content"])
        self.assertIn("7 days ago", self.request["messages"][0]["content"])

    def test_falls_back_to_the_template(self):
        l = lead(1)
        for client in (self.fake_client(error=RuntimeError("no network")), self.fake_client("x" * 900),
                       self.fake_client("")):
            self.patch(qualify, "client", lambda client=client: client)
            text = followup.Drafter(cfg(use_ai=True))(l, "dm", 7)
            self.assertIn("following up on the message", text)


if __name__ == "__main__":
    import unittest
    unittest.main()
