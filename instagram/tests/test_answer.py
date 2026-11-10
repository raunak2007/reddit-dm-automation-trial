"""test_answer.py - `python run.py answer` (answer.py): sending the answers approved in the shared inbox record
(inbox/store.py, pointed at a temp dir by helpers.StoreTest), with a fake send function and fake Message Requests
answers (helpers.FakeAPI) in place of instagram.com. test_mock_site.py sends one through the real page code.

    cd instagram/tests && ../../venv/bin/python -m unittest test_answer
"""
from __future__ import annotations

import csv
import sys
import unittest
from unittest import mock

from helpers import FakeAPI, StoreTest, config

import answer
import browser
import direct
import run
import send
import store
from send_dm import Result


class Sender:
    """A send function for answer.run: answers from a script ({person: Result or exception}, default 'sent'),
    presses (runs before_submit) unless the answer says it never got that far, and notes the entry's status at
    the moment of the press."""

    def __init__(self, test, answers=None, no_press=()):
        self.test, self.answers, self.no_press, self.calls, self.at_press = test, answers or {}, set(no_press), [], []

    def __call__(self, driver, url, text, user, name="", *, before_submit=None):
        self.calls.append((url, text, user, name))
        if user not in self.no_press:
            before_submit()
            self.at_press.append(self.test.status_of(user))
        answer = self.answers.get(user, Result("sent", "fake: sent"))
        if isinstance(answer, Exception):
            raise answer
        return answer


class AnswerTest(StoreTest):
    def setUp(self):
        super().setUp()
        self.fake = FakeAPI({"direct_v2/pending_inbox/": {"inbox": {"threads": [], "has_older": False}}})
        self.patch(browser, "make_driver", lambda c: self.fake)
        self.patch(browser, "pause", lambda *a: None)
        self.prepare = self.patch(send, "prepare", return_value="100")
        self.follow = self.patch(direct, "follow", side_effect=AssertionError("followed someone for an answer"))
        self.ids = {}

    def entry(self, person, text=None, status="approved", reply_id=None):
        reply_id = reply_id or f"item-{person}"
        e = self.answers.add_draft("instagram", reply_id, person=person, name=person.title(),
                                   chat_url="https://www.instagram.com/direct/t/1/", their_message="how much?",
                                   our_message="hey!", draft=text if text is not None else f"answer for {person}")
        if status != "drafted":
            self.answers.update(e["id"], status=status)
        self.ids[person] = e["id"]
        return e

    def status_of(self, person):
        return self.answers.get(self.ids[person])["status"]

    def requests(self, *people):
        self.fake.answers["direct_v2/pending_inbox/"] = {"inbox": {"threads": [
            {"thread_id": f"t{p}", "users": [{"pk": 5, "username": p}], "items": []} for p in people],
            "has_older": False}}

    def go(self, sender=None, **answers_cfg):
        cfg = config()
        cfg["answers"] = answers_cfg
        return answer.run(cfg, send_fn=sender or Sender(self))


