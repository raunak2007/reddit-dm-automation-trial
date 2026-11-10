"""test_stops.py - what send.py does when Instagram logs the account out, challenges it or blocks an action
(during the follow or the DM), and what the next run does after such a stop. No browser: FakeChrome
(helpers.py) shows the wording and URLs Instagram uses, and direct.py's real patterns read them.

    cd instagram && ../venv/bin/python -m unittest discover tests
"""
from __future__ import annotations

import unittest

from helpers import IG, SENDER, FakeChrome, StoreTest, config, lead, profile
from selenium.common.exceptions import TimeoutException

import direct
import send
import store
from send_dm import AccountBlocked, NoSuchUser, Result

BLOCKED = ("Action Blocked. This action was blocked. Please try again later. We restrict certain activity to "
           "protect our community.")


def chat_pages(l, text=None, **page):
    """A chat with l: its ig.me link redirects to /direct/t/<id>/, which shows their username (and `text`)."""
    thread = f"{IG}/direct/t/{abs(hash(l['username'])) % 10**9}/"
    shown = l["username"] + (f" {text}" if text else "")
    return {direct.chat_link(l["username"]): {"redirect": thread}, thread: {"text": shown, "composer": True, **page}}


class Sender:
    """A send function for send_loop that answers from a script ({username: Result or exception}, default 'sent')
    and logs ('dm', username) to `log`. press=True runs before_submit first, as a real press would."""

    def __init__(self, answers=None, press=(), log=None):
        self.answers, self.press, self.calls = answers or {}, set(press), []
        self.log = log if log is not None else []

    def __call__(self, driver, url, text, user, name="", *, before_submit=None):
        self.calls.append(user)
        self.log.append(("dm", user))
        if user in self.press:
            before_submit()
        answer = self.answers.get(user, Result("sent", "fake: sent"))
        if isinstance(answer, Exception):
            raise answer
        return answer


class FakeChromeTest(StoreTest):
    def setUp(self):
        super().setUp()
        self.patch(direct, "SETTLE", 0)
        self.patch(direct, "pause", lambda *a: None)


class BlockDuringFollow(FakeChromeTest):
    def test_block_from_follow_halts_before_any_dm(self):
        a, b = self.approve(lead(1), lead(2))
        follow = self.patch(direct, "follow", side_effect=AccountBlocked(f"Instagram is refusing this account: "
                                                                         f"{BLOCKED!r}", "rate_limited"))
        dm = Sender()
        counts = send.send_loop(None, send.approved_leads(), config(), send=dm)

        self.assertEqual(dm.calls, [], "no DM after Instagram blocked the follow")
        self.assertEqual(follow.call_count, 1, "B isn't followed either")
        self.assertTrue(counts["halted"])
        self.assertIn("rate_limited", counts["stopped"])
        self.assertEqual(send.report(counts), 2)
        self.assertEqual(self.statuses(a), ["approved", "rate_limited"])
        self.assertEqual(self.statuses(b), ["approved"])

    def test_the_next_run_follows_and_messages_the_same_person(self):
        a, b = self.approve(lead(1), lead(2))
        self.patch(direct, "follow", side_effect=AccountBlocked("action blocked", "rate_limited"))
        send.send_loop(None, send.approved_leads(), config(), send=Sender())

        log = []
        direct.follow.side_effect = lambda driver, user: log.append(("follow", user)) or "followed"
        counts = send.send_loop(None, send.approved_leads(), config(), send=Sender(log=log))
        self.assertEqual(log, [("follow", a["username"]), ("dm", a["username"]),
                               ("follow", b["username"]), ("dm", b["username"])])
        self.assertEqual(counts["sent"], 2)

    def test_block_shown_after_pressing_follow_halts(self):
        """direct.follow itself, on a profile where Instagram answers the press with "Action Blocked"."""
        a, b = self.approve(lead(1), lead(2))
        driver = FakeChrome({profile(a["username"]): {"text": a["username"], "follow": True,
                                                      "on_follow": {"text": f"{a['username']} {BLOCKED}"}}})
        dm = Sender()
        counts = send.send_loop(driver, [a, b], config(), send=dm)

        self.assertEqual(driver.follow_button.clicks, 1, "Follow pressed once, never retried")
        self.assertEqual(dm.calls, [])
        self.assertTrue(counts["halted"])
        self.assertEqual(self.statuses(a)[-1], "rate_limited")
        self.assertNotIn(profile(b["username"]), driver.visited)

    def test_challenge_or_logout_on_the_profile_halts(self):
        cases = {"restricted": f"{IG}/challenge/AXH3k/?next=/jane/", "login": f"{IG}/accounts/login/?next=/jane/"}
        for n, (kind, where) in enumerate(cases.items(), 1):
            with self.subTest(kind):
                a, = self.approve(lead(10 * n))
                driver = FakeChrome({profile(a["username"]): {"redirect": where}, where: {"text": "Instagram"}})
                dm = Sender()
                counts = send.send_loop(driver, [a], config(), send=dm)
                self.assertEqual(dm.calls, [])
                self.assertIn(f"({kind})", counts["stopped"])
                self.assertEqual(driver.follow_button.clicks, 0)

    def test_follow_that_fails_for_other_reasons_still_sends_the_dm(self):
        for failure in (TimeoutException("timed out after 20s waiting for @x's Follow button"),
                        NoSuchUser("Instagram says there is no account @x")):
            with self.subTest(type(failure).__name__):
                a, = self.approve(lead(len(store.history()) + 1))
                self.patch(direct, "follow", side_effect=failure)
                dm = Sender()
                counts = send.send_loop(None, [a], config(), send=dm)
                self.assertEqual(dm.calls, [a["username"]])
                self.assertFalse(counts["halted"])
                self.assertIn("sending the DM anyway", self.out.getvalue())

    def test_already_following_is_not_pressed_again(self):
        for shown in ("following", "requested"):
            with self.subTest(shown):
                driver = FakeChrome({profile("jane"): {"text": "jane", "following": True, "follow": True}})
                self.assertEqual(direct.follow(driver, "jane"), "already")
                self.assertEqual(driver.follow_button.clicks, 0)

    def test_follow_press_that_takes_returns_followed(self):
        driver = FakeChrome({profile("jane"): {"text": "jane", "follow": True}})
        self.assertEqual(direct.follow(driver, "jane"), "followed")
        self.assertEqual(driver.follow_button.clicks, 1)

    def test_auto_follow_off_never_opens_a_profile(self):
        a, = self.approve(lead(1))
        follow = self.patch(direct, "follow")
        send.send_loop(None, [a], config(auto_follow=False), send=Sender())
        follow.assert_not_called()

    def test_by_hand_block_during_follow_stops_before_the_chat(self):
        a, b = self.approve(lead(1), lead(2))
        self.patch(direct, "follow", side_effect=AccountBlocked("action blocked", "rate_limited"))
        opened = self.patch(direct, "open_for_hand")
        self.patch(send, "input", create=True, side_effect=AssertionError("asked you about a blocked person"))
        counts = send.walk_by_hand(None, [a, b], config())
        opened.assert_not_called()
        self.assertTrue(counts["halted"])
        self.assertEqual(self.statuses(a)[-1], "rate_limited")


