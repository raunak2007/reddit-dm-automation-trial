"""test_apimode.py - API mode (config.json "mode": "api", graph.py + apimode.py) against a fake Graph API: graph.http
is replaced, so nothing reaches the network. Covers the token (refresh, warnings, never printed), the professional
account check, discover + comments, private replies (already sent, the 7-day window, rate limits, the daily cap,
no follows), replies with reply_by, answers in the 24-hour window, follow-ups refused, livecheck, and browser mode
left as it was.

    cd instagram/tests && ../../venv/bin/python -m unittest test_apimode
"""
from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
import unittest
from datetime import datetime, timedelta
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from helpers import StoreTest, config, lead

import answer
import apimode
import browser
import direct
import discover
import followup
import graph
import livecheck
import replies
import run
import send
import store

TOKEN = "IGAAsecret-token-value-123"
ME = {"id": "9000", "user_id": "17840000000000001", "username": "biz.test", "account_type": "BUSINESS"}
NOW = datetime.now().replace(microsecond=0)


def gtime(dt: datetime) -> str:
    """A Graph API timestamp ("2026-11-08T10:00:00+0000") for a local time."""
    return dt.astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")


def body(obj, status=200):
    return status, json.dumps(obj)


def error(code, subcode=None, status=400, message="refused", transient=False):
    err = {"message": message, "type": "OAuthException", "code": code, "is_transient": transient}
    if subcode:
        err["error_subcode"] = subcode
    return status, json.dumps({"error": err})


class FakeGraph:
    """graph.http's stand-in. GETs answer from me / media / comments / conversations (each settable to an
    (status, body) error); POSTs to /{id}/messages go to on_post(body) -> (status, text), default a success.
    calls: every (method, url, params, body, headers)."""

    def __init__(self):
        self.calls, self.me, self.media, self.comments, self.convs = [], dict(ME), [], {}, []
        self.media_next = None
        self.refresh = body({"access_token": "IGAArefreshed-456", "token_type": "bearer", "expires_in": 5184000})
        self.errors, self.on_post = {}, (lambda b: body({"recipient_id": "x", "message_id": "mid.1"}))

    def __call__(self, method, url, *, params=None, body=None, headers=None, timeout=None):
        self.calls.append((method, url, params, body, headers))
        assert url.startswith("https://graph.instagram.com/"), url
        parts = urlsplit(url)
        path = parts.path.lstrip("/")
        if path == "refresh_access_token":
            return self.refresh
        assert (headers or {}).get("Authorization", "").startswith("Bearer "), "token in the header"
        path = path.split("/", 1)[1]                       # drop the version
        query = {**{k: v[0] for k, v in parse_qs(parts.query).items()}, **(params or {})}
        for prefix, answer in self.errors.items():
            if path.startswith(prefix) and method == ("POST" if prefix.endswith("messages") else "GET"):
                return answer
        if method == "POST":
            assert path.endswith("/messages"), path
            return self.on_post(body)
        if path == "me":
            if "account_type" in query.get("fields", "") and self.me.get("account_type") == "UNKNOWN_FIELD":
                return error(100, message="Tried accessing nonexisting field (account_type)")
            return globals()["body"]({k: v for k, v in self.me.items() if v != "UNKNOWN_FIELD"})
        if path == "me/media":
            if query.get("after") == "c1":
                return globals()["body"]({"data": self.media_next or []})
            page = {"data": self.media}
            if self.media_next:
                page["paging"] = {"next": "https://graph.instagram.com/v23.0/me/media?after=c1"}
            return globals()["body"](page)
        if path.endswith("/comments"):
            return globals()["body"]({"data": self.comments.get(path.split("/")[0], [])})
        if path == "me/conversations":
            return globals()["body"]({"data": self.convs})
        return error(100, status=404, message=f"unknown path {path}")

    def posts(self):
        return [c for c in self.calls if c[0] == "POST"]


def media(mid, days_ago, code=None, comments=3, reel=False):
    code = code or f"CODE{mid}"
    return {"id": mid, "caption": "x", "permalink": f"https://www.instagram.com/{'reel' if reel else 'p'}/{code}/",
            "media_type": "VIDEO" if reel else "IMAGE", "media_product_type": "REELS" if reel else "FEED",
            "timestamp": gtime(NOW - timedelta(days=days_ago)), "comments_count": comments}


