"""followup.py - one short follow-up DM to people who never answered our first one. Off unless you turn it on.

    python followup.py            send follow-ups now (python run.py followup)
    python followup.py --list     who would get one, and why the others won't (no browser)

Settings: config.json outreach.followup:
    enabled      false by default: nothing is sent until you set it to true
    after_days   6: only people whose DM went out at least this many days ago
    max_per_run  5 follow-ups a run at most
    use_ai       true: Claude drafts each one from prompts/followup.md (short, referring to the first DM); false, or
                 if Claude can't be reached, `template` is used ({first_name}, {sender_first_name}, {business_name},
                 {website}, {booking_link}, {offer})
    max_chars    500: a longer draft is replaced by the template

Who gets one: everyone with a 'sent' DM in data/contacted.csv from at least after_days ago who isn't in
data/replies.csv, hasn't been answered from the inbox (inbox.store.answered), hasn't had a follow-up (any
followup_submitting / _sent / _uncertain / _refused row: at most one, ever), isn't someone the AI thinks is under 18
(whatever outreach.minor_policy says), and whose DM text is in leads.json (to find it in the chat). Oldest DM first.

For each one: opens their chat and reads it (messenger.thread) right before sending. If they wrote anything after our
DM, it's recorded as a reply in data/replies.csv and no follow-up is sent; if we wrote something after it (an answer
sent by hand), it's skipped; if our DM can't be found in the chat, it's left for the next run. Otherwise the
follow-up is drafted, sent with messenger.send_message (the same checks as send.py: the chat is with them, Facebook
isn't refusing, the text isn't already there), with a 'followup_submitting' row right before Send is pressed once,
then followup_sent / followup_uncertain / followup_refused / followup_not_submitted / followup_stopped (see store.py).
A follow-up counts toward outreach.daily_dm_limit (store.sent_today). Same lock, delays and stops as send.py: exit 2
on anything account-wide (logged out, chat PIN, checkpoint, block) and on streaks of failures.
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import time
from datetime import datetime
from pathlib import Path

import browser  # noqa: F401  (puts the repo root, with send_dm.py and inbox/, on sys.path)
import messenger
import qualify
import replies
import send
import store
from inbox import store as inbox
from selenium.common.exceptions import TimeoutException
from send_dm import ACCOUNT_WIDE, AccountBlocked, MessageRefused, NoSuchUser, Result

HERE = Path(__file__).resolve().parent
CONFIG = HERE / "config.json"
PROMPT = HERE / "prompts" / "followup.md"
DEFAULTS = {
    "enabled": False, "after_days": 6, "max_per_run": 5, "use_ai": True, "max_chars": 500,
    "template": ("Hi {first_name}, following up on the message I sent you last week about your post. If you'd still "
                 "like a hand, I'm happy to help, and you can book a free call here: {booking_link}. No worries at "
                 "all if you've already sorted it out. {sender_first_name}"),
}
MAX_UNREADABLE_IN_A_ROW = 5
TOOL = {
    "name": "record_followup",
    "description": "Record the follow-up message.",
    "input_schema": {"type": "object", "properties": {"message": {"type": "string"}}, "required": ["message"]},
}
# send_message Result status (nothing pressed yet) -> the contacted.csv status it is recorded as.
RECORD = {"sent": "followup_sent", "refused": "followup_refused", "recipient": "followup_refused",
          "not_submitted": "followup_not_submitted", "invalid": "followup_not_submitted",
          "uncertain": "followup_uncertain", **{k: "followup_stopped" for k in ACCOUNT_WIDE}}


def load_config():
    return json.loads(CONFIG.read_text())


def settings(cfg):
    """outreach.followup over the defaults (a top-level followup block works too)."""
    return {**DEFAULTS, **cfg.get("followup", {}), **cfg["outreach"].get("followup", {})}


# ----------------------------------------------------------------- who
def why_not(akey, rows, cfg, leads, replied, fus, now=None):
    """Why this author must not get a follow-up now (checked again right before each), or None."""
    fu, now = settings(cfg), now or datetime.now()
    if akey.startswith("post:"):
        return "no profile link"
    dms = [r for r in rows if r["status"] not in store.FOLLOWUP_STATUSES]
    sent = [r for r in dms if r["status"] == "sent"]
    if not sent:
        return "no DM sent"
    if any(r["status"] == "recipient" for r in dms):
        return "Facebook says this person isn't available"
    mine = fus.get(akey, [])
    done = [r for r in mine if r["status"] in store.FOLLOWUP_DONE]
    if done:
        return f"already followed up ({done[-1]['status']}, {done[-1]['timestamp']})"
    tries = [r for r in mine if r["status"] in store.FOLLOWUP_ATTEMPTS]
    if len(tries) >= cfg["outreach"].get("max_attempts_per_recipient", store.MAX_ATTEMPTS):
        return f"gave up after {len(tries)} attempts"
    days = (now - datetime.fromisoformat(sent[-1]["timestamp"])).total_seconds() / 86400
    if days < fu["after_days"]:
        return f"DM sent {days:.1f} days ago (outreach.followup.after_days is {fu['after_days']})"
    if akey in replied:
        return "they replied"
    if inbox.answered("facebook", akey):
        return "answered from the inbox"
    lead = leads.get(sent[-1]["post_key"])
    if not lead:
        return "their lead isn't in leads.json"
    if lead.get("ai", {}).get("likely_minor", True):
        return "the AI thinks the author may be under 18"
    if not replies.our_dm(lead):
        return "our DM's text isn't in leads.json, so it can't be found in the chat"
    return None


def candidates(cfg, now=None):
    """[(author_key, name, post_key, dm text, days since the DM, lead)] for everyone who may get a follow-up now,
    oldest DM first."""
    leads, replied, fus, now = store.load_leads(), store.replies(), store.followups(), now or datetime.now()
    out = []
    for akey, rows in store.history().items():
        if why_not(akey, rows, cfg, leads, replied, fus, now):
            continue
        last = [r for r in rows if r["status"] == "sent"][-1]
        lead = leads[last["post_key"]]
        days = (now - datetime.fromisoformat(last["timestamp"])).days
        out.append((last["timestamp"], akey, lead.get("author") or last["author"], last["post_key"],
                    replies.our_dm(lead), days, lead))
    out.sort(key=lambda t: t[0])
    return [t[1:] for t in out]


def recheck(akey, cfg):
    return why_not(akey, store.history().get(akey, []), cfg, store.load_leads(), store.replies(), store.followups())


# ----------------------------------------------------------------- the chat
def after_dm(rows, dm):
    """What the chat shows after our DM: ('reply', their text), ('ours', our text: something we wrote after it), or
    (None, ''). LookupError if our DM isn't in the chat as a message from us."""
    reply = replies.find_reply(rows, dm)
    if reply is not None:
        return "reply", reply
    want = messenger.squash(dm)

    def is_dm(r):                                     # as in replies.find_reply
        have = messenger.squash(r.get("text"))
        return r.get("who") == "me" and have and (want in have or (len(have) >= 20 and have in want))

    mine = [i for i, r in enumerate(rows) if is_dm(r)]
    # A short piece of our DM (its link on a row of its own) or an empty row (a photo) isn't something new.
    later = [r for r in rows[mine[-1] + 1:] if r.get("who") == "me" and messenger.squash(r.get("text")) not in want]
    return ("ours", " / ".join(r["text"] for r in later)) if later else (None, "")