class AccountStopsDuringTheDM(FakeChromeTest):
    def test_logout_halts_and_the_next_run_starts_with_the_same_person(self):
        a, b, c = self.approve(lead(1), lead(2), lead(3))
        first = Sender({b["username"]: Result("login", "direct: Instagram sent this profile to /accounts/login")})
        counts = send.send_loop(None, send.approved_leads(), config(auto_follow=False), send=first)
        self.assertEqual(first.calls, [a["username"], b["username"]])
        self.assertTrue(counts["halted"])

        second = Sender()
        send.send_loop(None, send.approved_leads(), config(auto_follow=False), send=second)
        self.assertEqual(second.calls, [b["username"], c["username"]])
        for l in (a, b, c):
            self.assertEqual(self.statuses(l).count("sent"), 1)

    def test_action_blocked_in_a_chat_halts_before_typing(self):
        a, b = self.approve(lead(1), lead(2))
        driver = FakeChrome({**chat_pages(a, BLOCKED), **chat_pages(b)})
        counts = send.send_loop(driver, [a, b], config(auto_follow=False))
        self.assertIn("(rate_limited)", counts["stopped"])
        self.assertEqual(driver.box.typed, [])
        self.assertNotIn(direct.chat_link(b["username"]), driver.visited)

    def test_block_after_send_was_pressed_is_not_sent_twice(self):
        a, = self.approve(lead(1))
        first = Sender({a["username"]: Result("rate_limited", "direct: action blocked")}, press={a["username"]})
        send.send_loop(None, send.approved_leads(), config(auto_follow=False), send=first)
        self.assertEqual(self.statuses(a), ["approved", "submitting", "rate_limited"])

        driver = FakeChrome(chat_pages(a, a["approved"]["dm"]))
        counts = send.send_loop(driver, send.approved_leads(), config(auto_follow=False))
        self.assertEqual(counts["sent"], 1)
        self.assertEqual(driver.box.typed, [], "the message was already in the chat: nothing typed")

    def test_crash_after_send_was_pressed_is_held_not_resent(self):
        a, b = self.approve(lead(1), lead(2))
        first = Sender({a["username"]: RuntimeError("chrome not reachable")}, press={a["username"]})
        send.send_loop(None, [a, b], config(auto_follow=False), send=first)
        self.assertIn(a["username"], store.held())

        second = Sender()
        send.send_loop(None, send.approved_leads(), config(auto_follow=False), send=second)
        self.assertNotIn(a["username"], second.calls)

    def test_held_sends_are_settled_at_the_start_of_the_next_run(self):
        went, didnt = self.approve(lead(1), lead(2))
        for l in (went, didnt):
            store.record(l["username"], l["key"], "uncertain", "fake")
        self.patch(direct, "was_sent", side_effect=lambda d, url, text, user, name: user == went["username"])
        send.resolve_held(None)
        self.assertEqual(self.statuses(went)[-1], "sent")
        self.assertEqual(self.statuses(didnt)[-1], "cleared")
        self.assertEqual([l["key"] for l in send.approved_leads()], [didnt["key"]])

    def test_a_block_while_settling_held_sends_stops_checking(self):
        a, b = self.approve(lead(1), lead(2))
        for l in (a, b):
            store.record(l["username"], l["key"], "uncertain", "fake")
        was_sent = self.patch(direct, "was_sent", side_effect=AccountBlocked("action blocked", "rate_limited"))
        send.resolve_held(None)
        self.assertEqual(was_sent.call_count, 1)
        self.assertEqual(set(store.held()), {a["username"], b["username"]})