def comment(cid, user, uid, hours_ago, text="how do essays work?"):
    return {"id": cid, "text": text, "timestamp": gtime(NOW - timedelta(hours=hours_ago)),
            "from": {"id": uid, "username": user}}


def msg(mid, who, hours_ago, text="hi"):
    return {"id": mid, "created_time": gtime(NOW - timedelta(hours=hours_ago)), "from": who, "message": text}


US = {"id": ME["user_id"], "username": ME["username"]}


def conv(cid, user, igsid, *messages):
    return {"id": cid, "updated_time": gtime(NOW), "messages": {"data": sorted(messages, key=lambda m:
                                                                                m["created_time"], reverse=True)}}


def api_cfg(**outreach):
    c = config(**outreach)
    c["mode"] = "api"
    c["instagram"].update(my_username="biz.test", max_comments=500, include_replies=True)
    c["discover"] = {"lookback_days": 7, "max_posts": 6, "accounts": [], "posts": [], "skip_unchanged": True}
    c["replies"] = {"max_pages": 3, "messages_per_chat": 20}
    return c


class ApiTest(StoreTest):
    def setUp(self):
        super().setUp()
        self.fake = FakeGraph()
        self.patch(graph, "http", self.fake)
        self.patch(graph, "env_token", lambda: TOKEN)
        self.patch(browser, "make_driver", side_effect=AssertionError("API mode opened Chrome"))
        self.patch(direct, "follow", side_effect=AssertionError("API mode followed someone"))
        self.patch(subprocess, "run", side_effect=AssertionError("ran a subprocess"))
        self.patch(graph.time, "sleep", lambda s: None)       # the read retries' backoff

    def tearDown(self):
        self.assertNotIn(TOKEN, self.out.getvalue(), "the token was printed")
        for call in self.fake.calls:
            if not call[1].endswith("refresh_access_token"):
                self.assertNotIn(TOKEN, call[1] + json.dumps(call[2] or {}), "the token went in a URL")


class Token(ApiTest):
    def test_a_new_env_token_is_stored_with_its_date_and_used_in_the_header(self):
        acc = apimode.connect(api_cfg())
        self.assertEqual((acc.ig_id, acc.username), (ME["user_id"], "biz.test"))
        saved = json.loads(graph.token_file().read_text())
        self.assertEqual(saved["token"], TOKEN)
        self.assertEqual(datetime.fromisoformat(saved["obtained_at"]).date(), datetime.now().date())
        self.assertEqual(oct(graph.token_file().stat().st_mode & 0o777), "0o600")
        self.assertEqual(self.fake.calls[-1][4]["Authorization"], f"Bearer {TOKEN}")
        self.assertFalse([c for c in self.fake.calls if "refresh" in c[1]], "a new token isn't refreshed")

    def age(self, days, expires_in_days=60):
        graph.current()
        state = json.loads(graph.token_file().read_text())
        state.update(obtained_at=(NOW - timedelta(days=days)).isoformat(),
                     expires_at=(NOW - timedelta(days=days) + timedelta(days=expires_in_days)).isoformat())
        graph.token_file().write_text(json.dumps(state))

    def test_refreshed_once_older_than_30_days_and_the_new_one_kept(self):
        self.age(31)
        apimode.connect(api_cfg())
        [refresh] = [c for c in self.fake.calls if c[1].endswith("refresh_access_token")]
        self.assertEqual(refresh[2]["grant_type"], "ig_refresh_token")
        saved = json.loads(graph.token_file().read_text())
        self.assertEqual(saved["token"], "IGAArefreshed-456")
        self.assertGreater(datetime.fromisoformat(saved["expires_at"]), NOW + timedelta(days=59))
        self.assertEqual(self.fake.calls[-1][4]["Authorization"], "Bearer IGAArefreshed-456")
        self.assertIn("refreshed the Instagram API token", self.out.getvalue())
        self.assertNotIn("IGAArefreshed-456", self.out.getvalue())
        apimode.connect(api_cfg())                         # the .env token is the same one: the refreshed one stays
        self.assertEqual(json.loads(graph.token_file().read_text())["token"], "IGAArefreshed-456")

    def test_not_refreshed_before_30_days(self):
        self.age(10)
        apimode.connect(api_cfg())
        self.assertFalse([c for c in self.fake.calls if "refresh" in c[1]])

    def test_a_failed_refresh_warns_and_the_token_still_works(self):
        self.age(31)
        self.fake.refresh = error(190, message="cannot refresh")
        apimode.connect(api_cfg())
        self.assertIn("couldn't refresh", self.out.getvalue())
        self.assertEqual(json.loads(graph.token_file().read_text())["token"], TOKEN)

    def test_warns_before_it_expires_and_stops_once_expired(self):
        self.age(50)
        self.fake.refresh = error(190)
        apimode.connect(api_cfg())
        self.assertIn("expires on", self.out.getvalue())
        self.age(61)
        with self.assertRaises(browser.Halt) as stop:
            apimode.connect(api_cfg())
        self.assertIn("expired", str(stop.exception))

    def test_a_new_token_in_env_replaces_the_stored_one(self):
        self.age(31)
        apimode.connect(api_cfg())
        self.patch(graph, "env_token", lambda: "IGAAbrand-new-789")
        apimode.connect(api_cfg())
        self.assertEqual(json.loads(graph.token_file().read_text())["token"], "IGAAbrand-new-789")

    def test_no_token_stops_with_2(self):
        self.patch(graph, "env_token", lambda: "")
        with self.assertRaises(browser.Halt) as stop:
            apimode.connect(api_cfg())
        self.assertIn("IG_ACCESS_TOKEN", str(stop.exception))