class Sends(AnswerTest):
    def test_sends_each_approved_answer_once_and_marks_sending_before_the_press(self):
        self.entry("jane.doe")
        self.entry("sam.p")
        self.entry("drafted.one", status="drafted")
        self.entry("facebook.person")
        self.answers.update(self.ids["facebook.person"], platform="facebook")       # another platform's entry
        dm = Sender(self)
        self.assertEqual(self.go(dm), 0)
        self.assertEqual([c[2] for c in dm.calls], ["jane.doe", "sam.p"])
        self.assertEqual(dm.calls[0][:2], (direct.chat_link("jane.doe"), "answer for jane.doe"))
        self.assertEqual(dm.calls[0][3], "Jane.Doe", "their name, also accepted as proof the chat is theirs")
        self.assertEqual(dm.at_press, ["sending", "sending"])
        self.assertEqual((self.status_of("jane.doe"), self.status_of("sam.p")), ("sent", "sent"))
        self.assertTrue(self.answers.get(self.ids["jane.doe"])["sent_at"])
        self.assertEqual(self.status_of("drafted.one"), "drafted")
        self.follow.assert_not_called()
        self.assertFalse(store.CONTACTED.exists(), "contacted.csv isn't touched")

    def test_stuck_entries_turn_uncertain_first_and_arent_resent(self):
        self.entry("jane.doe", status="sending")
        self.entry("sam.p")
        dm = Sender(self)
        self.go(dm)
        self.assertEqual(self.status_of("jane.doe"), "uncertain")
        self.assertEqual([c[2] for c in dm.calls], ["sam.p"])

    def test_nothing_approved_opens_no_browser(self):
        self.entry("jane.doe", status="drafted")
        self.patch(browser, "make_driver", side_effect=AssertionError("opened Chrome"))
        self.assertEqual(self.go(), 0)

    def test_takes_send_pys_lock(self):
        self.entry("jane.doe")
        with store.lock():
            with self.assertRaises(SystemExit) as stop:
                self.go()
        self.assertIn("already using", str(stop.exception))
        self.assertEqual(self.status_of("jane.doe"), "approved")

    def test_max_per_run(self):
        for n in range(4):
            self.entry(f"p{n}")
        dm = Sender(self)
        self.go(dm, max_per_run=2)
        self.assertEqual(len(dm.calls), 2)
        self.assertEqual([self.status_of(f"p{n}") for n in range(4)], ["sent", "sent", "approved", "approved"])
        self.assertIn("answers.max_per_run (2) reached", self.out.getvalue())

    def test_default_cap_is_ten(self):
        for n in range(12):
            self.entry(f"p{n}")
        dm = Sender(self)
        self.go(dm)
        self.assertEqual(len(dm.calls), 10)

    def test_paces_between_sends(self):
        self.entry("a")
        self.entry("b")
        cfg = config(min_delay_seconds=30, max_delay_seconds=30)
        with mock.patch.object(answer.time, "sleep") as sleep:
            answer.run(cfg, send_fn=Sender(self))
        sleep.assert_called_once_with(30)

    def test_an_answer_already_in_the_chat_is_recorded_sent_without_a_press(self):
        self.entry("jane.doe")
        dm = Sender(self, {"jane.doe": Result("sent", "direct: the message was already in the chat")},
                   no_press={"jane.doe"})
        self.go(dm)
        self.assertEqual(self.status_of("jane.doe"), "sent")

    def test_empty_text_fails_without_opening_the_chat(self):
        self.entry("jane.doe", text="   ")
        dm = Sender(self)
        self.go(dm)
        self.assertEqual(dm.calls, [])
        self.assertEqual(self.status_of("jane.doe"), "failed")


class MessageRequests(AnswerTest):
    def test_a_chat_in_message_requests_fails_with_a_reason_and_is_never_accepted(self):
        self.entry("jane.doe")
        self.entry("sam.p")
        self.requests("jane.doe")
        dm = Sender(self)
        self.go(dm)
        self.assertEqual([c[2] for c in dm.calls], ["sam.p"])
        e = self.answers.get(self.ids["jane.doe"])
        self.assertEqual(e["status"], "failed")
        self.assertIn("Message Requests", e["reason"])
        self.assertIn("Accept it in Instagram", e["reason"])
        self.assertEqual(self.fake.posted, [], "nothing POSTed: no request accepted")
        for url in self.fake.fetched:
            self.assertIn("direct_v2/pending_inbox/", url)

    def recorded(self, how):
        with open(store.REPLIES, "w", newline="") as f:
            w = csv.DictWriter(f, store.REPLY_FIELDS)
            w.writeheader()
            w.writerow({"username": "jane.doe", "item_id": "item-jane.doe", "how": how})

    def test_requests_endpoint_silent_goes_by_replies_csv(self):
        store.DATA.mkdir()
        self.fake.answers["direct_v2/pending_inbox/"] = (404, "{}")
        self.entry("jane.doe")
        self.recorded("request")
        dm = Sender(self)
        self.go(dm)
        self.assertEqual((dm.calls, self.status_of("jane.doe")), ([], "failed"))

        self.answers.update(self.ids["jane.doe"], status="approved")      # approved again; replies.csv says inbox
        self.recorded("api")
        self.go(dm)
        self.assertEqual(self.status_of("jane.doe"), "sent")

    def test_a_request_accepted_since_is_sent(self):
        store.DATA.mkdir()
        self.entry("jane.doe")
        self.recorded("request")                     # it was a request when replies.py read it; not any more
        self.go()
        self.assertEqual(self.status_of("jane.doe"), "sent")

    def test_challenge_while_reading_requests_exits_2_before_any_send(self):
        self.entry("jane.doe")
        self.fake.answers["direct_v2/pending_inbox/"] = (400, '{"message":"checkpoint_required"}')
        dm = Sender(self)
        with self.assertRaises(browser.Halt):
            self.go(dm)
        self.assertEqual((dm.calls, self.status_of("jane.doe")), ([], "approved"))


