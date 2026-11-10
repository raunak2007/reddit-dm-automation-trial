"""replies.py - find who wrote back after our DM, and list them in data/replies.csv. Nothing is typed or sent.

    python replies.py                 check the chats (python run.py replies)
    python replies.py --list          who has replied so far (no browser)

For everyone in data/contacted.csv with a 'sent' DM who isn't in data/replies.csv yet, most recent DM first and at
most outreach.replies_per_run chats a run: opens their chat (messenger.open_chat: the same checks as a send, that
it is with them and Facebook isn't refusing this account), reads its messages (messenger.thread), finds our DM in it
(the text in leads.json: the approved message, else your edit, else the draft), and counts as a reply any message
*from them* after it. Theirs vs ours comes from each row's screen-reader label ("You sent" / their name) and, without
one, from which side its text sits on; rows that can't be told apart never count, and nor do their messages from
before our DM. A reply is recorded once: author_key, author, post_key, seen_at, a short snippet, the full text of
their message(s) after our DM (up to store.REPLY_TEXT_MAX characters) and the chat URL.
If our DM can't be found in the chat (not loaded, edited, deleted), that chat is left for the next run.

Everyone already in data/replies.csv is checked again on later runs for anything new, unless their latest inbox
entry (../inbox/store.py) is dismissed or an answer to them is sending / uncertain: each message from them after our
latest message in the chat (our DM, a follow-up, an answer, or anything we sent by hand) becomes its own row, with a
seen_at unique to that person (the inbox keys Facebook replies on author_key:seen_at, and inbox/draft.py folds a new
row into a fresh draft, replacing an untouched one). Messages already in data/replies.csv since our latest message
aren't recorded again, and our own messages never count. The chat is only trusted if one of our messages to them (the
DM, a follow-up or an answer) is in it. Order within the per-run cap: people with no answer sent yet (a draft waiting,
or none), most recent reply first; then people we've answered (those whose last reply is older than our latest answer
first); then DMed chats never checked.

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
from inbox import store as inbox
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


def to_recheck(cfg):
    """(waiting_draft, answered): the people whose chat is read again for further messages, as
    (author_key, author, post_key, dm text, since, our texts). since: when we last wrote to them that we know of
    (new messages are looked for after our latest message in the chat; replies.csv rows from since on are already
    recorded); our texts: what we sent them (one must be in the chat as ours to trust it).
      waiting_draft  in replies.csv with no answer sent yet: their inbox entry is drafted / approved / failed, or
                     there's none. since = our DM (or follow-up). Most recent reply first.
      answered       an answer was sent (inbox status 'sent'). since = the latest answer. People whose last reply is
                     older than that answer first, then the rest, most recent answer first.
    People whose latest entry is dismissed, or with an answer that's sending / uncertain, are left alone."""
    leads, hist, fus = store.load_leads(), store.history(), store.followups()
    entries = {}
    for a in inbox.load().values():
        if a["platform"] == "facebook" and a.get("person"):
            entries.setdefault(a["person"], []).append(a)
    replied = {r["author_key"] for r in store.reply_rows()}
    drafts, answered = [], []
    for akey in replied | set(entries):
        mine = sorted(entries.get(akey, []), key=lambda a: a.get("created_at") or "")
        sent = sorted((a for a in mine if a["status"] == "sent"), key=lambda a: a.get("sent_at") or "")
        dms = [r for r in hist.get(akey, []) if r["status"] == "sent"]
        lead = leads.get(dms[-1]["post_key"], {}) if dms else {}
        post_key = dms[-1]["post_key"] if dms else ""
        name = lead.get("author") or (mine[-1]["name"] if mine else "") or (dms[-1]["author"] if dms else "") or akey
        last_reply = max((r["seen_at"] for r in store.reply_rows(akey)), default="")
        if sent:
            since = sent[-1].get("sent_at") or sent[-1].get("created_at") or ""
            ours = [t for t in [our_dm(lead)] + [(a.get("text") or "").strip() for a in sent] if t]
            answered.append((last_reply < since, since, (akey, name, post_key, our_dm(lead), since, ours)))
            continue
        if akey not in replied or any(a["status"] in ("sending", "uncertain") for a in mine) or \
                (mine and mine[-1]["status"] == "dismissed") or not dms:
            continue
        ups = [r for r in fus.get(akey, []) if r["status"] in store.FOLLOWUP_COUNTED]
        since = max([dms[-1]["timestamp"]] + [r["timestamp"] for r in ups])
        ours = [t for t in [our_dm(lead)] + [r["note"] for r in ups if r["status"] == "followup_submitting"] if t]
        drafts.append((last_reply, (akey, name, post_key, our_dm(lead), since, ours)))
    drafts.sort(key=lambda t: t[0], reverse=True)
    answered.sort(key=lambda t: t[1], reverse=True)      # most recent answer first...
    answered.sort(key=lambda t: not t[0])                 # ...waiting on them before the rest (stable)
    return [t[-1] for t in drafts], [t[-1] for t in answered]