# ----------------------------------------------------------------- the draft
def template_text(cfg, lead):
    fu, values = settings(cfg), qualify.business_values(cfg)
    first = (lead.get("ai", {}).get("first_name") or "").strip() or "there"
    return fu["template"].format(**values, first_name=first).strip()


class Drafter:
    """Drafts each follow-up with Claude (qualify.py's client and model) from prompts/followup.md; the config
    template when use_ai is off, Claude can't be reached, or the draft is empty or too long."""

    def __init__(self, cfg):
        self.cfg, self.fu, self.client, self.off = cfg, settings(cfg), None, not settings(cfg)["use_ai"]

    def system(self):
        return PROMPT.read_text().format(**qualify.business_values(self.cfg))

    def message(self, lead, dm, days):
        return (qualify.post_message(lead) + f"\n\nTheir first name: {lead.get('ai', {}).get('first_name') or 'unknown'}"
                f"\n\nOur first message, sent {days} days ago, with no reply since:\n\"\"\"\n{dm}\n\"\"\"")

    def __call__(self, lead, dm, days):
        if not self.off:
            try:
                if self.client is None:
                    self.client = qualify.client()
                resp = self.client.messages.create(
                    model=self.cfg["ai"]["model"], max_tokens=400,
                    system=[{"type": "text", "text": self.system(), "cache_control": {"type": "ephemeral"}}],
                    tools=[TOOL], tool_choice={"type": "tool", "name": "record_followup"},
                    messages=[{"role": "user", "content": self.message(lead, dm, days)}])
                text = next(b for b in resp.content if b.type == "tool_use").input.get("message", "").strip()
                if text and len(text) <= self.fu["max_chars"] and messenger.squash(text) != messenger.squash(dm):
                    return text
                print(f"    [!] Claude's draft was empty or over {self.fu['max_chars']} chars; using the template")
            except Exception as exc:                  # no key, no network, API error: the template still works
                print(f"    [!] couldn't draft with Claude ({type(exc).__name__}: {exc}); using the template")
                if type(exc).__name__ in ("AuthenticationError", "AnthropicError"):
                    self.off = True                   # don't try again for every person this run
        return template_text(self.cfg, lead)


