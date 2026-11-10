"""test_replies.py - `python run.py replies` (replies.py) with fake inbox answers from Instagram's web API
(helpers.FakeAPI) in place of instagram.com: reading the inbox page by page, matching chats to the people
data/contacted.csv records as sent, counting only their messages after our DM, deduping by item id, the full text
column (and reading files from before it), Message Requests (how=request, never accepted), the chat-list fallback,
and the `replied` count in run.py stats.

    cd instagram && ../venv/bin/python -m unittest tests/test_replies.py
"""
from __future__ import annotations

import csv
import unittest
from datetime import datetime, timedelta

from helpers import FakeAPI, StoreTest, config

import browser
import replies
import run
import store

SENT = datetime(2026, 11, 8, 10, 0, 0)


def us(dt):
    """An inbox timestamp: microseconds since the epoch, as a string, the way Instagram sends it."""
    return str(int(dt.timestamp() * 1_000_000))


def msg(item_id, user_id, minutes_after_send, text="hi", **extra):
    return {"item_id": item_id, "user_id": user_id, "timestamp": us(SENT + timedelta(minutes=minutes_after_send)),
            "item_type": "text", "text": text, **extra}


def thread(tid, user, pk, *items, group=False, last=None):
    users = [{"pk": pk, "username": user, "full_name": user.title()}]
    if group:
        users.append({"pk": 9, "username": "someone.else"})
    stamps = [i["timestamp"] for i in items] or [us(SENT)]
    return {"thread_id": tid, "is_group": group, "users": users, "items": list(items),
            "last_activity_at": us(last) if last else max(stamps)}


ME = 100


class Matching(unittest.TestCase):
    sent = {"jane.doe": SENT.isoformat(timespec="seconds"), "sam.p": SENT.isoformat(timespec="seconds")}

    def test_only_their_messages_after_our_send_count(self):
        t = thread("111", "jane.doe", 7,
                   msg("i4", 7, 90, "how much does it cost?"), msg("i3", ME, 1, "hey jane! ..."),
                   msg("i2", 7, -60, "an older message from before our DM"), msg("i1", 7, 5, "  thanks!\nwho is this "))
        rows, seen = replies.match([t], self.sent)
        self.assertEqual(seen, {"jane.doe"})
        self.assertEqual([r["item_id"] for r in rows], ["i1", "i4"], "oldest first; ours and the older one left out")
        first = rows[0]
        self.assertEqual(first["username"], "jane.doe")
        self.assertEqual(first["snippet"], "thanks! who is this")
        self.assertEqual(first["thread_url"], "https://www.instagram.com/direct/t/111/")
        self.assertEqual(first["replied_at"], (SENT + timedelta(minutes=5)).isoformat(timespec="seconds"))
        self.assertEqual(first["sent_at"], self.sent["jane.doe"])
        self.assertEqual(first["how"], "api")

    def test_a_chat_with_no_reply_is_seen_but_has_no_rows(self):
        rows, seen = replies.match([thread("222", "sam.p", 8, msg("s1", ME, 0))], self.sent)
        self.assertEqual((rows, seen), ([], {"sam.p"}))

    def test_group_chats_and_people_never_dmed_dont_count(self):
        threads = [thread("333", "jane.doe", 7, msg("g1", 7, 10), group=True),
                   thread("444", "stranger", 5, msg("x1", 5, 10))]
        self.assertEqual(replies.match(threads, self.sent), ([], set()))

    def test_username_case_doesnt_matter(self):
        rows, _ = replies.match([thread("555", "Jane.Doe", 7, msg("c1", 7, 3))], self.sent)
        self.assertEqual([r["username"] for r in rows], ["jane.doe"])

    def test_snippets_for_other_kinds_of_message(self):
        self.assertEqual(replies.snippet({"item_type": "like"}), "[like]")
        self.assertEqual(replies.snippet({"item_type": "voice_media"}), "[voice message]")
        self.assertEqual(replies.snippet({"item_type": "link", "link": {"text": "look https://x.y"}}), "look https://x.y")
        self.assertEqual(replies.snippet({"item_type": "weird_new_kind"}), "[weird_new_kind]")
        long = replies.snippet({"text": "word " * 100})
        self.assertEqual(len(long), replies.SNIPPET)
        self.assertTrue(long.endswith("..."))

    def test_full_text_keeps_the_whole_message_up_to_the_cap(self):
        long = "line one\nline two " + "word " * 1200
        t = thread("111", "jane.doe", 7, msg("i1", 7, 5, long))
        [row] = replies.match([t], self.sent)[0]
        self.assertTrue(row["text"].startswith("line one\nline two"), "line breaks kept")
        self.assertEqual(len(row["text"]), replies.TEXT_MAX)
        self.assertEqual(len(row["snippet"]), replies.SNIPPET)
        self.assertEqual(replies.full_text({"item_type": "voice_media"}), "[voice message]")
        self.assertEqual(replies.full_text({"text": "  ok!  "}), "ok!")

    def test_rows_from_message_requests_say_so(self):
        [row] = replies.match([thread("1", "jane.doe", 7, msg("r1", 7, 5))], self.sent, how="request")[0]
        self.assertEqual(row["how"], "request")

    def test_timestamps_in_any_unit(self):
        for ts in (us(SENT), str(int(SENT.timestamp() * 1000)), int(SENT.timestamp())):
            self.assertEqual(replies.when(ts), SENT)
        self.assertIsNone(replies.when(None))


