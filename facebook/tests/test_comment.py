"""test_comment.py - which leads comment.py comments on, and what a run does on a block, a crash, the daily cap and
a second run. No browser: a fake poster answers for the page (test_mock_site.py runs the real page code), and
store.py's real ledger decides the rest.

    cd facebook && ../venv/bin/python -m unittest discover tests -p test_comment.py
"""
from __future__ import annotations

import json
import sys
import unittest
from unittest import mock

from helpers import FakeChrome, SENDER, StoreTest, config, lead

import comment
import queue_server
import run
import send
import store
from comment import Posted
from send_dm import AccountBlocked


def minor(n, name=None, **extra):
    """A lead the AI thinks is under 18: no DM under minor_policy comment_only, only the public comment."""
    l = lead(n, name)
    l.pop("approved")
    l["ai"].update(likely_minor=True, comment=f"Good luck {l['author']}, here is a tip.")
    l.update(post_url=f"https://www.facebook.com/groups/g/posts/{n}/", text=f"Post number {n} about essays", **extra)
    return l


def no_profile(n):
    """A lead whose author link couldn't be read: it can't be DMed, so the comment is the only way to reach them."""
    l = minor(n)
    l["ai"]["likely_minor"] = False
    l["author_key"] = None
    return l


class Poster:
    """Stands in for comment.post_comment: answers from a script ({post_key: Posted or exception}, default
    'commented'), remembers which posts it was called for, and runs before_submit for the posts in press."""

    def __init__(self, answers=None, press=None):
        self.answers, self.calls = answers or {}, []
        self.press = press

    def __call__(self, driver, url, text, author, snippet, *, before_submit=None):
        key = "p:" + url.rstrip("/").rsplit("/", 1)[-1]
        self.calls.append(key)
        if self.press is None or key in self.press:
            before_submit()
        answer = self.answers.get(key, Posted("commented", "fake: commented"))
        if isinstance(answer, Exception):
            raise answer
        return answer


def on(**kw):
    return config(auto_comment=True, auto_comment_minors=True, **kw)


class CommentTest(StoreTest):
    def save(self, *leads):
        store.save_leads({**store.load_leads(), **{l["post_key"]: l for l in leads}})
        return list(leads)

    def akey(self, l):
        return l["author_key"] or f"post:{l['post_key']}"

    def statuses(self, l):
        return [r["status"] for r in store.history().get(self.akey(l), [])]


class WhichLeads(CommentTest):
    def test_minors_are_skipped_by_default(self):
        kid, = self.save(minor(1))
        self.assertIn("auto_comment_minors", comment.why_not(kid, config(auto_comment=True)))
        self.assertEqual(comment.candidates(config(auto_comment=True)), [])
        self.assertEqual([l["post_key"] for l in comment.candidates(on())], [kid["post_key"]])

    def test_a_lead_that_can_be_dmed_is_left_to_send(self):
        adult, = self.save(minor(1))
        adult["ai"]["likely_minor"] = False
        self.save(adult)
        self.assertIn("send.py", comment.why_not(adult, on()))

    def test_a_lead_with_no_profile_link_is_commented_on_by_default(self):
        anon, = self.save(no_profile(1))
        self.assertEqual(comment.candidates(config(auto_comment=True)), [anon])

    def test_minor_policy_skip_means_no_comment_either(self):
        kid, = self.save(minor(1))
        self.assertIn("skip", comment.why_not(kid, on(minor_policy="skip")))

    def test_leads_handled_in_the_queue_are_left_alone(self):
        a, b, c = self.save(minor(1), minor(2), minor(3))
        store.record(a["author_key"], a["author"], a["post_key"], "skipped")
        store.record(b["author_key"], b["author"], b["post_key"], "not_relevant")
        self.assertEqual([l["post_key"] for l in comment.candidates(on())], [c["post_key"]])

    def test_an_author_already_dmed_or_commented_on_is_never_commented_on(self):
        dmed, marked, held = self.save(minor(1), minor(2), minor(3))
        other = {**minor(4), "author_key": dmed["author_key"]}            # dmed's other post
        self.save(other)
        store.record(dmed["author_key"], dmed["author"], "p:elsewhere", "sent")
        store.record(marked["author_key"], marked["author"], "p:elsewhere", "commented")   # "Mark commented"
        store.record(held["author_key"], held["author"], "p:elsewhere", "uncertain")       # a DM that may have gone
        self.assertEqual(comment.candidates(on()), [])

    def test_an_author_with_a_dm_waiting_is_not_commented_on(self):
        kid, = self.save(minor(1))
        store.record(kid["author_key"], kid["author"], "p:other", "approved")
        self.assertIn("approved and waiting", comment.why_not(kid, on()))