# ----------------------------------------------------------------- the run
def new_counts():
    counts = {s: 0 for s in ("sent", "uncertain", "refused", "not_submitted", "replied", "skipped", "unreadable")}
    counts.update(stopped="", halted=False)
    return counts


STREAKS = {"not_submitted": send.STREAKS["not_submitted"], "uncertain": send.STREAKS["uncertain"],
           "refused": send.STREAKS["refused"]}


def followup_loop(driver, people, cfg, send_fn=None, open_chat=None, read=None, draft=None):
    """Send each eligible follow-up within the daily DM cap. Returns counts plus 'stopped' and 'halted'."""
    out = cfg["outreach"]
    verify = out.get("verify_after_send", False)
    send_fn = send_fn or (lambda *a, **k: messenger.send_message(*a, verify=verify, **k))
    open_chat, read, draft = open_chat or messenger.open_chat, read or messenger.thread, draft or Drafter(cfg)
    cap = out["daily_dm_limit"]
    counts, streak, unreadable = new_counts(), {"status": None, "n": 0}, 0
    for i, (akey, name, post_key, dm, days, lead) in enumerate(people):
        done_today = store.sent_today()
        if done_today >= cap:
            counts["stopped"] = f"hit the daily cap ({done_today}/{cap} DMs today, follow-ups included)"
            break
        why = recheck(akey, cfg)
        if why:
            print(f"[=] skipping {name or akey}: {why}")
            counts["skipped"] += 1
            continue
        url = store.messenger_url(akey)
        print(f"\n[+] follow-up to {name} (DM {days} days ago; {done_today + 1}/{cap} today)")

        try:                                          # right before sending: did they (or we) write since?
            open_chat(driver, url, name)
            seen, said = after_dm(read(driver, name), dm)
        except AccountBlocked as exc:
            store.record(akey, name, post_key, "followup_stopped", f"{exc.kind}: {exc}")
            counts["stopped"], counts["halted"] = f"account-wide problem ({exc.kind}): {exc}", True
            break
        except (MessageRefused, NoSuchUser) as exc:
            store.record(akey, name, post_key, "followup_refused", f"before typing: {exc}")
            counts["refused"] += 1
            print(f"    [!] {exc}")
            continue
        except (LookupError, RuntimeError, TimeoutException) as exc:
            print(f"    [!] couldn't read the chat ({type(exc).__name__}: {exc}); trying again next run")
            counts["unreadable"] += 1
            unreadable += 1
            if unreadable >= MAX_UNREADABLE_IN_A_ROW:
                counts["stopped"], counts["halted"] = f"{unreadable} chats in a row couldn't be read", True
                break
            continue
        unreadable = 0
        if seen == "reply":
            replies.record(akey, name, post_key, said, url)
            counts["replied"] += 1
            print(f"    they replied ({said[:60]!r}); recorded in data/replies.csv, no follow-up")
            continue
        if seen == "ours":
            counts["skipped"] += 1
            print(f"    we already wrote to them after the DM ({said[:60]!r}); no follow-up")
            continue

        text = draft(lead, dm, days)
        submitted = []

        def before_submit():
            store.record(akey, name, post_key, "followup_submitting", text)
            submitted.append(True)

        try:
            result = send_fn(driver, url, text, name, before_submit=before_submit)
        except Exception as exc:                      # a bug or a dead browser, not a Facebook answer
            result = Result("uncertain" if submitted else "not_submitted", f"{type(exc).__name__}: {exc}")
        if submitted and result.status not in ("sent", "not_submitted"):
            status = "followup_uncertain"             # Send was pressed: whatever Facebook said, it may be out
        else:
            status = RECORD.get(result.status, "followup_not_submitted")
        note = f"{result.status}: {result.reason}" if status == "followup_stopped" else result.reason
        store.record(akey, name, post_key, status, note)

        if result.status in ACCOUNT_WIDE:
            counts["stopped"], counts["halted"] = f"account-wide problem ({result.status}): {result.reason}", True
            break
        kind = status.removeprefix("followup_")
        kind = {"stopped": "not_submitted"}.get(kind, kind)
        counts[kind] += 1
        if kind != "sent":
            print(f"    [!] {status}: {result.reason}")
        streak = ({"status": kind, "n": streak["n"] + 1} if kind == streak["status"] else {"status": kind, "n": 1})
        limit, why = STREAKS.get(kind, (None, ""))
        if limit and streak["n"] >= limit:
            counts["stopped"], counts["halted"] = f"{streak['n']} {why}", True
            break
        if i + 1 < len(people) and store.sent_today() < cap:
            delay = random.uniform(out["min_delay_seconds"], out["max_delay_seconds"])
            print(f"[+] Waiting {delay:.0f}s before the next one...")
            time.sleep(delay)
    return counts


