"""Offline tests for inbox/: reading the three replies.csv formats, grouping, drafting once, decline detection, and
the page's server endpoints. Claude is mocked; every data path points at a temp dir.

    venv/bin/python -m unittest discover inbox/tests
"""
from __future__ import annotations

import csv
import io
import json
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

INBOX = Path(__file__).resolve().parents[1]
if str(INBOX) not in sys.path:
    sys.path.insert(0, str(INBOX))

import draft    # noqa: E402
import server   # noqa: E402
import store    # noqa: E402

CFG = json.loads((INBOX / "config.json").read_text())

REDDIT_FIELDS = ["message_id", "username", "kind", "received_at", "subject", "snippet", "permalink", "recorded_at"]
FB_FIELDS = ["author_key", "author", "post_key", "seen_at", "snippet", "chat_url"]
IG_FIELDS = ["username", "item_id", "replied_at", "snippet", "thread_url", "sent_at", "found_at", "how"]


class FakeClient:
    """Stands in for anthropic.Anthropic(): returns record_answer tool calls, and keeps every request."""

    def __init__(self, answer="Happy to help! Here's a thought...", intent="question", dismiss=False):
        self.calls, self.answer, self.intent, self.dismiss = [], answer, intent, dismiss
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kw):
        self.calls.append(kw)
        block = SimpleNamespace(type="tool_use", input={"intent": self.intent, "suggest_dismiss": self.dismiss,
                                                        "reason": "they asked a question", "answer": self.answer})
        return SimpleNamespace(content=[block])


def write_csv(path: Path, fields, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fields)
        w.writeheader()
        w.writerows({k: r.get(k, "") for k in fields} for r in rows)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        data = self.tmp / "inbox"
        paths = {
            "reddit": {"replies": self.tmp / "reddit/replies.csv", "leads": self.tmp / "reddit/leads.json",
                       "contacted": self.tmp / "contacted.csv"},
            "facebook": {"replies": self.tmp / "facebook/replies.csv", "leads": self.tmp / "facebook/leads.json",
                         "contacted": self.tmp / "facebook/contacted.csv"},
            "instagram": {"replies": self.tmp / "instagram/replies.csv", "leads": self.tmp / "instagram/leads.json",
                          "contacted": self.tmp / "instagram/contacted.csv"},
        }
        self.paths = paths
        for d in ("reddit", "facebook", "instagram"):
            (self.tmp / d).mkdir()
        for p in [mock.patch.object(store, "DATA", data), mock.patch.object(store, "ANSWERS", data / "answers.json"),
                  mock.patch.object(store, "LOCK", data / "answers.lock"), mock.patch.object(draft, "PATHS", paths),
                  mock.patch.object(draft, "client", side_effect=AssertionError("real Claude client used"))]:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def run_draft(self, cl, **kw):
        with redirect_stdout(io.StringIO()) as out:
            made = draft.run(cl=cl, cfg=CFG, **kw)
        self.out = out.getvalue()
        return made

    # --- fixtures
    def reddit(self, rows, text=False):
        fields = REDDIT_FIELDS + (["text"] if text else [])
        write_csv(self.paths["reddit"]["replies"], fields, rows)

    def reddit_lead(self, user="Alice_1", message="Hi Alice, I saw your post about essays..."):
        self.paths["reddit"]["leads"].write_text(json.dumps({
            "p1": {"author": user, "created_at": "2026-11-01", "ai": {"message": message, "subject": "Essays"}}}))
        write_csv(self.paths["reddit"]["contacted"], ["username", "status", "timestamp", "reason"],
                  [{"username": user, "status": "sent", "timestamp": "2026-11-01T10:00:00"}])


