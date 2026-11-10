"""test_livecheck.py - python run.py livecheck: its settings, its exit codes, and that tests/test_live.py, which it
runs against the real site, never types, presses or records anything. Nothing here touches facebook.com: the live
checks themselves are run against FakeChrome (helpers.py).

    cd facebook && ../venv/bin/python -m unittest discover tests -p test_livecheck.py
"""
from __future__ import annotations

import importlib
import io
import json
import os
import re
import unittest
from types import SimpleNamespace
from unittest import mock

from helpers import FOLDER, SENDER, FakeChrome, StoreTest

import browser
import livecheck
import messenger
import store

LIVE_FILE = FOLDER / "tests" / "test_live.py"
OK_OUT = "test_inbox_is_usable ... ok\n\n----------------------------------------------------------------------\n" \
         "Ran 6 tests in 30.1s\n\nOK (skipped=4)\n"


def failed(text):
    return ("test_inbox_is_usable ... FAIL\n\n======================================================================\n"
            f"FAIL: test_inbox_is_usable\n----------------------------------------------------------------------\n"
            f"Traceback (most recent call last):\n{text}\n\nRan 6 tests in 30.1s\n\nFAILED (failures=1, skipped=4)\n")


class Settings(unittest.TestCase):
    def test_config_block_replaces_the_environment(self):
        cfg = {"livecheck": {"chat": "1000123", "name": "Jane Doe", "post": "", "post_author": "", "post_text": ""}}
        with mock.patch.dict(os.environ, {"FB_LIVE_POST": "https://stray", "PATH": os.environ.get("PATH", "")}):
            env = livecheck.environment(cfg)
        self.assertEqual((env["FB_LIVE"], env["FB_LIVE_STRICT"]), ("1", "1"))
        self.assertEqual((env["FB_LIVE_CHAT"], env["FB_LIVE_NAME"]), ("1000123", "Jane Doe"))
        self.assertNotIn("FB_LIVE_POST", env, "a stray shell setting isn't used")
        self.assertIn("PATH", env)
        self.assertNotIn("FB_LIVE_CHAT", livecheck.environment({}))


class Verdict(unittest.TestCase):
    def test_exit_codes(self):
        self.assertEqual(livecheck.verdict(0, OK_OUT)[0], 0)
        self.assertEqual(livecheck.verdict(0, "Ran 6 tests in 0.0s\n\nOK (skipped=6)\n")[0], 1, "nothing checked")
        for account in ("send_dm.StepFailed: live_inbox: AccountBlocked: Facebook is refusing this account: "
                        "'You're Temporarily Blocked'",
                        "RuntimeError: not logged in: Not logged in to Facebook in this profile. Run: python run.py login",
                        "AssertionError: 'Enter your PIN' != '' : Messenger wants the chat PIN: enter it once",
                        "AccountBlocked: Facebook sent this profile to https://www.facebook.com/checkpoint/123",
                        "AssertionError: '200' != '100' : logged in as a different account than outreach.sender_user_id"):
            self.assertEqual(livecheck.verdict(1, failed(account))[0], 2, account)
        self.assertEqual(livecheck.verdict(1, failed("AssertionError: no [role=row] messages found"))[0], 1)
        self.assertEqual(livecheck.verdict(1, failed("TimeoutException: the Messenger chat's message box"))[0], 1)


class Run(StoreTest):
    def test_runs_test_live_under_the_lock_and_returns_its_verdict(self):
        seen = {}

        def runner(cmd, cwd, env, **kw):
            seen.update(cmd=cmd, cwd=cwd, env=env)
            with self.assertRaises(SystemExit, msg="send.py's lock is held while it runs"):
                with store.lock():
                    pass
            return SimpleNamespace(returncode=1, stdout=failed("AccountBlocked: You're Temporarily Blocked"))

        self.assertEqual(livecheck.run({"livecheck": {"chat": "1"}}, runner=runner), 2)
        self.assertEqual(seen["cmd"][-2:], ["tests/test_live.py", "-v"])
        self.assertEqual((seen["cwd"], seen["env"]["FB_LIVE"], seen["env"]["FB_LIVE_CHAT"]), (livecheck.HERE, "1", "1"))
        self.assertEqual(livecheck.run({}, runner=lambda *a, **k: SimpleNamespace(returncode=0, stdout=OK_OUT)), 0)


