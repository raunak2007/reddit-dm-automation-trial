"""test_mock_site.py - messenger.py's real page code (its _JS, the waits, typing, pressing Send) in a headless
Chrome against mock Messenger chats (mock_site.py). Nothing here touches facebook.com; test_live.py does.
Skipped if Chrome can't start. Takes about a minute.

    cd facebook && ../venv/bin/python -m unittest tests/test_mock_site.py
"""
from __future__ import annotations

import io
import os
import re
import sys
import tempfile
import unittest
from unittest import mock

import helpers  # noqa: F401  (puts the facebook folder on sys.path)
from mock_site import MockSite, chat_page, headless_chrome

import messenger
from send_dm import AccountBlocked

site = driver = None
PATH = "/messages/t/1001"
TEXT = "Hi Jane, saw your post about essays. Happy to help: https://example.com/#get-started"


def setUpModule():
    global site, driver
    driver = headless_chrome()
    site = MockSite()


def tearDownModule():
    if driver:
        driver.quit()
    if site:
        site.close()


class MockChat(unittest.TestCase):
    def setUp(self):
        site.reset()
        for p in (mock.patch.object(messenger, "CHAT_URL", re.compile(rf"^{re.escape(site.base)}/messages/t/[^/?#]+/?$")),
                  mock.patch.object(messenger, "SETTLE", 1),
                  mock.patch.object(sys, "stdout", io.StringIO())):
            p.start()
            self.addCleanup(p.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cwd = os.getcwd()
        os.chdir(tmp.name)                          # failed-step screenshots land here
        self.addCleanup(os.chdir, cwd)
        self.pressed = []

    def send(self, name="Jane Doe", text=TEXT, **kw):
        return messenger.send_message(driver, site.url(PATH), text, name,
                                      before_submit=lambda: self.pressed.append(True), **kw)

    def box_text(self):
        return driver.execute_script("const b = document.querySelector('[role=textbox]'); return b ? b.innerText : ''")


class Sending(MockChat):
    def test_sends_once_and_records_it_without_reloading_the_chat(self):
        site.pages[PATH] = chat_page(site, "Jane Doe")
        with mock.patch.object(messenger, "verify_in_chat", wraps=messenger.verify_in_chat) as check:
            result = self.send()
        self.assertEqual(result.status, "sent", result.reason)
        self.assertIn("not checked", result.reason)
        check.assert_not_called()
        self.assertEqual(site.sent[PATH], [TEXT], "pressed exactly once")
        self.assertEqual(self.pressed, [True])

    def test_verify_confirms_it_in_the_reloaded_chat(self):
        site.pages[PATH] = chat_page(site, "Jane Doe")
        with mock.patch.object(messenger, "verify_in_chat", wraps=messenger.verify_in_chat) as check:
            result = self.send(verify=True)
        self.assertEqual(result.status, "sent", result.reason)
        self.assertEqual(result.reason, "messenger: confirmed in the chat")
        check.assert_called_once()
        self.assertEqual(site.sent[PATH], [TEXT], "pressed exactly once")

    def test_message_already_in_the_chat_is_not_sent_again(self):
        site.pages[PATH] = chat_page(site, "Jane Doe")
        site.sent[PATH] = [TEXT]
        result = self.send()
        self.assertEqual(result.status, "sent")
        self.assertIn("already", result.reason)
        self.assertEqual(site.sent[PATH], [TEXT])
        self.assertEqual(self.pressed, [])

    def test_send_that_fails_after_the_press_is_uncertain(self):
        site.pages[PATH] = chat_page(site, "Jane Doe", mode="fail")
        result = self.send()
        self.assertEqual(result.status, "uncertain", result.reason)
        self.assertEqual(self.pressed, [True])
        self.assertNotIn(PATH, site.sent)

    def test_chat_with_someone_else_is_not_typed_into(self):
        site.pages[PATH] = chat_page(site, "John Roe")
        result = self.send(name="Jane Doe")
        self.assertEqual(result.status, "not_submitted", result.reason)
        self.assertEqual(self.box_text().strip(), "")
        self.assertEqual(self.pressed, [])


class Stops(MockChat):
    def test_pin_prompt_stops_before_typing(self):
        site.pages[PATH] = chat_page(site, "Jane Doe", dialog="Enter your PIN to restore your chat history")
        result = self.send()
        self.assertEqual(result.status, "login", result.reason)
        self.assertIn("PIN", result.reason)
        self.assertEqual(self.box_text().strip(), "")
        self.assertEqual(self.pressed, [])

    def test_block_dialog_stops_before_typing(self):
        site.pages[PATH] = chat_page(site, "Jane Doe", dialog="You're Temporarily Blocked. It looks like you "
                                                             "were misusing this feature by going too fast.")
        result = self.send()
        self.assertEqual(result.status, "rate_limited", result.reason)
        self.assertEqual(self.pressed, [])

    def test_cant_message_notice_is_a_refusal(self):
        site.pages[PATH] = chat_page(site, "Jane Doe", notice="You can't message this account.")
        self.assertEqual(self.send().status, "refused")

    def test_redirect_to_login_stops_the_run(self):
        site.pages[PATH] = chat_page(site, "Jane Doe")
        site.redirects[PATH] = "/login/"
        site.pages["/login/"] = "<html><body><div>Log in to Facebook</div><input name=email></body></html>"
        self.assertEqual(self.send().status, "login")


class ReadingThePage(MockChat):
    def test_our_own_draft_is_never_read_as_facebook_talking(self):
        site.pages[PATH] = chat_page(site, "Jane Doe", draft="are you temporarily blocked? log in to facebook, "
                                                             "enter your PIN, you can't message this account")
        driver.get(site.url(PATH))
        messenger.check_account(driver)             # raises if the draft were read as a notice
        messenger.check_recipient(driver, "Jane Doe")

    def test_finds_exactly_one_box_and_the_send_button(self):
        site.pages[PATH] = chat_page(site, "Jane Doe")
        driver.get(site.url(PATH))
        self.assertIsNotNone(messenger.composer(driver))
        self.assertEqual(len(messenger.js(driver, "send")), 1)

    def test_emoji_drawn_as_images_still_match(self):
        site.pages[PATH] = ('<div role="main"><h2>Jane Doe</h2><div>hey jane <img alt="\U0001F44B" src="x.png"> '
                            'good luck!</div></div>')
        driver.get(site.url(PATH))
        self.assertTrue(messenger.in_chat(driver, "hey jane \U0001F44B good luck!"))

    def test_account_wide_notice_raises_with_its_kind(self):
        site.pages[PATH] = chat_page(site, "Jane Doe", dialog="Confirm your identity to keep using Facebook")
        driver.get(site.url(PATH))
        with self.assertRaises(AccountBlocked) as blocked:
            messenger.check_account(driver)
        self.assertEqual(blocked.exception.kind, "restricted")


if __name__ == "__main__":
    unittest.main()