class ReadingTest(Base):
    def test_reddit_snippet_only(self):
        self.reddit([{"message_id": "t4_a", "username": "Alice_1", "kind": "message",
                      "received_at": "2026-11-02T10:00:00", "snippet": "short version", "permalink": "https://r/x"}])
        [r] = draft.read_replies("reddit")
        self.assertEqual((r["reply_id"], r["person"], r["text"], r["chat_url"]),
                         ("t4_a", "Alice_1", "short version", "https://r/x"))
        self.assertEqual(r["extra"]["kind"], "message")

    def test_reddit_text_wins_and_empty_text_falls_back(self):
        self.reddit([{"message_id": "t4_a", "username": "A", "received_at": "1", "snippet": "short", "text": "the full"},
                     {"message_id": "t4_b", "username": "A", "received_at": "2", "snippet": "only snip", "text": ""}],
                    text=True)
        self.assertEqual([r["text"] for r in draft.read_replies("reddit")], ["the full", "only snip"])

    def test_facebook_with_and_without_text(self):
        row = {"author_key": "100042", "author": "Maria Lopez", "post_key": "p:9", "seen_at": "2026-11-03T09:00:00",
               "snippet": "yes please", "chat_url": "https://www.facebook.com/messages/t/100042"}
        write_csv(self.paths["facebook"]["replies"], FB_FIELDS, [row])
        [r] = draft.read_replies("facebook")
        self.assertEqual(r["reply_id"], "100042:2026-11-03T09:00:00")
        self.assertEqual((r["person"], r["name"], r["text"]), ("100042", "Maria Lopez", "yes please"))
        self.assertEqual(r["extra"]["post_key"], "p:9")
        write_csv(self.paths["facebook"]["replies"], FB_FIELDS + ["text"], [dict(row, text="yes please, how much?")])
        self.assertEqual(draft.read_replies("facebook")[0]["text"], "yes please, how much?")

    def test_instagram_with_and_without_text(self):
        row = {"username": "jamie.k", "item_id": "i1", "replied_at": "2026-11-04T08:00:00", "snippet": "hi!",
               "thread_url": "https://www.instagram.com/direct/t/123/"}
        write_csv(self.paths["instagram"]["replies"], IG_FIELDS, [row])
        [r] = draft.read_replies("instagram")
        self.assertEqual((r["reply_id"], r["person"], r["text"], r["chat_url"]),
                         ("i1", "jamie.k", "hi!", "https://www.instagram.com/direct/t/123/"))
        write_csv(self.paths["instagram"]["replies"], IG_FIELDS + ["text"], [dict(row, text="hi! tell me more")])
        self.assertEqual(draft.read_replies("instagram")[0]["text"], "hi! tell me more")

    def test_instagram_api_mode_reply_by_reaches_the_entry_and_the_page(self):
        row = {"username": "jamie.k", "item_id": "m1", "replied_at": "2026-11-04T08:00:00", "snippet": "how much?",
               "reply_by": "2026-11-05T08:00:00"}
        write_csv(self.paths["instagram"]["replies"], IG_FIELDS + ["text", "reply_by"], [row])
        self.assertEqual(draft.read_replies("instagram")[0]["extra"], {"reply_by": "2026-11-05T08:00:00"})
        [e] = self.run_draft(FakeClient())
        self.assertEqual(e["reply_by"], "2026-11-05T08:00:00")
        page = (Path(draft.__file__).parent / "page.html").read_text()
        self.assertIn("answer by ", page)
        self.assertIn("left < 3", page, "red under 3 hours left")

    def test_instagram_browser_rows_carry_no_deadline(self):
        write_csv(self.paths["instagram"]["replies"], IG_FIELDS,
                  [{"username": "jamie.k", "item_id": "i1", "replied_at": "2026-11-04T08:00:00", "snippet": "hi"}])
        [e] = self.run_draft(FakeClient())
        self.assertNotIn("reply_by", e)

    def test_missing_files_are_empty(self):
        self.assertEqual(draft.pending(), [])

    def test_original_dms(self):
        self.reddit_lead()
        self.assertTrue(draft.original("reddit", "alice_1")["message"].startswith("Hi Alice"))
        self.assertTrue(draft.original("reddit", "alice_1")["sent"])
        self.paths["facebook"]["leads"].write_text(json.dumps({"p:9": {
            "author": "Maria Lopez", "author_key": "100042", "ai": {"dm": "draft dm"},
            "approved": {"dm": "the approved dm", "at": "x"}}}))
        self.assertEqual(draft.original("facebook", "100042", {"extra": {"post_key": "p:9"}})["message"],
                         "the approved dm")
        self.assertEqual(draft.original("facebook", "100042")["message"], "the approved dm")
        self.paths["instagram"]["leads"].write_text(json.dumps({"sc1:jamie.k": {
            "username": "jamie.k", "full_name": "Jamie K", "ai": {"dm": "draft"}, "approved": {"dm": "ig approved"}}}))
        write_csv(self.paths["instagram"]["contacted"], ["username", "lead_key", "status", "timestamp", "note"],
                  [{"username": "jamie.k", "lead_key": "sc1:jamie.k", "status": "sent", "timestamp": "t"}])
        o = draft.original("instagram", "jamie.k")
        self.assertEqual((o["message"], o["name"], o["sent"]), ("ig approved", "Jamie K", True))
        self.assertEqual(draft.original("instagram", "nobody")["message"], "")


