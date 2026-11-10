"""test_live.py - read-only checks against the real facebook.com, with the Chrome profile in config.json. Off
unless FB_LIVE=1. Nothing is typed, sent or recorded: it checks that messenger.py still finds what it needs on
today's Facebook (logged in as the sender, an inbox with no PIN prompt or block, one chat's message box) and
that none of its block/PIN patterns fire on ordinary pages.

    cd facebook
    FB_LIVE=1 ../venv/bin/python -m unittest tests/test_live.py -v
    FB_LIVE=1 FB_LIVE_CHAT=<author_key> FB_LIVE_NAME="Jane Doe" ../venv/bin/python -m unittest tests/test_live.py -v
                                    also open that chat (someone you've messaged before), check its box, and
                                    print who messenger.thread thinks wrote each message (check it by eye)
    FB_LIVE=1 FB_LIVE_POST=<post url> FB_LIVE_POST_AUTHOR="Jane Doe" FB_LIVE_POST_TEXT="first words of the post" \
        ../venv/bin/python -m unittest tests/test_live.py -v
                                    also open that post and check comment.py finds exactly its own comment box
    python run.py livecheck         the same, with those settings from config.json's livecheck block, and an exit
                                    code for a scheduler (0 ok, 2 account problem, 1 anything else)

FB_LIVE_STRICT=1 (livecheck sets it): a profile that isn't logged in fails the run instead of skipping it.
Everything here is read-only: pages are opened and read, never typed into or clicked (tests/test_livecheck.py checks
this file for any call that could type, press or record).

Close send.py / run.py first: Chrome won't open the same profile twice. A failed check saves its screenshot and
page listing (step*_failed.png / _dom.txt) in debug/live/; when the markup changed, copy it into mock_site.py so
test_mock_site.py covers it from then on.
"""
from __future__ import annotations

import json
import os
import unittest

import helpers  # noqa: F401  (puts the facebook folder on sys.path)

import browser
import comment
import messenger
import store
from send_dm import step

LIVE = os.environ.get("FB_LIVE") == "1"
STRICT = os.environ.get("FB_LIVE_STRICT") == "1"
CHAT, NAME = os.environ.get("FB_LIVE_CHAT", ""), os.environ.get("FB_LIVE_NAME", "")
POST, POST_AUTHOR = os.environ.get("FB_LIVE_POST", ""), os.environ.get("FB_LIVE_POST_AUTHOR", "")
POST_TEXT = os.environ.get("FB_LIVE_POST_TEXT", "")
OUT = helpers.FOLDER / "debug" / "live"
cfg = driver = None


@unittest.skipUnless(LIVE, "set FB_LIVE=1 to check against the real facebook.com")
class LiveFacebook(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        global cfg, driver
        OUT.mkdir(parents=True, exist_ok=True)
        cls.cwd = os.getcwd()
        os.chdir(OUT)
        cfg = json.loads((helpers.FOLDER / "config.json").read_text())
        driver = browser.make_driver(cfg)
        try:
            browser.ensure_logged_in(driver, cfg, interactive=False)
        except SystemExit as exc:
            driver.quit()
            if STRICT:                            # livecheck: logged out is an account problem, not a skip
                raise RuntimeError(f"not logged in: {exc}") from None
            raise unittest.SkipTest(f"not logged in: {exc}")

    @classmethod
    def tearDownClass(cls):
        driver.quit()
        os.chdir(cls.cwd)

    def test_logged_in_as_the_sender(self):
        me = (driver.get_cookie("c_user") or {}).get("value")
        self.assertTrue(me, "no c_user cookie: Facebook didn't say who is logged in")
        expected = str(cfg["outreach"].get("sender_user_id") or "")
        if expected:
            self.assertEqual(me, expected, "logged in as a different account than outreach.sender_user_id")

    def test_feed_trips_no_block_or_pin_pattern(self):
        with step(driver, "live_feed", "reading the News Feed with messenger.py's patterns"):
            driver.get("https://www.facebook.com/")
            browser.pause(cfg, 0.6)
            messenger.check_account(driver)

    def test_inbox_is_usable(self):
        with step(driver, "live_inbox", "opening the Messenger inbox"):
            driver.get(messenger.INBOX)
            browser.pause(cfg, 0.6)
            self.assertEqual(messenger.js(driver, "notice", messenger.PIN), "",
                             "Messenger wants the chat PIN: enter it once with python send.py --by-hand")
            messenger.check_account(driver)

    @unittest.skipUnless(CHAT and NAME, "set FB_LIVE_CHAT and FB_LIVE_NAME to check one chat")
    def test_a_chat_opens_with_one_empty_box(self):
        with step(driver, "live_chat", f"opening the chat with {NAME} (nothing is typed)"):
            messenger.open_chat(driver, store.messenger_url(CHAT), NAME)
            box = messenger.composer(driver)
            self.assertIsNotNone(box, "not exactly one message box")
            self.assertEqual(messenger.squash(messenger.sdm(driver, "read", box)), "",
                             "the box already holds a draft; clear it before a run")
            print(f"\n    Send buttons found: {len(messenger.js(driver, 'send'))} (0 means Enter is used)")

    @unittest.skipUnless(CHAT and NAME, "set FB_LIVE_CHAT and FB_LIVE_NAME to read one chat's messages")
    def test_a_chat_tells_their_messages_from_ours(self):
        with step(driver, "live_rows", f"reading who wrote each message with {NAME} (nothing is typed)"):
            messenger.open_chat(driver, store.messenger_url(CHAT), NAME)
            rows = messenger.thread(driver, NAME)
            self.assertTrue(rows, "no [role=row] messages found: update rows in messenger._JS")
            print()
            for r in rows[-15:]:
                print(f"    {r['who'] or '?':<4} label={r['label'][:20]!r:<24} side={r['side'] or '-':<4} {r['text'][:60]!r}")
            self.assertTrue(any(r["who"] == "me" for r in rows), "none of the messages read as ours")

    @unittest.skipUnless(POST and POST_AUTHOR and POST_TEXT, "set FB_LIVE_POST, FB_LIVE_POST_AUTHOR and "
                                                             "FB_LIVE_POST_TEXT to check one post's comment box")
    def test_a_post_shows_exactly_its_own_comment_box(self):
        with step(driver, "live_post", f"finding {POST_AUTHOR}'s post and its comment box (nothing is typed)"):
            box = comment.open_post(driver, POST, POST_AUTHOR, POST_TEXT[:comment.SNIPPET])
            self.assertEqual(messenger.squash(messenger.sdm(driver, "read", box)), "", "the box already holds a draft")
            print(f"\n    comment box: aria-label={box.get_attribute('aria-label')!r}")


if __name__ == "__main__":
    unittest.main()
