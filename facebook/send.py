"""send.py - send DMs to the leads qualify.py found, the way run_loop.py sends the Reddit queue (run_loop.py and
ledger.py on master): no approving, no pressing Send yourself.

With outreach.auto_approve (the default), every new lead at or above ai.min_score is approved with its drafted DM
(or your edit of it, if you edited it in the queue) at the start of each run, then sent, best score first; leads
you skipped, unapproved or marked in the queue are left alone. Send is pressed for you. With auto_approve false,
only leads you clicked "Approve for sending" on are sent. --by-hand opens each chat with the message typed in and
waits for you to press Enter in Messenger instead.

Each run: takes a lock (one run at a time), checks the browser is logged in as config.json's
outreach.sender_user_id, then for each approved lead opens their chat, checks it is with them and doesn't
already hold this message, types it, sends it once, and records 'sent' only once the message is in the chat
after reloading it. A send that can't be confirmed is recorded 'uncertain' and held until you resolve it.
The run stops (exit code 2) on anything account-wide (logged out, blocked, checkpoint, chat PIN) and on streaks
of failures, refusals or uncertain sends. outreach.daily_dm_limit counts every DM sent today, from here or
marked sent by hand in the queue, and authors the AI thinks are under 18 are never sent to unless
outreach.minor_policy is "allow".

    python send.py                  approve new leads and press Send for each, on its own
    python send.py --by-hand        you press Send in each chat
    python send.py --list           the approved leads and why any would be skipped (no browser)
    python send.py held             leads whose last send couldn't be confirmed
    python send.py resolve <author_key> --sent | --not-sent
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
import queue_server
import store
from queue_server import dm_allowed
from send_dm import ACCOUNT_WIDE, AccountBlocked, Result

HERE = Path(__file__).resolve().parent
CONFIG = HERE / "config.json"
MAX_FAILS_IN_A_ROW = 3      # not_submitted in a row: usually the account or the page, not the people
MAX_REFUSALS_IN_A_ROW = 3   # refusals from several unrelated people in a row
MAX_UNCERTAIN_IN_A_ROW = 2  # sends that can't be confirmed in a row: stop before making it worse
STREAKS = {
    "not_submitted": (MAX_FAILS_IN_A_ROW, "failures before sending in a row with no person-specific reason; "
                                          "check the account and the saved step*_failed files"),
    "refused": (MAX_REFUSALS_IN_A_ROW, "different people refused in a row. Facebook doesn't say why; refusals "
                                       "across unrelated people fit a restriction on this account better than "
                                       "each one's settings, but don't prove it"),
    "uncertain": (MAX_UNCERTAIN_IN_A_ROW, "sends in a row couldn't be confirmed in the chat. Check them "
                                          "(python send.py held) before sending more"),
}
NOTES = {
    "refused": "Facebook won't let this account message them, without saying why; may retry later",
    "not_submitted": "failed before Send was pressed; will retry on a later run",
    "uncertain": "Send was pressed but the message couldn't be confirmed; HELD until you check it",
    "recipient": "Facebook says this person isn't available; won't retry",
    "invalid": "bad entry; not sent",
}


def load_config():
    return json.loads(CONFIG.read_text())


def approved_leads():
    """Every lead whose last queue action is an approval that hasn't gone out yet, best score first."""
    handled = store.handled_posts()
    leads = [l for l in store.load_leads().values() if handled.get(l["post_key"]) in store.WAITING]
    return sorted(leads, key=lambda l: -l.get("ai", {}).get("score", 0))


def why_not(lead, cfg, max_attempts):
    """Why this lead must not be sent now (checked again right before each send), or None."""
    if store.handled_posts().get(lead["post_key"]) not in store.WAITING:
        return "no longer approved in the queue"
    if not lead.get("approved", {}).get("dm", "").strip():
        return "no approved message"
    if not lead.get("author_key"):
        return "no profile link to message them at"
    if not dm_allowed(lead, cfg):
        return "the AI thinks the author is under 18 and outreach.minor_policy doesn't allow a DM"
    return store.skip_reason(lead["author_key"], max_attempts)


def target(lead):
    return store.messenger_url(lead["author_key"]), lead["approved"]["dm"], lead.get("author") or lead["author_key"]