class Account(ApiTest):
    def test_a_personal_account_is_refused_clearly(self):
        self.fake.me["account_type"] = "PERSONAL"
        with self.assertRaises(browser.Halt) as stop:
            apimode.connect(api_cfg())
        self.assertEqual(stop.exception.code, 2)
        self.assertIn("isn't a professional (Business or Creator)", str(stop.exception))

    def test_creator_accounts_are_professional(self):
        self.fake.me["account_type"] = "MEDIA_CREATOR"
        self.assertEqual(apimode.connect(api_cfg()).username, "biz.test")

    def test_account_type_not_given_is_refused(self):
        self.fake.me["account_type"] = "UNKNOWN_FIELD"
        with self.assertRaises(browser.Halt) as stop:
            apimode.connect(api_cfg())
        self.assertIn("not given", str(stop.exception))

    def test_wrong_account(self):
        c = api_cfg()
        c["instagram"]["my_username"] = "ameer.husss"
        with self.assertRaises(browser.Halt) as stop:
            apimode.connect(c)
        self.assertIn("@biz.test", str(stop.exception))
        c = api_cfg()
        c["api"] = {"ig_user_id": "123"}
        with self.assertRaises(browser.Halt):
            apimode.connect(c)

    def test_a_dead_token_stops_with_2(self):
        self.fake.errors["me"] = error(190, message="Error validating access token")
        with self.assertRaises(browser.Halt) as stop:
            apimode.connect(api_cfg())
        self.assertIn("doesn't work", str(stop.exception))

    def test_mode_must_be_browser_or_api(self):
        c = api_cfg()
        c["mode"] = "graph"
        with self.assertRaises(SystemExit):
            apimode.is_api(c)


