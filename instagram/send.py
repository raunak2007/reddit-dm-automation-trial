"""send.py - send DMs to the commenters qualify.py picked, the way run_loop.py sends the Reddit queue (run_loop.py
and ledger.py on master): no approving, no pressing Send, no following by hand.

With outreach.auto_approve (the default), every new lead at or above ai.min_score is approved with its drafted DM
(or your edit of it, if you edited it on the review page) at the start of each run, then sent, best score first;
leads you skipped, unapproved or marked are left alone. Send is pressed for you. With auto_approve false, only
commenters you clicked "Approve for sending" on are sent. --by-hand opens each chat with the message typed in and
waits for you to press Enter in Instagram instead. With outreach.auto_follow (the default), each commenter is
followed from their profile right before their DM (skipped if this account already follows them or has asked to);
a follow that fails is noted and the DM goes ahead.

Each run: takes a lock (one run at a time), checks the browser is logged in as config.json's
outreach.sender_user_id, then for each approved commenter opens their chat, checks it is with them and doesn't
already hold this message, types it, sends it once, and records 'sent' only once the message is in the chat
after reloading it. A send that can't be confirmed is recorded 'uncertain' and held; at the start of the next
run (outreach.auto_resolve, on by default) each held person's chat is reloaded: if the message is in it, it is
recorded 'sent'; if the chat loaded and the message isn't there, 'cleared', and that run sends it again; if the
check can't tell, it stays held.
The run stops (exit code 2) on anything account-wide (logged out, action blocked, challenge, locked) and on
streaks of failures, refusals or uncertain sends. outreach.daily_dm_limit counts every DM sent today, from here
or marked sent by hand, and commenters the AI thinks are under 18 are never sent to unless
outreach.minor_policy is "allow".

    python send.py                  approve new leads and press Send for each, on its own
    python send.py --by-hand        you press Send in each chat
    python send.py --list           the approved commenters and why any would be skipped (no browser)
    python send.py held             people whose last send couldn't be confirmed (checked on the next run)
    python send.py resolve <username> --sent | --not-sent      settle one by hand instead
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import time
from pathlib import Path

import browser
import direct
import queue_server
import store
from send_dm import ACCOUNT_WIDE, AccountBlocked, Result

HERE = Path(__file__).resolve().parent
CONFIG = HERE / "config.json"
MAX_FAILS_IN_A_ROW = 3      # not_submitted in a row: usually the account or the page, not the people
MAX_REFUSALS_IN_A_ROW = 3   # refusals from several unrelated people in a row
MAX_UNCERTAIN_IN_A_ROW = 2  # sends that can't be confirmed in a row: stop before making it worse
STREAKS = {
    "not_submitted": (MAX_FAILS_IN_A_ROW, "failures before sending in a row with no person-specific reason; "
                                          "check the account and the saved step*_failed files"),
    "refused": (MAX_REFUSALS_IN_A_ROW, "different people refused in a row. Instagram doesn't say why; refusals "
                                       "across unrelated people fit a restriction on this account better than "
                                       "each one's settings, but don't prove it"),
    "uncertain": (MAX_UNCERTAIN_IN_A_ROW, "sends in a row couldn't be confirmed in the chat; the next run "
                                          "checks them again before sending more (python send.py held)"),
}
NOTES = {
    "refused": "Instagram won't let this account message them, without saying why; may retry later",
    "not_submitted": "failed before Send was pressed; will retry on a later run",
    "uncertain": "Send was pressed but the message couldn't be confirmed; HELD until the next run checks the chat",
    "recipient": "Instagram says there is no such account; won't retry",
    "invalid": "bad entry; not sent",
}


def load_config():
    return json.loads(CONFIG.read_text())


def dm_allowed(lead, cfg):
    return not lead["ai"]["likely_minor"] or cfg["outreach"]["minor_policy"] == "allow"


def approved_leads():
    """Every lead whose last action is an approval that hasn't gone out yet, best score first."""
    done = store.handled()
    leads = [l for l in store.load_leads().values() if done.get(l["key"]) in store.WAITING]
    return sorted(leads, key=lambda l: -l.get("ai", {}).get("score", 0))


def why_not(lead, cfg, max_attempts):
    """Why this lead must not be sent now (checked again right before each send), or None."""
    if store.handled().get(lead["key"]) not in store.WAITING:
        return "no longer approved on the review page"
    if not lead.get("approved", {}).get("dm", "").strip():
        return "no approved message"
    if "ai" not in lead or not dm_allowed(lead, cfg):
        return "the AI thinks they're under 18 and outreach.minor_policy doesn't allow a DM"
    return store.skip_reason(lead["username"], max_attempts)


