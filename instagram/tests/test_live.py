"""test_live.py - checks against the real instagram.com, with the Chrome profile in config.json. Off unless
IG_LIVE=1. Read-only unless you add IG_LIVE_FOLLOW=1: nothing is typed, sent or recorded. It checks that
direct.py still finds what it needs on today's Instagram (logged in as the sender, an inbox with no block or
challenge, exactly one Follow / Following button in a profile's header, one chat's message box) and that none of
its block patterns fire on ordinary pages.

    cd instagram
    IG_LIVE=1 ../venv/bin/python -m unittest tests/test_live.py -v
    IG_LIVE=1 IG_LIVE_PROFILE=<username> ...   also find the Follow button on that profile, without pressing it
    IG_LIVE=1 IG_LIVE_PROFILE=<username> IG_LIVE_FOLLOW=1 ...   and press it (a real follow; use a test account)
    IG_LIVE=1 IG_LIVE_CHAT=<username> ...      also open the chat with them and check its box
    IG_LIVE_READONLY=1                         press nothing at all: no Follow (IG_LIVE_FOLLOW is ignored) and no
                                               "Not Now" on a notifications dialog. python run.py livecheck sets it.

Close send.py / run.py first: Chrome won't open the same profile twice. A failed check saves its screenshot and
page listing (step*_failed.png / _dom.txt) in debug/live/; when the markup changed, copy it into mock_site.py so
test_mock_site.py covers it from then on.
"""
from __future__ import annotations

import json
import os
import unittest

import helpers  # noqa: F401  (puts the instagram folder on sys.path)

import browser
import direct
from send_dm import step

LIVE = os.environ.get("IG_LIVE") == "1"
PROFILE = os.environ.get("IG_LIVE_PROFILE", "").lower().removeprefix("@")
READONLY = os.environ.get("IG_LIVE_READONLY") == "1"
FOLLOW = os.environ.get("IG_LIVE_FOLLOW") == "1" and not READONLY
CHAT = os.environ.get("IG_LIVE_CHAT", "").lower().removeprefix("@")
OUT = helpers.FOLDER / "debug" / "live"
cfg = driver = None


@unittest.skipUnless(LIVE, "set IG_LIVE=1 to check against the real instagram.com")
class LiveInstagram(unittest.TestCase):
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
            raise unittest.SkipTest(f"not logged in: {exc}")

    @classmethod
    def tearDownClass(cls):
        driver.quit()
        os.chdir(cls.cwd)

    def test_logged_in_as_the_sender(self):
        me = (driver.get_cookie("ds_user_id") or {}).get("value")
        self.assertTrue(me, "no ds_user_id cookie: Instagram didn't say who is logged in")
        expected = str(cfg["outreach"].get("sender_user_id") or "")
        if expected:
            self.assertEqual(me, expected, "logged in as a different account than outreach.sender_user_id")

    def test_feed_trips_no_block_pattern(self):
        with step(driver, "live_feed", "reading the home feed with direct.py's patterns"):
            driver.get(f"{direct.IG}/")
            browser.pause(cfg, 0.6)
            direct.check_account(driver)

    def test_inbox_is_usable(self):
        with step(driver, "live_inbox", "opening the Direct inbox"):
            driver.get(direct.INBOX)
            browser.pause(cfg, 0.6)
            if not READONLY:
                direct.dismiss_dialogs(driver)
            direct.check_account(driver)

    @unittest.skipUnless(PROFILE, "set IG_LIVE_PROFILE to check a profile's Follow button")
    def test_profile_header_has_exactly_one_follow_button(self):
        with step(driver, "live_profile", f"finding @{PROFILE}'s Follow button (not pressing it)"):
            driver.get(f"{direct.IG}/{PROFILE}/")
            direct.wait_for(driver, lambda d: direct.check_account(d) or direct.js(d, "following")
                            or direct.js(d, "follow"), f"@{PROFILE}'s Follow / Following button")
            if not READONLY:
                direct.dismiss_dialogs(driver)
            follow, following = direct.js(driver, "follow"), direct.js(driver, "following")
            print(f"\n    @{PROFILE}: {len(follow)} Follow button(s), already following/requested: {following}")
            self.assertTrue(following or len(follow) == 1,
                            "expected exactly one Follow button in the profile header (more means suggested "
                            "accounts' buttons are counted, none means the header markup changed)")

    @unittest.skipUnless(PROFILE and FOLLOW, "set IG_LIVE_PROFILE and IG_LIVE_FOLLOW=1 to really press Follow")
    def test_follow_really_follows(self):
        with step(driver, "live_follow", f"following @{PROFILE} (a real follow)"):
            done = direct.follow(driver, PROFILE)
            print(f"\n    @{PROFILE}: {done}")
            self.assertTrue(direct.js(driver, "following"), "the header doesn't say Following / Requested")

    @unittest.skipUnless(CHAT, "set IG_LIVE_CHAT to check one chat")
    def test_a_chat_opens_with_one_empty_box(self):
        with step(driver, "live_chat", f"opening the chat with @{CHAT} (nothing is typed)"):
            direct.open_chat(driver, direct.chat_link(CHAT), CHAT)
            box = direct.composer(driver)
            self.assertIsNotNone(box, "not exactly one message box")
            self.assertEqual(direct.squash(direct.sdm(driver, "read", box)), "",
                             "the box already holds a draft; clear it before a run")
            print(f"\n    Send buttons found: {len(direct.js(driver, 'send'))} (0 means Enter is used)")


if __name__ == "__main__":
    unittest.main()
