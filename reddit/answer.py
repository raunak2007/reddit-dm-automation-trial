"""answer.py - send the answers you approved on the inbox page to the Reddit users who replied.

The shared record is the repo root's inbox/store.py (inbox/data/answers.json): the inbox page drafts an answer to
each reply in data/replies.csv and you approve it (or edit it, or dismiss it). This sends the approved Reddit
ones, oldest approval first, and nothing else:

    store.stuck("reddit")     first: entries a run left 'sending' (it died after marking one) become 'uncertain',
                              so nothing is resent blind. Check those in the sent folder yourself.
    store.approved("reddit")  then each approved entry, up to answers.max_per_run (10) a run:
        entry["text"] goes to entry["person"] through send_dm.compose_link / send_compose (the root sender's
        compose flow: prefilled fields checked, Send clicked once). Subject: "re: <their subject>" when
        data/replies.csv has the reply (by reply_id) as a private message, else answers.subject.
        'sending' is written in before_submit, right before Send is clicked, and only if the entry is still
        approved with the same text (the page may have changed it since the run started); then
            sent        Reddit accepted it
            failed      not sent: failed before Send, refused, no such user, a bad entry (approve it again
                        on the page to retry)
            uncertain   Send was clicked but it couldn't be confirmed: check the sent folder
        with the reason.

These are answers in a conversation that already exists, so they don't go through ledger.send_loop:
ledger.skip_reason would refuse everyone here (they're all 'sent' in contacted.csv), and contacted.csv is not
written. They don't count toward the root daily cap either: answers.max_per_run is their cap.

Like send.py: ledger.lock() (one sending run at a time across every Reddit script), logged in as the root
config.json's sender_username and the sent folder pulled in (send.start -> ledger.prepare), the root config's
min/max_delay_seconds between sends, and a stop with exit code 2 on anything account-wide (logged out, wrong
account, rate-limited, restricted) or on the ledger's failure streaks (ledger.STREAKS).

    python answer.py
"""
from __future__ import annotations

import random
import re
import sys
import time

import store  # first: it puts the repo root on sys.path
import browser  # noqa: E402
import ledger  # noqa: E402
import send  # noqa: E402
from inbox import store as inbox  # noqa: E402
from send_dm import ACCOUNT_WIDE, Result, compose_link, send_compose  # noqa: E402

PLATFORM = "reddit"
DEFAULT_SUBJECT = "re: About your post"


def settings(cfg: dict) -> dict:
    a = cfg.get("answers") or {}
    return {"max_per_run": int(a.get("max_per_run", 10)), "subject": (a.get("subject") or DEFAULT_SUBJECT).strip()}


def subject_for(entry: dict, cfg: dict) -> str:
    """'re: <their subject>' for a private message we know the subject of (data/replies.csv by reply_id, else the
    kind / subject inbox/draft.py copies into the entry); answers.subject otherwise (comment replies and mentions
    have no subject of their own: Reddit calls them "comment reply" etc.)."""
    row = store.reply_by_id(entry.get("reply_id") or "") or entry
    theirs = (row.get("subject") or "").strip() if row.get("kind") == "message" else ""
    if not theirs:
        return settings(cfg)["subject"][:100]
    return (theirs if re.match(r"re\s*:", theirs, re.I) else f"re: {theirs}")[:100]


def fail_unsent(aid: str, reason: str) -> bool:
    """Mark an approved entry failed before anything was opened for it. False if the page changed it meanwhile."""
    try:
        inbox.update(aid, expect=("approved",), status="failed", reason=reason[:300])
        return True
    except ValueError:
        return False


def finish(aid: str, result: Result) -> str:
    """Write the outcome of one send to the entry; returns the status written."""
    status = {"sent": "sent", "uncertain": "uncertain"}.get(result.status, "failed")
    inbox.update(aid, status=status, reason=f"{result.status}: {result.reason}"[:300])
    return status


