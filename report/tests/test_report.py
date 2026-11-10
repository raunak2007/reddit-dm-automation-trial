"""test_report.py - the email digest and the Google Sheet sync, offline: temp copies of every data file (patched
outcomes.FILES / ANSWERS / BOOKINGS), a fake smtplib.SMTP and a fake Sheets HTTP session. Nothing is sent and
no real sheet is touched.

    venv/bin/python -m unittest discover report/tests
"""
from __future__ import annotations

import csv
import io
import json
import re
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock
from urllib.parse import unquote

REPORT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPORT.parent))
sys.path.insert(0, str(REPORT))

import outcomes  # noqa: E402
import digest  # noqa: E402
import sheet  # noqa: E402
import importlib.util  # noqa: E402

spec = importlib.util.spec_from_file_location("report_run", REPORT / "run.py")
run = importlib.util.module_from_spec(spec)
spec.loader.exec_module(run)

TODAY = date.today().isoformat()
MONDAY, TUESDAY = "2026-11-09", "2026-11-10"


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
        d = self.dir = Path(tmp.name)
        files = {p: {k: d / p / Path(v).name for k, v in f.items()} for p, f in outcomes.FILES.items()}
        self.out = io.StringIO()
        for p in (mock.patch.object(outcomes, "FILES", files), mock.patch.object(outcomes, "ANSWERS", d / "answers.json"),
                  mock.patch.object(outcomes, "BOOKINGS", d / "bookings.csv"),
                  mock.patch.object(digest, "LOGS", d / "logs"), mock.patch.object(digest, "REPORTS", d / "reports"),
                  mock.patch.object(run, "CONFIG", d / "config.json"), mock.patch.object(run, "ENV", d / ".env"),
                  mock.patch.dict("os.environ", {"SMTP_PASSWORD": ""}),
                  mock.patch.object(sys, "stdout", self.out)):
            p.start()
            self.addCleanup(p.stop)
        write_csv(files["reddit"]["contacted"], [
            {"username": "alice_1", "status": "sent", "timestamp": f"{TODAY}T09:00:05", "reason": ""},
            {"username": "old_bob", "status": "sent", "timestamp": "2026-10-01T09:00:00", "reason": ""}])
        files["reddit"]["leads"].write_text(json.dumps({"p1": {"author": "alice_1", "subreddit": "ApplyingToCollege",
                                                              "ai": {"score": 82}}}))
        long = ("Yes please! Email me at alice.smith@gmail.com or call +1 (415) 555-0199, I applied on 2026-11-01 "
                + "and would love help with my essays " * 5)
        write_csv(files["reddit"]["replies"], [
            {"message_id": "m1", "username": "alice_1", "received_at": f"{TODAY}T11:00:00", "snippet": long,
             "text": long, "permalink": "https://www.reddit.com/message/messages/abc"},
            {"message_id": "m0", "username": "old_bob", "received_at": "2026-10-02T11:00:00", "snippet": "old",
             "text": "old", "permalink": ""}])
        write_csv(files["facebook"]["contacted"], [
            {"author_key": "1001", "author": "Jane Doe", "post_key": "x", "status": "sent", "timestamp": "2026-10-01T10:00:00", "note": ""}])
        write_csv(files["facebook"]["replies"], [
            {"author_key": "1001", "author": "Jane Doe", "post_key": "x", "seen_at": f"{TODAY}T12:00:00",
             "snippet": "how much does it cost?", "text": "", "chat_url": "javascript:alert(1)"}])
        outcomes.ANSWERS.write_text(json.dumps([{"platform": "reddit", "person": "alice_1", "status": "drafted"}]))
        (d / "logs").mkdir()
        (d / "logs" / "paused.json").write_text(json.dumps({"instagram": {"since": "2026-11-08T10:00:00",
                                                                        "why": "live check exited 1"}}))
        (d / "logs" / f"{TODAY}.log").write_text(
            "\n########## daily.py old run\n===== 10:00:00 reddit: reddit/run.py (in .)\n===== exit 1 after 3s\n"
            "\n########## daily.py 2026-11-09T10:00:00\n"
            "===== 10:00:01 facebook: run.py (in facebook)\nhello\n===== exit 2 after 5s\n"
            "\n===== skipped facebook: run.py replies: facebook stopped earlier today\n"
            "===== 10:01:00 reddit: reddit/run.py replies (in .)\n===== exit None after 600s\n"
            "===== 10:02:00 reddit: reddit/run.py answer (in .)\n===== exit 0 after 1s\n===== summary\nx\n")
        (d / "reports").mkdir()
        (d / "reports" / "2026-11-02.md").write_text("# old\n\nold headline\n")
        (d / "reports" / "2026-11-09.md").write_text(
            "# Outreach insights, 2026-11-09\n\n12 contacted, 3 replied (25.0%), 1 booked.\n\n### By platform\n\n"
            "| group | contacted | replied | reply rate | booked |\n|---|---:|---:|---:|---:|\n"
            "| reddit | 10 | 3 | 30.0% | 1 |\n| facebook | 2 | 0 | 0.0% | 0 |\n\n### By score\n\n| x | 1 | 2 | 3 | 4 |\n")

    def config(self, **over):
        cfg = {"email": {"enabled": True, "to": ["me@example.com"], "from": "bot@example.com", "username": "bot@example.com"},
               "sheet": {"enabled": True, "spreadsheet_id": "SHEET123", "credentials_file": "sa.json"}}
        for k, v in over.items():
            cfg[k].update(v)
        run.CONFIG.write_text(json.dumps(cfg))


