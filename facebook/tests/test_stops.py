"""test_stops.py - what send.py does when Facebook logs the account out, asks for the chat PIN or blocks it, and
what the next run does after such a stop. No browser: FakeChrome (helpers.py) shows the wording and URLs
Facebook uses, and messenger.py's real patterns read them.

    cd facebook && ../venv/bin/python -m unittest discover tests
"""
from __future__ import annotations

import unittest
from unittest import mock

from helpers import SENDER, FakeChrome, StoreTest, config, lead

import messenger
import send
import store
from send_dm import AccountBlocked, Result

PIN_TEXT = "End-to-end encrypted chat. Enter your PIN to restore your chat history"


def chat(l):
    return store.messenger_url(l["author_key"])


class Sender:
    """A send function for send_loop that answers from a script ({author_key: Result or exception}, default
    'sent') and remembers who it was called for. press=True runs before_submit first, as a real press would."""

    def __init__(self, answers=None, press=()):
        self.answers, self.press, self.calls = answers or {}, set(press), []

    def __call__(self, driver, url, text, name, *, before_submit=None):
        akey = url.rsplit("/", 1)[-1]
        self.calls.append(akey)
        if akey in self.press:
            before_submit()
        answer = self.answers.get(akey, Result("sent", "fake: sent"))
        if isinstance(answer, Exception):
            raise answer
        return answer


class FakeChromeTest(StoreTest):
    def setUp(self):
        super().setUp()
        self.patch(messenger, "SETTLE", 0)
        self.patch(messenger, "pause", lambda *a: None)


class AccountStopsHaltTheRun(FakeChromeTest):
    def test_logout_halts_and_the_next_run_starts_with_the_same_lead(self):
        a, b, c = self.approve(lead(1), lead(2), lead(3))
        first = Sender({b["author_key"]: Result("login", "messenger: Facebook sent this profile to /login")})
        counts = send.send_loop(None, send.approved_leads(), config(), send=first)

        self.assertEqual(first.calls, [a["author_key"], b["author_key"]], "nothing is sent after the logout")
        self.assertTrue(counts["halted"])
        self.assertIn("login", counts["stopped"])
        self.assertEqual(send.report(counts), 2)
        self.assertEqual(self.statuses(b), ["approved", "login"])
        self.assertEqual(self.statuses(c), ["approved"])

        second = Sender()
        counts = send.send_loop(None, send.approved_leads(), config(), send=second)
        self.assertEqual(second.calls, [b["author_key"], c["author_key"]], "A isn't sent twice; B and C go out")
        self.assertFalse(counts["halted"])
        for l in (a, b, c):
            self.assertEqual(self.statuses(l).count("sent"), 1)

    def test_pin_in_a_chat_halts_before_anything_is_typed(self):
        a, b = self.approve(lead(1, "Jane Doe"), lead(2, "John Roe"))
        driver = FakeChrome({chat(a): {"text": f"Jane Doe {PIN_TEXT}", "composer": True},
                             chat(b): {"text": "John Roe", "composer": True}})
        counts = send.send_loop(driver, send.approved_leads(), config())

        self.assertTrue(counts["halted"])
        self.assertIn("PIN", counts["stopped"])
        self.assertEqual(driver.box.typed, [])
        self.assertNotIn(chat(b), driver.visited, "the run stops at the PIN, John's chat is never opened")
        self.assertEqual(self.statuses(a), ["approved", "login"], "no 'submitting' row: Send was never near")
        self.assertIn(a["post_key"], [l["post_key"] for l in send.approved_leads()], "A is sent on the next run")

    def test_each_kind_of_block_halts_the_run(self):
        cases = {
            "login": {"redirect": "https://www.facebook.com/login/?next=%2Fmessages%2F"},
            "restricted": {"redirect": "https://www.facebook.com/checkpoint/828281030927956/"},
            "rate_limited": {"text": "You're Temporarily Blocked. It looks like you were misusing this feature",
                             "composer": True},
        }
        for n, (kind, page) in enumerate(cases.items(), 1):
            with self.subTest(kind):
                first, second = self.approve(lead(10 * n), lead(10 * n + 1))
                pages = {chat(first): page, chat(second): {"text": second["author"], "composer": True}}
                if "redirect" in page:
                    pages[page["redirect"]] = {"text": "Log in to Facebook"}
                driver = FakeChrome(pages)
                counts = send.send_loop(driver, [first, second], config())
                self.assertTrue(counts["halted"])
                self.assertIn(f"({kind})", counts["stopped"])
                self.assertEqual(self.statuses(first)[-1], kind)
                self.assertNotIn(chat(second), driver.visited)
                self.assertEqual(driver.box.typed, [])

    def test_streak_of_refusals_halts_the_run(self):
        leads = self.approve(*(lead(n) for n in range(1, 6)))
        refuse = Sender({l["author_key"]: Result("refused", "fake: can't message") for l in leads})
        counts = send.send_loop(None, leads, config(), send=refuse)
        self.assertEqual(len(refuse.calls), send.MAX_REFUSALS_IN_A_ROW)
        self.assertTrue(counts["halted"])

    def test_the_daily_cap_is_not_a_halt(self):
        leads = self.approve(lead(1), lead(2), lead(3))
        counts = send.send_loop(None, leads, config(daily_dm_limit=2), send=Sender())
        self.assertEqual(counts["sent"], 2)
        self.assertFalse(counts["halted"])
        self.assertEqual(send.report(counts), 0)