def prepare(driver, cfg, interactive=False):
    """Start of every run, before any chat is opened: confirm the browser is logged in as
    outreach.sender_user_id and the inbox is usable. Exits on any problem. Returns the sender's id."""
    browser.ensure_logged_in(driver, cfg, interactive=interactive)
    me = (driver.get_cookie("ds_user_id") or {}).get("value")
    expected = str(cfg["outreach"].get("sender_user_id") or "").strip()
    if not me:
        raise SystemExit("Instagram didn't say which account is logged in (no ds_user_id cookie). Log in again: "
                         "python run.py login")
    if not expected:
        raise SystemExit(f'config.json has no outreach.sender_user_id. This profile is logged in as Instagram user '
                         f'{me}; if that is @{cfg["instagram"]["my_username"]}, the account that should send, set  '
                         f'"sender_user_id": "{me}"  and run again.')
    if me != expected:
        raise SystemExit(f"Logged in as Instagram user {me}, but config.json's outreach.sender_user_id is {expected}; "
                         f"not sending.")
    driver.get(direct.INBOX)
    browser.pause(cfg, 0.6)
    direct.dismiss_dialogs(driver)
    try:
        direct.check_account(driver)
    except AccountBlocked as exc:
        raise SystemExit(f"Not sending ({exc.kind}): {exc}")
    print(f"[+] sending as Instagram user {me} (@{cfg['instagram']['my_username']})")
    return me


def resolve_held(driver):
    """Start of a run (outreach.auto_resolve): check the chat of everyone held. Message in the chat -> 'sent';
    the chat loaded without it -> 'cleared', so this run sends it again; anything else -> still held."""
    leads = store.load_leads()
    for user, row in store.held().items():
        lead = leads.get(row["lead_key"])
        if not lead or not lead.get("approved", {}).get("dm", "").strip():
            print(f"[=] @{user} is held, but the approved message isn't in leads.json; check it by hand: "
                  f"python send.py resolve {user} --sent | --not-sent")
            continue
        print(f"[+] checking the held send to @{user}")
        try:
            sent = direct.was_sent(driver, direct.chat_link(user), lead["approved"]["dm"], user,
                                   lead.get("full_name", ""))
        except AccountBlocked as exc:
            print(f"    [!] stopped checking held sends ({exc.kind}): {exc}")
            return
        except Exception as exc:
            print(f"    [!] couldn't tell ({type(exc).__name__}: {exc}); still held")
            continue
        store.resolve(user, sent, how="automatically")


def follow_first(driver, user, cfg):
    """outreach.auto_follow: follow them right before the DM. Returns a Result only if Instagram is refusing
    this account (the run stops); any other failure to follow is noted and the DM goes ahead."""
    try:
        done = direct.follow(driver, user)
        print(f"    {'followed' if done == 'followed' else 'already following'} @{user}")
        browser.pause(cfg, 0.3)
    except AccountBlocked as exc:
        return Result(exc.kind, f"follow: {exc}")
    except Exception as exc:
        print(f"    [!] couldn't follow @{user} ({type(exc).__name__}: {exc}); sending the DM anyway")
    return None


def new_counts():
    counts = {s: 0 for s in ("sent", "recipient", "refused", "invalid", "not_submitted", "uncertain", "skipped")}
    counts.update(stopped="", halted=False)
    return counts


def record(lead, result):
    store.record(lead["username"], lead["key"], result.status, result.reason)


