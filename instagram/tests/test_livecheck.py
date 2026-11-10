"""test_livecheck.py - `python run.py livecheck` (livecheck.py) without running the live checks: the environment it
gives tests/test_live.py (settings from config.json, IG_LIVE_FOLLOW never passed on, read-only), how it turns the
output into an exit code, and that every script's --help only prints help (Chrome is never opened).

    cd instagram/tests && ../../venv/bin/python -m unittest test_livecheck
"""
from __future__ import annotations

import importlib
import os
import re
import runpy
import subprocess
import sys
import unittest
from unittest import mock

import helpers
from helpers import StoreTest, config

import browser
import livecheck
import run
import store

PASSED = """test_feed_trips_no_block_pattern (test_live.LiveInstagram.test_feed_trips_no_block_pattern) ... ok
test_inbox_is_usable (test_live.LiveInstagram.test_inbox_is_usable) ... ok
----------------------------------------------------------------------
Ran 7 tests in 31.2s

OK (skipped=2)
"""
CHALLENGE = """test_inbox_is_usable (test_live.LiveInstagram.test_inbox_is_usable) ... ERROR
======================================================================
ERROR: test_inbox_is_usable (test_live.LiveInstagram.test_inbox_is_usable)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/x/instagram/tests/test_live.py", line 80, in test_inbox_is_usable
    direct.check_account(driver)
send_dm.AccountBlocked: Instagram sent this profile to https://www.instagram.com/challenge/AXH3k/

During handling of the above exception, another exception occurred:

send_dm.StepFailed: live_inbox: AccountBlocked: Instagram sent this profile to https://www.instagram.com/challenge/AXH3k/
----------------------------------------------------------------------
Ran 7 tests in 12.0s

FAILED (errors=1)
"""
WRONG_ACCOUNT = """======================================================================
FAIL: test_logged_in_as_the_sender (test_live.LiveInstagram.test_logged_in_as_the_sender)
----------------------------------------------------------------------
AssertionError: '999' != '100' : logged in as a different account than outreach.sender_user_id
FAILED (failures=1)
"""
LOGGED_OUT = """setUpClass (test_live.LiveInstagram) ... skipped 'not logged in: Not logged in to Instagram in this profile. Run: python run.py login'
----------------------------------------------------------------------
Ran 0 tests in 3.1s

OK (skipped=1)
"""
MARKUP = """ERROR: test_profile_header_has_exactly_one_follow_button (test_live.LiveInstagram)
----------------------------------------------------------------------
Traceback (most recent call last):
    direct.wait_for(driver, lambda d: direct.check_account(d) or direct.js(d, "following")
selenium.common.exceptions.TimeoutException: Message: timed out after 20s waiting for @x's Follow / Following button
send_dm.StepFailed: live_profile: TimeoutException: timed out after 20s waiting for @x's Follow / Following button
FAILED (errors=1)
"""


class Verdict(unittest.TestCase):
    def test_exit_codes(self):
        self.assertEqual(livecheck.verdict(0, PASSED), 0)
        self.assertEqual(livecheck.verdict(1, CHALLENGE), 2)
        self.assertEqual(livecheck.verdict(1, WRONG_ACCOUNT), 2)
        self.assertEqual(livecheck.verdict(0, LOGGED_OUT), 2, "a skip for not being logged in is an account problem")
        self.assertEqual(livecheck.verdict(1, MARKUP), 1, "the markup changed: not the account")
        self.assertEqual(livecheck.verdict(1, "Traceback ...\nselenium.common.exceptions.WebDriverException: "
                                              "Message: chrome not reachable\n"), 1)
        self.assertEqual(livecheck.verdict(1, "send_dm.StepFailed: live_feed: AccountBlocked: Instagram is "
                                              "refusing this account: 'Try Again Later'"), 2)