class DraftTest(Base):
    def test_groups_one_person_into_one_entry_keyed_on_newest(self):
        self.reddit_lead()
        self.reddit([{"message_id": "t4_2", "username": "Alice_1", "received_at": "2026-11-02T10:05:00",
                      "snippet": "Also, when are deadlines?"},
                     {"message_id": "t4_1", "username": "Alice_1", "received_at": "2026-11-02T10:00:00",
                      "snippet": "Thanks! What should my essay be about?"},
                     {"message_id": "t4_9", "username": "Bob", "received_at": "2026-11-02T11:00:00",
                      "snippet": "Interested, tell me more"}])
        cl = FakeClient()
        made = self.run_draft(cl)
        self.assertEqual(len(made), 2)
        self.assertEqual(len(cl.calls), 2)
        alice = store.get("reddit:t4_2")
        self.assertEqual(alice["covers"], ["t4_1", "t4_2"])
        self.assertIn("What should my essay be about?", alice["their_message"])
        self.assertLess(alice["their_message"].index("essay be about"), alice["their_message"].index("deadlines"))
        self.assertTrue(alice["our_message"].startswith("Hi Alice"))
        self.assertEqual((alice["status"], alice["text"], alice["draft"]), ("drafted", cl.answer, cl.answer))
        self.assertIsNone(store.get("reddit:t4_1"))
        # the request: cached system prompt, the forced tool, the model from config
        kw = cl.calls[0]
        self.assertEqual(kw["system"][0]["cache_control"], {"type": "ephemeral"})
        self.assertIn(CFG["business"]["booking_link"], kw["system"][0]["text"])
        self.assertEqual(kw["model"], CFG["ai"]["model"])
        self.assertEqual(kw["tool_choice"], {"type": "tool", "name": "record_answer"})

    def test_never_drafts_a_reply_twice(self):
        self.reddit([{"message_id": "t4_1", "username": "A", "received_at": "1", "snippet": "question?"},
                     {"message_id": "t4_2", "username": "A", "received_at": "2", "snippet": "another?"}])
        self.run_draft(FakeClient())
        cl = FakeClient()
        self.assertEqual(self.run_draft(cl), [])
        self.assertEqual(cl.calls, [])
        self.assertEqual(len(store.load()), 1)
        self.assertIsNone(store.add_entry("reddit", "t4_1", person="A", name="A", chat_url="", their_message="",
                                          our_message="", draft="x"))     # a grouped id is covered too

    def test_new_message_replaces_untouched_draft_but_not_edited_or_approved(self):
        self.reddit([{"message_id": "t4_1", "username": "A", "received_at": "1", "snippet": "first?"}])
        self.run_draft(FakeClient())
        self.reddit([{"message_id": "t4_1", "username": "A", "received_at": "1", "snippet": "first?"},
                     {"message_id": "t4_2", "username": "A", "received_at": "2", "snippet": "second?"}])
        self.run_draft(FakeClient())
        self.assertEqual(store.get("reddit:t4_1")["status"], "dismissed")
        new = store.get("reddit:t4_2")
        self.assertEqual(new["covers"], ["t4_1", "t4_2"])
        self.assertIn("first?", new["their_message"])
        # an edited draft stays; the newer message gets its own entry
        store.update("reddit:t4_2", text="my own words")
        self.reddit([{"message_id": "t4_1", "username": "A", "received_at": "1", "snippet": "first?"},
                     {"message_id": "t4_2", "username": "A", "received_at": "2", "snippet": "second?"},
                     {"message_id": "t4_3", "username": "A", "received_at": "3", "snippet": "third?"}])
        self.run_draft(FakeClient())
        self.assertEqual(store.get("reddit:t4_2")["status"], "drafted")
        self.assertEqual(store.get("reddit:t4_3")["covers"], ["t4_3"])

    def test_cap_and_dry_run(self):
        self.reddit([{"message_id": f"t4_{i}", "username": f"u{i}", "received_at": str(i), "snippet": "q?"}
                     for i in range(5)])
        self.assertEqual(self.run_draft(FakeClient(), dry_run=True), [])
        self.assertIn("would draft", self.out)
        self.assertFalse(store.ANSWERS.exists())
        self.assertEqual(len(self.run_draft(FakeClient(), limit=2)), 2)
        self.assertEqual(len(self.run_draft(FakeClient(), limit=10)), 3)

    def test_api_error_skips_and_retries_next_run(self):
        self.reddit([{"message_id": "t4_1", "username": "A", "received_at": "1", "snippet": "q?"}])
        bad = FakeClient()
        bad.messages = SimpleNamespace(create=mock.Mock(side_effect=RuntimeError("overloaded")))
        self.assertEqual(self.run_draft(bad), [])
        self.assertEqual(len(self.run_draft(FakeClient())), 1)