class Discover(ApiTest):
    def setUp(self):
        super().setUp()
        self.fake.media = [media("m1", 1, "POST1"), media("m2", 3, "REEL2", reel=True), media("m3", 20, "OLD3")]
        self.fake.comments = {
            "m1": [comment("c3", "jane.doe", "u7", 2, "and scholarships?"), comment("c2", "jane.doe", "u7", 30),
                   comment("c1", "biz.test", ME["user_id"], 5, "thanks all"),
                   comment("c0", "old.one", "u8", 24 * 8, "too old to answer")],
            "m2": [comment("c9", "sam.p", "u9", 10, "dm me")]}

    def test_lookback_is_clamped_to_7_days_and_said(self):
        c = api_cfg()
        c["discover"]["lookback_days"] = 30
        apimode.discover(c)
        self.assertIn("API mode reads 7 days at most", self.out.getvalue())
        self.assertNotIn("m3", [u for _, u, *_ in self.fake.calls if "/comments" in u and "m3" in u])

    def test_own_media_and_comments_become_leads_with_comment_ids(self):
        totals = apimode.discover(api_cfg())
        self.assertEqual((totals["read"], totals["new"]), (2, 2))
        leads = store.load_leads()
        jane = leads["POST1:jane.doe"]
        self.assertEqual((jane["comment_id"], jane["user_id"]), ("c3", "u7"), "their newest comment")
        self.assertEqual(jane["text"], "how do essays work?\nand scholarships?")
        self.assertEqual(jane["comment_ids"], ["c3", "c2"])
        self.assertTrue(jane["commented_at"])
        self.assertEqual(jane["post"], "https://www.instagram.com/p/POST1/")
        for key in ("key", "username", "full_name", "text", "shortcode", "post_owner", "found_at"):
            self.assertIn(key, jane)
        self.assertNotIn("POST1:biz.test", leads, "our own comment")
        self.assertNotIn("POST1:old.one", leads, "past the 7-day private-reply window")
        self.assertEqual(leads["REEL2:sam.p"]["post"], "https://www.instagram.com/reel/REEL2/")
        posts = store.load_posts()
        self.assertEqual(set(posts), {"POST1", "REEL2", "OLD3"})
        self.assertIsNone(posts["OLD3"]["last_read"])

    def test_pages_media_until_older_than_the_lookback(self):
        self.fake.media = [media("m1", 1, "POST1")]
        self.fake.media_next = [media("m4", 2, "NEXT4")]
        apimode.discover(api_cfg())
        self.assertIn("NEXT4", store.load_posts())

    def test_rate_limit_or_dead_token_stops_with_2(self):
        for answer in (error(4, message="Application request limit reached"), error(190), (429, "{}")):
            with self.subTest(answer=answer):
                self.fake.errors = {"me/media": answer}
                with self.assertRaises(browser.Halt) as stop:
                    apimode.discover(api_cfg())
                self.assertEqual(stop.exception.code, 2)

    def test_run_py_runs_the_api_pipeline(self):
        self.patch(run, "CONFIG", self.tmp / "config.json")
        (self.tmp / "config.json").write_text(json.dumps(api_cfg()))
        sent = self.patch(send, "run", return_value=0)
        self.patch(run.qualify, "run", return_value=0)
        with mock.patch.object(sys, "argv", ["run.py"]):
            self.assertEqual(run.cli(), 0)
        self.assertIn("POST1:jane.doe", store.load_leads())
        sent.assert_called_once()

    def test_a_link_reads_one_of_the_accounts_own_posts(self):
        from comments import run as read_one
        self.assertEqual(read_one(api_cfg(), "https://www.instagram.com/p/POST1/"), 1)
        with self.assertRaises(SystemExit):
            read_one(api_cfg(), "https://www.instagram.com/p/NOTOURS/")


def api_lead(n, user, comment_id, hours_ago=5, minor=False):
    l = lead(n, user)
    l.update(comment_id=comment_id, commented_at=(NOW - timedelta(hours=hours_ago)).isoformat(timespec="seconds"),
             user_id=f"u{n}")
    l["ai"]["likely_minor"] = minor
    return l


