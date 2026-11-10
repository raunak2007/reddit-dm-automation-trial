"""replies.py - find replies from the people this account has messaged, and note them in data/replies.csv.

Reads this account's inbox through the logged-in Chrome profile (browser.py): /message/inbox.json, newest first,
up to --pages pages of 100, each fetched with mark=false so nothing is marked read. Anything there written by
someone the root contacted.csv has as 'sent' (from any Reddit script: run_loop.py, open_queue.py or send.py),
after the first time they were sent to, counts as a reply: a private message, or a reply to one of this
account's comments or posts. Each is added to data/replies.csv once (by message id) with the username, when it
arrived, a short snippet and a permalink, and the run prints a summary.

It reads only. It doesn't mark anything read, reply, send, or change contacted.csv. It stops, keeping what it has,
on the same account-wide signals as scrape.py (logged out, rate-limited, blocked; browser.fetch_json).

    python replies.py [--pages N]

Exits 2 when it stopped on an account-wide problem, 0 otherwise.
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timedelta

import browser
import ledger
import store
from send_dm import AccountBlocked

INBOX = "https://old.reddit.com/message/inbox/.json?limit=100&mark=false&raw_json=1"
SLACK = timedelta(minutes=5)          # clock slack between contacted.csv's local time and Reddit's
KINDS = {"comment reply": "comment_reply", "post reply": "post_reply", "username mention": "mention"}


def snippet(text: str, n: int = 160) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= n else text[:n - 1].rstrip() + "…"


def parse_message(child: dict) -> dict | None:
    """One inbox child (t4 private message, t1 comment reply / mention) as a replies.csv row (without
    recorded_at), or None."""
    if not isinstance(child, dict) or child.get("kind") not in ("t1", "t4"):
        return None
    d = child.get("data") or {}
    if not d.get("author") or not d.get("name"):
        return None
    created = float(d.get("created_utc") or 0)
    if child["kind"] == "t4":
        thread = (d.get("first_message_name") or d["name"]).removeprefix("t4_")
        kind, link = "message", f"{store.REDDIT}/message/messages/{thread}"
    else:
        kind = KINDS.get((d.get("subject") or "").lower(), "comment_reply")
        link = store.REDDIT + d["context"] if d.get("context") else ""
    return {"message_id": d["name"], "username": d["author"], "kind": kind,
            "received_at": datetime.fromtimestamp(created).isoformat(timespec="seconds") if created else "",
            "subject": snippet(d.get("subject") or d.get("link_title") or "", 100),
            "snippet": snippet(d.get("body") or ""), "permalink": link}


def flatten(children: list):
    """Inbox children plus the later messages Reddit nests under a private message's "replies"."""
    for child in children or []:
        yield child
        nested = ((child or {}).get("data") or {}).get("replies")
        if isinstance(nested, dict):
            yield from flatten((nested.get("data") or {}).get("children"))


def match(children: list, sent: dict, known: set, sender: str | None = None) -> list[dict]:
    """The new replies among these inbox children. sent: {username key: first sent at} (store.first_sent());
    known: message ids already in replies.csv (added to as rows are matched)."""
    out = []
    for child in flatten(children):
        row = parse_message(child)
        if not row or row["message_id"] in known:
            continue
        who = ledger.key(row["username"])
        if who not in sent or (sender and who == ledger.key(sender)):
            continue
        if row["received_at"] and datetime.fromisoformat(row["received_at"]) < sent[who] - SLACK:
            continue                                     # written before we ever messaged them: not a reply
        known.add(row["message_id"])
        out.append(row)
    return out


def run(cfg: dict, cfg_root: dict | None = None, pages: int = 3, driver=None, fetch=None) -> int:
    """Read the inbox and record new replies. Returns the exit code (2: stopped on an account-wide problem)."""
    cfg_root = store.load_root_config() if cfg_root is None else cfg_root
    fetch = fetch or browser.fetch_json
    sent = store.first_sent()
    known = {r["message_id"] for r in store.load_replies()}
    if not sent:
        print("[+] contacted.csv has nobody marked sent yet: no replies to look for.")
        return 0
    own = driver is None
    driver = browser.make_driver(cfg) if own else driver
    new, read, code, after = [], 0, 0, None
    try:
        sender = browser.check_login(driver, cfg_root)
        for page in range(pages):
            browser.pause(cfg, 0.5)
            try:
                data = fetch(driver, INBOX + (f"&after={after}" if after else ""))["data"]
                children = data["children"]
            except browser.PageSkipped as exc:
                print(f"[!] the inbox couldn't be read: {exc}")
                break
            except (KeyError, TypeError):
                print("[!] the inbox page wasn't a listing; stopping")
                break
            read += len(children)
            found = match(children, sent, known, sender)
            if found:
                now = datetime.now().isoformat(timespec="seconds")
                store.add_replies([dict(r, recorded_at=now) for r in found])
                new += found
            after = data.get("after")
            if not after or not children:
                break
    except AccountBlocked as exc:
        print(f"[stopped] account-wide problem ({exc.kind}): {exc}. Kept what was read.")
        code = 2
    finally:
        if own:
            driver.quit()
    for r in new:
        print(f"  u/{r['username']:<24} {r['received_at']}  {r['kind']:<13} {r['snippet'][:70]!r}\n      {r['permalink']}")
    total = store.load_replies()
    print(f"[done] {read} inbox items read; {len(new)} new replies; {len(total)} replies from "
          f"{len({ledger.key(r['username']) for r in total})} of the {len(sent)} people messaged, in data/replies.csv")
    return code


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pages", type=int, default=3, help="inbox pages of 100 to read (default 3)")
    args = ap.parse_args()
    sys.exit(run(store.load_config(), pages=args.pages))


if __name__ == "__main__":
    main()