class DeclineTest(Base):
    def test_decline_kind(self):
        for t in ["Stop messaging me", "please don't message me again", "unsubscribe", "this is spam",
                  "Leave me alone.", "do not contact my son"]:
            self.assertEqual(draft.decline_kind(t), "stop", t)
        for t in ["No thanks", "not interested", "Nah I'm good", "We're all set, thank you!",
                  "no thank you, we already have a counselor"]:
            self.assertEqual(draft.decline_kind(t), "declined", t)
        for t in ["Yes please!", "How much does it cost?", "No thanks on the essays but how about scholarships?",
                  "I'm good at math but my essays are bad, can you help", "", "Thanks, what's the next step?"]:
            self.assertIsNone(draft.decline_kind(t), t)

    def test_stop_request_is_not_sent_to_claude_and_not_counted_as_answered(self):
        write_csv(self.paths["instagram"]["replies"], IG_FIELDS,
                  [{"username": "jamie.k", "item_id": "i1", "replied_at": "1", "snippet": "stop messaging me"}])
        cl = FakeClient()
        [e] = self.run_draft(cl)
        self.assertEqual(cl.calls, [])
        self.assertEqual((e["suggest"], e["intent"], e["text"], e["status"]), ("dismiss", "stop", "", "drafted"))
        self.assertFalse(store.answered("instagram", "jamie.k"))

    def test_claude_flagged_decline(self):
        write_csv(self.paths["facebook"]["replies"], FB_FIELDS,
                  [{"author_key": "1", "author": "M", "seen_at": "1",
                    "snippet": "My daughter decided to go to community college first, so maybe another time"}])
        [e] = self.run_draft(FakeClient(answer="No problem, best of luck!", intent="declined", dismiss=True))
        self.assertEqual(e["suggest"], "dismiss")
        self.assertEqual(e["text"], "No problem, best of luck!")
        self.assertFalse(store.answered("facebook", "1"))


