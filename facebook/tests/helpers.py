"""helpers.py - what the facebook tests share.

StoreTest   each test gets an empty data/ folder of its own (store.py's paths point into a temp dir) and runs in
            that dir, so failed-step screenshots and DOM listings land there instead of in the repo
config()    config.json's shape with every delay at 0
FakeChrome  a stand-in driver that answers messenger.py's page helpers (notice, composer, has) from a dict of
            pages: {url: {"text": what the page says, "composer": True, "redirect": url}}. It runs the real
            patterns in messenger.py against that text, so a test shows what send.py does when Facebook shows it.
"""
from __future__ import annotations

import io
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

FOLDER = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(FOLDER))

import store  # noqa: E402

SENDER = "100"


def config(**outreach):
    cfg = {"outreach": {"auto_approve": False, "auto_resolve": True, "sender_user_id": SENDER,
                        "daily_dm_limit": 20, "min_delay_seconds": 0, "max_delay_seconds": 0,
                        "max_attempts_per_recipient": 3, "minor_policy": "comment_only"},
           "ai": {"min_score": 60},
           "browser": {"profile": "test", "user_data_dir": "", "min_page_delay_seconds": 0,
                       "max_page_delay_seconds": 0}}
    cfg["outreach"].update(outreach)
    return cfg


def squash(text):
    return re.sub(r"[\s​-‍﻿]+", "", text or "").lower()


class FakeBox:
    """The message box. Nothing should ever be typed into it in these tests; typed records it if it is."""

    def __init__(self):
        self.typed = []

    def send_keys(self, *keys):
        self.typed.extend(keys)

    def click(self):
        pass

    def is_enabled(self):
        return True


class FakeChrome:
    def __init__(self, pages=None, cookies=None):
        self.pages = pages or {}
        self.cookies = cookies or {}
        self.current_url = "about:blank"
        self.visited = []
        self.box = FakeBox()
        self.switch_to = SimpleNamespace(default_content=lambda: None)

    def page(self):
        return self.pages.get(self.current_url, {})

    def get(self, url):
        self.visited.append(url)
        self.current_url = self.pages.get(url, {}).get("redirect", url)

    def execute_script(self, script, fn=None, *args):
        page = self.page()
        text = page.get("text", "")
        if fn is None or fn.startswith("return"):  # browser.logged_in's own snippet
            return text
        if fn == "notice":
            m = re.search(args[0], text, re.I)
            return m.group(0) if m else ""
        if fn == "composer":
            return [self.box] if page.get("composer") else []
        if fn == "has":
            return squash(args[0]) in squash(text)
        if fn in ("send", "buttons"):
            return []
        return ""                                  # center, dump, ...

    def get_cookie(self, name):
        return {"name": name, "value": self.cookies[name]} if name in self.cookies else None

    def get_cookies(self):
        return [{"name": k, "value": v} for k, v in self.cookies.items()]

    def save_screenshot(self, path):
        return True


def lead(n, name=None):
    name = name or f"Person {n}"
    return {"post_key": f"p:{n}", "author": name, "author_key": f"10{n:03d}", "posted_at": "", "text": "",
            "ai": {"score": 95 - n, "relevant": True, "likely_minor": False, "dm": f"Hi {name}, draft",
                   "comment": ""},
            "approved": {"dm": f"Hi {name}, this is message {n}"}}


class StoreTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        data = self.tmp / "data"
        for name, value in (("DATA", data), ("LEADS", data / "leads.json"), ("CONTACTED", data / "contacted.csv"),
                            ("LOCK", data / "send.lock")):
            self.patch(store, name, value)
        cwd = os.getcwd()
        os.chdir(self.tmp)
        self.addCleanup(os.chdir, cwd)
        self.out = io.StringIO()
        self.patch(sys, "stdout", self.out)

    def patch(self, target, name, value=mock.DEFAULT, **kw):
        p = mock.patch.object(target, name, value, **kw) if value is not mock.DEFAULT else \
            mock.patch.object(target, name, **kw)
        thing = p.start()
        self.addCleanup(p.stop)
        return thing

    def approve(self, *leads):
        """Save the leads and approve each, as the queue (or outreach.auto_approve) would."""
        store.save_leads({**store.load_leads(), **{l["post_key"]: l for l in leads}})
        for l in leads:
            store.record(l["author_key"], l["author"], l["post_key"], "approved", l["approved"]["dm"])
        return list(leads)

    def statuses(self, l):
        return [r["status"] for r in store.history().get(l["author_key"], [])]
