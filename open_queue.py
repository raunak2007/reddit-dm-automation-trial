"""open_queue.py - walk a DM queue and open each compose link for a human to send.

By default this does NOT submit anything. It reads a queue HTML file (one <li class=c> per entry, with
an a.b reddit.com/message/compose/ link, a <pre> body and an input[data-u] Sent checkbox), opens the
next eligible entry's compose link exactly as written, and waits for you to click Send yourself. When
you say it's sent, it looks for the message in the account's sent folder: found -> recorded 'sent',
not found -> recorded 'uncertain' and held. Results go to contacted.csv (ledger.py, shared with
run_loop.py), and which entries are left is worked out from it on every run: anything sent, held or
given up on is passed over, and anything that failed is offered again. Only the entries you skip with
[k] are kept separately, in queue_state/<queue>.json.

Like run_loop.py, it stops at config.json's daily_limit_per_account (counted from contacted.csv across
every run), stops on anything account-wide (logged out, rate-limited, restricted) and after
MAX_UNCERTAIN_IN_A_ROW sends in a row that can't be confirmed, and records a recipient Reddit refuses
('refused') or doesn't know ('recipient') before you are asked to send.

With --send it submits each entry itself, via the same loop as run_loop.py (check prefilled fields,
click Send once, confirm in the sent folder; stop on account-wide problems; hold uncertain sends).

Entries whose link, checkbox, heading or <pre> body disagree are reported and never opened. Before
either mode opens anything it takes a lock (one run at a time), checks the browser is logged in as
config.json's sender_username, and records everyone in that account's sent folder; the queue page's
own Sent checkmarks must have been imported once with `python ledger.py import-checkmarks` (or pass
--no-checkmarks).

Usage:
    python open_queue.py <queue.html> [--profile account_1] [--reset]
    python open_queue.py <queue.html> --list
    python open_queue.py <queue.html> --check [--profile account_1]
    python open_queue.py <queue.html> --send [--profile account_1]

Options:
    --profile NAME   Chrome profile under accounts/ to use (default: account_1)
    --reset          Offer the entries you skipped with [k] again (who was sent stays in contacted.csv)
    --list           Print the parsed queue and exit (no browser; only what contacted.csv already knows)
    --check          Log in, pull in the account's Reddit sent folder, then print the queue with who would be
                     skipped and why. Sends nothing; use it to see which entries were already messaged
    --send           Click Send automatically instead of waiting for you
    --no-checkmarks  Go ahead without importing the queue page's Sent checkmarks
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path

import undetected_chromedriver as uc
from chrome_version import chrome_major
from selenium.common.exceptions import TimeoutException

import ledger
from send_dm import (ACCOUNT_WIDE, AccountBlocked, MessageRefused, NoSuchUser, NotInSent, Result, check_account,
                     check_recipient, classify, compose_form, parse_compose_link, verify_in_sent, verify_sender,
                     wait_until_loaded)

HERE = Path(__file__).resolve().parent
STATE_DIR = HERE / "queue_state"
CONFIG = HERE / "config.json"

class _QueueHTML(HTMLParser):
    """Collects, per <li class=c>: the a.b href, the <pre> text, the input[data-u] and the .h handle."""

    def __init__(self):
        super().__init__(convert_charrefs=True)          # entities in text and attributes are decoded
        self.cards, self.card, self.pre, self.bold = [], None, None, None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        classes = (a.get("class") or "").split()
        if tag == "li" and "c" in classes:
            self.card = {"href": None, "pre": None, "data_u": None, "handle": None, "hrefs": 0}
            self.cards.append(self.card)
        elif self.card is None:
            return
        elif tag == "a" and "b" in classes:
            self.card["hrefs"] += 1
            self.card["href"] = self.card["href"] or a.get("href")
        elif tag == "pre":
            self.pre = []
        elif tag == "input" and "data-u" in a:
            self.card["data_u"] = a["data-u"]
        elif tag == "b" and self.card["handle"] is None:
            self.bold = []

    def handle_data(self, data):
        if self.pre is not None:
            self.pre.append(data)
        if self.bold is not None:
            self.bold.append(data)

    def handle_endtag(self, tag):
        if tag == "pre" and self.pre is not None and self.card is not None:
            self.card["pre"] = "".join(self.pre)
            self.pre = None
        elif tag == "b" and self.bold is not None:
            m = re.search(r"u/([A-Za-z0-9_-]+)", "".join(self.bold))
            self.card["handle"] = m and m.group(1)
            self.bold = None
        elif tag == "li":
            self.card = None


def _body(text):
    """Line endings normalized and the outer whitespace HTML adds around <pre> removed; nothing else."""
    return (text or "").replace("\r\n", "\n").strip()


def parse_queue(path: Path):
    """Every entry with its compose link exactly as written in the HTML, plus the recipient, subject and
    body that link carries. An entry whose parts disagree (link vs checkbox vs heading recipient, link
    vs <pre> body, a subject unlike every other entry's) gets a 'problem' and must not be sent."""
    parser = _QueueHTML()
    parser.feed(path.read_text(encoding="utf-8"))
    entries = []
    for i, card in enumerate(parser.cards, 1):
        e = {"index": i, "to": card["data_u"] or card["handle"] or "", "subject": "", "message": "",
             "link": card["href"] or "", "problem": None}
        problems = []
        if not card["href"]:
            problems.append("no compose link (a.b)")
        else:
            try:
                want = parse_compose_link(card["href"])
                e.update(to=want["to"], subject=want["subject"], message=want["message"])
            except ValueError as exc:
                problems.append(str(exc))
        if card["hrefs"] > 1:
            problems.append(f"{card['hrefs']} compose links")
        for label, other in (("checkbox data-u", card["data_u"]), ("heading u/", card["handle"])):
            if other and e["to"] and other.lower() != e["to"].lower():
                problems.append(f"link is to u/{e['to']} but the {label} says u/{other}")
        if card["pre"] is None:
            problems.append("no <pre> body to check the link against")
        elif card["href"] and _body(card["pre"]) != _body(e["message"]):
            problems.append("the link's message differs from the <pre> body")
        if card["href"] and not e["subject"].strip():
            problems.append("the link has no subject")
        e["problem"] = "; ".join(problems) or None
        entries.append(e)
    subjects = Counter(e["subject"] for e in entries if not e["problem"])
    if len(subjects) > 1:                                # the subject is meant to be the same for every entry
        usual = subjects.most_common(1)[0][0]
        for e in entries:
            if not e["problem"] and e["subject"] != usual:
                e["problem"] = f"subject {e['subject']!r} differs from the other entries' {usual!r}"
    for e in entries:
        if e["problem"]:
            print(f"[!] entry {e['index']} (u/{e['to'] or '?'}): {e['problem']}; it won't be sent", file=sys.stderr)
    return entries


def state_path(queue_path: Path) -> Path:
    STATE_DIR.mkdir(exist_ok=True)
    return STATE_DIR / f"{queue_path.stem}.json"


def load_state(queue_path: Path) -> dict:
    """{"skipped": [usernames you skipped with k]}. Older files held a cursor instead; it is ignored, since
    contacted.csv already says who is done."""
    p = state_path(queue_path)
    if not p.exists():
        return {"skipped": []}
    return {"skipped": json.loads(p.read_text()).get("skipped", [])}


def save_state(queue_path: Path, state: dict) -> None:
    state_path(queue_path).write_text(json.dumps(state, indent=2))


def make_driver(profile_name: str):
    profile_dir = HERE / "accounts" / profile_name / "chrome_profile"
    if not profile_dir.is_dir():
        sys.exit(f"Profile folder not found: {profile_dir}\n"
                 f"Run test_connection.py first to log in to account '{profile_name}'.")
    for lock in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        lock_path = profile_dir / lock
        if lock_path.exists():
            lock_path.unlink()
    opts = uc.ChromeOptions()
    opts.add_argument(f"--user-data-dir={profile_dir}")
    opts.add_argument("--no-first-run")
    opts.add_argument("--no-default-browser-check")
    driver = uc.Chrome(options=opts, version_main=chrome_major())
    driver.set_window_size(1400, 900)
    return driver


def page_says(driver, username):
    """A Result for what the compose page shows now (refused, no such user, or an account-wide problem),
    else None."""
    try:
        check_account(driver)
        check_recipient(driver, username)
    except (AccountBlocked, MessageRefused, NoSuchUser) as exc:
        return Result(classify(exc), f"compose (by hand): {exc}")
    return None


def open_entry(driver, entry):
    """Open the entry's compose link. A Result if Reddit refuses before you could send, else None."""
    driver.get(entry["link"])
    try:
        wait_until_loaded(driver, compose_form, "the compose form's To / Title / Message fields")
    except AccountBlocked as exc:
        return Result(exc.kind, f"compose (by hand): {exc}")
    except TimeoutException:
        print("    [!] the compose form didn't show up; look at the window before sending")
        return page_says(driver, entry["to"])
    time.sleep(1.5)                                      # Reddit checks the To field a moment after loading
    return page_says(driver, entry["to"])


def confirm_by_hand(driver, entry, opened_at, sender):
    """After you say you clicked Send: (Result, why the walk must stop or None). 'sent' if the message is in
    the sent folder, Reddit's own answer if the compose page shows one, else 'uncertain'."""
    if "/message/compose" in driver.current_url:         # anywhere else, page text isn't about this send
        said = page_says(driver, entry["to"])
        if said is not None:
            return said, None
    want = parse_compose_link(entry["link"])
    print("    checking the sent folder...")
    try:
        verify_in_sent(driver, want, opened_at, sender, timeout=30)
        return Result("sent", "compose (by hand): confirmed in the sent folder"), None
    except NotInSent as exc:
        return Result("uncertain", f"compose (by hand): marked sent, but {exc}"), None
    except RuntimeError as exc:                          # the sent folder itself couldn't be read
        result = Result("uncertain", f"compose (by hand): marked sent, but the sent folder couldn't be read: {exc}")
        try:
            verify_sender(driver, sender)
        except AccountBlocked as blocked:
            return result, f"account-wide problem ({blocked.kind}): {blocked}"
        return result, f"the sent folder couldn't be read ({exc}); nothing can be confirmed until it can"


def walk_by_hand(driver, queue_path: Path, entries: list, state: dict, cfg: dict, sender: str) -> dict:
    """Offer each eligible entry in turn. Returns counts like ledger.send_loop, for ledger.report."""
    cap = cfg["daily_limit_per_account"]
    max_attempts = cfg.get("max_attempts_per_recipient", ledger.MAX_ATTEMPTS)
    counts = {s: 0 for s in ("sent", "recipient", "refused", "invalid", "not_submitted", "uncertain", "skipped")}
    counts.update(stopped="", halted=False)
    uncertain_in_a_row = 0

    def stop(why, halted=True):
        counts["stopped"], counts["halted"] = why, halted

    for entry in entries:
        username = entry["to"]
        why = (entry["problem"]
               or (ledger.key(username) in map(ledger.key, state["skipped"])
                   and "you skipped it with [k] (--reset to be offered it again)")
               or ledger.skip_reason(username, max_attempts))
        if why:
            print(f"[=] skipping u/{username}: {why}")
            counts["invalid" if entry["problem"] else "skipped"] += 1
            continue
        done_today = ledger.sent_today()
        if done_today >= cap:
            stop(f"hit the daily cap ({done_today}/{cap} sent today)", halted=False)
            break

        print(f"\n[{entry['index']}/{len(entries)}] u/{username} ({done_today + 1}/{cap} today)")
        print(f"    subject: {entry['subject']!r}")
        print(f"    body:    {len(entry['message'])} chars")
        opened_at = time.time()
        refused = open_entry(driver, entry)
        while refused is None:
            print("    → Reddit compose tab is open with the message prefilled.")
            print("    → Review it. Click Send yourself if you want to send it.")
            print("    → Then come back here.")
            choice = input("    [s]ent  [k]skip  [o]pen-in-browser-again  [q]uit  > ").strip().lower()
            if choice != "o":
                break
            refused = open_entry(driver, entry)

        if refused is not None:
            result, halt = refused, None
            print("    Reddit answered before anything was sent:")
        elif choice == "q":
            stop("you quit", halted=False)
            break
        elif choice == "k":
            state["skipped"].append(username)
            save_state(queue_path, state)
            counts["skipped"] += 1
            print("    skipped")
            continue
        else:                                            # anything else: "I clicked Send"
            result, halt = confirm_by_hand(driver, entry, opened_at, sender)

        ledger.record(username, result)
        if result.status in ACCOUNT_WIDE:
            stop(f"account-wide problem ({result.status}): {result.reason}")
            break
        counts[result.status] += 1
        if result.status == "sent":
            print("    confirmed in the sent folder")
        else:
            print(f"    [!] {ledger.NOTES.get(result.status, result.status)}: {result.reason}")
        if halt:
            stop(halt)
            break
        uncertain_in_a_row = uncertain_in_a_row + 1 if result.status == "uncertain" else 0
        if uncertain_in_a_row >= ledger.MAX_UNCERTAIN_IN_A_ROW:
            stop(f"{uncertain_in_a_row} {ledger.STREAKS['uncertain'][1]}")
            break
    return counts


def print_list(queue_path: Path, entries: list):
    """Every entry with why it would be skipped, then a tally."""
    skipped_by_hand = {ledger.key(u) for u in load_state(queue_path)["skipped"]}
    tally = Counter()
    for e in entries:
        why = e["problem"] or (e["to"] and ledger.skip_reason(e["to"]))
        if not why and ledger.key(e["to"]) in skipped_by_hand:
            why = "you skipped it with [k]"
        tally[(why or "would be offered").split(" (")[0].split(":")[0]] += 1
        print(f"  {e['index']:>3}. u/{e['to']:<32} subject={e['subject']!r} "
              f"body={len(e['message'])} chars" + (f"  [skip: {why}]" if why else ""))
    print("\n" + "\n".join(f"  {n:>3}  {what}" for what, n in tally.most_common()))
    note = ledger.checkmarks_note(queue_path)
    if note:
        print(note)
    elif not ledger.checkmarks_imported(queue_path):
        print("\n[!] " + ledger.checkmark_help(queue_path))


def check(queue_path: Path, entries: list, profile: str):
    """--check: refresh contacted.csv from the account's sent folder, then list. Nothing is sent."""
    cfg = json.loads(CONFIG.read_text())
    driver = make_driver(profile)
    try:
        ledger.prepare(driver, cfg)
    finally:
        driver.quit()
    print_list(queue_path, entries)


def run(queue_path: Path, profile: str, reset: bool, list_only: bool, auto_send: bool, no_checkmarks: bool,
        check_only: bool = False):
    entries = parse_queue(queue_path)
    if not entries:
        sys.exit("No entries found in that queue file.")
    print(f"[+] Parsed {len(entries)} entries from {queue_path.name}")

    if list_only:
        return print_list(queue_path, entries)
    if check_only:
        with ledger.lock():
            return check(queue_path, entries, profile)

    if not no_checkmarks and not ledger.checkmarks_imported(queue_path):
        sys.exit(ledger.checkmark_help(queue_path))
    note = ledger.checkmarks_note(queue_path)
    if note:
        print(note)
    with ledger.lock():
        return walk(queue_path, entries, profile, reset, auto_send)


def walk(queue_path: Path, entries: list, profile: str, reset: bool, auto_send: bool) -> int:
    cfg = json.loads(CONFIG.read_text())
    state = {"skipped": []} if reset else load_state(queue_path)
    if reset:
        save_state(queue_path, state)
    driver = make_driver(profile)
    code = 0

    try:
        sender = ledger.prepare(driver, cfg)
        if auto_send:
            targets = [(e["to"], e["link"]) for e in entries if not e["problem"]]
            code = ledger.report(ledger.send_loop(driver, targets, cfg, sender))
        else:
            code = ledger.report(walk_by_hand(driver, queue_path, entries, state, cfg, sender))
            print(f"       skipped by hand: {state_path(queue_path)}")
            input("Press Enter to close Chrome... ")
    except KeyboardInterrupt:
        print("\n[+] Interrupted. Everything recorded so far is in contacted.csv.")
    finally:
        driver.quit()
    return code


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("queue", type=Path, help="path to the queue .html")
    ap.add_argument("--profile", default="account_1")
    ap.add_argument("--reset", action="store_true")
    ap.add_argument("--list", action="store_true", dest="list_only")
    ap.add_argument("--send", action="store_true", dest="auto_send")
    ap.add_argument("--no-checkmarks", action="store_true")
    ap.add_argument("--check", action="store_true", dest="check_only")
    args = ap.parse_args()
    if not args.queue.is_file():
        sys.exit(f"Queue file not found: {args.queue}")
    sys.exit(run(args.queue.resolve(), args.profile, args.reset, args.list_only, args.auto_send,
                 args.no_checkmarks, args.check_only))


if __name__ == "__main__":
    main()