class Environment(unittest.TestCase):
    def test_settings_come_from_config_and_follow_is_never_passed_on(self):
        cfg = config()
        cfg["livecheck"] = {"profile": "@Some.Profile", "chat": "a.friend"}
        env = livecheck.environment(cfg, {"PATH": "/bin", "IG_LIVE_FOLLOW": "1", "IG_LIVE_PROFILE": "shell.one",
                                          "IG_LIVE_CHAT": "shell.two", "IG_LIVE": "0"})
        self.assertEqual({k: v for k, v in env.items() if k.startswith("IG_LIVE")},
                         {"IG_LIVE": "1", "IG_LIVE_READONLY": "1", "IG_LIVE_PROFILE": "some.profile",
                          "IG_LIVE_CHAT": "a.friend"})
        self.assertEqual(env["PATH"], "/bin")

    def test_no_profile_or_chat_set_means_neither_check(self):
        env = livecheck.environment(config(), {"IG_LIVE_PROFILE": "shell.one"})
        self.assertNotIn("IG_LIVE_PROFILE", env)
        self.assertNotIn("IG_LIVE_CHAT", env)

    def test_read_only_turns_off_follow_and_dialog_presses_in_test_live(self):
        import test_live
        try:
            with mock.patch.dict(os.environ, {"IG_LIVE_READONLY": "1", "IG_LIVE_FOLLOW": "1"}):
                importlib.reload(test_live)
                self.assertTrue(test_live.READONLY)
                self.assertFalse(test_live.FOLLOW)
            source = (helpers.FOLDER / "tests" / "test_live.py").read_text()
            guarded = re.findall(r"if not READONLY:\n\s+direct\.dismiss_dialogs\(driver\)", source)
            self.assertEqual(len(guarded), source.count("direct.dismiss_dialogs(driver)"), "every press is guarded")
        finally:
            importlib.reload(test_live)


class Run(StoreTest):
    def fake_run(self, code, output):
        self.calls = []

        def fake(cmd, **kw):
            self.calls.append((cmd, kw))
            return subprocess.CompletedProcess(cmd, code, output, "")
        return self.patch(subprocess, "run", side_effect=fake)

    def test_runs_only_test_live_read_only_and_returns_the_verdict(self):
        cfg = config()
        cfg["livecheck"] = {"chat": "a.friend"}
        for code, output, want in ((0, PASSED, 0), (1, CHALLENGE, 2), (1, MARKUP, 1)):
            with self.subTest(want=want):
                self.fake_run(code, output)
                self.assertEqual(livecheck.run(cfg), want)
                [(cmd, kw)] = self.calls
                self.assertEqual(cmd[-3:], ["-p", "test_live.py", "-v"])
                self.assertEqual(kw["env"]["IG_LIVE"], "1")
                self.assertEqual(kw["env"]["IG_LIVE_READONLY"], "1")
                self.assertEqual(kw["env"]["IG_LIVE_CHAT"], "a.friend")
                self.assertNotIn("IG_LIVE_FOLLOW", kw["env"])

    def test_timeout_is_1(self):
        self.patch(subprocess, "run", side_effect=subprocess.TimeoutExpired("x", 1))
        self.assertEqual(livecheck.run(config()), 1)

    def test_takes_send_pys_lock(self):
        self.fake_run(0, PASSED)
        with store.lock():
            with self.assertRaises(SystemExit):
                livecheck.run(config())
        self.assertEqual(self.calls, [])

    def test_run_py_livecheck(self):
        called = self.patch(livecheck, "run", return_value=2)
        self.patch(run, "CONFIG", self.tmp / "config.json")
        (self.tmp / "config.json").write_text("{}")
        with mock.patch.object(sys, "argv", ["run.py", "livecheck"]):
            self.assertEqual(run.cli(), 2)
        called.assert_called_once()


class HelpOnlyPrintsHelp(StoreTest):
    """--help on every script (and run.py <command> --help) prints the help and exits 0 before Chrome opens."""

    def setUp(self):
        super().setUp()
        self.patch(browser, "make_driver", side_effect=AssertionError("--help opened Chrome"))
        self.patch(subprocess, "run", side_effect=AssertionError("--help ran the live checks"))

    def test_scripts(self):
        for script in ("answer.py", "followup.py", "livecheck.py", "replies.py", "discover.py", "send.py",
                       "qualify.py", "direct.py", "comments.py"):
            with self.subTest(script):
                self.out.truncate(0)
                with mock.patch.object(sys, "argv", [script, "--help"]):
                    with self.assertRaises(SystemExit) as done:
                        runpy.run_path(str(helpers.FOLDER / script), run_name="__main__")
                self.assertIn(done.exception.code, (0, None))
                self.assertIn("usage:", self.out.getvalue())

    def test_run_py_commands(self):
        for args in (["--help"], ["answer", "--help"], ["followup", "-h"], ["livecheck", "--help"], ["help"]):
            with self.subTest(args):
                self.out.truncate(0)
                with mock.patch.object(sys, "argv", ["run.py", *args]):
                    self.assertEqual(run.cli(), 0)
                self.assertIn("python run.py answer", self.out.getvalue())


if __name__ == "__main__":
    unittest.main()
