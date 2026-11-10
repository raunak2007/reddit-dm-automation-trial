"""test_mock_site.py - direct.py's real page code (its _JS, the Follow button lookup, the waits, typing, pressing
Send) in a headless Chrome against mock Instagram profiles and chats (mock_site.py). Nothing here touches
instagram.com; test_live.py does. Skipped if Chrome can't start. Takes about a minute.

    cd instagram && ../venv/bin/python -m unittest tests/test_mock_site.py
"""
from __future__ import annotations

import io
import os
import re
import sys
import tempfile
import unittest
from unittest import mock

import helpers  # noqa: F401  (puts the instagram folder on sys.path)
from helpers import config
from mock_site import MockSite, chat_page, grid_page, headless_chrome, inbox_page, profile_page
from selenium.common.exceptions import TimeoutException

import direct
import discover
import replies
import send_dm
from send_dm import AccountBlocked, NoSuchUser

site = driver = None
USER, THREAD = "jane.doe", "/direct/t/340282366841710300949128/"
TEXT = "hey jane! saw your comment about essays - happy to hop on a free call: https://example.com/#get-started"


def setUpModule():
    global site, driver
    driver = headless_chrome()
    site = MockSite()


def tearDownModule():
    if driver:
        driver.quit()
    if site:
        site.close()