def report(counts):
    print(f"\n[done] follow-ups sent {counts['sent']}, uncertain {counts['uncertain']}, refused {counts['refused']}, "
          f"failed before sending {counts['not_submitted']}, replied since {counts['replied']}, chats unreadable "
          f"{counts['unreadable']}, skipped {counts['skipped']}; {store.sent_today()} DMs today in total")
    if counts["stopped"]:
        print(f"[stopped] {counts['stopped']}")
    return 2 if counts["halted"] else 0


def run(cfg):
    fu = settings(cfg)
    if not fu["enabled"]:
        print("[+] Follow-ups are off (set outreach.followup.enabled to true in config.json).")
        return 0
    people = candidates(cfg)[:fu["max_per_run"]]
    if not people:
        print("[+] Nobody to follow up with.")
        return 0
    print(f"[+] {len(people)} people to follow up with")
    with store.lock():
        cap = cfg["outreach"]["daily_dm_limit"]
        if store.sent_today() >= cap:
            print(f"[+] Already {store.sent_today()}/{cap} DMs today. Nothing to do until tomorrow.")
            return 0
        driver = browser.make_driver(cfg)
        try:
            send.prepare(driver, cfg)
            return report(followup_loop(driver, people, cfg))
        except KeyboardInterrupt:
            print("\n[+] Interrupted. Everything recorded so far is in data/contacted.csv.")
            return 0
        finally:
            driver.quit()


def print_list(cfg):
    leads, replied, fus, n = store.load_leads(), store.replies(), store.followups(), 0
    for akey, rows in store.history().items():
        if not any(r["status"] == "sent" for r in rows):
            continue
        why = why_not(akey, rows, cfg, leads, replied, fus)
        n += not why
        print(f"  {rows[-1]['author'] or akey:<30}" + (f"  [skip: {why}]" if why else "  [will follow up]"))
    fu = settings(cfg)
    print(f"{n} to follow up with ({fu['max_per_run']} a run; {'on' if fu['enabled'] else 'OFF: outreach.followup.enabled'})")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", action="store_true", dest="list_only")
    args = ap.parse_args()
    cfg = load_config()
    if args.list_only:
        print_list(cfg)
    else:
        sys.exit(run(cfg))


if __name__ == "__main__":
    browser.halting(main)