class Digest(Fixture):
    def test_scrub_and_names(self):
        s = digest.scrub("hi  me@x.co.uk\nor 415-555-0199, see you 2026-11-01")
        self.assertEqual(s, "hi [email] or [phone], see you 2026-11-01")
        self.assertLessEqual(len(digest.scrub("x" * 500)), 120)
        self.assertEqual(digest.display_name("facebook", "1001", "Jane Doe"), "Jane")
        self.assertEqual(digest.display_name("facebook", "1001", "1001"), "(Facebook user)")
        self.assertEqual(digest.display_name("instagram", "jane.doe"), "@jane.doe")

    def test_gather(self):
        d = digest.gather(TODAY, include_weekly=True)
        self.assertEqual([(r["platform"], r["who"]) for r in d["replies"]], [("reddit", "u/alice_1"), ("facebook", "Jane")])
        alice = d["replies"][0]
        self.assertLessEqual(len(alice["snippet"]), 120)
        self.assertNotIn("alice.smith", alice["snippet"])
        self.assertNotIn("555-0199", alice["snippet"])
        self.assertEqual(alice["link"], "https://www.reddit.com/message/messages/abc")
        self.assertEqual(d["replies"][1]["link"], "", "only https links")
        self.assertEqual(d["waiting"]["to_review"], 1)
        self.assertIn("instagram", d["paused"])
        self.assertEqual(d["problems"], {"stopped": ["facebook: run.py"], "failed": ["reddit: reddit/run.py replies (timed out)"],
                                         "skipped": ["facebook: run.py replies: facebook stopped earlier today"]},
                         "only the last run in the log counts")
        reddit = next(p for p in d["platforms"] if p["platform"] == "reddit")
        self.assertEqual((reddit["today"]["dms"], reddit["totals"]["contacted"], reddit["totals"]["replied"]), (1, 2, 2))
        self.assertEqual(d["weekly"]["headline"], "12 contacted, 3 replied (25.0%), 1 booked.")
        self.assertEqual([p["platform"] for p in d["weekly"]["platforms"]], ["reddit", "facebook"])

    def test_weekly_only_on_mondays(self):
        self.assertIsNotNone(digest.gather(MONDAY)["weekly"])
        self.assertIsNone(digest.gather(TUESDAY)["weekly"])

    def test_html_is_escaped_and_has_no_personal_data(self):
        d = digest.gather(TODAY, include_weekly=True)
        d["replies"][0]["snippet"] = "<script>x</script>"
        msg = digest.message(d, {"to": ["me@example.com"], "from": "bot@example.com"})
        html = msg.get_body(("html",)).get_content()
        text = msg.get_body(("plain",)).get_content()
        self.assertIn("&lt;script&gt;", html)
        self.assertNotIn("<script>", html)
        for body in (html, text):
            self.assertNotIn("Doe", body)
            self.assertIn("instagram is PAUSED", body)
            self.assertIn("This week", body)
        self.assertIn("[needs you]", msg["Subject"])


