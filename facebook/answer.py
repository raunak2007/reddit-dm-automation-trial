"""answer.py - send the answers you approved on the inbox page (../inbox) into each person's Messenger chat.

    python answer.py            send every approved Facebook answer (python run.py answer)
    python answer.py --list     what's approved and waiting (no browser)

The shared record is ../inbox/store.py (inbox/data/answers.json): the inbox page drafts and approves, this sends.
Each run takes the same lock as send.py (they share the Chrome profile), then:
  1. inbox.stuck("facebook"): any entry a run left 'sending' (it died after marking it) becomes 'uncertain', so it
     is never sent again blind. This happens inside the lock, so a run still sending can't be mistaken for a dead one.
  2. checks the browser is logged in as outreach.sender_user_id and Messenger is usable (send.prepare).
  3. for each approved entry, oldest approval first, at most answers.max_per_run (10) a run: re-reads it (the page
     may have changed it), opens the chat (entry chat_url, else the person's Messenger link) with
     messenger.send_message, which checks the chat is with entry["name"], that Facebook isn't refusing this account
     or this person, and that the text isn't already in the chat (then it's 'sent'); types entry["text"], marks the
     entry 'sending' right before pressing Send once, and records the outcome with the reason:
         sent       Messenger took it (or it was already in the chat)
         failed     not sent (refused, unavailable, a page that didn't load, a bad entry): approve it again to retry
         uncertain  Send was pressed but it couldn't be confirmed (or Facebook showed a block after the press):
                    look in the chat yourself before doing anything else with it
     An account-wide problem before Send was pressed (logged out, chat PIN, checkpoint, block) leaves the entry
     'approved' with the reason, for the next run, and stops this one with exit code 2.
It waits answers.min_delay_seconds to max_delay_seconds (default outreach's min/max_delay_seconds) between sends and
stops (exit 2) after MAX_FAILS_IN_A_ROW failures or MAX_UNCERTAIN_IN_A_ROW unconfirmed sends in a row. Answers don't
count toward outreach.daily_dm_limit: they go to people who wrote to us.
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import time
from pathlib import Path

import browser  # noqa: F401  (puts the repo root, with send_dm.py and inbox/, on sys.path)
import messenger
import send
import store
from inbox import store as inbox
from send_dm import ACCOUNT_WIDE, Result

HERE = Path(__file__).resolve().parent
CONFIG = HERE / "config.json"
PLATFORM = "facebook"
PER_RUN = 10
MAX_FAILS_IN_A_ROW = 3
MAX_UNCERTAIN_IN_A_ROW = 2


def load_config():
    return json.loads(CONFIG.read_text())


def settings(cfg):
    a, out = cfg.get("answers", {}), cfg["outreach"]
    return {"max_per_run": a.get("max_per_run", PER_RUN),
            "lo": a.get("min_delay_seconds", out["min_delay_seconds"]),
            "hi": a.get("max_delay_seconds", out["max_delay_seconds"])}


def chat_url(entry):
    url = (entry.get("chat_url") or "").strip()
    return url if messenger.CHAT_URL.match(url) else store.messenger_url(entry["person"])


def outcome(result, pressed):
    """(inbox status, reason) for a send_message Result. pressed: 'sending' was written, so Send was (about to be)
    pressed. None for an account-wide stop before Send: the entry stays approved."""
    if result.status == "sent":
        return "sent", result.reason
    if pressed and result.status != "not_submitted":
        return "uncertain", f"{result.status}: {result.reason}"
    if result.status in ACCOUNT_WIDE:
        return None
    return "failed", f"{result.status}: {result.reason}"


def settle(aid, expect, **fields):
    """inbox.update, unless the page changed the entry first (then it is left as the page set it). True if written."""
    try:
        inbox.update(aid, expect=expect, **fields)
        return True
    except ValueError as exc:
        print(f"    [!] not recorded: {exc}")
        return False


def new_counts():
    return {"sent": 0, "failed": 0, "uncertain": 0, "skipped": 0, "stopped": "", "halted": False}


def answer_loop(driver, entries, cfg, send_fn=None):
    """Send each approved entry. Returns counts plus 'stopped' and 'halted', as send.send_loop does."""
    s = settings(cfg)
    verify = cfg["outreach"].get("verify_after_send", False)
    send_fn = send_fn or (lambda *a, **k: messenger.send_message(*a, verify=verify, **k))
    counts, fails, unsure = new_counts(), 0, 0
    for i, entry in enumerate(entries):
        aid = entry["id"]
        now = inbox.get(aid)
        if not now or now["status"] != "approved" or now["platform"] != PLATFORM:
            print(f"[=] skipping {aid}: no longer approved ({now and now['status']})")
            counts["skipped"] += 1
            continue
        text, name = (now.get("text") or "").strip(), (now.get("name") or "").strip()
        url = chat_url(now)
        if not text or not name:
            settle(aid, ("approved",), status="failed",
                   reason="not sent: the entry has no text or no name to check the chat against")
            counts["failed"] += 1
            continue
        print(f"\n[+] answering {name} ({i + 1}/{len(entries)})")
        pressed, changed = [], []

        def before_submit():
            cur = inbox.get(aid) or {}
            if cur.get("status") != "approved" or cur.get("text") != now["text"]:
                changed.append(cur.get("status"))      # edited or taken back on the page while we typed
                raise RuntimeError("the answer changed on the inbox page while it was being typed; not sending")
            inbox.update(aid, expect=("approved",), status="sending", reason="about to press Send")
            pressed.append(True)

        try:
            result = send_fn(driver, url, now["text"], name, before_submit=before_submit)
        except Exception as exc:                       # a bug or a dead browser, not a Facebook answer
            result = Result("uncertain" if pressed else "not_submitted", f"{type(exc).__name__}: {exc}")
        if changed and not pressed:
            print(f"    [=] it changed on the inbox page ({changed[0]}); left for the next run")
            counts["skipped"] += 1
            continue
        settled = outcome(result, bool(pressed))
        if settled is None:
            settle(aid, ("approved",), reason=f"not sent, will retry: the run stopped ({result.status}): "
                                              f"{result.reason}")
        else:
            status, reason = settled
            if not settle(aid, ("sending",) if pressed else ("approved",), status=status, reason=reason):
                counts["skipped"] += 1
                continue
            counts[status] += 1
            if status != "sent":
                print(f"    [!] {status}: {reason}")

        if result.status in ACCOUNT_WIDE:
            counts["stopped"], counts["halted"] = f"account-wide problem ({result.status}): {result.reason}", True
            break
        fails = fails + 1 if settled and settled[0] == "failed" else 0
        unsure = unsure + 1 if settled and settled[0] == "uncertain" else 0
        if fails >= MAX_FAILS_IN_A_ROW:
            counts["stopped"], counts["halted"] = f"{fails} answers in a row failed; check the account", True
            break
        if unsure >= MAX_UNCERTAIN_IN_A_ROW:
            counts["stopped"], counts["halted"] = f"{unsure} answers in a row couldn't be confirmed", True
            break
        if i + 1 < len(entries):
            delay = random.uniform(s["lo"], s["hi"])
            print(f"[+] Waiting {delay:.0f}s before the next one...")
            time.sleep(delay)
    return counts


def report(counts):
    print(f"\n[done] answers sent {counts['sent']}, failed {counts['failed']}, uncertain {counts['uncertain']}, "
          f"skipped {counts['skipped']}")
    if counts["stopped"]:
        print(f"[stopped] {counts['stopped']}")
    return 2 if counts["halted"] else 0


def run(cfg):
    with store.lock():
        for a in inbox.stuck(PLATFORM):
            print(f"[!] {a['name'] or a['person']}: a run stopped while sending this answer; now 'uncertain' "
                  f"(look in the chat: {a['chat_url'] or store.messenger_url(a['person'])})")
        entries = inbox.approved(PLATFORM)
        if not entries:
            print("[+] No approved answers to send.")
            return 0
        entries = entries[:settings(cfg)["max_per_run"]]
        print(f"[+] {len(entries)} approved answers to send")
        driver = browser.make_driver(cfg)
        try:
            send.prepare(driver, cfg)
            return report(answer_loop(driver, entries, cfg))
        except KeyboardInterrupt:
            print("\n[+] Interrupted. Every answer's status is in inbox/data/answers.json.")
            return 0
        finally:
            driver.quit()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", action="store_true", dest="list_only")
    args = ap.parse_args()
    cfg = load_config()
    if args.list_only:
        entries = inbox.approved(PLATFORM)
        for a in entries:
            print(f"  {a['approved_at']}  {a['name'] or a['person']:<30} {len(a['text'])} chars  {chat_url(a)}")
        print(f"{len(entries)} approved; {settings(cfg)['max_per_run']} a run")
    else:
        sys.exit(run(cfg))


if __name__ == "__main__":
    browser.halting(main)
