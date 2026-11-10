"""test_discover.py - how `python run.py` finds posts on its own (discover.py), with fake answers from Instagram's web
API (helpers.FakeAPI) in place of instagram.com: reading the media feed and the reels (clips/user/), the lookback
and max_posts, remembering which posts were read, the profile-page fallback, and stopping with exit code 2 on an
account-wide problem.

    cd instagram && ../venv/bin/python -m unittest tests/test_discover.py
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta
from unittest import mock

from helpers import FakeAPI, StoreTest, config

import browser
import comments
import direct
import discover
import qualify
import run
import send
import store
from send_dm import AccountBlocked

NOW = datetime(2026, 11, 9, 12, 0, 0)
ME_ID = "4242"


def item(code, days_ago, comments_n=3, *, reel=False, pinned=False, now=NOW, **extra):
    """One media item as feed/user/<id>/ gives it."""
    return {"code": code, "pk": str(abs(hash(code)) % 10**12), "taken_at": int((now - timedelta(days=days_ago)).timestamp()),
            "comment_count": comments_n, "product_type": "clips" if reel else "feed", "user": {"username": "me.test"},
            **({"timeline_pinned_user_ids": [int(ME_ID)]} if pinned else {}), **extra}


def profile_info(user="me.test", uid=ME_ID, edges=()):
    return {"data": {"user": {"id": uid, "username": user, "edge_owner_to_timeline_media": {"edges": list(edges)}}}}


def cfg(**discover_keys):
    c = config()
    c["instagram"].update(max_comments=500, include_replies=True)
    c["discover"] = {"lookback_days": 7, "max_posts": 6, "accounts": [], "posts": [], "skip_unchanged": True,
                     **discover_keys}
    return c


class FeedParsing(unittest.TestCase):
    def test_feed_items_become_posts_and_reels_get_reel_links(self):
        posts = discover.feed_posts({"items": [item("POSTA", 1, 5), item("REELB", 2, reel=True, comments_disabled=True),
                                               {"pk": "1"}]})
        self.assertEqual([p["shortcode"] for p in posts], ["POSTA", "REELB"], "an item without a code is dropped")
        a, b = posts
        self.assertEqual(a["url"], "https://www.instagram.com/p/POSTA/")
        self.assertEqual(b["url"], "https://www.instagram.com/reel/REELB/")
        self.assertEqual(a["comment_count"], 5)
        self.assertEqual(a["owner"], "me.test")
        self.assertEqual(a["taken_at"], (NOW - timedelta(days=1)).isoformat(timespec="seconds"))
        self.assertTrue(b["comments_disabled"])
        self.assertFalse(a["pinned"])

    def test_profile_info_edges_are_the_fallback_list(self):
        node = {"node": {"shortcode": "EDGE1", "taken_at_timestamp": int(NOW.timestamp()),
                         "edge_media_to_comment": {"count": 9}, "product_type": "clips"}}
        [p] = discover.profile_posts(profile_info(edges=[node])["data"]["user"])
        self.assertEqual((p["shortcode"], p["comment_count"], p["url"]),
                         ("EDGE1", 9, "https://www.instagram.com/reel/EDGE1/"))


class FromApi(StoreTest):
    def setUp(self):
        super().setUp()
        self.patch(browser, "pause", lambda *a: None)

    def test_pages_the_feed_until_posts_are_older_than_the_lookback(self):
        pages = {None: {"items": [item("A", 1), item("B", 3)], "more_available": True, "next_max_id": "c1"},
                 "c1": {"items": [item("C", 6), item("D", 9)], "more_available": True, "next_max_id": "c2"},
                 "c2": {"items": [item("E", 12)], "more_available": False}}
        fake = FakeAPI({"users/web_profile_info/": profile_info(),
                        f"feed/user/{ME_ID}/": lambda url: pages[url.split("max_id=")[1] if "max_id=" in url else None]})
        posts = discover.from_api(fake, cfg(), "me.test", NOW - timedelta(days=7))
        self.assertEqual([p["shortcode"] for p in posts], ["A", "B", "C", "D"], "stops after the page that went past")
        self.assertEqual(len([u for u in fake.fetched if "feed/user" in u]), 2)

    def test_an_old_pinned_post_doesnt_stop_the_paging(self):
        pages = {None: {"items": [item("A", 2), item("PIN", 300, pinned=True)], "more_available": True,
                        "next_max_id": "c1"},
                 "c1": {"items": [item("B", 4)], "more_available": False}}
        pages[None]["items"].reverse()                # pinned first, as Instagram shows it
        fake = FakeAPI({"users/web_profile_info/": profile_info(),
                        f"feed/user/{ME_ID}/": lambda url: pages["c1" if "max_id=" in url else None]})
        posts = discover.from_api(fake, cfg(), "me.test", NOW - timedelta(days=7))
        self.assertEqual([p["shortcode"] for p in posts], ["PIN", "A", "B"])

    def test_feed_not_answering_falls_back_to_the_profile_answer(self):
        node = {"node": {"shortcode": "EDGE1", "taken_at_timestamp": int(NOW.timestamp())}}
        fake = FakeAPI({"users/web_profile_info/": profile_info(edges=[node])})
        self.assertEqual([p["shortcode"] for p in discover.from_api(fake, cfg(), "me.test", NOW)], ["EDGE1"])

    def test_no_answer_at_all_is_none(self):
        self.assertIsNone(discover.from_api(FakeAPI(), cfg(), "me.test", NOW))

    def test_api_refusals_are_account_wide(self):
        for answer, kind in (((401, "{}"), "login"), ((200, '{"require_login":true}'), "login"),
                             ((400, '{"message":"checkpoint_required","status":"fail"}'), "restricted"),
                             ((400, '{"message":"challenge_required"}'), "restricted"),
                             ((429, ""), "rate_limited"), ((400, '{"message":"feedback_required"}'), "rate_limited")):
            with self.subTest(answer=answer):
                with self.assertRaises(AccountBlocked) as stop:
                    discover.from_api(FakeAPI({"users/": answer}), cfg(), "me.test", NOW)
                self.assertEqual(stop.exception.kind, kind)

    def test_profile_grid_fallback_reads_post_links(self):
        fake = FakeAPI(grid=[{"kind": "p", "code": "G1"}, {"kind": "reel", "code": "G2"}])
        posts = discover.from_page(fake, cfg(), "me.test")
        self.assertEqual(fake.visited, [f"{direct.IG}/me.test/", f"{direct.IG}/me.test/reels/"],
                         "the profile grid, then its Reels tab")
        self.assertEqual([(p["shortcode"], p["url"], p["taken_at"]) for p in posts],
                         [("G1", "https://www.instagram.com/p/G1/", None), ("G2", "https://www.instagram.com/reel/G2/", None)])

    def test_find_uses_the_page_when_the_api_doesnt_answer_and_adds_watched_links(self):
        fake = FakeAPI(grid=[{"kind": "p", "code": "G1"}])
        found = discover.find(fake, cfg(posts=["https://www.instagram.com/p/WATCH1/", "not a link"]), NOW)
        self.assertEqual([(p["shortcode"], p["source"]) for p in found], [("G1", "@me.test"), ("WATCH1", "watch")])

    def test_find_reads_extra_accounts_once_each(self):
        fake = FakeAPI({"users/web_profile_info/": profile_info(), f"feed/user/{ME_ID}/": {"items": [item("A", 1)]}})
        discover.find(fake, cfg(accounts=["@Me.Test", "other.one"]), NOW)
        asked = [u.split("username=")[1] for u in fake.fetched if "web_profile_info" in u]
        self.assertEqual(asked, ["me.test", "other.one"])


def found(*posts):
    return [dict({"owner": "me.test", "comment_count": None, "comments_disabled": False, "pinned": False,
                  "source": "@me.test", "url": f"https://www.instagram.com/p/{p['shortcode']}/"}, **p) for p in posts]


def at(days_ago):
    return (NOW - timedelta(days=days_ago)).isoformat(timespec="seconds")


class Lookback(unittest.TestCase):
    def codes(self, posts):
        return [p["shortcode"] for p in posts]

    def test_only_posts_within_the_lookback_newest_first(self):
        posts = found({"shortcode": "OLD", "taken_at": at(8)}, {"shortcode": "A", "taken_at": at(2)},
                      {"shortcode": "B", "taken_at": at(0.5)}, {"shortcode": "EDGE", "taken_at": at(6.9)})
        read, skip = discover.select(posts, {}, cfg(), NOW)
        self.assertEqual(self.codes(read), ["B", "A", "EDGE"])
        self.assertEqual([(p["shortcode"], why[:10]) for p, why in skip], [("OLD", "older than")])

    def test_max_posts_per_account_but_not_for_watched_links(self):
        posts = found(*({"shortcode": f"P{i}", "taken_at": at(i * 0.1)} for i in range(5)),
                      {"shortcode": "W1", "taken_at": None, "source": "watch"},
                      {"shortcode": "X1", "taken_at": at(0.2), "source": "@other"})
        read, _ = discover.select(posts, {}, cfg(max_posts=2), NOW)
        self.assertEqual(sorted(self.codes(read)), ["P0", "P1", "W1", "X1"])

    def test_undated_posts_count_from_when_first_seen(self):
        posts = found({"shortcode": "G1", "taken_at": None}, {"shortcode": "G2", "taken_at": None},
                      {"shortcode": "G3", "taken_at": None})
        state = {"G1": {"first_seen": at(10)}, "G2": {"first_seen": at(1)}}
        read, _ = discover.select(posts, state, cfg(), NOW)
        self.assertEqual(self.codes(read), ["G3", "G2"], "G3 is new (seen now), G1 was first seen 10 days ago")

    def test_comments_off_skipped(self):
        read, skip = discover.select(found({"shortcode": "A", "taken_at": at(1), "comments_disabled": True}), {},
                                     cfg(), NOW)
        self.assertEqual(read, [])
        self.assertIn("switched off", skip[0][1])

    def test_unchanged_comment_count_is_skipped_unless_turned_off(self):
        posts = found({"shortcode": "SAME", "taken_at": at(1), "comment_count": 4},
                      {"shortcode": "MORE", "taken_at": at(1), "comment_count": 6},
                      {"shortcode": "NEVER", "taken_at": at(1), "comment_count": 4})
        state = {"SAME": {"last_read": at(0.5), "comment_count": 4}, "MORE": {"last_read": at(0.5), "comment_count": 5},
                 "NEVER": {"last_read": None, "comment_count": None}}
        read, skip = discover.select(posts, state, cfg(), NOW)
        self.assertEqual(sorted(self.codes(read)), ["MORE", "NEVER"])
        self.assertIn("no new comments", skip[0][1])
        read, _ = discover.select(posts, state, cfg(skip_unchanged=False), NOW)
        self.assertEqual(len(read), 3)

    def test_a_post_found_twice_is_read_once(self):
        posts = found({"shortcode": "A", "taken_at": at(1)}, {"shortcode": "A", "taken_at": None, "source": "watch"})
        read, _ = discover.select(posts, {}, cfg(), NOW)
        self.assertEqual([(p["shortcode"], p["source"]) for p in read], [("A", "@me.test")])


class RunAndRemember(StoreTest):
    """discover.run end to end with a fake browser: what it reads, and what data/posts.json remembers."""

    def setUp(self):
        super().setUp()
        self.patch(browser, "pause", lambda *a: None)
        self.patch(browser, "ensure_logged_in", lambda *a, **k: None)
        self.items = [item("NEW1", 0.5, 2, now=datetime.now()), item("NEW2", 2, 0, now=datetime.now()),
                      item("OLD1", 20, 7, now=datetime.now())]
        self.fake = FakeAPI({"users/web_profile_info/": profile_info(),
                             f"feed/user/{ME_ID}/": lambda url: {"items": self.items, "more_available": False}})
        self.patch(browser, "make_driver", lambda c: self.fake)
        self.reads = []

        def read_post(driver, c, url):
            self.reads.append(comments.shortcode(url))
            return {"shortcode": self.reads[-1], "owner": "me.test", "comments": 2, "commenters": 2, "new": 1,
                    "how": "fake"}
        self.read_post = self.patch(comments, "read_post", side_effect=read_post)

    def test_reads_recent_posts_and_remembers_them(self):
        totals = discover.run(cfg())
        self.assertEqual(self.reads, ["NEW1", "NEW2"])
        self.assertEqual((totals["read"], totals["new"]), (2, 2))
        posts = store.load_posts()
        self.assertEqual(set(posts), {"NEW1", "NEW2", "OLD1"}, "every post found is remembered")
        self.assertEqual((posts["NEW1"]["reads"], posts["NEW1"]["comment_count"]), (1, 2))
        self.assertTrue(posts["NEW1"]["last_read"])
        self.assertIsNone(posts["OLD1"]["last_read"], "outside the lookback: never read")

    def test_next_run_rereads_only_posts_with_new_comments(self):
        discover.run(cfg())
        self.reads.clear()
        self.items[1]["comment_count"] = 3                   # someone commented on NEW2
        discover.run(cfg())
        self.assertEqual(self.reads, ["NEW2"])
        self.assertEqual(store.load_posts()["NEW2"]["reads"], 2)
        self.assertEqual(store.load_posts()["NEW1"]["reads"], 1)
        self.reads.clear()
        discover.run(cfg(skip_unchanged=False))
        self.assertEqual(self.reads, ["NEW1", "NEW2"], "re-read every run when skip_unchanged is off")

    def test_first_seen_is_kept_across_runs(self):
        self.fake.answers = {}                               # API silent: profile grid, no dates
        self.fake.grid = [{"kind": "p", "code": "G1"}]
        discover.run(cfg())
        posts = store.load_posts()
        posts["G1"]["first_seen"] = (datetime.now() - timedelta(days=30)).isoformat(timespec="seconds")
        store.save_posts(posts)
        self.reads.clear()
        discover.run(cfg())
        self.assertEqual(self.reads, [], "an undated post first seen 30 days ago is past the lookback")
        self.assertTrue(store.load_posts()["G1"]["first_seen"].startswith(posts["G1"]["first_seen"][:10]))

    def test_a_post_that_fails_to_read_is_tried_next_run(self):
        calls = []

        def flaky(driver, c, url):
            calls.append(url)
            if len(calls) == 1:
                raise RuntimeError("page died")
            return {"shortcode": "x", "owner": "me.test", "comments": 1, "commenters": 1, "new": 0, "how": "fake"}
        self.read_post.side_effect = flaky
        totals = discover.run(cfg())
        self.assertEqual((totals["read"], totals["failed"]), (1, 1))
        self.assertIsNone(store.load_posts()["NEW1"]["last_read"])

    def test_account_wide_answer_stops_with_exit_code_2(self):
        self.fake.answers[f"feed/user/{ME_ID}/"] = (400, '{"message":"checkpoint_required"}')
        with self.assertRaises(browser.Halt) as stop:
            discover.run(cfg())
        self.assertEqual(stop.exception.code, 2)
        self.assertIn("restricted", str(stop.exception))
        self.assertEqual(self.reads, [])

    def test_block_while_reading_a_post_stops_the_rest(self):
        self.read_post.side_effect = AccountBlocked("Instagram is rate-limiting this account", "rate_limited")
        with self.assertRaises(browser.Halt):
            discover.run(cfg())
        self.assertEqual(self.read_post.call_count, 1)

    def test_challenge_page_after_login_stops(self):
        self.fake.current_url = "https://www.instagram.com/challenge/abc/"
        self.patch(self.fake, "get", lambda url: None)
        with self.assertRaises(browser.Halt):
            discover.run(cfg())
        self.assertEqual(self.fake.fetched, [])

    def test_only_reads_with_get_requests_except_the_reels_form(self):
        discover.run(cfg())
        self.assertNotIn("method", comments._FETCH, "the in-page fetch is a plain GET")
        self.assertTrue(self.fake.fetched)
        gets = [u for u in self.fake.fetched if u not in {url for url, _ in self.fake.posted}]
        self.assertTrue(all(u.startswith("https://www.instagram.com/api/v1/users/web_profile_info/") or
                            u.startswith(f"https://www.instagram.com/api/v1/feed/user/{ME_ID}/") or
                            u.startswith("https://www.instagram.com/api/v1/clips/user/?") for u in gets), gets)
        self.assertEqual({url for url, _ in self.fake.posted}, {"https://www.instagram.com/api/v1/clips/user/"})

    def test_api_refuses_a_post_to_anything_but_a_read_endpoint(self):
        for path in ("friendships/create/1/", "direct_v2/threads/1/items/2/seen/", "web/likes/1/like/",
                     "direct_v2/threads/1/approve/"):
            with self.subTest(path):
                with self.assertRaises(ValueError):
                    comments.api(self.fake, path, form={"x": "1"})
        self.assertEqual(self.fake.posted, [])


def clip(code, days_ago, comments_n=3, *, pinned=False, now=NOW):
    """One reel as clips/user/ gives it: wrapped in {"media": ...}."""
    media = item(code, days_ago, comments_n, reel=True, now=now)
    if pinned:
        media["clips_tab_pinned_user_ids"] = [int(ME_ID)]
    return {"media": media}


class Reels(StoreTest):
    """Reels shared only to the Reels tab aren't in feed/user/: clips/user/ finds them."""

    def setUp(self):
        super().setUp()
        self.patch(browser, "pause", lambda *a: None)

    def fake(self, feed, clip_pages, **answers):
        def clips(url):
            n = int(url.split("max_id=c")[1].split("&")[0]) if "max_id=c" in url else 0
            return {"items": clip_pages[n], "paging_info": {"more_available": n + 1 < len(clip_pages),
                                                            "max_id": f"c{n + 1}"}, "status": "ok"}
        return FakeAPI({"users/web_profile_info/": profile_info(),
                        f"feed/user/{ME_ID}/": {"items": feed, "more_available": False},
                        "clips/user/": clips, **answers})

    def test_reels_are_added_once_with_reel_links(self):
        fake = self.fake([item("POST1", 1), item("BOTH", 2, reel=True)],
                         [[clip("BOTH", 2), clip("ONLYREEL", 3)]])
        posts = discover.from_api(fake, cfg(), "me.test", NOW - timedelta(days=7))
        self.assertEqual([p["shortcode"] for p in posts], ["POST1", "BOTH", "ONLYREEL"], "BOTH is kept once")
        only = posts[-1]
        self.assertEqual(only["url"], "https://www.instagram.com/reel/ONLYREEL/")
        self.assertEqual((only["owner"], only["comment_count"]), ("me.test", 3))
        [(url, form)] = fake.posted
        self.assertEqual(url, "https://www.instagram.com/api/v1/clips/user/")
        self.assertIn(f"target_user_id={ME_ID}", form)

    def test_reels_page_until_older_than_the_lookback_and_an_old_pinned_one_doesnt_stop_it(self):
        fake = self.fake([], [[clip("PIN", 200, pinned=True), clip("R1", 1)], [clip("R2", 5), clip("R3", 9)],
                              [clip("R4", 12)]])
        posts = discover.from_api(fake, cfg(), "me.test", NOW - timedelta(days=7))
        self.assertEqual([p["shortcode"] for p in posts], ["PIN", "R1", "R2", "R3"])
        self.assertEqual(len(fake.posted), 2)
        self.assertIn("max_id=c1", fake.posted[1][1])

    def test_reels_page_by_max_id_when_more_available_is_missing(self):
        pages = [[clip("R1", 1)], [clip("R2", 2)]]

        def clips(url):                          # like instagrapi reads it: paging_info.max_id, no more_available
            n = 1 if "max_id=c1" in url else 0
            return {"items": pages[n], "paging_info": {"max_id": "c1"} if n == 0 else {}, "status": "ok"}
        fake = FakeAPI({"users/web_profile_info/": profile_info(), f"feed/user/{ME_ID}/": {"items": []},
                        "clips/user/": clips})
        posts = discover.from_api(fake, cfg(), "me.test", NOW - timedelta(days=7))
        self.assertEqual([p["shortcode"] for p in posts], ["R1", "R2"])
        self.assertEqual(len(fake.posted), 2, "stops when no max_id comes back")

    def test_post_not_answering_asks_with_a_get(self):
        fake = self.fake([item("POST1", 1)], [[clip("R1", 1)]])
        clips = fake.answers["clips/user/"]
        posting = lambda: fake.posted and fake.posted[-1][0] == fake.fetched[-1]   # noqa: E731 (this call a POST?)
        fake.answers["clips/user/"] = lambda url: (405, "{}") if posting() else clips(url)
        posts = discover.from_api(fake, cfg(), "me.test", NOW - timedelta(days=7))
        self.assertEqual([p["shortcode"] for p in posts], ["POST1", "R1"])
        self.assertEqual(len(fake.posted), 1, "one POST, then GETs")
        self.assertTrue(any(u.startswith("https://www.instagram.com/api/v1/clips/user/?target_user_id=")
                            for u in fake.fetched))

    def test_reels_endpoint_silent_leaves_the_feed(self):
        fake = self.fake([item("POST1", 1)], [[]])
        fake.answers["clips/user/"] = (404, "{}")
        self.assertEqual([p["shortcode"] for p in discover.from_api(fake, cfg(), "me.test", NOW)], ["POST1"])

    def test_reels_count_toward_max_posts_and_the_lookback(self):
        fake = self.fake([item("P1", 1)], [[clip("R1", 0.5), clip("R2", 2), clip("ROLD", 10)]])
        found = discover.find(fake, cfg(max_posts=2), NOW - timedelta(days=7))
        read, skip = discover.select(found, {}, cfg(max_posts=2), NOW)
        self.assertEqual([p["shortcode"] for p in read], ["R1", "P1"])
        self.assertEqual({p["shortcode"]: why[:10] for p, why in skip}, {"R2": "past disco", "ROLD": "older than"})

    def test_a_block_from_the_reels_endpoint_is_account_wide(self):
        fake = self.fake([item("P1", 1)], [[]])
        fake.answers["clips/user/"] = (400, '{"message":"feedback_required"}')
        with self.assertRaises(AccountBlocked) as stop:
            discover.from_api(fake, cfg(), "me.test", NOW)
        self.assertEqual(stop.exception.kind, "rate_limited")


