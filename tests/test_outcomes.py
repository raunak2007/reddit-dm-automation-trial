"""test_outcomes.py - outcomes.py reading every platform's files into one view, and bookings/run.py matching a
booking to the person it came from. Temp copies of every data file; nothing real is read or written.

    venv/bin/python -m unittest discover tests
"""
from __future__ import annotations

import csv
import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import outcomes  # noqa: E402

sys.path.insert(0, str(ROOT / "bookings"))
import importlib.util  # noqa: E402

spec = importlib.util.spec_from_file_location("bookings_run", ROOT / "bookings" / "run.py")
bookings = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bookings)

TODAY = date.today().isoformat()


def write_csv(path: Path, rows: list[dict]):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, list(rows[0]))
        w.writeheader()
        w.writerows(rows)


class Fixture(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        d = Path(tmp.name)
        files = {p: {k: d / p / Path(v).name for k, v in f.items()} for p, f in outcomes.FILES.items()}
        files["reddit"]["contacted"] = d / "contacted.csv"
        for p in (mock.patch.object(outcomes, "FILES", files), mock.patch.object(outcomes, "ANSWERS", d / "answers.json"),
                  mock.patch.object(outcomes, "BOOKINGS", d / "bookings.csv"),
                  mock.patch.object(bookings, "BOOKINGS", d / "bookings.csv"), mock.patch.object(bookings, "DATA", d)):
            p.start()
            self.addCleanup(p.stop)
        self.files = files
        write_csv(files["reddit"]["contacted"], [
            {"username": "Alice_1", "status": "submitting", "timestamp": f"{TODAY}T09:00:00", "reason": ""},
            {"username": "Alice_1", "status": "sent", "timestamp": f"{TODAY}T09:00:05", "reason": ""},
            {"username": "bob", "status": "uncertain", "timestamp": "2026-01-01T09:00:00", "reason": ""}])
        files["reddit"]["leads"].parent.mkdir(parents=True, exist_ok=True)
        files["reddit"]["leads"].write_text(json.dumps({"p1": {"author": "Alice_1", "subreddit": "ApplyingToCollege",
                                                              "ai": {"score": 82, "categories": ["essays"], "likely_minor": False,
                                                                     "message": "Hi Alice"}}}))
        write_csv(files["reddit"]["replies"], [{"message_id": "m1", "username": "alice_1", "received_at": f"{TODAY}T11:00:00",
                                                "snippet": "thanks!"}])
        write_csv(files["facebook"]["contacted"], [
            {"author_key": "1001", "author": "Jane Doe", "post_key": "x", "status": "sent", "timestamp": "2026-10-01T10:00:00", "note": ""},
            {"author_key": "1001", "author": "Jane Doe", "post_key": "x", "status": "followup_sent", "timestamp": f"{TODAY}T10:00:00", "note": ""},
            {"author_key": "1002", "author": "Sam Lee", "post_key": "y", "status": "commented", "timestamp": f"{TODAY}T10:05:00", "note": ""}])
        write_csv(files["instagram"]["contacted"], [
            {"username": "jane.doe", "lead_key": "k", "status": "sent", "timestamp": "2026-10-02T10:00:00", "note": ""}])
        (outcomes.ANSWERS).write_text(json.dumps([{"platform": "reddit", "person": "alice_1", "status": "drafted"}]))


class Outcomes(Fixture):
    def by(self, platform):
        return {p["person"]: p for p in outcomes.people(platform)}

    def test_reads_sends_replies_and_leads(self):
        alice = self.by("reddit")["alice_1"]
        self.assertTrue(alice["first_sent_at"].endswith("09:00:05"))
        self.assertEqual((alice["replies"], alice["score"], alice["source"]), (1, 82, "ApplyingToCollege"))
        self.assertEqual(alice["message"], "Hi Alice")
        self.assertTrue(self.by("reddit")["bob"]["held"])

    def test_followups_and_comments(self):
        fb = self.by("facebook")
        self.assertTrue(fb["1001"]["followup_at"].startswith(TODAY))
        self.assertTrue(fb["1002"]["commented_at"].startswith(TODAY))
        self.assertFalse(fb["1002"]["first_sent_at"], "a comment isn't a DM")

    def test_today_and_summary(self):
        d = outcomes.on_day(outcomes.people("reddit"), TODAY)
        self.assertEqual((d["dms"], d["replies"], d["held"]), (1, 1, 1))
        text = outcomes.summary(TODAY)
        self.assertTrue(text.startswith("1 DMs, 1 follow-ups, 1 new replies"), text)
        self.assertIn("1 answers to review", text)
        self.assertIn("facebook: 0 DMs, 1 followups, 1 comments", text)

    def test_missing_files_are_empty(self):
        self.files["instagram"]["contacted"].unlink()
        self.assertEqual(outcomes.people("instagram"), [])


class Bookings(Fixture):
    def test_tag_keeps_the_fragment(self):
        self.assertEqual(bookings.tag("https://x.com/?a=1#get-started", "reddit", "u/Alice_1"),
                         "https://x.com/?a=1&utm_source=reddit&utm_medium=dm&utm_content=alice_1#get-started")

    def test_matching(self):
        people = outcomes.everyone()
        self.assertEqual(bookings.match({"utm_source": "reddit", "utm_content": "Alice_1"}, people), ("reddit", "alice_1", "utm"))
        self.assertEqual(bookings.match({"answers": ["my insta is @jane.doe"]}, people), ("instagram", "jane.doe", "handle"))
        self.assertEqual(bookings.match({"name": "Jane Doe"}, people), ("facebook", "1001", "name"))
        self.assertEqual(bookings.match({"name": "Jane"}, people), ("", "", ""), "a first name alone isn't enough")
        self.assertEqual(bookings.match({"name": "Sam Lee"}, people), ("", "", ""), "commented on, never DMed")

    def test_csv_import_once_and_hand_links_kept(self):
        export = Path(self.files["reddit"]["replies"]).parent / "export.csv"
        write_csv(export, [{"Invitee Name": "Jane Doe", "Invitee Email": "j@x.com", "Start Date & Time": "2026-11-10 10:00",
                            "Created At": f"{TODAY} 09:00", "Reddit or Instagram username?": ""}])
        found = bookings.from_csv(export)
        self.assertEqual(bookings.record(found, "export.csv"), (1, 1))
        self.assertEqual(bookings.record(bookings.from_csv(export), "export.csv"), (0, 0), "not recorded twice")
        rows = bookings.load()
        (bid,) = rows
        self.assertEqual((rows[bid]["platform"], rows[bid]["person"], rows[bid]["how"]), ("facebook", "1001", "name"))
        self.assertTrue(outcomes.people("facebook")[0]["booked_at"].startswith(TODAY))


if __name__ == "__main__":
    unittest.main()