class PrivateReplies(ApiTest):
    def test_sends_a_private_reply_to_the_comment_after_a_submitting_row(self):
        a, = self.approve(api_lead(1, "jane.doe", "c11"))

        def on_post(b):
            self.assertEqual(self.statuses(a)[-1], "submitting", "recorded before the call")
            return body({"recipient_id": "u1", "message_id": "mid.1"})
        self.fake.on_post = on_post
        self.assertEqual(send.run(api_cfg()), 0)
        [post] = self.fake.posts()
        self.assertTrue(post[1].endswith(f"/v23.0/{ME['user_id']}/messages"))
        self.assertEqual(post[3], {"recipient": {"comment_id": "c11"}, "message": {"text": a["approved"]["dm"]}})
        self.assertEqual(self.statuses(a), ["approved", "submitting", "sent"])
        send.run(api_cfg())
        self.assertEqual(len(self.fake.posts()), 1, "one reply per comment, one DM per person, ever")

    def test_already_sent_is_recorded_sent(self):
        a, = self.approve(api_lead(1, "jane.doe", "c11"))
        self.fake.on_post = lambda b: error(10, 2534025, message="A private reply has already been sent")
        send.run(api_cfg())
        self.assertEqual(self.statuses(a)[-1], "sent")
        self.assertIn("already sent", store.history()["jane.doe"][-1]["note"])

    def test_the_window_closing_is_expired_and_final(self):
        a, b = self.approve(api_lead(1, "jane.doe", "c11"), api_lead(2, "old.one", "c22", hours_ago=24 * 7 + 1))
        self.fake.on_post = lambda body_: error(10, 2534022, message="This message is sent outside of allowed window")
        self.assertEqual(send.run(api_cfg()), 0)
        self.assertEqual((self.statuses(a)[-1], self.statuses(b)[-1]), ("expired", "expired"))
        self.assertEqual(len(self.fake.posts()), 1, "a comment already 7 days old isn't even tried")
        self.assertIn("7-day", store.skip_reason("jane.doe"))
        self.assertEqual(send.approved_leads(), [], "never tried again")

    def test_a_rate_limit_stops_with_2(self):
        a, b = self.approve(api_lead(1, "jane.doe", "c11"), api_lead(2, "sam.p", "c22"))
        self.fake.on_post = lambda b_: error(4, message="Application request limit reached")
        self.assertEqual(send.run(api_cfg()), 2)
        self.assertEqual(len(self.fake.posts()), 1, "nothing after the rate limit")
        self.assertEqual(self.statuses(a)[-1], "rate_limited")
        self.assertEqual(self.statuses(b), ["approved"])

    def test_no_answer_or_a_5xx_is_uncertain_and_held(self):
        a, = self.approve(api_lead(1, "jane.doe", "c11"))
        self.fake.on_post = lambda b: (502, "Bad Gateway")
        send.run(api_cfg())
        self.assertEqual(self.statuses(a)[-1], "uncertain")
        self.assertIn("jane.doe", store.held())
        send.run(api_cfg())
        self.assertEqual(len(self.fake.posts()), 1, "a held reply isn't sent again")

    def test_a_held_reply_found_in_its_conversation_is_settled_sent(self):
        a, = self.approve(api_lead(1, "jane.doe", "c11"))
        store.record("jane.doe", a["key"], "submitting", "")
        self.fake.convs = [conv("t1", "jane.doe", "s1", msg("m1", US, 3, a["approved"]["dm"]),
                                msg("m2", {"id": "s1", "username": "jane.doe"}, 1, "ok"))]
        send.run(api_cfg())
        self.assertEqual(self.statuses(a)[-1], "sent")
        self.assertEqual(self.fake.posts(), [])

    def test_daily_cap_and_minors_apply_and_nobody_is_followed(self):
        leads = self.approve(*(api_lead(n, f"p{n}", f"c{n}") for n in range(1, 4)))
        minor, = self.approve(api_lead(9, "kid", "c9", minor=True))
        self.assertEqual(send.run(api_cfg(daily_dm_limit=2, auto_follow=True)), 0)
        self.assertEqual(len(self.fake.posts()), 2)
        self.assertNotIn("sent", self.statuses(minor))
        self.assertIn("daily cap", self.out.getvalue())

    def test_delays_apply(self):
        self.approve(api_lead(1, "a", "c1"), api_lead(2, "b", "c2"))
        with mock.patch.object(send.time, "sleep") as sleep:
            send.run(api_cfg(min_delay_seconds=45, max_delay_seconds=45))
        sleep.assert_called_once_with(45)

    def test_a_lead_without_a_comment_id_isnt_sent(self):
        l = lead(1, "jane.doe")
        self.approve(l)
        send.run(api_cfg())
        self.assertEqual(self.fake.posts(), [])

    def test_by_hand_is_browser_only(self):
        self.approve(api_lead(1, "jane.doe", "c11"))
        with self.assertRaises(SystemExit):
            send.run(api_cfg(), by_hand=True)


