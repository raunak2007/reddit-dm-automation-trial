"""helpers.py - what the reddit tests share. Nothing here opens a browser or touches reddit.com.

LedgerTest   each test gets an empty folder of its own: this folder's data/ files (store.py) and the root ledger's
             contacted.csv, queue_state/ and lock (ledger.py) all point into it, and the test runs in it, so failed-
             step files land there and the real contacted.csv is never read or written
config()     reddit/config.json's shape with every delay at 0; root_config() the root config.json's
FakeDriver   a stand-in Chrome: {url: text} pages, where text is what the page's body shows (JSON for Reddit's .json
             URLs). Prefix matches let one entry answer every page of a listing; "redirect" sends a URL elsewhere.
post() / listing() / message()   Reddit's JSON shapes for a post, a listing page and an inbox item
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

FOLDER = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(FOLDER))

import store  # noqa: E402
import ledger  # noqa: E402
from send_dm import ME_URL, SENT_URL  # noqa: E402

SENDER = "aristo_counsel"


def config(**outreach):
    cfg = json.loads((FOLDER / "config.json").read_text())
    cfg["search"].update(subreddits=["ApplyingToCollege", "CollegeEssays"], keywords=["essay help"],
                         lookback_days=7, max_pages_per_listing=3, max_requests_per_run=50)
    cfg["outreach"].update(auto_approve=True, minor_policy="skip", subject="About your post")
    cfg["outreach"].update(outreach)
    cfg["browser"].update(min_request_delay_seconds=0, max_request_delay_seconds=0)
    return cfg


def root_config(**kw):
    cfg = {"sender_username": SENDER, "daily_limit_per_account": 10, "min_delay_seconds": 0, "max_delay_seconds": 0,
           "verify_after_send": False, "max_attempts_per_recipient": 3}
    cfg.update(kw)
    return cfg


def ago(**kw) -> float:
    return (datetime.now() - timedelta(**kw)).timestamp()


def post(pid, author="student_a", sub="ApplyingToCollege", created=None, **kw):
    d = {"id": pid, "name": f"t3_{pid}", "author": author, "subreddit": sub, "title": f"Title {pid}",
         "selftext": f"I need help with my essay ({pid})", "permalink": f"/r/{sub}/comments/{pid}/title/",
         "created_utc": ago(hours=2) if created is None else created, "stickied": False, "over_18": False,
         "num_comments": 1, "link_flair_text": "Essays"}
    d.update(kw)
    return {"kind": "t3", "data": d}


def listing(*children, after=None) -> str:
    return json.dumps({"kind": "Listing", "data": {"children": list(children), "after": after}})


def message(mid, author, body="thanks!", created=None, kind="t4", **kw):
    d = {"id": mid, "name": f"{kind}_{mid}", "author": author, "body": body, "subject": "re: About your post",
         "created_utc": ago(minutes=10) if created is None else created, "dest": SENDER}
    if kind == "t1":
        d.update(subject="comment reply", context=f"/r/ApplyingToCollege/comments/abc/x/{mid}/?context=3")
    d.update(kw)
    return {"kind": kind, "data": d}


class FakeDriver:
    def __init__(self, pages=None, logged_in_as=SENDER):
        self.pages = dict(pages or {})
        if logged_in_as is not None:
            self.pages.setdefault(ME_URL, json.dumps({"data": {"name": logged_in_as}}))
        self.pages.setdefault(SENT_URL, listing())
        self.current_url = "about:blank"
        self.visited = []
        self.quit_called = False

    def _find(self, url):
        if url in self.pages:
            return self.pages[url]
        hits = [k for k in self.pages if url.startswith(k)]
        return self.pages[max(hits, key=len)] if hits else None

    def get(self, url):
        self.visited.append(url)
        page = self._find(url)
        self.current_url = page["redirect"] if isinstance(page, dict) and "redirect" in page else url

    def execute_script(self, script, *args):
        page = self._find(self.current_url)
        if page is None:
            page = self._find(self.visited[-1]) if self.visited else None
        if isinstance(page, dict):
            return page.get("text", "")
        return page if page is not None else "<html><body>404 page not found</body></html>"

    def set_window_size(self, *a):
        pass

    def save_screenshot(self, path):
        return True

    def quit(self):
        self.quit_called = True


class LedgerTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        data, state = self.tmp / "data", self.tmp / "queue_state"
        for name, value in (("DATA", data), ("LEADS", data / "leads.json"), ("QUEUE", data / "queue.html"),
                            ("REPLIES", data / "replies.csv")):
            self.patch(store, name, value)
        for name, value in (("LOG", self.tmp / "contacted.csv"), ("STATE", state),
                            ("IMPORTS", state / "checkmark_imports.json"), ("LOCK", state / "send.lock")):
            self.patch(ledger, name, value)
        self.patch(store, "load_root_config", lambda: root_config())
        cwd = os.getcwd()
        os.chdir(self.tmp)
        self.addCleanup(os.chdir, cwd)
        self.out = io.StringIO()
        self.patch(sys, "stdout", self.out)
        self.patch(sys, "stderr", io.StringIO())

    def patch(self, target, name, value=mock.DEFAULT, **kw):
        p = mock.patch.object(target, name, value, **kw) if value is not mock.DEFAULT else \
            mock.patch.object(target, name, **kw)
        thing = p.start()
        self.addCleanup(p.stop)
        return thing

    def mark(self, username, status, when=None, reason="test"):
        ledger.record(username, ledger.Result(status, reason), when)

    def statuses(self, username):
        return [r["status"] for r in ledger._history().get(ledger.key(username), [])]
