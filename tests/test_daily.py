"""test_daily.py - daily.py running jobs in order, skipping a platform's later jobs after an account problem
(exit 2), carrying on with the others, and notifying. The jobs are tiny Python scripts, not the platforms.

    venv/bin/python -m unittest discover tests
"""
from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import daily  # noqa: E402


class Daily(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.ran = self.dir / "ran.txt"
        self.notes = []
        for p in (mock.patch.object(daily, "HERE", self.dir), mock.patch.object(daily, "CONFIG", self.dir / "s.json"),
                  mock.patch.object(daily, "LOGS", self.dir / "logs"), mock.patch.object(daily, "PAUSED", self.dir / "logs" / "paused.json"),
                  mock.patch.object(daily, "PYTHON", Path(sys.executable)),
                  mock.patch.object(daily, "summary", lambda: "3 DMs, 0 follow-ups, 1 new replies\nreddit: 3 DMs"),
                  mock.patch.object(daily, "notify", lambda title, text: self.notes.append(title)),
                  mock.patch.object(sys, "stdout", io.StringIO())):
            p.start()
            self.addCleanup(p.stop)

    def job(self, name, tag, code=0, enabled=True, **extra):
        script = self.dir / f"{tag}.py"
        script.write_text(f"open({str(self.ran)!r}, 'a').write({tag + chr(10)!r}); print('hello from {tag}'); "
                          f"raise SystemExit({code})")
        return {"name": name, "dir": ".", "run": [script.name], "enabled": enabled, **extra}

    def run_daily(self, jobs, *argv):
        daily.CONFIG.write_text(json.dumps({"timeout_minutes": 1, "jobs": jobs}))
        with mock.patch.object(sys, "argv", ["daily.py", *argv]):
            return daily.main()

    def ran_tags(self):
        return self.ran.read_text().split() if self.ran.exists() else []

    def test_runs_every_enabled_job_in_order_and_logs_output(self):
        code = self.run_daily([self.job("reddit", "r1"), self.job("facebook", "f1"),
                               self.job("facebook", "off", enabled=False)])
        self.assertEqual(code, 0)
        self.assertEqual(self.ran_tags(), ["r1", "f1"])
        log = next(daily.LOGS.glob("*.log")).read_text()
        self.assertIn("hello from r1", log)
        self.assertEqual(self.notes, ["Outreach today"], "only the summary")
        self.assertIn("3 DMs", (daily.LOGS / f"summary-{daily.date.today().isoformat()}.txt").read_text())

    def test_account_problem_skips_that_platform_only_and_notifies(self):
        code = self.run_daily([self.job("facebook", "f1", code=2), self.job("facebook", "f2"),
                               self.job("instagram", "i1")])
        self.assertEqual(code, 2)
        self.assertEqual(self.ran_tags(), ["f1", "i1"])
        self.assertEqual(self.notes, ["Outreach: facebook needs you", "Outreach today"])

    def test_crash_is_notified_and_the_platform_carries_on(self):
        code = self.run_daily([self.job("reddit", "r1", code=1), self.job("reddit", "r2")])
        self.assertEqual(code, 1)
        self.assertEqual(self.ran_tags(), ["r1", "r2"])
        self.assertEqual(self.notes, ["Outreach: reddit failed", "Outreach today"])

    def test_only_and_dry_run(self):
        jobs = [self.job("reddit", "r1"), self.job("instagram", "i1")]
        self.assertEqual(self.run_daily(jobs, "--dry-run"), 0)
        self.assertEqual(self.ran_tags(), [])
        self.run_daily(jobs, "--only", "instagram")
        self.assertEqual(self.ran_tags(), ["i1"])

    def today(self):
        return daily.WEEKDAYS[daily.date.today().weekday()]

    def other_day(self):
        return daily.WEEKDAYS[(daily.date.today().weekday() + 1) % 7]

    def test_weekly_jobs_run_only_on_their_day(self):
        self.run_daily([self.job("insights", "w1", weekly=self.today()), self.job("insights", "w2", weekly=self.other_day())])
        self.assertEqual(self.ran_tags(), ["w1"])

    def test_failed_live_check_pauses_the_platform_until_it_passes(self):
        jobs = [self.job("facebook", "send"), self.job("facebook", "check", code=1, role="livecheck", weekly=self.today()),
                self.job("reddit", "r1")]
        self.assertEqual(self.run_daily(jobs), 2)
        self.assertEqual(self.ran_tags(), ["check", "r1"], "the live check runs first; a failure skips the platform")
        self.assertIn("facebook", daily.paused())
        self.assertIn("Outreach: facebook paused", self.notes)

        self.ran.unlink()                           # the next day: not its weekly day, but paused -> re-checked
        jobs[1].update(weekly=self.other_day())
        self.run_daily(jobs)
        self.assertEqual(self.ran_tags(), ["check", "r1"], "still failing: still paused")

        self.ran.unlink()
        jobs[1] = self.job("facebook", "check", code=0, role="livecheck", weekly=self.other_day())
        self.assertEqual(self.run_daily(jobs), 0)
        self.assertEqual(self.ran_tags(), ["check", "send", "r1"], "passes again: resumed the same day")
        self.assertNotIn("facebook", daily.paused())
        self.assertIn("Outreach: facebook resumed", self.notes)

    def test_resume_by_hand(self):
        daily.set_paused("instagram", "test")
        self.run_daily([], "--resume", "instagram")
        self.assertEqual(daily.paused(), {})

    def test_cleanup_removes_only_old_failure_files(self):
        import os
        old, new = self.dir / "step3_send_failed.png", self.dir / "step4_confirm_failed_dom.txt"
        keep = self.dir / "venv" / "step1_failed.png"
        keep.parent.mkdir()
        for f in (old, new, keep):
            f.write_text("x")
        for f in (old, keep):
            os.utime(f, (0, 0))
        self.assertEqual(daily.cleanup(14), 1)
        self.assertFalse(old.exists())
        self.assertTrue(new.exists() and keep.exists())

    def test_default_schedule_is_written_on_first_run(self):
        self.assertFalse(daily.CONFIG.exists())
        self.assertEqual(daily.load()["jobs"], daily.DEFAULT["jobs"])
        self.assertTrue(daily.CONFIG.exists())


if __name__ == "__main__":
    unittest.main()