class ServerTest(Base):
    def setUp(self):
        super().setUp()
        self.srv = server.make_server(0)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"
        for rid in ("a", "b", "c"):
            store.add_entry("reddit", rid, person=f"user_{rid}", name=f"user_{rid}", chat_url="https://r/x",
                            their_message="question?", our_message="hi", draft=f"draft {rid}", covers=[rid])

    def post(self, path, body, headers=None):
        req = urllib.request.Request(self.base + path, json.dumps(body).encode(),
                                     {"Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_page_and_state(self):
        with urllib.request.urlopen(self.base + "/") as r:
            self.assertIn(b"Reply inbox", r.read())
        with urllib.request.urlopen(self.base + "/api/state") as r:
            s = json.loads(r.read())
        self.assertEqual(len(s["entries"]), 3)
        self.assertIn("run.py answer", s["note"])

    def test_edit_approve_unapprove_dismiss_restore(self):
        self.assertEqual(self.post("/api/edit", {"id": "reddit:a", "text": "edited"})[0], 200)
        self.assertEqual(store.get("reddit:a")["text"], "edited")
        self.assertEqual(store.get("reddit:a")["draft"], "draft a")
        code, j = self.post("/api/approve", {"id": "reddit:a", "text": "final words"})
        self.assertEqual(code, 200)
        a = store.get("reddit:a")
        self.assertEqual((a["status"], a["text"]), ("approved", "final words"))
        self.assertTrue(a["approved_at"])
        self.assertEqual([x["id"] for x in store.approved("reddit")], ["reddit:a"])
        self.assertEqual(self.post("/api/unapprove", {"id": "reddit:a"})[0], 200)
        self.assertEqual(store.get("reddit:a")["status"], "drafted")
        self.assertEqual(self.post("/api/dismiss", {"id": "reddit:b"})[0], 200)
        self.assertEqual(store.get("reddit:b")["status"], "dismissed")
        self.assertEqual(self.post("/api/edit", {"id": "reddit:b", "text": "x"})[0], 409)
        self.assertEqual(self.post("/api/restore", {"id": "reddit:b"})[0], 200)
        self.assertEqual(store.get("reddit:b")["status"], "drafted")

    def test_refuses_to_change_sent_or_sending(self):
        store.update("reddit:a", status="sent")
        store.update("reddit:b", status="sending")
        for path, body in [("/api/edit", {"text": "x"}), ("/api/approve", {"text": "x"}), ("/api/dismiss", {}),
                           ("/api/redraft", {}), ("/api/resolve", {"sent": False}), ("/api/unapprove", {})]:
            for aid in ("reddit:a", "reddit:b"):
                code, j = self.post(path, {"id": aid, **body})
                self.assertEqual(code, 409, (path, aid, j))
        self.assertEqual((store.get("reddit:a")["status"], store.get("reddit:a")["text"]), ("sent", "draft a"))
        self.assertEqual(store.get("reddit:b")["status"], "sending")

    def test_bad_requests(self):
        self.assertEqual(self.post("/api/approve", {"id": "reddit:a", "text": "   "})[0], 400)
        self.assertEqual(self.post("/api/edit", {"id": "reddit:nope", "text": "x"})[0], 404)
        self.assertEqual(self.post("/api/edit", {"id": "reddit:a", "text": 5})[0], 400)
        self.assertEqual(self.post("/api/nothing", {"id": "reddit:a"})[0], 404)
        self.assertEqual(self.post("/api/dismiss", {"id": "reddit:a"}, {"Origin": "http://evil.example"})[0], 403)
        self.assertEqual(store.get("reddit:a")["status"], "drafted")

    def test_uncertain_mark_sent_or_not_sent(self):
        store.update("reddit:a", status="uncertain", reason="couldn't confirm")
        store.update("reddit:b", status="uncertain", reason="couldn't confirm")
        self.assertEqual(self.post("/api/dismiss", {"id": "reddit:a"})[0], 409)
        self.assertEqual(self.post("/api/resolve", {"id": "reddit:a", "sent": True})[0], 200)
        self.assertEqual(store.get("reddit:a")["status"], "sent")
        self.assertEqual(self.post("/api/resolve", {"id": "reddit:b", "sent": False})[0], 200)
        self.assertEqual(store.get("reddit:b")["status"], "approved")
        self.assertEqual(self.post("/api/resolve", {"id": "reddit:c", "sent": True})[0], 409)   # drafted

    def test_redraft_calls_claude_again(self):
        store.update("reddit:a", status="approved", text="old approved")
        cl = FakeClient(answer="a fresh answer")
        with mock.patch.object(draft, "client", return_value=cl):
            code, j = self.post("/api/redraft", {"id": "reddit:a", "hint": "shorter"})
        self.assertEqual(code, 200, j)
        a = store.get("reddit:a")
        self.assertEqual((a["status"], a["text"], a["draft"]), ("drafted", "a fresh answer", "a fresh answer"))
        self.assertIn("shorter", cl.calls[0]["messages"][0]["content"])


if __name__ == "__main__":
    unittest.main()