class MockInstagram(unittest.TestCase):
    def setUp(self):
        site.reset()
        base = re.escape(site.base)
        for p in (mock.patch.object(direct, "IG", site.base),
                  mock.patch.object(direct, "OPEN_URL", re.compile(rf"^{base}/m/[A-Za-z0-9._]+/?$")),
                  mock.patch.object(direct, "CHAT_URL", re.compile(rf"^{base}/direct/t/[^/?#]+/?$")),
                  mock.patch.object(direct, "SETTLE", 1),
                  # a missing button fails in 4s instead of 20
                  mock.patch.object(direct, "wait_for", lambda d, probe, what, timeout=4:
                                    send_dm.wait_for(d, probe, what, timeout=min(timeout, 4))),
                  mock.patch.object(sys, "stdout", io.StringIO())):
            p.start()
            self.addCleanup(p.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cwd = os.getcwd()
        os.chdir(tmp.name)                          # failed-step screenshots land here
        self.addCleanup(os.chdir, cwd)

    def header_button(self):
        return driver.execute_script("return document.getElementById('follow').innerText.trim()")

    def suggested_buttons(self):
        return driver.execute_script("return [...document.querySelectorAll('#below button, #below [role=button]')]"
                                     ".map(b => b.innerText.trim())")


class FollowButton(MockInstagram):
    def test_presses_the_header_follow_not_a_suggested_one(self):
        site.pages[f"/{USER}/"] = profile_page(USER, suggested=True)
        self.assertEqual(direct.follow(driver, USER), "followed")
        self.assertEqual(self.header_button(), "Following")
        self.assertEqual(self.suggested_buttons(), ["Follow", "Follow"], "no suggested account was followed")

    def test_finds_exactly_one_follow_button_on_the_profile(self):
        site.pages[f"/{USER}/"] = profile_page(USER, suggested=True)
        driver.get(site.url(f"/{USER}/"))
        self.assertEqual(len(direct.js(driver, "follow")), 1)
        self.assertFalse(direct.js(driver, "following"))

    def test_private_account_is_requested(self):
        site.pages[f"/{USER}/"] = profile_page(USER, then="requested")
        self.assertEqual(direct.follow(driver, USER), "followed")
        self.assertEqual(self.header_button(), "Requested")

    def test_follow_back_counts(self):
        site.pages[f"/{USER}/"] = profile_page(USER, button="Follow Back")
        self.assertEqual(direct.follow(driver, USER), "followed")

    def test_already_following_or_requested_is_not_pressed(self):
        for shown in ("Following", "Requested"):
            with self.subTest(shown):
                site.pages[f"/{USER}/"] = profile_page(USER, button=shown, then="block")
                self.assertEqual(direct.follow(driver, USER), "already")
                self.assertEqual(self.header_button(), shown)

    def test_notifications_dialog_is_closed_first(self):
        site.pages[f"/{USER}/"] = profile_page(USER, notifications=True)
        self.assertEqual(direct.follow(driver, USER), "followed")

    def test_both_block_popups_stop_with_rate_limited(self):
        for popup in ("block", "limit"):
            with self.subTest(popup):
                site.pages[f"/{USER}/"] = profile_page(USER, then=popup)
                with self.assertRaises(AccountBlocked) as blocked:
                    direct.follow(driver, USER)
                self.assertEqual(blocked.exception.kind, "rate_limited")

    def test_press_that_does_nothing_times_out(self):
        site.pages[f"/{USER}/"] = profile_page(USER, then="nothing")
        with self.assertRaises(TimeoutException):
            direct.follow(driver, USER)

    def test_profile_without_a_header_follow_times_out_rather_than_pressing_a_suggested_one(self):
        site.pages[f"/{USER}/"] = profile_page(USER, button="Edit profile", suggested=True)
        with self.assertRaises(TimeoutException):
            direct.follow(driver, USER)
        self.assertEqual(self.suggested_buttons(), ["Follow", "Follow"])

    def test_missing_account(self):
        site.pages[f"/{USER}/"] = ("<main role=main><h2>Sorry, this page isn't available.</h2>The link you followed "
                                   "may be broken, or the page may have been removed.</main>")
        with self.assertRaises(NoSuchUser):
            direct.follow(driver, USER)


class Chat(MockInstagram):
    def setUp(self):
        super().setUp()
        site.redirects[f"/m/{USER}"] = THREAD
        self.pressed = []

    def send(self, text=TEXT, **kw):
        return direct.send_message(driver, site.url(f"/m/{USER}"), text, USER, "Jane Doe",
                                   before_submit=lambda: self.pressed.append(True), **kw)

    def test_sends_once_and_records_it_without_reloading_the_chat(self):
        site.pages[THREAD] = chat_page(site, USER, name="Jane Doe")
        with mock.patch.object(direct, "verify_in_chat", wraps=direct.verify_in_chat) as check:
            result = self.send()
        self.assertEqual(result.status, "sent", result.reason)
        self.assertIn("not checked", result.reason)
        check.assert_not_called()
        self.assertEqual(site.sent[THREAD], [TEXT])
        self.assertEqual(self.pressed, [True])

    def test_verify_confirms_it_in_the_reloaded_chat(self):
        site.pages[THREAD] = chat_page(site, USER, name="Jane Doe")
        with mock.patch.object(direct, "verify_in_chat", wraps=direct.verify_in_chat) as check:
            result = self.send(verify=True)
        self.assertEqual(result.status, "sent", result.reason)
        self.assertEqual(result.reason, "direct: confirmed in the chat")
        check.assert_called_once()
        self.assertEqual(site.sent[THREAD], [TEXT])

    def test_message_already_in_the_chat_is_not_sent_again(self):
        site.pages[THREAD] = chat_page(site, USER)
        site.sent[THREAD] = [TEXT]
        self.assertEqual(self.send().status, "sent")
        self.assertEqual(site.sent[THREAD], [TEXT])
        self.assertEqual(self.pressed, [])

    def test_block_popup_in_the_chat_stops_before_typing(self):
        site.pages[THREAD] = chat_page(site, USER, dialog="Try Again Later. We limit how often you can do certain "
                                                          "things on Instagram to protect our community.")
        result = self.send()
        self.assertEqual(result.status, "rate_limited", result.reason)
        self.assertEqual(self.pressed, [])

    def test_cant_message_notice_is_a_refusal(self):
        site.pages[THREAD] = chat_page(site, USER, notice="Not everyone can message this account.")
        self.assertEqual(self.send().status, "refused")

    def test_our_own_draft_is_never_read_as_instagram_talking(self):
        site.pages[THREAD] = chat_page(site, USER, draft="action blocked? try again later, we restrict certain "
                                                         "activity, log in to instagram")
        driver.get(site.url(THREAD))
        direct.check_account(driver)
        direct.check_recipient(driver, USER)


class Fallbacks(MockInstagram):
    """The page-reading fallbacks for when Instagram's API doesn't answer: discover.py's profile grid and
    replies.py's chat list, with their real page code."""

    def test_profile_grid_links_newest_first_once_each(self):
        site.pages["/me.test/"] = grid_page("me.test", ["/me.test/p/POST1/", "/me.test/reel/REEL2/", "/p/POST3/"])
        posts = discover.from_page(driver, config(), "me.test")
        self.assertEqual([(p["shortcode"], p["url"]) for p in posts],
                         [("POST1", "https://www.instagram.com/p/POST1/"),
                          ("REEL2", "https://www.instagram.com/reel/REEL2/"),
                          ("POST3", "https://www.instagram.com/p/POST3/")], "the nav's post link isn't one")

    def test_profile_grid_with_a_block_stops(self):
        site.pages["/me.test/"] = profile_page("me.test", notifications=False).replace(
            "<article>", '<div role="dialog">Action Blocked. This action was blocked. Please try again later. We '
                         'restrict certain activity to protect our community.</div><article>')
        with self.assertRaises(AccountBlocked):
            discover.from_page(driver, config(), "me.test")

    def test_inbox_chat_list_finds_replies(self):
        site.pages["/direct/inbox/"] = inbox_page([("Jane Doe", "yes! how does the call work", "2h"),
                                                   ("Sam P", "You: hey sam! saw your comment", "1d"),
                                                   ("Other Person", "hello", "5m")])
        rows = replies.via_page(driver, config())
        sent = {"jane.doe": "2026-11-01T10:00:00", "sam.p": "2026-11-01T10:00:00"}
        out, seen = replies.from_rows(rows, sent, {"jane.doe": "Jane Doe", "sam.p": "Sam P"},
                                      replies.datetime(2026, 11, 9, 12, 0))
        self.assertEqual(seen, {"jane.doe", "sam.p"})
        self.assertEqual([(r["username"], r["snippet"], r["replied_at"]) for r in out],
                         [("jane.doe", "yes! how does the call work", "2026-11-09T10:00:00")])


if __name__ == "__main__":
    unittest.main()
