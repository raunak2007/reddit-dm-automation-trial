"""test_run.py - insights/run.py's numbers (score buckets, rates, the breakdown tables) from made-up people.

    venv/bin/python -m unittest discover insights/tests
"""
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("insights_run", Path(__file__).resolve().parent.parent / "run.py")
run = importlib.util.module_from_spec(spec)
spec.loader.exec_module(run)


def person(platform, sent=True, replied=False, booked=False, score=None, categories=(), source="", followup=False):
    return {"platform": platform, "person": "x", "first_sent_at": "2026-10-01T10:00" if sent else "",
            "replied_at": "2026-10-02T10:00" if replied else "", "booked_at": "2026-10-03" if booked else "",
            "followup_at": "2026-10-05" if followup else "", "answered_at": "", "held": False, "score": score,
            "categories": list(categories), "source": source, "message": "Hi there"}


class Numbers(unittest.TestCase):
    def test_bucket(self):
        self.assertEqual(run.bucket(None), "unscored")
        self.assertEqual(run.bucket(67), "60-69")
        self.assertEqual(run.bucket(100), "90-99")

    def test_rate_counts_only_people_sent_to(self):
        r = run.rate([person("reddit", replied=True, booked=True), person("reddit"), person("reddit", sent=False, replied=True)])
        self.assertEqual(r, {"n": 2, "replied": 1, "booked": 1, "reply_rate": 50.0, "book_rate": 50.0})
        self.assertEqual(run.rate([])["reply_rate"], 0.0)

    def test_breakdown_sorts_and_drops_small_groups(self):
        ps = ([person("reddit", replied=True, categories=["essays"])] * 2 + [person("reddit", categories=["essays"])] * 2
              + [person("facebook", categories=["scholarships"])] * 3 + [person("instagram", replied=True)])
        rows = run.breakdown(ps, lambda p: p["categories"] or ["(none)"], 2)
        self.assertEqual([label for label, _ in rows], ["essays", "scholarships"])
        self.assertEqual(rows[0][1]["reply_rate"], 50.0)

    def test_numbers_report(self):
        ps = [person("reddit", replied=True, booked=True, score=85, source="ApplyingToCollege"), person("reddit", score=62)]
        text = run.numbers(ps, 1)
        self.assertIn("2 contacted, 1 replied (50.0%), 1 booked", text)
        self.assertIn("| reddit | 2 | 1 | 50.0% | 1 |", text)
        self.assertIn("| 80-89 | 1 | 1 | 100.0% | 1 |", text)
        self.assertIn("reddit: ApplyingToCollege", text)


if __name__ == "__main__":
    unittest.main()
