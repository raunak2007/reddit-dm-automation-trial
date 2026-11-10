"""replies.py - find who wrote back after our DM, and list them in data/replies.csv. Nothing is typed or sent.

    python replies.py                 check the chats (python run.py replies)
    python replies.py --list          who has replied so far (no browser)

For everyone in data/contacted.csv with a 'sent' DM who isn't in data/replies.csv yet, most recent DM first and at
most outreach.replies_per_run chats a run: opens their chat (messenger.open_chat: the same checks as a send, that
it is with them and Facebook isn't refusing this account), reads its messages (messenger.thread), finds our DM in it
(the text in leads.json: the approved message, else your edit, else the draft), and counts as a reply any message
*from them* after it. Theirs vs ours comes from each row's screen-reader label ("You sent" / their name) and, without
one, from which side its text sits on; rows that can't be told apart never count, and nor do their messages from
before our DM. A reply is recorded once: author_key, author, post_key, seen_at, a short snippet and the chat URL.
If our DM can't be found in the chat (not loaded, edited, deleted), that chat is left for the next run.

Opening a chat shows their messages as seen, as it would by hand. It waits outreach.reply_min_delay_seconds to
reply_max_delay_seconds between chats, takes the same lock as send.py, and stops (exit code 2) on anything
account-wide (logged out, checkpoint, block, chat PIN) or after MAX_UNREADABLE_IN_A_ROW chats it couldn't read.
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import time
from pathlib import Path

import browser
import messenger
import send
import store
from selenium.common.exceptions import TimeoutException
from send_dm import AccountBlocked, MessageRefused, NoSuchUser

HERE = Path(__file__).resolve().parent
CONFIG = HERE / "config.json"
PER_RUN = 30
MAX_UNREADABLE_IN_A_ROW = 5     # chats in a row that wouldn't open or read: the page changed, or the account
SNIPPET = 160


def our_dm(lead):
    return (lead.get("approved", {}).get("dm") or lead.get("edited", {}).get("dm")
            or lead.get("ai", {}).get("dm") or "").strip()


def to_check(cfg):
    """[(author_key, author, post_key, dm text)] for everyone DMed who isn't in replies.csv, most recent DM first."""
    leads, done, out = store.load_leads(), store.replies(), []
    for akey, rows in store.history().items():
        sent = [r for r in rows if r["status"] == "sent"]
        if not sent or akey in done or akey.startswith("post:"):
            continue
        last = sent[-1]
        lead = leads.get(last["post_key"], {})
        out.append((last["timestamp"], akey, lead.get("author") or last["author"], last["post_key"], our_dm(lead)))
    out.sort(reverse=True)
    return [t[1:] for t in out]


def find_reply(rows, dm):
    """What they wrote after our DM, from messenger.thread rows: their messages' text joined ('' for a message
    with no text, e.g. a photo, shown as '[attachment]'), or None if they wrote nothing after it. LookupError if our
    DM isn't among our rows (then nothing can be said)."""
    want = messenger.squash(dm)

    def is_ours(r):
        have = messenger.squash(r.get("text"))
        return r.get("who") == "me" and have and (want in have or (len(have) >= 20 and have in want))

    mine = [i for i, r in enumerate(rows) if is_ours(r)]
    if not want or not mine:
        raise LookupError("our DM isn't in the chat as a message from us")
    theirs = [r for r in rows[mine[-1] + 1:] if r.get("who") == "them"]
    if not theirs:
        return None
    return " / ".join(r["text"].strip() or "[attachment]" for r in theirs)


def check_loop(driver, people, cfg, open_chat=None, read=None):
    """Open each chat and record replies. Returns counts plus 'stopped' and 'halted', as send.send_loop does."""
    out = cfg["outreach"]
    open_chat, read = open_chat or messenger.open_chat, read or messenger.thread
    lo, hi = out.get("reply_min_delay_seconds", 20), out.get("reply_max_delay_seconds", 60)
    counts = {"checked": 0, "replied": 0, "no_reply": 0, "unreadable": 0, "skipped": 0, "stopped": "", "halted": False}
    unreadable = 0
    for i, (akey, name, post_key, dm) in enumerate(people):
        if akey in store.replies():
            counts["skipped"] += 1
            continue
        if not dm:
            print(f"[=] {name or akey}: our DM's text isn't in leads.json; can't tell a reply from it")
            counts["skipped"] += 1
            continue
        url = store.messenger_url(akey)
        print(f"[+] {name or akey}")
        try:
            open_chat(driver, url, name or akey)
            reply = find_reply(read(driver, name or akey), dm)
        except AccountBlocked as exc:
            counts["stopped"], counts["halted"] = f"account-wide problem ({exc.kind}): {exc}", True
            break
        except (MessageRefused, NoSuchUser, LookupError, RuntimeError, TimeoutException) as exc:
            print(f"    [!] couldn't read the chat ({type(exc).__name__}: {exc}); trying again next run")
            counts["unreadable"] += 1
            unreadable += 1
            if unreadable >= MAX_UNREADABLE_IN_A_ROW:
                counts["stopped"], counts["halted"] = (f"{unreadable} chats in a row couldn't be read; check the "
                                                       f"account and messenger.py's rows reader"), True
                break
        else:
            unreadable = 0
            counts["checked"] += 1
            if reply is None:
                counts["no_reply"] += 1
                print("    no reply yet")
            else:
                store.record_reply(akey, name, post_key, reply[:SNIPPET], url)
                counts["replied"] += 1
                print(f"    REPLIED: {reply[:80]!r}")
        if i + 1 < len(people):
            time.sleep(random.uniform(lo, hi))
    return counts


def report(counts):
    print(f"\n[done] checked {counts['checked']} chats: {counts['replied']} replied, {counts['no_reply']} no reply yet, "
          f"{counts['unreadable']} couldn't be read; {len(store.replies())} replies in data/replies.csv")
    if counts["stopped"]:
        print(f"[stopped] {counts['stopped']}")
    return 2 if counts["halted"] else 0


def run(cfg):
    people = to_check(cfg)[:cfg["outreach"].get("replies_per_run", PER_RUN)]
    if not people:
        print("[+] No DMed chats to check.")
        return 0
    print(f"[+] checking {len(people)} chats for replies")
    with store.lock():
        driver = browser.make_driver(cfg)
        try:
            send.prepare(driver, cfg)
            return report(check_loop(driver, people, cfg))
        except KeyboardInterrupt:
            print("\n[+] Interrupted. Replies found so far are in data/replies.csv.")
            return 0
        finally:
            driver.quit()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", action="store_true", dest="list_only")
    args = ap.parse_args()
    cfg = json.loads(CONFIG.read_text())
    if args.list_only:
        for r in store.replies().values():
            print(f"{r['seen_at']}  {r['author'] or r['author_key']:<30} {r['snippet'][:80]!r}  {r['chat_url']}")
        print(f"{len(store.replies())} replies; {len(to_check(cfg))} DMed chats not replied yet")
    else:
        sys.exit(run(cfg))


if __name__ == "__main__":
    browser.halting(main)