class FakeSMTP:
    instances = []

    def __init__(self, host, port, timeout=None):
        self.host, self.port, self.calls, self.sent = host, port, [], []
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.calls.append("quit")

    def ehlo(self):
        self.calls.append("ehlo")

    def starttls(self, context=None):
        self.calls.append("starttls")

    def login(self, user, password):
        self.calls.append(("login", user, password))

    def send_message(self, msg):
        self.sent.append(msg)


class Email(Fixture):
    def setUp(self):
        super().setUp()
        FakeSMTP.instances = []
        p = mock.patch("smtplib.SMTP", FakeSMTP)
        p.start()
        self.addCleanup(p.stop)

    def test_dry_run_prints_and_sends_nothing(self):
        self.config()
        self.assertEqual(run.main(["email", "--dry-run"]), 0)
        self.assertEqual(FakeSMTP.instances, [])
        self.assertIn("Subject: Outreach", self.out.getvalue())
        self.assertIn("nothing sent", self.out.getvalue())

    def test_sends_with_starttls_and_the_env_password(self):
        self.config(email={"smtp_host": "smtp.example.com", "smtp_port": 2525})
        run.ENV.write_text("SMTP_PASSWORD='app pass word'\n")
        self.assertEqual(run.main(["email"]), 0)
        (s,) = FakeSMTP.instances
        self.assertEqual((s.host, s.port), ("smtp.example.com", 2525))
        self.assertEqual(s.calls, ["ehlo", "starttls", "ehlo", ("login", "bot@example.com", "app pass word"), "quit"])
        (msg,) = s.sent
        self.assertEqual(msg["To"], "me@example.com")
        self.assertTrue(msg.is_multipart())

    def test_missing_password_or_settings_fail_cleanly(self):
        self.config()
        self.assertEqual(run.main(["email"]), 1)
        self.config(email={"to": []})
        run.ENV.write_text("SMTP_PASSWORD=x\n")
        self.assertEqual(run.main(["email"]), 1)
        self.assertEqual(FakeSMTP.instances, [])

    def test_smtp_error_is_exit_1(self):
        self.config()
        run.ENV.write_text("SMTP_PASSWORD=x\n")
        with mock.patch.object(FakeSMTP, "login", side_effect=OSError("auth failed")):
            self.assertEqual(run.main(["email"]), 1)
        self.assertIn("auth failed", self.out.getvalue())


class FakeResponse:
    def __init__(self, status, body):
        self.status_code, self.body = status, body
        self.content = json.dumps(body).encode()
        self.text = self.content.decode()

    def json(self):
        return self.body


class FakeSheetsHTTP:
    """Just enough of the Sheets v4 REST API: spreadsheet metadata, addSheet, values get / clear / update."""

    def __init__(self, tabs=None, status=200):
        self.tabs, self.calls, self.status = dict(tabs or {}), [], status

    def request(self, method, url, timeout=None, json=None):
        self.calls.append((method, url))
        if self.status != 200:
            return FakeResponse(self.status, {"error": {"message": "The caller does not have permission"}})
        m = re.match(r"https://sheets\.googleapis\.com/v4/spreadsheets/(\w+)(.*)", url)
        assert m and m.group(1) == "SHEET123", url
        rest = m.group(2)
        if method == "GET" and rest.startswith("?fields"):
            return FakeResponse(200, {"sheets": [{"properties": {"title": t}} for t in self.tabs]})
        if method == "POST" and rest == ":batchUpdate":
            for r in json["requests"]:
                self.tabs[r["addSheet"]["properties"]["title"]] = []
            return FakeResponse(200, {})
        tab = unquote(rest.removeprefix("/values/").split("?")[0]).removesuffix(":clear").split("!")[0]
        assert tab in self.tabs, f"no tab {tab}"
        if method == "GET":
            return FakeResponse(200, {"values": self.tabs[tab]} if self.tabs[tab] else {})
        if method == "POST" and rest.endswith(":clear"):
            self.tabs[tab] = []
            return FakeResponse(200, {})
        if method == "PUT":
            assert "valueInputOption=RAW" in url
            self.tabs[tab] = [[("" if v is None else v) for v in row] for row in json["values"]]
            return FakeResponse(200, {})
        raise AssertionError(f"unexpected {method} {url}")


