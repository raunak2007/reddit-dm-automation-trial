"""test_backup.py - backup.py copying the send records, spotting a record that shrank, vanished or broke, never
making a damaged copy the baseline, restoring, and pruning old backups. All in a temp folder.

    venv/bin/python -m unittest discover tests
"""
from __future__ import annotations

import io
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import backup  # noqa: E402

HEADER = "author_key,author,post_key,status,timestamp,note\n"


class Backups(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        for p in (mock.patch.object(backup, "HERE", self.root), mock.patch.object(backup, "BACKUPS", self.root / "backups"),
                  mock.patch.object(sys, "stdout", io.StringIO())):
            p.start()
            self.addCleanup(p.stop)
        self.fb = self.root / "facebook" / "data" / "contacted.csv"
        self.write(self.fb, 3)
        (self.root / "contacted.csv").write_text("username,status,timestamp,reason\nbob,sent,2026-01-01T00:00:00,\n")

    def write(self, path, n):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(HEADER + "".join(f"{i},n,p,sent,2026-01-01T00:00:00,\n" for i in range(n)))

    def yesterday(self):
        return (date.today() - timedelta(days=1)).isoformat()

    def test_copies_every_record_keeping_paths(self):
        dest, copied, _ = backup.backup()
        self.assertEqual(copied, 2)
        self.assertEqual((dest / "facebook/data/contacted.csv").read_text(), self.fb.read_text())
        self.assertEqual(backup.check(), [])

    def test_growth_is_fine_shrinking_and_vanishing_are_not(self):
        backup.backup(self.yesterday())
        self.write(self.fb, 5)
        self.assertEqual(backup.check(), [])
        self.write(self.fb, 1)
        (problem,) = backup.check()
        self.assertEqual((problem["path"], problem["platform"]), ("facebook/data/contacted.csv", "facebook"))
        self.assertIn("shrank from 3 to 1", problem["problem"])
        (self.root / "contacted.csv").unlink()
        self.assertEqual({p["platform"] for p in backup.check()}, {"facebook", "reddit"})

    def test_damaged_record_never_becomes_the_baseline(self):
        backup.backup(self.yesterday())
        self.write(self.fb, 1)
        problems = backup.check()
        backup.backup(skip={p["path"] for p in problems})          # what daily.py does
        self.assertTrue(backup.check(), "still flagged the next time: the good copy is the baseline")

    def test_backing_up_by_hand_accepts_a_change_you_meant(self):
        backup.backup(self.yesterday())
        self.write(self.fb, 1)
        backup.backup()
        self.assertEqual(backup.check(), [])

    def test_unparseable_json_is_flagged_and_not_copied(self):
        answers = self.root / "inbox" / "data" / "answers.json"
        answers.parent.mkdir(parents=True)
        answers.write_text('[{"id": "a"}]')
        backup.backup(self.yesterday())
        answers.write_text('[{"id": "a"')
        self.assertIn("doesn't parse", backup.check()[0]["problem"])
        dest, _, _ = backup.backup()
        self.assertFalse((dest / "inbox/data/answers.json").exists())

    def test_restore_keeps_the_current_file(self):
        backup.backup(self.yesterday())
        self.write(self.fb, 1)
        self.assertEqual(backup.restore(self.yesterday(), ["facebook/data/contacted.csv"]), ["facebook/data/contacted.csv"])
        self.assertEqual(backup.rows(self.fb), 3)
        self.assertEqual(len(list(self.fb.parent.glob("contacted.csv.before-restore-*"))), 1)

    def test_old_backups_are_pruned(self):
        old = (date.today() - timedelta(days=backup.KEEP_DAYS + 1)).isoformat()
        backup.backup(old)
        backup.backup(self.yesterday())
        _, _, pruned = backup.backup()
        self.assertEqual(pruned, 1)
        self.assertEqual([d.name for d in backup.days()], [self.yesterday(), date.today().isoformat()])


if __name__ == "__main__":
    unittest.main()