def send_loop(driver, leads, cfg, send=direct.send_message):
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
            print(f"[=] skipping @{lead['username']}: {why}")
            counts["skipped"] += 1
            continue

        user = lead["username"]
        print(f"\n[+] @{user} ({done_today + 1}/{cap} today)")
        submitted = []

        def before_submit():
            record(lead, Result("submitting", "about to press Send"))
            submitted.append(True)

        result = follow_first(driver, user, cfg) if out.get("auto_follow", True) else None
        if result is None:
            try:
                result = send(driver, direct.chat_link(user), lead["approved"]["dm"], user, lead.get("full_name", ""),
                              before_submit=before_submit)
            except Exception as exc:                      # a bug or a dead browser, not an Instagram answer
                result = Result("uncertain" if submitted else "not_submitted", f"{type(exc).__name__}: {exc}")
        if result.status != "invalid" or submitted:       # invalid entries never reached Instagram: nothing to keep
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
            print(f"[=] skipping @{lead['username']}: {why}")
            counts["skipped"] += 1
            continue
        done_today = store.sent_today()
        if done_today >= cap:
            stop(f"hit the daily cap ({done_today}/{cap} sent today)", halted=False)
            break

        user, text, name = lead["username"], lead["approved"]["dm"], lead.get("full_name", "")
        print(f"\n[{n}/{len(leads)}] @{user} ({done_today + 1}/{cap} today), score {lead['ai']['score']}")
        print(f"    {len(text)} chars; profile: https://www.instagram.com/{user}/")
        said = follow_first(driver, user, cfg) if out.get("auto_follow", True) else None
        chat_url = direct.chat_link(user)
        if said is None:
            said, chat_url = direct.open_for_hand(driver, direct.chat_link(user), text, user, name)
        while said is None:
            print("    → The chat is open with the approved message typed in.")
            print("    → Read it, and press Enter in Instagram if you want to send it. Then come back here.")
            choice = input("    [s]ent  [k]skip for now  [o]pen-again  [q]uit  > ").strip().lower()
            if choice != "o":
                break
            said, chat_url = direct.open_for_hand(driver, direct.chat_link(user), text, user, name)

        if said is not None:
            result = said
            print("    Instagram answered before anything was sent:" if said.status != "sent" else
                  "    This message is already in the chat:")
        elif choice == "q":
            stop("you quit", halted=False)
            break
        elif choice == "k":
            counts["skipped"] += 1
            print("    skipped for this run; it stays approved")
            continue
        else:                                            # anything else: "I pressed Send"
            result = direct.confirm_by_hand(driver, chat_url, text, user)

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
          f"no such account {counts['recipient']}, failed before sending {counts['not_submitted']}, "
          f"bad entries {counts['invalid']}, skipped {counts['skipped']}; {store.sent_today()} sent today in total")
    if counts["stopped"]:
        print(f"[stopped] {counts['stopped']}")
    waiting = store.held()
    if waiting:
        print(f"[!] {len(waiting)} people HELD with an uncertain outcome: python send.py held")
    return 2 if counts["halted"] else 0


def print_list(leads, cfg):
    max_attempts = cfg["outreach"].get("max_attempts_per_recipient", store.MAX_ATTEMPTS)
    for n, lead in enumerate(leads, 1):
        why = why_not(lead, cfg, max_attempts)
        print(f"  {n:>3}. @{lead['username']:<30} score {lead.get('ai', {}).get('score', '?'):>3}  "
              f"{len(lead.get('approved', {}).get('dm', ''))} chars" + (f"  [skip: {why}]" if why else ""))
    print(f"{len(leads)} approved; {store.sent_today()}/{cfg['outreach']['daily_dm_limit']} DMs sent today")


def run(cfg, by_hand=False):
    out = cfg["outreach"]
    if out.get("auto_approve", True):
        n = queue_server.auto_approve()
        print(f"[+] auto-approved {n} new commenters with their drafted DMs (outreach.auto_approve)")
    leads = approved_leads()
    resolving = out.get("auto_resolve", True) and bool(store.held())
    if not leads and not resolving:
        print("[+] Nothing approved to send.")
        return 0
    print(f"[+] {len(leads)} approved commenters" + (f", {len(store.held())} held to check" if resolving else ""))
    with store.lock():
        cap = out["daily_dm_limit"]
        if store.sent_today() >= cap and not resolving:
            raise SystemExit(f"Already sent {store.sent_today()}/{cap} today. Nothing to do until tomorrow.")
        driver = browser.make_driver(cfg)
        try:
            prepare(driver, cfg, interactive=by_hand)
            if resolving:
                resolve_held(driver)
                leads = approved_leads()
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
    ap.add_argument("username", nargs="?", help="for resolve: the username from python send.py held")
    ap.add_argument("--by-hand", action="store_true", help="open each chat and wait for you to press Send")
    ap.add_argument("--list", action="store_true", dest="list_only")
    which = ap.add_mutually_exclusive_group()
    which.add_argument("--sent", action="store_true", help="resolve: it did go out, never message them again")
    which.add_argument("--not-sent", action="store_true", help="resolve: it didn't go out, eligible again")
    args = ap.parse_args()
    cfg = load_config()

    if args.cmd == "held":
        waiting = store.held()
        for user, r in waiting.items():
            print(f"@{user}  {r['status']}  {r['timestamp']}  {r['note']}")
        print(f"{len(waiting)} held. The next python send.py checks each chat and settles them; to settle one by "
              f"hand: python send.py resolve <username> --sent | --not-sent" if waiting else "Nothing held.")
    elif args.cmd == "resolve":
        if not args.username or not (args.sent or args.not_sent):
            ap.error("resolve needs a username and --sent or --not-sent")
        store.resolve(args.username, args.sent)
    elif args.list_only:
        print_list(approved_leads(), cfg)
    else:
        sys.exit(run(cfg, args.by_hand))


if __name__ == "__main__":
    main()