class Sheet(Fixture):
    def test_rows(self):
        people = sheet.people_rows()
        self.assertEqual(len(people[0]), len(sheet.PEOPLE_HEADER))
        by = {(r[0], r[1]): r for r in people}
        self.assertEqual(by[("facebook", "1001")][2], "Jane", "first name only")
        alice = by[("reddit", "alice_1")]
        self.assertEqual((alice[6], alice[9], alice[10], alice[11]), (1, 82, "ApplyingToCollege", "sent"))
        daily = {(r[0], r[1]): r for r in sheet.daily_rows()}
        self.assertEqual(daily[(TODAY, "reddit")][2:7], [1, 0, 0, 1, 0])
        self.assertEqual(daily[(TODAY, "reddit")][7:], [2, 2, 100.0])
        self.assertEqual(daily[("2026-10-01", "reddit")][7:], [1, 0, 0.0], "to-date numbers as of that day")
        self.assertIn((TODAY, "instagram"), daily, "a row for every platform today")

    def test_dry_run_touches_nothing(self):
        self.config()
        with mock.patch.object(sheet, "session", side_effect=AssertionError("no network in a dry run")):
            self.assertEqual(run.main(["sheet", "--dry-run"]), 0)
        self.assertIn("== People", self.out.getvalue())

    def test_sync_creates_tabs_overwrites_people_and_upserts_daily(self):
        self.config()
        http = FakeSheetsHTTP({"Daily": [sheet.DAILY_HEADER, ["2026-01-01", "reddit", 9], [TODAY, "reddit", 999]],
                               "People": [["stale"], ["rows"], ["x"], ["y"], ["z"]]})
        cfg = run.load_config()
        self.assertEqual(run.cmd_sheet(cfg, False, http=http), 0)
        self.assertEqual(http.tabs["People"][0], sheet.PEOPLE_HEADER)
        self.assertEqual(len(http.tabs["People"]), 1 + len(sheet.people_rows()), "overwritten, no stale rows left")
        daily = {(r[0], r[1]): r for r in http.tabs["Daily"][1:]}
        self.assertEqual(daily[("2026-01-01", "reddit")], ["2026-01-01", "reddit", 9], "other days kept")
        self.assertEqual(daily[(TODAY, "reddit")][2], 1, "today's row replaced")
        self.assertEqual(len(http.tabs["Daily"]) - 1, len({*daily}), "one row per date+platform")
        self.assertEqual(run.cmd_sheet(cfg, False, http=http), 0)
        self.assertEqual(len(http.tabs["Daily"]) - 1, len(daily), "a second sync adds nothing")

        empty = FakeSheetsHTTP({"Sheet1": []})
        self.assertEqual(run.cmd_sheet(cfg, False, http=empty), 0)
        self.assertIn("People", empty.tabs)
        self.assertIn("Daily", empty.tabs)

    def test_permission_error_is_exit_1_with_a_hint(self):
        self.config()
        self.assertEqual(run.cmd_sheet(run.load_config(), False, http=FakeSheetsHTTP(status=403)), 1)
        self.assertIn("shared with the service account", self.out.getvalue())

    def test_missing_library_or_key_is_exit_1(self):
        self.config(sheet={"credentials_file": str(self.dir / "missing.json")})
        with mock.patch.dict(sys.modules, {"google": None, "google.oauth2": None, "google.auth": None,
                                           "google.auth.transport.requests": None}):
            self.assertEqual(run.main(["sheet"]), 1)
        self.assertIn("pip install -r report/requirements.txt", self.out.getvalue())

    def test_missing_spreadsheet_id(self):
        self.config(sheet={"spreadsheet_id": ""})
        self.assertEqual(run.main(["sheet"]), 1)


if __name__ == "__main__":
    unittest.main()