class Posting(CommentTest):
    def test_records_commented_the_way_mark_commented_does(self):
        kid, = self.save(minor(1))
        counts = comment.comment_loop(None, comment.candidates(on()), on(), post=Poster())
        self.assertEqual(counts["commented"], 1)
        self.assertEqual(self.statuses(kid), ["comment_submitting", "commented"])
        self.assertEqual(store.history()[kid["author_key"]][0]["note"], kid["ai"]["comment"],
                         "the submitting row keeps the text, so a held comment can be looked for")
        self.assertIn("already commented", store.skip_reason(kid["author_key"]), "never DMed after it")
        self.assertEqual(queue_server.status_of(kid, on(), store.handled_posts(), store.history()), "commented")
        self.assertEqual(store.commented_today(), 1)

    def test_the_queue_edit_of_the_comment_is_what_gets_posted(self):
        kid = minor(1)
        kid["edited"] = {"comment": "My own words"}
        self.save(kid)
        poster = mock.Mock(return_value=Posted("commented", "fake"))
        comment.comment_loop(None, [kid], on(), post=poster)
        self.assertEqual(poster.call_args.args[2], "My own words")

    def test_no_double_comment_on_a_second_run_or_another_post_by_them(self):
        kid, again = self.save(minor(1), {**minor(2), "author_key": "10001"})
        first = Poster()
        comment.comment_loop(None, comment.candidates(on()), on(), post=first)
        self.assertEqual(first.calls, [kid["post_key"]], "their second post is skipped once the first is done")
        second = Poster()
        comment.comment_loop(None, [kid, again], on(), post=second)
        self.assertEqual(second.calls, [])
        self.assertEqual(self.statuses(kid).count("commented"), 1)

    def test_daily_comment_cap_counts_mark_commented_and_is_not_a_halt(self):
        by_hand, *leads = self.save(minor(1), minor(2), minor(3), minor(4))
        store.record(by_hand["author_key"], by_hand["author"], by_hand["post_key"], "commented", "by hand")
        poster = Poster()
        counts = comment.comment_loop(None, comment.candidates(on()), on(daily_comment_limit=2), post=poster)
        self.assertEqual(len(poster.calls), 1, "one by hand + one here = the cap of 2")
        self.assertFalse(counts["halted"])
        self.assertIn("daily comment cap", counts["stopped"])
        self.assertEqual(comment.report(counts), 0)

    def test_the_comment_cap_is_separate_from_the_dm_cap(self):
        kid, = self.save(minor(1))
        for n in range(3):
            store.record(f"x{n}", "", f"p:x{n}", "sent")
        counts = comment.comment_loop(None, [kid], on(daily_dm_limit=3), post=Poster())
        self.assertEqual(counts["commented"], 1)


class Stops(CommentTest):
    def test_a_block_before_posting_halts_the_run_and_the_next_run_retries(self):
        a, b = self.save(minor(1), minor(2))
        first = Poster({a["post_key"]: Posted("rate_limited", "comment: You're Temporarily Blocked")}, press=())
        counts = comment.comment_loop(None, comment.candidates(on()), on(), post=first)
        self.assertEqual(first.calls, [a["post_key"]], "nothing after the block")
        self.assertTrue(counts["halted"])
        self.assertIn("rate_limited", counts["stopped"])
        self.assertEqual(comment.report(counts), 2)
        self.assertEqual(self.statuses(a), ["comment_stopped"])
        self.assertIn("rate_limited", store.history()[a["author_key"]][-1]["note"])
        self.assertEqual(self.statuses(b), [])

        second = Poster()
        comment.comment_loop(None, comment.candidates(on()), on(), post=second)
        self.assertEqual(second.calls, [a["post_key"], b["post_key"]])

    def test_a_block_after_enter_holds_the_comment_and_halts(self):
        a, b = self.save(minor(1), minor(2))
        first = Poster({a["post_key"]: Posted("uncertain", "comment: blocked after Enter", "rate_limited")})
        counts = comment.comment_loop(None, comment.candidates(on()), on(), post=first)
        self.assertTrue(counts["halted"])
        self.assertEqual(first.calls, [a["post_key"]])
        self.assertEqual(self.statuses(a), ["comment_submitting", "comment_uncertain"])
        self.assertIn(a["post_key"], store.held_comments())
        self.assertIn("HELD", store.skip_reason(a["author_key"]), "a held comment holds their DM too")
        self.assertEqual(store.commented_today(), 1, "it may be on the post, so it counts against the cap")
        code, body = queue_server.mark({"post_key": a["post_key"], "status": "commented"})
        self.assertEqual(code, 409, body)

        second = Poster()
        comment.comment_loop(None, comment.candidates(on()), on(), post=second)
        self.assertEqual(second.calls, [b["post_key"]], "held: never posted again")

    def test_a_crash_after_enter_is_held(self):
        a, = self.save(minor(1))
        comment.comment_loop(None, [a], on(), post=Poster({a["post_key"]: RuntimeError("chrome not reachable")}))
        self.assertEqual(self.statuses(a), ["comment_submitting", "comment_uncertain"])

    def test_killed_between_submitting_and_the_outcome_is_held(self):
        a, = self.save(minor(1))
        store.record(a["author_key"], a["author"], a["post_key"], "comment_submitting", a["ai"]["comment"])
        self.assertEqual(comment.candidates(on()), [])
        self.assertIn(a["post_key"], store.held_comments())

    def test_a_failure_before_enter_is_retried_up_to_max_attempts(self):
        a, = self.save(minor(1))
        fail = Poster({a["post_key"]: Posted("not_submitted", "fake: no box")}, press=())
        for _ in range(3):
            comment.comment_loop(None, comment.candidates(on()), on(), post=fail)
        self.assertEqual(len(fail.calls), 3)
        self.assertIn("gave up", comment.why_not(a, on()))
        self.assertEqual(queue_server.status_of(a, on(), store.handled_posts(), store.history()), "todo",
                         "still in the queue to comment by hand")

    def test_streak_of_closed_posts_halts(self):
        leads = self.save(*(minor(n) for n in range(1, 6)))
        closed = Poster({l["post_key"]: Posted("closed", "fake: only members can comment") for l in leads}, press=())
        counts = comment.comment_loop(None, comment.candidates(on()), on(), post=closed)
        self.assertEqual(len(closed.calls), comment.MAX_CLOSED_IN_A_ROW)
        self.assertTrue(counts["halted"])
        self.assertIn("comment_closed", self.statuses(leads[0]))
        self.assertIn("comment_closed", comment.why_not(leads[0], on()))