def prepare(driver, cfg, interactive=False):
    """Start of every run, before any chat is opened: confirm the browser is logged in as
    outreach.sender_user_id and Messenger is usable. Exits on any problem. Returns the sender's id."""
    browser.ensure_logged_in(driver, cfg, interactive=interactive)
    me = (driver.get_cookie("c_user") or {}).get("value")
    expected = str(cfg["outreach"].get("sender_user_id") or "").strip()
    if not me:
        raise SystemExit("Facebook didn't say which account is logged in (no c_user cookie). Log in again: "
                         "python run.py login")
    if not expected:
        raise SystemExit(f'config.json has no outreach.sender_user_id. This profile is logged in as Facebook user '
                         f'{me}; if that is the account that should send, set  "sender_user_id": "{me}"  and run again.')
    if me != expected:
        raise SystemExit(f"Logged in as Facebook user {me}, but config.json's outreach.sender_user_id is {expected}; "
                         f"not sending.")
    driver.get(messenger.INBOX)
    browser.pause(cfg, 0.6)
    if messenger.js(driver, "notice", messenger.PIN):
        if not interactive:
            raise SystemExit("Messenger is asking for this account's chat PIN (end-to-end encrypted chats). Enter "
                             "it once with python send.py --by-hand, then run again.")
        print("[!] Messenger is asking for this account's chat PIN (end-to-end encrypted chats).")
        input("    Enter it in the Chrome window, then press Enter here... ")
    try:
        messenger.check_account(driver)
    except AccountBlocked as exc:
        raise SystemExit(f"Not sending ({exc.kind}): {exc}")
    print(f"[+] sending as Facebook user {me}")
    return me


def new_counts():
    counts = {s: 0 for s in ("sent", "recipient", "refused", "invalid", "not_submitted", "uncertain", "skipped")}
    counts.update(stopped="", halted=False)
    return counts


def record(lead, result):
    store.record(lead["author_key"], lead.get("author", ""), lead["post_key"], result.status, result.reason)


def send_loop(driver, leads, cfg, send=messenger.send_message):
    """Send each eligible lead within the daily cap and delays. Returns a count per status for this run, plus
    'stopped' (why it ended early, '' if it got through the list) and 'halted' (True if it stopped for anything
    other than the daily cap)."""
    out = cfg["outreach"]
    cap, max_attempts = out["daily_dm_limit"], out.get("max_attempts_per_recipient", store.MAX_ATTEMPTS)
    counts, streak = new_counts(), {"status": None, "n": 0}
    for i, lead in enumerate(leads):
        done_today = store.sent_today()
        if done_today >= cap:
            counts["stopped"] = f"hit the daily cap ({done_today}/{cap} sent today)"
            break
        why = why_not(lead, cfg, max_attempts)
        if why:
            print(f"[=] skipping {lead.get('author') or lead['post_key']}: {why}")
            counts["skipped"] += 1
            continue

        url, text, name = target(lead)
        print(f"\n[+] {name} ({done_today + 1}/{cap} today)")
        submitted = []

        def before_submit():
            record(lead, Result("submitting", "about to press Send"))
            submitted.append(True)

        try:
            result = send(driver, url, text, name, before_submit=before_submit)
        except Exception as exc:                          # a bug or a dead browser, not a Facebook answer
            result = Result("uncertain" if submitted else "not_submitted", f"{type(exc).__name__}: {exc}")
        if result.status != "invalid" or submitted:       # invalid entries never reached Facebook: nothing to keep
            record(lead, result)

        if result.status in ACCOUNT_WIDE:
            counts["stopped"], counts["halted"] = f"account-wide problem ({result.status}): {result.reason}", True
            break
        counts[result.status] += 1
        if result.status != "sent":
            print(f"    [!] {NOTES.get(result.status, result.status)}: {result.reason}")
        if result.status == "invalid":
            continue
        streak = ({"status": result.status, "n": streak["n"] + 1} if result.status == streak["status"]
                  else {"status": result.status, "n": 1})
        limit, why = STREAKS.get(result.status, (None, ""))
        if limit and streak["n"] >= limit:
            counts["stopped"], counts["halted"] = f"{streak['n']} {why}", True
            break

        if i + 1 < len(leads) and store.sent_today() < cap:
            delay = random.uniform(out["min_delay_seconds"], out["max_delay_seconds"])
            print(f"[+] Waiting {delay:.0f}s before the next one...")
            time.sleep(delay)
    return counts


def walk_by_hand(driver, leads, cfg):
    """Open each eligible lead's chat with the message typed in and wait for you. Same counts as send_loop."""
    out = cfg["outreach"]
    cap, max_attempts = out["daily_dm_limit"], out.get("max_attempts_per_recipient", store.MAX_ATTEMPTS)
    counts, uncertain_in_a_row = new_counts(), 0

    def stop(why, halted=True):
        counts["stopped"], counts["halted"] = why, halted

    for n, lead in enumerate(leads, 1):
        why = why_not(lead, cfg, max_attempts)
        if why:
            print(f"[=] skipping {lead.get('author') or lead['post_key']}: {why}")
            counts["skipped"] += 1
            continue
        done_today = store.sent_today()
        if done_today >= cap:
            stop(f"hit the daily cap ({done_today}/{cap} sent today)", halted=False)
            break

        url, text, name = target(lead)
        print(f"\n[{n}/{len(leads)}] {name} ({done_today + 1}/{cap} today), score {lead['ai']['score']}")
        print(f"    {len(text)} chars")
        said = messenger.open_for_hand(driver, url, text, name)
        while said is None:
            print("    → The chat is open with the approved message typed in.")
            print("    → Read it, and press Enter in Messenger if you want to send it. Then come back here.")
            choice = input("    [s]ent  [k]skip for now  [o]pen-again  [q]uit  > ").strip().lower()
            if choice != "o":
                break
            said = messenger.open_for_hand(driver, url, text, name)

        if said is not None:
            result = said
            print("    Facebook answered before anything was sent:" if said.status != "sent" else
                  "    This message is already in the chat:")
        elif choice == "q":
            stop("you quit", halted=False)
            break
        elif choice == "k":
            counts["skipped"] += 1
            print("    skipped for this run; it stays approved")
            continue
        else:                                            # anything else: "I pressed Send"
            result = messenger.confirm_by_hand(driver, url, text, name)

        record(lead, result)
        if result.status in ACCOUNT_WIDE:
            stop(f"account-wide problem ({result.status}): {result.reason}")
            break
        counts[result.status] += 1
        if result.status == "sent":
            print("    confirmed in the chat")
        else:
            print(f"    [!] {NOTES.get(result.status, result.status)}: {result.reason}")
        uncertain_in_a_row = uncertain_in_a_row + 1 if result.status == "uncertain" else 0
        if uncertain_in_a_row >= MAX_UNCERTAIN_IN_A_ROW:
            stop(f"{uncertain_in_a_row} {STREAKS['uncertain'][1]}")
            break
    return counts