class ChatList(unittest.TestCase):
    """The fallback's matching of chat-list rows, when the inbox endpoint doesn't answer."""
    now = SENT + timedelta(days=1)
    sent = {"jane.doe": SENT.isoformat(timespec="seconds"), "sam.p": SENT.isoformat(timespec="seconds"),
            "no.chat": SENT.isoformat(timespec="seconds")}
    names = {"jane.doe": "Jane Doe", "sam.p": ""}

    def test_rows_by_name_or_username_and_only_when_the_preview_isnt_ours(self):
        rows = [{"lines": ["Jane Doe", "Active 2h ago", "sounds good, how much? · 2h"], "href": ""},
                {"lines": ["sam.p", "You: hey sam! saw your comment · 1d"], "href": "https://www.instagram.com/direct/t/9/"},
                {"lines": ["Someone", "hello · 5m"], "href": ""}]
        out, seen = replies.from_rows(rows, self.sent, self.names, self.now)
        self.assertEqual(seen, {"jane.doe", "sam.p"})
        [r] = out
        self.assertEqual((r["username"], r["snippet"], r["how"]), ("jane.doe", "sounds good, how much?", "page"))
        self.assertEqual(r["replied_at"], (self.now - timedelta(hours=2)).isoformat(timespec="seconds"))
        self.assertEqual(r["thread_url"], "https://ig.me/m/jane.doe", "no link on the row: the chat's ig.me link")
        again, _ = replies.from_rows(rows, self.sent, self.names, self.now + timedelta(hours=1))
        self.assertEqual(again[0]["item_id"], r["item_id"], "the same preview later is the same item")

    def test_a_vague_age_never_dates_a_reply_before_our_dm(self):
        rows = [{"lines": ["Jane Doe", "ok! · 3w"], "href": ""}]
        [r] = replies.from_rows(rows, self.sent, self.names, self.now)[0]
        self.assertGreater(r["replied_at"], self.sent["jane.doe"])