def plan(cfg):
    """Every chat to open this run, before the per-run cap: people with an answer still to send (a waiting draft,
    or none yet), then people we've answered, then DMed chats never checked."""
    drafts, answered = to_recheck(cfg)
    seen, out = set(), []
    for p in drafts + answered + to_check(cfg):
        if p[0] not in seen:
            seen.add(p[0])
            out.append(p)
    return out


def new_messages(rows, ours, recorded):
    """Their messages after our latest message in the chat that aren't recorded yet, oldest first. rows: from
    messenger.thread; ours: texts we sent them (one must be in the chat as ours, else LookupError); recorded: texts
    of their rows in replies.csv since our latest answer, oldest first (matched in order, so a repeated 'ok' after
    a recorded 'ok' still counts as new)."""
    wants = [messenger.squash(t) for t in ours if messenger.squash(t)]

    def is_ours(r):
        have = messenger.squash(r.get("text"))
        return r.get("who") == "me" and have and any(w in have or (len(have) >= 20 and have in w) for w in wants)

    if not any(is_ours(r) for r in rows):
        raise LookupError("none of our messages to them is in the chat as a message from us")
    last_me = max(i for i, r in enumerate(rows) if r.get("who") == "me")
    theirs = [(r.get("text") or "").strip() or "[attachment]" for r in rows[last_me + 1:] if r.get("who") == "them"]
    done, at, new = messenger.squash("".join(recorded)), 0, []
    for text in theirs:
        piece = messenger.squash(text)
        hit = done.find(piece, at) if not new and piece else -1
        if hit >= 0:
            at = hit + len(piece)
        else:
            new.append(text)
    return new


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


def record(akey, name, post_key, reply, url):
    """Write what find_reply() returned to data/replies.csv (followup.py uses this too). True if a row was added."""
    return store.record_reply(akey, name, post_key, reply[:SNIPPET], url, text=reply[:store.REPLY_TEXT_MAX])


def check_loop(driver, people, cfg, open_chat=None, read=None):
    """Open each chat and record replies. Returns counts plus 'stopped' and 'halted', as send.send_loop does."""
    out = cfg["outreach"]
    open_chat, read = open_chat or messenger.open_chat, read or messenger.thread
    lo, hi = out.get("reply_min_delay_seconds", 20), out.get("reply_max_delay_seconds", 60)
    counts = {"checked": 0, "replied": 0, "no_reply": 0, "unreadable": 0, "skipped": 0, "new_messages": 0,
              "stopped": "", "halted": False}
    unreadable = 0
    for i, (akey, name, post_key, dm, *again) in enumerate(people):
        if again:
            got = recheck_one(driver, (akey, name, post_key, dm, *again), counts, open_chat, read)
            if got == "halt":
                break
            unreadable = 0 if got == "ok" else unreadable + 1
            if unreadable >= MAX_UNREADABLE_IN_A_ROW:
                counts["stopped"], counts["halted"] = (f"{unreadable} chats in a row couldn't be read; check the "
                                                       f"account and messenger.py's rows reader"), True
                break
            if i + 1 < len(people):
                time.sleep(random.uniform(lo, hi))
            continue
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
                record(akey, name, post_key, reply, url)
                counts["replied"] += 1
                print(f"    REPLIED: {reply[:80]!r}")
        if i + 1 < len(people):
            time.sleep(random.uniform(lo, hi))
    return counts


def recheck_one(driver, person, counts, open_chat, read):
    """Re-read one answered person's chat and record each new message from them. 'halt' on an account-wide stop."""
    akey, name, post_key, dm, since, ours = person
    url = store.messenger_url(akey)
    print(f"[+] {name or akey} (looking for anything new since {since})")
    try:
        open_chat(driver, url, name or akey)
        recorded = [r["text"] for r in store.reply_rows(akey) if r["seen_at"] >= since]
        new = new_messages(read(driver, name or akey), ours, recorded)
    except AccountBlocked as exc:
        counts["stopped"], counts["halted"] = f"account-wide problem ({exc.kind}): {exc}", True
        return "halt"
    except (MessageRefused, NoSuchUser, LookupError, RuntimeError, TimeoutException) as exc:
        print(f"    [!] couldn't read the chat ({type(exc).__name__}: {exc}); trying again next run")
        counts["unreadable"] += 1
        return "unreadable"
    counts["checked"] += 1
    for text in new:
        store.add_reply(akey, name, post_key, text, url)
        print(f"    NEW MESSAGE: {text[:80]!r}")
    counts["new_messages"] += len(new)
    if not new:
        counts["no_reply"] += 1
        print("    nothing new")
    return "ok"


def report(counts):
    print(f"\n[done] checked {counts['checked']} chats: {counts['replied']} replied, {counts['no_reply']} no reply yet, "
          f"{counts['unreadable']} couldn't be read, {counts.get('new_messages', 0)} new messages after our answers; "
          f"{len(store.replies())} people in data/replies.csv")
    if counts["stopped"]:
        print(f"[stopped] {counts['stopped']}")
    return 2 if counts["halted"] else 0


def run(cfg):
    people = plan(cfg)[:cfg["outreach"].get("replies_per_run", PER_RUN)]
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
