"""answer.py - send the answers you approved to people who replied to our DM.

    python run.py answer         (or python answer.py)

The answers live in the shared record at the repo root, inbox/data/answers.json (inbox/store.py): its page
drafts an answer to each reply and you approve it there. This sends each approved Instagram entry, oldest
approval first, and records how it went in that same record. contacted.csv isn't touched, nobody is followed,
and an answer doesn't count toward outreach.daily_dm_limit (it goes into a chat they wrote in).

Each run: takes send.py's lock (one run on the Chrome profile at a time), turns any entry a dead run left
'sending' into 'uncertain' (inbox store.stuck, so nothing is resent blind), checks the browser is logged in as
outreach.sender_user_id (send.prepare), asks Message Requests (read only) which of these people's chats sit
there, then for each approved entry:

  1. reads it again; it must still be 'approved' (you may have changed it on the page meanwhile).
  2. If their chat is in Message Requests, it is marked 'failed': the request has to be accepted first, and
     this never accepts one. Accept it in Instagram, then approve the answer again.
  3. Opens ig.me/m/<person> (direct.chat_link) and checks it is a chat showing their username or name, that
     Instagram isn't refusing this account or them, and that the answer isn't already there.
  4. Types the approved text, marks the entry 'sending' right before pressing Send, presses it once, and marks
     it 'sent' once the message box clears ('uncertain' if Send was pressed and that couldn't be confirmed,
     'failed' if it never got as far as Send; 'failed' can be approved again, 'uncertain' needs a look).

Pacing: outreach.min_delay_seconds to max_delay_seconds between answers, at most answers.max_per_run (10) per
run. It stops with exit code 2 on anything account-wide (logged out, a challenge, an action block, a locked
account; the entry stays 'approved' if Send wasn't pressed) and after 3 failures or 2 uncertain sends in a row.
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
import replies
import send
import store
from inbox import store as answers           # the shared record (repo root on sys.path via browser.py)
from send_dm import ACCOUNT_WIDE, AccountBlocked, Result

HERE = Path(__file__).resolve().parent
CONFIG = HERE / "config.json"
PLATFORM = "instagram"
DEFAULTS = {"max_per_run": 10}
MAX_FAILED_IN_A_ROW = 3
MAX_UNCERTAIN_IN_A_ROW = 2
REQUEST = ("their chat is in Message Requests; this never accepts a request. Accept it in Instagram, then "
           "approve the answer again")


class Changed(RuntimeError):
    """The entry stopped being 'approved' (changed on the page) before Send was pressed: nothing is sent."""


def settings(cfg: dict) -> dict:
    return {**DEFAULTS, **cfg.get("answers", {})}


def in_requests(entry: dict, pending: set[str] | None) -> bool:
    """Is this reply's chat in Message Requests? Asked of Instagram at the start of the run (pending); if that
    didn't answer, what replies.py recorded for this reply (how=request)."""
    person = entry["person"].lower()
    if pending is not None:
        return person in pending
    ids = {str(entry.get("reply_id"))} | {str(c) for c in entry.get("covers") or ()}   # grouped replies (draft.py)
    return any(r["item_id"] in ids and r.get("how") == "request" for r in store.load_replies())


def mark(aid: str, status: str, reason: str, expect: tuple) -> bool:
    """Write the outcome unless the entry has moved on (then it isn't ours to change). True if written."""
    try:
        answers.update(aid, expect=expect, status=status, reason=reason[:500])
        return True
    except (KeyError, ValueError) as exc:
        print(f"    [!] not recording {status!r}: {exc}")
        return False


def outcome(result: Result, pressed: bool) -> str:
    """The inbox status for a send's Result: sent / uncertain (Send pressed, not confirmed) / failed (not sent)."""
    if result.status == "sent":
        return "sent"
    if result.status == "uncertain" or (pressed and result.status in ACCOUNT_WIDE):
        return "uncertain"
    return "failed"


def send_one(driver, entry: dict, cfg: dict, send_fn) -> tuple[str, Result | None]:
    """Send one approved answer. Returns (the status written or 'skipped' / 'approved', the Result if it got as
    far as the chat)."""
    aid, person, text = entry["id"], entry["person"].lower().removeprefix("@"), entry.get("text") or ""
    pressed = []

    def before_submit():
        try:
            answers.update(aid, expect=("approved",), status="sending", reason="")
        except (KeyError, ValueError) as exc:
            raise Changed(f"the answer changed before Send was pressed ({exc}); not sending") from exc
        pressed.append(True)

    try:
        result = send_fn(driver, direct.chat_link(person), text, person, entry.get("name") or "",
                         before_submit=before_submit)
    except Exception as exc:                          # a bug or a dead browser, not an Instagram answer
        result = Result("uncertain" if pressed else "not_submitted", f"{type(exc).__name__}: {exc}")
    now = answers.get(aid)
    if not pressed and (not now or now["status"] != "approved"):
        print(f"    [=] the answer was changed on the page meanwhile ({now and now['status']}); left as it is")
        return "skipped", result
    if not pressed and result.status in ACCOUNT_WIDE:
        return "approved", result                     # nothing went out: it stays approved for the next run
    status = outcome(result, bool(pressed))
    mark(aid, status, result.reason, ("sending",) if pressed else ("approved",))
    return status, result