class RestartAfterAStop(FakeChromeTest):
    def test_block_after_send_was_pressed_is_not_sent_twice(self):
        """Facebook blocks the account while the message is going out. The block is recorded (not held), so
        the next run opens the chat again: it must find the message there and not press Send a second time."""
        a, = self.approve(lead(1, "Jane Doe"))
        first = Sender({a["author_key"]: Result("rate_limited", "messenger: temporarily blocked")},
                       press={a["author_key"]})
        self.assertTrue(send.send_loop(None, send.approved_leads(), config(), send=first)["halted"])
        self.assertEqual(self.statuses(a), ["approved", "submitting", "rate_limited"])

        driver = FakeChrome({chat(a): {"text": f"Jane Doe {a['approved']['dm']}", "composer": True}})
        counts = send.send_loop(driver, send.approved_leads(), config())
        self.assertEqual(counts["sent"], 1)
        self.assertEqual(driver.box.typed, [], "the message was already in the chat: nothing typed")
        self.assertEqual(self.statuses(a), ["approved", "submitting", "rate_limited", "sent"])

    def test_crash_after_send_was_pressed_is_held_not_resent(self):
        a, b = self.approve(lead(1), lead(2))
        first = Sender({a["author_key"]: RuntimeError("chrome not reachable")}, press={a["author_key"]})
        counts = send.send_loop(None, [a, b], config(), send=first)
        self.assertEqual(counts["uncertain"], 1)
        self.assertIn(a["author_key"], store.held())

        second = Sender()
        send.send_loop(None, send.approved_leads(), config(), send=second)
        self.assertNotIn(a["author_key"], second.calls, "held: only the chat check may settle it")

    def test_killed_between_submitting_and_the_outcome_is_held(self):
        a, = self.approve(lead(1))
        store.record(a["author_key"], a["author"], a["post_key"], "submitting", "about to press Send")
        self.assertEqual(send.approved_leads(), [])
        self.assertIn("HELD", store.skip_reason(a["author_key"]))
        self.assertEqual(store.sent_today(), 1, "it may have gone out, so it counts against the cap")

    def test_held_sends_are_settled_at_the_start_of_the_next_run(self):
        went, didnt, unsure = self.approve(lead(1), lead(2), lead(3))
        for l in (went, didnt, unsure):
            store.record(l["author_key"], l["author"], l["post_key"], "uncertain", "fake")
        answers = {went["author"]: True, didnt["author"]: False, unsure["author"]: RuntimeError("no chat")}

        def was_sent(driver, url, text, name):
            if isinstance(answers[name], Exception):
                raise answers[name]
            return answers[name]

        self.patch(messenger, "was_sent", was_sent)
        send.resolve_held(None)
        self.assertEqual(self.statuses(went)[-1], "sent")
        self.assertEqual(self.statuses(didnt)[-1], "cleared")
        self.assertEqual(self.statuses(unsure)[-1], "uncertain")
        self.assertEqual([l["post_key"] for l in send.approved_leads()], [didnt["post_key"]])

    def test_a_block_while_settling_held_sends_stops_checking(self):
        a, b = self.approve(lead(1), lead(2))
        for l in (a, b):
            store.record(l["author_key"], l["author"], l["post_key"], "uncertain", "fake")
        was_sent = self.patch(messenger, "was_sent", side_effect=AccountBlocked("temporarily blocked", "rate_limited"))
        send.resolve_held(None)
        self.assertEqual(was_sent.call_count, 1)
        self.assertEqual(set(store.held()), {a["author_key"], b["author_key"]})