class TestLiveIsReadOnly(unittest.TestCase):
    """test_live.py is the one test file that runs against the real facebook.com: nothing in it may type, press,
    send, comment or write to the records."""
    FORBIDDEN = (r"send_keys", r"\.click\(", r"\bclick\(", r"type_text", r"type_message", r"type_comment", r"submit",
                 r"send_message", r"post_comment", r"open_for_hand", r"confirm_by_hand", r"\bKeys\b",
                 r"ActionChains", r"store\.record", r"record_reply", r"save_leads", r"execute_cdp_cmd", r"\binput\(",
                 r"\"set\"", r"'set'", r"\binbox\.", r"resolve")

    def test_no_call_that_could_type_press_or_record(self):
        code = "\n".join(line for line in LIVE_FILE.read_text().splitlines()
                         if not line.lstrip().startswith("#"))
        code = re.sub(r'"""[\s\S]*?"""', "", code)          # docstrings describe; they don't run
        for pattern in self.FORBIDDEN:
            self.assertIsNone(re.search(pattern, code), f"test_live.py uses {pattern!r}")

    def test_what_it_calls_only_reads(self):
        code = re.sub(r'"""[\s\S]*?"""', "", LIVE_FILE.read_text())
        calls = set(re.findall(r"\b(messenger|comment|browser|store)\.(\w+)\(", code))
        allowed = {("browser", "make_driver"), ("browser", "ensure_logged_in"), ("browser", "pause"),
                   ("messenger", "check_account"), ("messenger", "js"), ("messenger", "open_chat"),
                   ("messenger", "composer"), ("messenger", "squash"), ("messenger", "sdm"), ("messenger", "thread"),
                   ("store", "messenger_url"), ("comment", "open_post")}
        self.assertEqual(calls - allowed, set(), "a new call in test_live.py: check it only reads, then allow it here")
        js_fns = set(re.findall(r"messenger\.js\(driver, ['\"](\w+)['\"]", code)) | \
            set(re.findall(r"messenger\.sdm\(driver, ['\"](\w+)['\"]", code))
        self.assertEqual(js_fns - {"notice", "send", "read"}, set())


class TestLiveAgainstAFakeSite(StoreTest):
    """Run test_live.py's own checks with FakeChrome standing in for Chrome: they pass, and nothing is typed."""

    def run_live(self, pages, cookies=None, **env):
        driver = FakeChrome(pages, cookies={"c_user": SENDER} if cookies is None else cookies)
        self.patch(browser, "make_driver", lambda cfg: driver)
        self.patch(browser, "pause", lambda *a, **k: None)
        self.patch(messenger, "SETTLE", 0)
        self.patch(driver, "quit", lambda: None, create=True)
        with mock.patch.dict(os.environ, {"FB_LIVE": "1", **env}):
            import test_live
            live = importlib.reload(test_live)
        self.addCleanup(lambda: importlib.reload(live))           # back to FB_LIVE off for the rest of the suite
        self.patch(live, "OUT", self.tmp / "live")
        cfg = {"browser": {"profile": "test", "user_data_dir": "", "min_page_delay_seconds": 0,
                           "max_page_delay_seconds": 0}, "outreach": {"sender_user_id": SENDER}}
        (self.tmp / "config.json").write_text(json.dumps(cfg))
        self.patch(live.helpers, "FOLDER", self.tmp)               # test_live reads FOLDER/config.json
        stream = io.StringIO()
        result = unittest.TextTestRunner(stream=stream, verbosity=2).run(
            unittest.defaultTestLoader.loadTestsFromTestCase(live.LiveFacebook))
        return result, driver, stream.getvalue()

    def test_the_checks_pass_on_a_healthy_fake_site_without_typing(self):
        chat = store.messenger_url("1001")
        pages = {"https://www.facebook.com/": {"text": "News Feed"}, messenger.INBOX: {"text": "Chats"},
                 chat: {"text": "Jane Doe hello", "composer": True,
                        "rows": [{"who": "me", "text": "hello", "label": "You sent", "side": "me"}]}}
        result, driver, out = self.run_live(pages, FB_LIVE_CHAT="1001", FB_LIVE_NAME="Jane Doe")
        self.assertTrue(result.wasSuccessful(), out)
        self.assertEqual(result.testsRun - len(result.skipped), 5, out)
        self.assertEqual(driver.box.typed, [])
        self.assertIn(chat, driver.visited)

    def test_logged_out_fails_under_livecheck_and_is_an_account_problem(self):
        pages = {"https://www.facebook.com/": {"text": "Log in to Facebook. Forgot password?"}}
        result, driver, out = self.run_live(pages, cookies={}, FB_LIVE_STRICT="1")
        self.assertFalse(result.wasSuccessful())
        self.assertEqual(livecheck.verdict(1, out)[0], 2, out)
        result, driver, out = self.run_live(pages, cookies={})
        self.assertTrue(result.wasSuccessful(), "without FB_LIVE_STRICT it's a skip, as before")

    def test_a_block_on_the_inbox_is_an_account_problem(self):
        pages = {"https://www.facebook.com/": {"text": "News Feed"},
                 messenger.INBOX: {"text": "You're Temporarily Blocked from sending messages"}}
        result, driver, out = self.run_live(pages, FB_LIVE_STRICT="1")
        self.assertFalse(result.wasSuccessful())
        self.assertEqual(livecheck.verdict(1, out)[0], 2, out)


if __name__ == "__main__":
    unittest.main()