class Run(StoreTest):
    def setUp(self):
        super().setUp()
        self.patch(browser, "pause", lambda *a: None)
        self.patch(browser, "ensure_logged_in", lambda *a, **k: None)
        self.fake = FakeAPI()
        self.patch(browser, "make_driver", lambda c: self.fake)
        at = SENT.isoformat(timespec="seconds")
        store.DATA.mkdir()
        with open(store.CONTACTED, "w", newline="") as f:          # jane and sam sent at SENT; old.one refused
            w = csv.writer(f)
            w.writerow(store.FIELDS)
            for user, status in (("jane.doe", "approved"), ("sam.p", "approved"), ("old.one", "approved"),
                                 ("jane.doe", "sent"), ("sam.p", "sent"), ("old.one", "refused")):
                w.writerow([user, f"POST:{user}", status, at, ""])
        self.contacted = store.CONTACTED.read_text()

    def inbox(self, *pages):
        """Pages of the inbox, chained by cursor."""
        def answer(url):
            n = int(url.split("cursor=c")[1]) if "cursor=c" in url else 0
            return {"inbox": {"threads": pages[n], "has_older": n + 1 < len(pages), "oldest_cursor": f"c{n + 1}"},
                    "viewer": {"pk": ME}}
        self.fake.answers["direct_v2/inbox/"] = answer

    def test_records_replies_once_and_leaves_contacted_csv_alone(self):
        self.inbox([thread("1", "jane.doe", 7, msg("j2", 7, 30, "yes please"), msg("j1", ME, 0, "hey jane"))],
                   [thread("2", "sam.p", 8, msg("s1", ME, 0, "hey sam")),
                    thread("3", "old.one", 9, msg("o1", 9, 30, "not DMed by us: refused"))])
        got = replies.run(config())
        self.assertEqual((got["dmed"], got["replied"], got["new"], got["missing"]), (2, 1, 1, 0))
        [row] = store.load_replies()
        self.assertEqual((row["username"], row["item_id"], row["snippet"]), ("jane.doe", "j2", "yes please"))
        self.assertEqual(store.CONTACTED.read_text(), self.contacted, "contacted.csv isn't changed")
        self.assertIn("1 of 2 people DMed have replied", self.out.getvalue())

        again = replies.run(config())
        self.assertEqual((again["new"], again["replied"]), (0, 1))
        self.assertEqual(len(store.load_replies()), 1, "deduped by item id")

    def test_paging_stops_once_everyone_dmed_is_found(self):
        self.inbox([thread("1", "jane.doe", 7), thread("2", "sam.p", 8)], [thread("3", "x", 3)])
        replies.run(config())
        self.assertEqual(len([u for u in self.fake.fetched if "direct_v2/inbox" in u]), 1)

    def test_paging_stops_at_chats_older_than_the_first_dm(self):
        self.inbox([thread("1", "jane.doe", 7), thread("9", "x", 3, last=SENT - timedelta(days=2))],
                   [thread("2", "sam.p", 8)])
        got = replies.run(config())
        self.assertEqual(len([u for u in self.fake.fetched if "direct_v2/inbox" in u]), 1)
        self.assertEqual(got["missing"], 1)

    def test_reads_only_the_inbox_and_never_marks_seen(self):
        self.inbox([thread("1", "jane.doe", 7, msg("j2", 7, 30)), thread("2", "sam.p", 8)])
        replies.run(config())
        self.assertTrue(self.fake.fetched)
        for url in self.fake.fetched:
            self.assertTrue(url.startswith("https://www.instagram.com/api/v1/direct_v2/inbox/?"), url)
            self.assertNotIn("seen", url)
        self.assertEqual(self.fake.visited, [], "no chat (or any page) was opened")

    def pending(self, *pages):
        def answer(url):
            n = int(url.split("cursor=c")[1]) if "cursor=c" in url else 0
            return {"inbox": {"threads": pages[n], "has_older": n + 1 < len(pages), "oldest_cursor": f"c{n + 1}"}}
        self.fake.answers["direct_v2/pending_inbox/"] = answer

    def test_message_requests_are_read_for_people_not_in_the_inbox(self):
        self.inbox([thread("1", "jane.doe", 7, msg("j2", 7, 30, "hi from the inbox"))])
        self.pending([thread("2", "sam.p", 8, msg("s2", 8, 40, "hi from requests\nsecond line"))])
        got = replies.run(config())
        self.assertEqual((got["replied"], got["new"], got["missing"]), (2, 2, 0))
        rows = {r["username"]: r for r in store.load_replies()}
        self.assertEqual(rows["jane.doe"]["how"], "api")
        self.assertEqual((rows["sam.p"]["how"], rows["sam.p"]["text"]), ("request", "hi from requests\nsecond line"))
        self.assertIn("Message Requests, not accepted", self.out.getvalue())
        for url in self.fake.fetched:
            self.assertRegex(url, r"^https://www\.instagram\.com/api/v1/direct_v2/(inbox|pending_inbox)/\?")
            self.assertNotRegex(url, r"seen|approve|accept")
        self.assertEqual(self.fake.posted, [], "nothing is POSTed: no request accepted, nothing marked seen")
        self.assertEqual(self.fake.visited, [])

    def test_message_requests_arent_read_when_everyone_is_in_the_inbox(self):
        self.inbox([thread("1", "jane.doe", 7), thread("2", "sam.p", 8)])
        self.pending([thread("3", "sam.p", 8, msg("s9", 8, 40))])
        replies.run(config())
        self.assertFalse([u for u in self.fake.fetched if "pending_inbox" in u])

    def test_message_requests_not_answering_still_saves_the_inbox(self):
        self.inbox([thread("1", "jane.doe", 7, msg("j2", 7, 30))])
        got = replies.run(config())
        self.assertEqual((got["new"], got["missing"]), (1, 1))
        self.assertIn("Message Requests didn't answer", got["how"])

    def test_a_challenge_from_message_requests_exits_2(self):
        self.inbox([thread("1", "jane.doe", 7, msg("j2", 7, 30))])
        self.fake.answers["direct_v2/pending_inbox/"] = (400, '{"message":"challenge_required"}')
        with self.assertRaises(browser.Halt):
            replies.run(config())

    def test_pending_users_and_one_thread(self):
        self.pending([thread("2", "sam.p", 8)])
        self.assertEqual(replies.pending_users(self.fake, config(), ["Sam.P", "jane.doe"]), {"sam.p"})
        self.fake.answers["direct_v2/pending_inbox/"] = (404, "{}")
        self.assertIsNone(replies.pending_users(self.fake, config(), ["sam.p"]))
        self.fake.answers["direct_v2/threads/77/"] = {"thread": thread("77", "jane.doe", 7, msg("x", 7, 1))}
        self.assertEqual(replies.thread(self.fake, config(), "77")["thread_id"], "77")
        self.assertIsNone(replies.thread(self.fake, config(), "78"))

    def test_an_old_replies_file_without_text_is_read_and_upgraded(self):
        with open(store.REPLIES, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["username", "item_id", "replied_at", "snippet", "thread_url", "sent_at", "found_at", "how"])
            w.writerow(["jane.doe", "old1", "2026-11-08T11:00:00", "an old reply", "https://x/", "", "", "api"])
        [old] = store.load_replies()
        self.assertEqual((old["text"], old["thread_url"]), ("an old reply", "https://x/"), "text falls back to the snippet")
        self.inbox([thread("2", "sam.p", 8, msg("s2", 8, 40, "new one"))], [thread("1", "jane.doe", 7)])
        replies.run(config())
        with open(store.REPLIES, newline="") as f:
            self.assertEqual(next(csv.reader(f)), store.REPLY_FIELDS, "rewritten with the new header")
        rows = {r["item_id"]: r for r in store.load_replies()}
        self.assertEqual((rows["old1"]["snippet"], rows["old1"]["thread_url"]), ("an old reply", "https://x/"))
        self.assertEqual(rows["s2"]["text"], "new one")

    def test_inbox_endpoint_silent_falls_back_to_the_chat_list(self):
        rows = [{"lines": ["jane.doe", "ok cool · 1h"], "href": ""}]
        self.patch(replies, "via_page", lambda driver, c: rows)
        got = replies.run(config())
        self.assertEqual((got["new"], store.load_replies()[0]["how"]), (1, "page"))

    def test_account_wide_answer_exits_2(self):
        self.fake.answers["direct_v2/inbox/"] = (400, '{"message":"challenge_required"}')
        with self.assertRaises(browser.Halt) as stop:
            replies.run(config())
        self.assertEqual(stop.exception.code, 2)
        self.assertEqual(store.load_replies(), [])

    def test_nobody_sent_means_no_browser(self):
        store.CONTACTED.unlink()
        self.patch(browser, "make_driver", side_effect=AssertionError("opened Chrome"))
        self.assertEqual(replies.run(config())["dmed"], 0)

    def test_stats_counts_replied(self):
        self.inbox([thread("1", "jane.doe", 7, msg("j2", 7, 30), msg("j3", 7, 31)), thread("2", "sam.p", 8)])
        replies.run(config())
        self.out.truncate(0)
        run.stats(config())
        out = self.out.getvalue()
        self.assertIn("sent      2", out)
        self.assertIn("replied   1 (50% of sent)", out)


if __name__ == "__main__":
    unittest.main()