class PrepareStopsBeforeAnyChat(FakeChromeTest):
    def driver(self, inbox, cookies=None):
        pages = {f"{IG}/": {"text": "Home Reels Messages"}, direct.INBOX: inbox}
        if "redirect" in inbox:
            pages[inbox["redirect"]] = {"text": "Help us confirm it's you"}
        return FakeChrome(pages, cookies)

    def test_challenge_stops_the_run(self):
        with self.assertRaises(SystemExit) as stop:
            send.prepare(self.driver({"redirect": f"{IG}/challenge/AXH3k/"}), config())
        self.assertIn("restricted", str(stop.exception))

    def test_action_blocked_on_the_inbox_stops_the_run(self):
        with self.assertRaises(SystemExit) as stop:
            send.prepare(self.driver({"text": BLOCKED}), config())
        self.assertIn("rate_limited", str(stop.exception))

    def test_logged_out_stops_an_unattended_run(self):
        with self.assertRaises(SystemExit) as stop:
            send.prepare(self.driver({"text": "Messages"}, cookies={}), config())
        self.assertIn("python run.py login", str(stop.exception))

    def test_wrong_account_stops_the_run(self):
        with self.assertRaises(SystemExit) as stop:
            send.prepare(self.driver({"text": "Messages"}, cookies={"sessionid": "x", "ds_user_id": "999"}), config())
        self.assertIn("999", str(stop.exception))

    def test_right_account_passes(self):
        self.assertEqual(send.prepare(self.driver({"text": "Messages"}), config()), SENDER)


class InstagramWording(unittest.TestCase):
    """direct.check_account / check_recipient on the wording Instagram uses (add new wording here when a real run
    meets it)."""

    def kind(self, text, url=f"{IG}/direct/t/1/"):
        driver = FakeChrome({url: {"text": text}})
        driver.current_url = url
        try:
            direct.check_account(driver)
            direct.check_recipient(driver, "jane")
        except Exception as exc:
            from send_dm import classify
            return classify(exc)
        return None

    def test_wording(self):
        cases = [
            (BLOCKED, "rate_limited"),
            ("Try Again Later. We limit how often you can do certain things on Instagram to protect our community.",
             "rate_limited"),
            ("Please wait a few minutes before you try again.", "rate_limited"),
            ("Log in to Instagram", "login"),
            ("You've been logged out", "login"),
            ("Your account has been temporarily locked", "restricted"),
            ("We suspended your account", "restricted"),
            ("Help us confirm it's you", "restricted"),
            ("We detected suspicious login activity", "restricted"),
            ("Sorry, this page isn't available.", "recipient"),
            ("Not everyone can message this account.", "refused"),
            ("jane  Active now  hey jane!", None),
        ]
        for text, want in cases:
            with self.subTest(text):
                self.assertEqual(self.kind(text), want)

    def test_urls(self):
        self.assertEqual(self.kind("", f"{IG}/challenge/AXH3k/"), "restricted")
        self.assertEqual(self.kind("", f"{IG}/accounts/suspended/"), "restricted")
        self.assertEqual(self.kind("", f"{IG}/accounts/login/?next=x"), "login")


if __name__ == "__main__":
    unittest.main()


class VerifyAfterSend(FakeChromeTest):
    """outreach.verify_after_send reaches direct.send_message: off by default, so a sent DM isn't looked for."""

    def sent_with(self, cfg):
        self.approve(lead(1))
        real = self.patch(direct, "send_message", return_value=Result("sent", "fake: sent"))
        send.send_loop(None, send.approved_leads(), cfg)
        return real.call_args.kwargs["verify"]

    def test_off_by_default(self):
        self.assertFalse(self.sent_with(config(auto_follow=False)))

    def test_on_when_configured(self):
        self.assertTrue(self.sent_with(config(auto_follow=False, verify_after_send=True)))