class Replies(ApiTest):
    def setUp(self):
        super().setUp()
        sent_at = (NOW - timedelta(hours=10)).isoformat(timespec="seconds")
        store.DATA.mkdir(exist_ok=True)
        with open(store.CONTACTED, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(store.FIELDS)
            for user in ("jane.doe", "sam.p"):
                w.writerow([user, f"P:{user}", "sent", sent_at, ""])
        jane = {"id": "s7", "username": "jane.doe"}
        self.fake.convs = [
            conv("t1", "jane.doe", "s7", msg("m0", jane, 30, "an old message before our reply"),
                 msg("m1", US, 10, "hey jane!"), msg("m2", jane, 5, "how much?\nand when?"), msg("m3", jane, 2, "hi?")),
            conv("t2", "sam.p", "s8", msg("m9", US, 10, "hey sam!"))]

    def test_their_messages_after_our_reply_with_reply_by(self):
        got = replies.run(api_cfg())
        self.assertEqual((got["replied"], got["new"]), (1, 2))
        self.assertEqual(got["missing"], 1, "a conversation holding only our message doesn't say who it is with")
        rows = store.load_replies()
        self.assertEqual([r["item_id"] for r in rows], ["m2", "m3"])
        first = rows[0]
        self.assertEqual((first["text"], first["thread_id"], first["user_id"], first["how"]),
                         ("how much?\nand when?", "t1", "s7", "graph"))
        latest = NOW - timedelta(hours=2)
        self.assertEqual(first["reply_by"], (latest + timedelta(hours=24)).isoformat(timespec="seconds"),
                         "their latest message + 24h")
        self.assertEqual(replies.run(api_cfg())["new"], 0, "deduped by message id")

    def test_a_dead_token_while_reading_stops_with_2(self):
        self.fake.errors["me/conversations"] = error(190)
        with self.assertRaises(browser.Halt):
            replies.run(api_cfg())


class Answers(ApiTest):
    def entry(self, person, status="approved"):
        e = self.answers.add_draft("instagram", f"m-{person}", person=person, name=person, chat_url="",
                                   their_message="how much?", our_message="hey", draft=f"answer for {person}")
        return self.answers.update(e["id"], status=status)

    def status(self, e):
        return self.answers.get(e["id"])

    def chat(self, user, igsid, hours_ago):
        return conv(f"t-{user}", user, igsid, msg(f"o-{user}", US, hours_ago + 1, "hey"),
                    msg(f"x-{user}", {"id": igsid, "username": user}, hours_ago, "how much?"))

    def test_inside_the_window_sends_to_their_igsid(self):
        e = self.entry("jane.doe")
        self.fake.convs = [self.chat("jane.doe", "s7", 3)]

        def on_post(b):
            self.assertEqual(self.status(e)["status"], "sending", "marked before the call")
            return body({"recipient_id": "s7", "message_id": "mid.2"})
        self.fake.on_post = on_post
        self.assertEqual(answer.run(api_cfg()), 0)
        [post] = self.fake.posts()
        self.assertEqual(post[3], {"recipient": {"id": "s7"}, "message": {"text": "answer for jane.doe"}})
        self.assertEqual(self.status(e)["status"], "sent")

    def test_outside_the_window_fails_with_when_it_closed(self):
        e = self.entry("jane.doe")
        self.fake.convs = [self.chat("jane.doe", "s7", 30)]
        answer.run(api_cfg())
        closed = NOW - timedelta(hours=30) + timedelta(hours=24)
        self.assertEqual(self.status(e)["status"], "failed")
        self.assertEqual(self.status(e)["reason"], f"the 24-hour window closed on {closed:%Y-%m-%d %H:%M}")
        self.assertEqual(self.fake.posts(), [])

    def test_conversations_silent_go_by_replies_csv(self):
        e = self.entry("jane.doe")
        self.fake.errors["me/conversations"] = (500, "oops")
        store.add_replies([{"username": "jane.doe", "item_id": "m-jane.doe", "user_id": "s7",
                            "reply_by": (NOW + timedelta(hours=5)).isoformat()}])
        answer.run(api_cfg())
        self.assertEqual(self.status(e)["status"], "sent")
        self.assertEqual(self.fake.posts()[0][3]["recipient"], {"id": "s7"})

    def test_unknown_igsid_fails(self):
        e = self.entry("jane.doe")
        answer.run(api_cfg())
        self.assertIn("Instagram id isn't known", self.status(e)["reason"])

    def test_rate_limit_fails_it_and_stops_with_2(self):
        a, b = self.entry("jane.doe"), self.entry("sam.p")
        self.fake.convs = [self.chat("jane.doe", "s7", 1), self.chat("sam.p", "s8", 1)]
        self.fake.on_post = lambda b_: error(613, message="Calls to this api have exceeded the rate limit")
        self.assertEqual(answer.run(api_cfg()), 2)
        self.assertEqual((self.status(a)["status"], self.status(b)["status"]), ("failed", "approved"))

    def test_no_answer_is_uncertain_and_stuck_ones_turn_uncertain_first(self):
        stuck = self.entry("old.one", status="sending")
        e = self.entry("jane.doe")
        self.fake.convs = [self.chat("jane.doe", "s7", 1)]
        self.fake.on_post = lambda b: (503, "")
        answer.run(api_cfg())
        self.assertEqual((self.status(stuck)["status"], self.status(e)["status"]), ("uncertain", "uncertain"))


class FollowupsAndLivecheck(ApiTest):
    def test_followups_are_refused_with_exit_0(self):
        c = api_cfg()
        c["outreach"]["followup"] = {"enabled": True}
        self.assertEqual(followup.run(c), 0)
        self.assertIn("Follow-ups aren't possible in API mode", self.out.getvalue())
        self.assertEqual(self.fake.calls, [])

    def test_livecheck_passes_without_a_browser(self):
        self.fake.media = [media("m1", 1)]
        self.assertEqual(livecheck.run(api_cfg()), 0)
        paths = [urlsplit(c[1]).path for c in self.fake.calls]
        self.assertEqual(paths, ["/v23.0/me", "/v23.0/me/media", "/v23.0/m1/comments", "/v23.0/me/conversations"])
        self.assertEqual(self.fake.posts(), [], "reads only")

    def test_livecheck_account_problems_are_2(self):
        for setup in (lambda: self.fake.me.update(account_type="PERSONAL"),
                      lambda: self.fake.errors.update({"me": error(190)}),
                      lambda: self.fake.errors.update({"me/conversations": error(10, message="needs permission")})):
            with self.subTest():
                self.fake.me, self.fake.errors = dict(ME), {}
                setup()
                self.assertEqual(livecheck.run(api_cfg()), 2)

    def test_livecheck_other_failures_are_1(self):
        self.fake.media = [media("m1", 1)]
        self.fake.errors = {"m1/comments": (500, "oops")}
        self.assertEqual(livecheck.run(api_cfg()), 1)

        def unreachable(*a, **k):
            raise graph.Unreachable("couldn't reach graph.instagram.com (URLError)")
        self.patch(graph, "http", unreachable)
        self.assertEqual(livecheck.run(api_cfg()), 1)


class BrowserModeUntouched(ApiTest):
    def test_browser_mode_never_touches_the_api(self):
        self.patch(graph, "http", side_effect=AssertionError("browser mode called the Graph API"))
        self.patch(apimode, "connect", side_effect=AssertionError("browser mode connected to the API"))
        for c in (config(), {**config(), "mode": "browser"}):
            self.assertFalse(apimode.is_api(c))
            self.assertEqual(send.run(c), 0)              # nothing approved: no browser either
        self.assertEqual(discover.settings(config())["lookback_days"], 7)

    def test_browser_mode_followups_still_follow_their_own_rules(self):
        c = config()
        self.assertEqual(followup.run(c), 0)
        self.assertIn("off", self.out.getvalue())


if __name__ == "__main__":
    unittest.main()