def run(cfg: dict, send_fn=None) -> int:
    """Send the approved Instagram answers. Returns the exit code (2: stopped on an account-wide problem or a
    streak)."""
    a, out = settings(cfg), cfg["outreach"]
    send_fn = send_fn or (lambda *args, **kw: direct.send_message(*args, verify=out.get("verify_after_send", False),
                                                                  **kw))
    with store.lock():
        for e in answers.stuck(PLATFORM):
            print(f"[!] @{e['person']}: a run stopped while sending this answer; now 'uncertain' (look at the chat)")
        todo = answers.approved(PLATFORM)
        if not todo:
            print("[+] No approved Instagram answers to send.")
            return 0
        print(f"[+] {len(todo)} approved answers; at most {a['max_per_run']} this run (answers.max_per_run)")
        counts = {"sent": 0, "failed": 0, "uncertain": 0, "skipped": 0}
        stopped, halted, streak = "", False, {"status": None, "n": 0}
        driver = browser.make_driver(cfg)
        try:
            send.prepare(driver, cfg)
            try:
                pending = replies.pending_users(driver, cfg, [e["person"] for e in todo])
            except AccountBlocked as exc:
                raise browser.Halt(f"account-wide problem while reading Message Requests ({exc.kind}): {exc}")
            if pending is None:
                print("[!] Message Requests didn't answer; going by what python run.py replies recorded")
            tried = 0
            for entry in todo:
                if tried >= a["max_per_run"]:
                    stopped = f"answers.max_per_run ({a['max_per_run']}) reached; the rest go next run"
                    break
                entry = answers.get(entry["id"])
                if not entry or entry["status"] != "approved":
                    counts["skipped"] += 1
                    continue
                person = entry["person"]
                print(f"\n[+] answering @{person}")
                if not (entry.get("text") or "").strip() or not person.strip():
                    mark(entry["id"], "failed", "no text to send or no person", ("approved",))
                    counts["failed"] += 1
                    continue
                if in_requests(entry, pending):
                    print(f"    [!] {REQUEST}")
                    mark(entry["id"], "failed", REQUEST, ("approved",))
                    counts["failed"] += 1
                    continue
                if tried:
                    delay = random.uniform(out["min_delay_seconds"], out["max_delay_seconds"])
                    print(f"[+] Waiting {delay:.0f}s before the next one...")
                    time.sleep(delay)
                tried += 1
                status, result = send_one(driver, entry, cfg, send_fn)
                if status in counts:
                    counts[status] += 1
                if result is not None and result.status in ACCOUNT_WIDE:
                    stopped, halted = f"account-wide problem ({result.status}): {result.reason}", True
                    break
                if status == "sent":
                    print("    sent")
                elif result is not None and status != "skipped":
                    print(f"    [!] {status}: {result.reason}")
                if status not in ("failed", "uncertain"):
                    streak = {"status": None, "n": 0}
                    continue
                streak = {"status": status, "n": streak["n"] + 1 if streak["status"] == status else 1}
                limit = MAX_FAILED_IN_A_ROW if status == "failed" else MAX_UNCERTAIN_IN_A_ROW
                if streak["n"] >= limit:
                    stopped, halted = f"{streak['n']} answers in a row {status}; check the account and the chats", True
                    break
        except KeyboardInterrupt:
            print("\n[+] Interrupted. Everything recorded so far is in inbox/data/answers.json.")
            return 0
        finally:
            driver.quit()
    print(f"\n[done] answers sent {counts['sent']}, failed {counts['failed']}, uncertain {counts['uncertain']}, "
          f"skipped {counts['skipped']}")
    if stopped:
        print(f"[stopped] {stopped}")
    return 2 if halted else 0


def main():
    argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter).parse_args()
    try:
        sys.exit(run(json.loads(CONFIG.read_text())))
    except browser.Halt as stop:
        print(f"[stopped] {stop}")
        sys.exit(2)


if __name__ == "__main__":
    main()
