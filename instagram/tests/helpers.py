"""helpers.py - what the instagram tests share.

StoreTest   each test gets an empty data/ folder of its own (store.py's paths point into a temp dir) and runs in
            that dir, so failed-step screenshots and DOM listings land there instead of in the repo. The shared
            inbox record (the repo root's inbox/store.py, as self.answers) points into the temp dir too.
config()    config.json's shape with every delay at 0
FakeChrome  a stand-in driver that answers direct.py's page helpers (notice, composer, has, follow, following)
            from a dict of pages: {url: {"text": what the page says, "composer": True, "redirect": url,
            "follow": True (a Follow button in the profile header), "following": True,
            "on_follow": {what changes on the page once Follow is pressed}}}. It runs the real patterns in
            direct.py against that text, so a test shows what send.py does when Instagram shows it.
FakeAPI     a FakeChrome that also answers comments.api's in-page fetches from a dict of fake JSON answers:
            {"<path prefix after /api/v1/>": dict (200 + that JSON), (status, body), or a function of the full
            URL returning either}. The longest matching prefix wins; anything else is a 404. fetched lists every
            URL asked for; posted lists (URL, form body) for the form POSTs (comments.api's form=), whose answer
            function gets "<URL>?<form body>".
"""
from __future__ import annotations

import io
import json
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
sys.path.insert(1, str(FOLDER.parent))           # the repo root: inbox/store.py, send_dm.py

import store  # noqa: E402
from inbox import store as answers  # noqa: E402

SENDER = "100"
IG = "https://www.instagram.com"


def config(**outreach):
    cfg = {"outreach": {"auto_approve": False, "auto_resolve": True, "auto_follow": True, "sender_user_id": SENDER,
                        "daily_dm_limit": 20, "min_delay_seconds": 0, "max_delay_seconds": 0,
                        "max_attempts_per_recipient": 3, "minor_policy": "skip"},
           "ai": {"min_score": 50},
           "instagram": {"my_username": "me.test"},
           "browser": {"profile": "test", "user_data_dir": "", "min_page_delay_seconds": 0,
                       "max_page_delay_seconds": 0}}
    cfg["outreach"].update(outreach)
    return cfg


def squash(text):
    return re.sub(r"[\s​-‍﻿]+", "", text or "").lower()


class FakeElement:
    """The message box or a Follow button. typed records anything typed into it; clicks counts presses."""

    def __init__(self, on_click=None):
        self.typed, self.clicks, self.on_click = [], 0, on_click

    def send_keys(self, *keys):
        self.typed.extend(keys)

    def click(self):
        self.clicks += 1
        if self.on_click:
            self.on_click()

    def is_enabled(self):
        return True


class FakeChrome:
    def __init__(self, pages=None, cookies=None):
        self.pages = pages or {}
        self.cookies = cookies if cookies is not None else {"sessionid": "x", "ds_user_id": SENDER}
        self.current_url = "about:blank"
        self.visited = []
        self.box = FakeElement()
        self.follow_button = FakeElement(self._followed)
        self.switch_to = SimpleNamespace(default_content=lambda: None)

    def page(self):
        return self.pages.setdefault(self.current_url, {})

    def _followed(self):
        page = self.page()
        page.update(page.get("on_follow", {"following": True}))

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
        if fn == "follow":
            return [self.follow_button] if page.get("follow") and not page.get("following") else []
        if fn == "following":
            return bool(page.get("following"))
        if fn in ("send", "buttons"):
            return []
        return ""                                  # center, dump, ...

    def get_cookie(self, name):
        return {"name": name, "value": self.cookies[name]} if name in self.cookies else None

    def get_cookies(self):
        return [{"name": k, "value": v} for k, v in self.cookies.items()]

    def save_screenshot(self, path):
        return True


class FakeAPI(FakeChrome):
    def __init__(self, answers=None, grid=None, **kw):
        super().__init__(**kw)
        self.answers, self.grid, self.fetched, self.posted = answers or {}, grid or [], [], []
        self.current_url = f"{IG}/"

    def execute_async_script(self, script, url, app_id, form=None):
        self.fetched.append(url)
        if form is not None:
            self.posted.append((url, form))
        path = url.split("/api/v1/", 1)[1]
        keys = sorted((k for k in self.answers if path.startswith(k)), key=len, reverse=True)
        answer = self.answers[keys[0]] if keys else (404, "{}")
        if callable(answer):
            answer = answer(url if form is None else f"{url}?{form}")
        status, body = answer if isinstance(answer, tuple) else (200, json.dumps(answer))
        return {"status": status, "body": body}

    def execute_script(self, script, fn=None, *args):
        if "(p|reel|tv)" in script:                 # discover._GRID: the profile grid's links
            return self.grid
        return super().execute_script(script, fn, *args)

    def quit(self):
        pass


def profile(user):
    return f"{IG}/{user}/"


def lead(n, user=None):
    user = user or f"person.{n}"
    return {"key": f"POST{n}:{user}", "username": user, "full_name": f"Person {n}", "text": "", "post": "POST",
            "ai": {"score": 95 - n, "relevant": True, "likely_minor": False, "dm": f"hey {user}, draft"},
            "approved": {"dm": f"hey {user}! this is message {n}"}}


class StoreTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        data = self.tmp / "data"
        for name, value in (("DATA", data), ("LEADS", data / "leads.json"), ("CONTACTED", data / "contacted.csv"),
                            ("LOCK", data / "send.lock"), ("POSTS", data / "posts.json"),
                            ("REPLIES", data / "replies.csv")):
            self.patch(store, name, value)
        inbox = self.tmp / "inbox"
        for name, value in (("DATA", inbox), ("ANSWERS", inbox / "answers.json"), ("LOCK", inbox / "answers.lock")):
            self.patch(answers, name, value)
        self.answers = answers
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
        """Save the leads and approve each, as the review page (or outreach.auto_approve) would."""
        store.save_leads({**store.load_leads(), **{l["key"]: l for l in leads}})
        for l in leads:
            store.record(l["username"], l["key"], "approved", l["approved"]["dm"])
        return list(leads)

    def statuses(self, l):
        return [r["status"] for r in store.history().get(l["username"], [])]