def report(counts):
    """Print a run summary; returns the exit code (2 if the run halted on an account or streak problem)."""
    print(f"\n[done] confirmed sent {counts['sent']}, uncertain {counts['uncertain']}, refused {counts['refused']}, "
          f"unavailable {counts['recipient']}, failed before sending {counts['not_submitted']}, "
          f"bad entries {counts['invalid']}, skipped {counts['skipped']}; {store.sent_today()} sent today in total")
    if counts["stopped"]:
        print(f"[stopped] {counts['stopped']}")
    waiting = store.held()
    if waiting:
        print(f"[!] {len(waiting)} lead(s) HELD with an uncertain outcome: python send.py held")
    return 2 if counts["halted"] else 0


def print_list(leads, cfg):
    max_attempts = cfg["outreach"].get("max_attempts_per_recipient", store.MAX_ATTEMPTS)
    for n, lead in enumerate(leads, 1):
        why = why_not(lead, cfg, max_attempts)
        print(f"  {n:>3}. {lead.get('author') or '?':<30} score {lead.get('ai', {}).get('score', '?'):>3}  "
              f"{len(lead.get('approved', {}).get('dm', ''))} chars" + (f"  [skip: {why}]" if why else ""))
    print(f"{len(leads)} approved; {store.sent_today()}/{cfg['outreach']['daily_dm_limit']} DMs sent today")


def run(cfg, by_hand=False):
    if cfg["outreach"].get("auto_approve", True):
        n = queue_server.auto_approve()
        print(f"[+] auto-approved {n} new leads with their drafted DMs (outreach.auto_approve)")
    leads = approved_leads()
    if not leads:
        print("[+] Nothing approved to send.")
        return 0
    print(f"[+] {len(leads)} approved leads")
    with store.lock():
        cap = cfg["outreach"]["daily_dm_limit"]
        if store.sent_today() >= cap:
            raise SystemExit(f"Already sent {store.sent_today()}/{cap} today. Nothing to do until tomorrow.")
        driver = browser.make_driver(cfg)
        try:
            prepare(driver, cfg, interactive=by_hand)
            if not by_hand:
                return report(send_loop(driver, leads, cfg))
            code = report(walk_by_hand(driver, leads, cfg))
            input("Press Enter to close Chrome... ")
            return code
        except KeyboardInterrupt:
            print("\n[+] Interrupted. Everything recorded so far is in data/contacted.csv.")
            return 0
        finally:
            driver.quit()

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", nargs="?", default="run", choices=["run", "held", "resolve"])
    ap.add_argument("author_key", nargs="?", help="for resolve: the author_key from python send.py held")
    ap.add_argument("--by-hand", action="store_true", help="open each chat and wait for you to press Send")
    ap.add_argument("--list", action="store_true", dest="list_only")
    which = ap.add_mutually_exclusive_group()
    which.add_argument("--sent", action="store_true", help="resolve: it did go out, never message them again")
    which.add_argument("--not-sent", action="store_true", help="resolve: it didn't go out, eligible again")
    args = ap.parse_args()
    cfg = load_config()

    if args.cmd == "held":
        waiting = store.held()
        for akey, r in waiting.items():
            print(f"{akey}  {r['author']}  {r['status']}  {r['timestamp']}  {r['note']}")
        print(f"{len(waiting)} held. Check each chat ({store.messenger_url('<author_key>')}), then "
              f"python send.py resolve <author_key> --sent | --not-sent" if waiting else "Nothing held.")
    elif args.cmd == "resolve":
        if not args.author_key or not (args.sent or args.not_sent):
            ap.error("resolve needs an author_key and --sent or --not-sent")
        store.resolve(args.author_key, args.sent)
    elif args.list_only:
        print_list(approved_leads(), cfg)
    else:
        sys.exit(run(cfg, args.by_hand))


if __name__ == "__main__":
    main()