class HeldComments(CommentTest):
    def hold(self, *leads):
        for l in leads:
            store.record(l["author_key"], l["author"], l["post_key"], "comment_submitting", l["ai"]["comment"])
            store.record(l["author_key"], l["author"], l["post_key"], "comment_uncertain", "fake")

    def test_found_on_the_post_is_commented_and_not_found_stays_held(self):
        there, missing = self.save(minor(1), minor(2))
        self.hold(there, missing)
        seen = []

        def check(driver, url, text, author, snippet):
            seen.append(text)
            if author != there["author"]:
                raise RuntimeError("the comment isn't showing on the post")
            return True

        comment.resolve_held(None, check)
        self.assertEqual(seen, [there["ai"]["comment"], missing["ai"]["comment"]])
        self.assertEqual(self.statuses(there)[-1], "commented")
        self.assertEqual(list(store.held_comments()), [missing["post_key"]], "never cleared automatically")

    def test_resolve_by_hand(self):
        a, = self.save(minor(1))
        self.hold(a)
        store.resolve_comment(a["post_key"], False)
        self.assertEqual(self.statuses(a)[-1], "comment_cleared")
        self.assertEqual(comment.candidates(on()), [a], "eligible again")

    def test_a_block_while_checking_stops_checking(self):
        a, b = self.save(minor(1), minor(2))
        self.hold(a, b)
        check = mock.Mock(side_effect=AccountBlocked("temporarily blocked", "rate_limited"))
        comment.resolve_held(None, check)
        self.assertEqual(check.call_count, 1)
        self.assertEqual(len(store.held_comments()), 2)


class Prepare(CommentTest):
    def test_a_block_on_the_home_page_stops_before_any_post(self):
        driver = FakeChrome({"https://www.facebook.com/": {"text": "News Feed  You're Temporarily Blocked"}},
                            {"c_user": SENDER})
        with self.assertRaises(SystemExit) as stop:
            comment.prepare(driver, on())
        self.assertIn("rate_limited", str(stop.exception))
        self.assertEqual(stop.exception.code, 2)


class RunExitCodes(StoreTest):
    """python run.py: exit 2 when sending or commenting halted; commenting only with outreach.auto_comment."""

    def pipeline(self, cfg, send_run, comment_run):
        (self.tmp / "config.json").write_text(json.dumps(cfg))
        self.patch(run, "CONFIG", self.tmp / "config.json")
        self.patch(run.scrape, "run")
        self.patch(run.qualify, "run")
        self.patch(run.send, "run", side_effect=send_run)
        commented = self.patch(run.comment, "run", side_effect=comment_run)
        self.patch(sys, "argv", ["run.py"])
        with self.assertRaises(SystemExit) as done:
            run.main()
        return done.exception.code, commented.called

    def test_comment_halt_exits_2(self):
        self.assertEqual(self.pipeline(on(), lambda cfg: 0, lambda cfg: 2), (2, True))

    def test_send_halt_exits_2_without_commenting(self):
        self.assertEqual(self.pipeline(on(), lambda cfg: 2, lambda cfg: 0), (2, False))

    def test_send_stopping_before_it_starts_still_comments(self):
        def capped(cfg):
            raise SystemExit("Already sent 20/20 today. Nothing to do until tomorrow.")
        self.assertEqual(self.pipeline(on(), capped, lambda cfg: 0), (1, True))

    def test_no_commenting_unless_auto_comment(self):
        self.assertEqual(self.pipeline(config(), lambda cfg: 0, lambda cfg: 2), (0, False))


if __name__ == "__main__":
    unittest.main()