class Stops(AnswerTest):
    def test_account_wide_before_the_press_leaves_it_approved_and_exits_2(self):
        self.entry("jane.doe")
        self.entry("sam.p")
        dm = Sender(self, {"jane.doe": Result("rate_limited", "direct: action blocked")}, no_press={"jane.doe"})
        self.assertEqual(self.go(dm), 2)
        self.assertEqual([c[2] for c in dm.calls], ["jane.doe"])
        self.assertEqual((self.status_of("jane.doe"), self.status_of("sam.p")), ("approved", "approved"))

    def test_account_wide_after_the_press_is_uncertain_and_exits_2(self):
        self.entry("jane.doe")
        dm = Sender(self, {"jane.doe": Result("login", "direct: logged out after Send")})
        self.assertEqual(self.go(dm), 2)
        self.assertEqual(self.status_of("jane.doe"), "uncertain")

    def test_crash_after_the_press_is_uncertain_before_it_failed(self):
        self.entry("a")
        self.entry("b")
        dm = Sender(self, {"a": RuntimeError("chrome not reachable"), "b": RuntimeError("chrome not reachable")},
                   no_press={"b"})
        self.go(dm)
        self.assertEqual((self.status_of("a"), self.status_of("b")), ("uncertain", "failed"))

    def test_refusals_fail_and_three_in_a_row_stop_with_2(self):
        for n in range(4):
            self.entry(f"p{n}")
        dm = Sender(self, {f"p{n}": Result("refused", "direct: can't message") for n in range(4)},
                   no_press={f"p{n}" for n in range(4)})
        self.assertEqual(self.go(dm), 2)
        self.assertEqual([self.status_of(f"p{n}") for n in range(4)], ["failed", "failed", "failed", "approved"])
        self.assertIn("can't message", self.answers.get(self.ids["p0"])["reason"])

    def test_two_uncertain_in_a_row_stop(self):
        for n in range(3):
            self.entry(f"p{n}")
        dm = Sender(self, {f"p{n}": Result("uncertain", "direct: not confirmed") for n in range(3)})
        self.assertEqual(self.go(dm), 2)
        self.assertEqual(len(dm.calls), 2)

    def test_changed_on_the_page_before_the_press_is_left_alone(self):
        self.entry("jane.doe")

        def dismissed_meanwhile(driver, url, text, user, name="", *, before_submit=None):
            self.answers.update(self.ids["jane.doe"], status="dismissed")
            try:
                before_submit()
            except answer.Changed as exc:
                return Result("not_submitted", f"direct: step3_send: Changed: {exc}")
            raise AssertionError("pressed Send on a dismissed answer")
        self.go(dismissed_meanwhile)
        self.assertEqual(self.status_of("jane.doe"), "dismissed")

    def test_wrong_account_stops_before_anything(self):
        self.entry("jane.doe")
        self.prepare.side_effect = SystemExit("Logged in as Instagram user 999")
        dm = Sender(self)
        with self.assertRaises(SystemExit):
            self.go(dm)
        self.assertEqual((dm.calls, self.status_of("jane.doe")), ([], "approved"))


class RunPy(StoreTest):
    def test_run_py_answer(self):
        called = self.patch(answer, "run", return_value=2)
        self.patch(run, "CONFIG", self.tmp / "config.json")
        (self.tmp / "config.json").write_text("{}")
        with mock.patch.object(sys, "argv", ["run.py", "answer"]):
            self.assertEqual(run.cli(), 2)
        called.assert_called_once()

    def test_halt_exits_2(self):
        self.patch(answer, "run", side_effect=browser.Halt("logged out"))
        self.patch(run, "CONFIG", self.tmp / "config.json")
        (self.tmp / "config.json").write_text("{}")
        with mock.patch.object(sys, "argv", ["run.py", "answer"]):
            self.assertEqual(run.cli(), 2)


if __name__ == "__main__":
    unittest.main()
