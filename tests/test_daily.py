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
                  mock.patch.object(daily, "LOGS", self.dir / "logs"), mock.patch.object(daily, "PYTHON", Path(sys.executable)),
                  mock.patch.object(daily, "notify", lambda title, text: self.notes.append(title)),
                  mock.patch.object(sys, "stdout", io.StringIO())):
            p.start()
            self.addCleanup(p.stop)

    def job(self, name, tag, code=0, enabled=True):
        script = self.dir / f"{tag}.py"
        script.write_text(f"open({str(self.ran)!r}, 'a').write({tag + chr(10)!r}); print('hello from {tag}'); "
                          f"raise SystemExit({code})")
        return {"name": name, "dir": ".", "run": [script.name], "enabled": enabled}

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
        self.assertEqual(self.notes, [])

    def test_account_problem_skips_that_platform_only_and_notifies(self):
        code = self.run_daily([self.job("facebook", "f1", code=2), self.job("facebook", "f2"),
                               self.job("instagram", "i1")])
        self.assertEqual(code, 2)
        self.assertEqual(self.ran_tags(), ["f1", "i1"])
        self.assertEqual(self.notes, ["Outreach: facebook needs you"])

    def test_crash_is_notified_and_the_platform_carries_on(self):
        code = self.run_daily([self.job("reddit", "r1", code=1), self.job("reddit", "r2")])
        self.assertEqual(code, 1)
        self.assertEqual(self.ran_tags(), ["r1", "r2"])
        self.assertEqual(self.notes, ["Outreach: reddit failed"])

    def test_only_and_dry_run(self):
        jobs = [self.job("reddit", "r1"), self.job("instagram", "i1")]
        self.assertEqual(self.run_daily(jobs, "--dry-run"), 0)
        self.assertEqual(self.ran_tags(), [])
        self.run_daily(jobs, "--only", "instagram")
        self.assertEqual(self.ran_tags(), ["i1"])

    def test_default_schedule_is_written_on_first_run(self):
        self.assertFalse(daily.CONFIG.exists())
        self.assertEqual(daily.load()["jobs"], daily.DEFAULT["jobs"])
        self.assertTrue(daily.CONFIG.exists())


if __name__ == "__main__":
    unittest.main()