def run(cfg: dict, cfg_root: dict | None = None, make_driver=None, send_fn=None) -> int:
    """Send the approved answers. Returns the exit code (2: halted on an account-wide problem or a streak)."""
    cfg_root = store.load_root_config() if cfg_root is None else cfg_root
    for a in inbox.stuck(PLATFORM):
        print(f"[!] u/{a['person']}: a run stopped while sending this answer; marked uncertain. Check the sent "
              f"folder (https://www.reddit.com/message/sent/).")
    todo = inbox.approved(PLATFORM)
    limit = settings(cfg)["max_per_run"]
    if not todo:
        print("[+] No approved Reddit answers to send.")
        return 0
    if len(todo) > limit:
        print(f"[+] {len(todo)} approved; sending the oldest {limit} (answers.max_per_run)")
        todo = todo[:limit]
    verify = cfg_root.get("verify_after_send", False)
    send_fn = send_fn or (lambda *a, **k: send_compose(*a, verify=verify, **k))
    counts = {s: 0 for s in ("sent", "failed", "uncertain", "skipped")}
    stopped, halted, streak = "", False, {"status": None, "n": 0}
    with ledger.lock():
        driver = (make_driver or browser.make_driver)(cfg)
        try:
            sender, code = send.start(driver, cfg_root)
            if not sender:
                return code
            for i, entry in enumerate(todo):
                aid, person, text = entry["id"], entry["person"], entry["text"]
                try:
                    if ledger.key(person) == ledger.key(sender):
                        raise ValueError("that's this account")
                    link = compose_link(person, text, subject_for(entry, cfg))
                except ValueError as exc:
                    print(f"[!] u/{person}: not sent: {exc}")
                    counts["failed" if fail_unsent(aid, f"invalid: {exc}") else "skipped"] += 1
                    continue
                print(f"\n[+] answering u/{person} ({i + 1}/{len(todo)})")
                state = {"marked": False, "changed": ""}

                def before_submit():
                    now = inbox.get(aid) or {}
                    if now.get("status") != "approved" or now.get("text") != text:
                        state["changed"] = f"the entry is now {now.get('status')!r} or its text was edited"
                        raise RuntimeError(f"not clicking Send: {state['changed']}")
                    inbox.update(aid, expect=("approved",), status="sending", reason="about to click Send")
                    state["marked"] = True

                try:
                    result = send_fn(driver, link, sender=sender, before_submit=before_submit)
                except Exception as exc:                  # a bug or a dead browser, not a Reddit answer
                    result = Result("uncertain" if state["marked"] else "not_submitted", f"{type(exc).__name__}: {exc}")
                if state["changed"] and not state["marked"]:
                    print(f"    [=] skipped: {state['changed']} since this run started; left as it is")
                    counts["skipped"] += 1
                    continue
                counts[finish(aid, result)] += 1
                if result.status != "sent":
                    print(f"    [!] {ledger.NOTES.get(result.status, result.status)}: {result.reason}")
                if result.status in ACCOUNT_WIDE:
                    stopped, halted = f"account-wide problem ({result.status}): {result.reason}", True
                    break
                if result.status == "invalid":
                    continue
                streak = ({"status": result.status, "n": streak["n"] + 1} if result.status == streak["status"]
                          else {"status": result.status, "n": 1})
                limit_n, why = ledger.STREAKS.get(result.status, (None, ""))
                if limit_n and streak["n"] >= limit_n:
                    stopped, halted = f"{streak['n']} {why}", True
                    break
                if i + 1 < len(todo):
                    delay = random.uniform(cfg_root["min_delay_seconds"], cfg_root["max_delay_seconds"])
                    print(f"[+] Waiting {delay:.0f}s before the next one...")
                    time.sleep(delay)
        finally:
            driver.quit()
    print(f"\n[done] answers sent {counts['sent']}, failed {counts['failed']}, uncertain {counts['uncertain']}, "
          f"skipped {counts['skipped']}; {len(inbox.approved(PLATFORM))} still approved")
    if stopped:
        print(f"[stopped] {stopped}")
    return 2 if halted else 0


def main():
    sys.exit(run(store.load_config()))


if __name__ == "__main__":
    main()