class ReadPost(StoreTest):
    """comments.read_post, which discover.run calls for each post, against a fake comments endpoint."""

    def setUp(self):
        super().setUp()
        self.patch(browser, "pause", lambda *a: None)

    def comment(self, pk, user, text):
        return {"pk": pk, "text": text, "created_at": int(NOW.timestamp()), "user": {"username": user, "pk": pk}}

    def test_merges_commenters_into_leads_and_rescores_changed_ones(self):
        page = {"comments": [self.comment(1, "jane.doe", "how do essays work?"), self.comment(2, "me.test", "dm me"),
                             self.comment(3, "jane.doe", "and scholarships")]}
        fake = FakeAPI({"media/": page})
        got = comments.read_post(fake, cfg(), "https://www.instagram.com/p/ABC/")
        self.assertEqual((got["comments"], got["commenters"], got["new"]), (3, 1, 1), "own account left out")
        leads = store.load_leads()
        self.assertEqual(leads["ABC:jane.doe"]["text"], "how do essays work?\nand scholarships")
        leads["ABC:jane.doe"]["ai"] = {"score": 90}
        store.save_leads(leads)
        page["comments"].append(self.comment(4, "jane.doe", "also internships"))
        self.assertEqual(comments.read_post(fake, cfg(), "https://www.instagram.com/p/ABC/")["new"], 0)
        self.assertNotIn("ai", store.load_leads()["ABC:jane.doe"], "a new comment from them: scored again")

    def test_logged_out_answer_is_account_wide(self):
        with self.assertRaises(AccountBlocked) as stop:
            comments.read_post(FakeAPI({"media/": (401, "")}), cfg(), "https://www.instagram.com/p/ABC/")
        self.assertEqual(stop.exception.kind, "login")