class PrepareStopsBeforeAnyChat(FakeChromeTest):
    def driver(self, inbox, me=SENDER):
        pages = {"https://www.facebook.com/": {"text": "News Feed"}, messenger.INBOX: inbox}
        if "redirect" in inbox:
            pages[inbox["redirect"]] = {"text": "Confirm your identity"}
        return FakeChrome(pages, {"c_user": me} if me else {})

    def test_pin_on_the_inbox_stops_an_unattended_run(self):
        with self.assertRaises(SystemExit) as stop:
            send.prepare(self.driver({"text": PIN_TEXT}), config())
        self.assertIn("PIN", str(stop.exception))

    def test_pin_on_the_inbox_waits_for_you_by_hand(self):
        self.patch(send, "input", create=True, return_value="")
        driver = self.driver({"text": PIN_TEXT})

        def entered_pin(prompt=""):
            driver.pages[messenger.INBOX] = {"text": "Chats"}
        send.input.side_effect = entered_pin
        self.assertEqual(send.prepare(driver, config(), interactive=True), SENDER)

    def test_checkpoint_stops_the_run(self):
        with self.assertRaises(SystemExit) as stop:
            send.prepare(self.driver({"redirect": "https://www.facebook.com/checkpoint/1501092823525282/"}), config())
        self.assertIn("restricted", str(stop.exception))

    def test_logged_out_stops_an_unattended_run(self):
        driver = FakeChrome({"https://www.facebook.com/": {"text": "Log in to Facebook  Create new account"}})
        with self.assertRaises(SystemExit) as stop:
            send.prepare(driver, config())
        self.assertIn("python run.py login", str(stop.exception))

    def test_wrong_account_stops_the_run(self):
        with self.assertRaises(SystemExit) as stop:
            send.prepare(self.driver({"text": "Chats"}, me="999"), config())
        self.assertIn("999", str(stop.exception))


class FacebookWording(unittest.TestCase):
    """messenger.check_account / check_recipient on the wording Facebook uses (add new wording here when a
    real run meets it)."""

    def kind(self, text, url="https://www.facebook.com/messages/t/1"):
        driver = FakeChrome({url: {"text": text}})
        driver.current_url = url
        try:
            messenger.check_account(driver)
            messenger.check_recipient(driver, "Jane")
        except Exception as exc:
            from send_dm import classify
            return classify(exc)
        return None

    def test_wording(self):
        cases = [
            ("Enter your PIN", "login"),
            ("Create a PIN to secure your chats", "login"),
            ("Restore your chat history", "login"),
            ("Your session has expired. Please log in again.", "login"),
            ("You're Temporarily Blocked", "rate_limited"),
            ("We limit how often you can post, comment or do other things", "rate_limited"),
            ("You've reached the limit for message requests", "rate_limited"),
            ("Your account has been restricted", "restricted"),
            ("You can't use this feature right now", "restricted"),
            ("Confirm your identity", "restricted"),
            ("This person is unavailable on Messenger", "recipient"),
            ("You can't message this account", "refused"),
            ("Jane Doe  Active 2h ago  Hi Jane!", None),
        ]
        for text, want in cases:
            with self.subTest(text):
                self.assertEqual(self.kind(text), want)

    def test_urls(self):
        self.assertEqual(self.kind("", "https://www.facebook.com/checkpoint/123/"), "restricted")
        self.assertEqual(self.kind("", "https://www.facebook.com/login/?next=x"), "login")


if __name__ == "__main__":
    unittest.main()



class VerifyAfterSend(FakeChromeTest):
    """outreach.verify_after_send reaches messenger.send_message: off by default, so a sent DM isn't looked for."""

    def sent_with(self, cfg):
        self.approve(lead(1))
        real = self.patch(messenger, "send_message", return_value=Result("sent", "fake: sent"))
        send.send_loop(None, send.approved_leads(), cfg)
        return real.call_args.kwargs["verify"]

    def test_off_by_default(self):
        self.assertFalse(self.sent_with(config()))

    def test_on_when_configured(self):
        self.assertTrue(self.sent_with(config(verify_after_send=True)))