class RunPy(StoreTest):
    """python run.py with no link: discover -> qualify -> send, and its exit codes."""

    def setUp(self):
        super().setUp()
        self.log = []
        self.discover = self.patch(discover, "run", side_effect=lambda c: self.log.append("discover"))
        self.qualify = self.patch(qualify, "run", side_effect=lambda c: self.log.append("qualify"))
        self.send = self.patch(send, "run", side_effect=lambda c: self.log.append("send") or 0)
        self.patch(run, "CONFIG", self.tmp / "config.json")
        (self.tmp / "config.json").write_text('{"ai": {"min_score": 50}, "outreach": {"daily_dm_limit": 20}}')

    def cli(self, *args):
        with mock.patch.object(sys, "argv", ["run.py", *args]):
            return run.cli()

    def test_no_link_and_auto_run_the_whole_pipeline(self):
        for args in ((), ("auto",)):
            with self.subTest(args=args):
                self.log.clear()
                self.assertEqual(self.cli(*args), 0)
                self.assertEqual(self.log, ["discover", "qualify", "send"])

    def test_account_wide_stop_while_reading_exits_2_and_sends_nothing(self):
        self.discover.side_effect = browser.Halt("account-wide problem while reading posts (login): logged out")
        self.assertEqual(self.cli(), 2)
        self.assertEqual(self.log, [])
        self.assertIn("[stopped] account-wide problem", self.out.getvalue())

    def test_send_halting_still_exits_2(self):
        self.send.side_effect = lambda c: 2
        self.assertEqual(self.cli(), 2)

    def test_a_link_still_reads_just_that_post(self):
        read = self.patch(comments, "run", return_value=1)
        self.assertEqual(self.cli("https://www.instagram.com/p/ABC/"), 0)
        read.assert_called_once()
        self.discover.assert_not_called()

    def test_a_link_whose_read_halts_exits_2(self):
        self.patch(comments, "run", side_effect=browser.Halt("logged out (login)"))
        self.assertEqual(self.cli("https://www.instagram.com/p/ABC/"), 2)


if __name__ == "__main__":
    unittest.main()
