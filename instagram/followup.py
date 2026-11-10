"""followup.py - one follow-up DM, ever, to people who got our DM and never wrote back.

    python run.py followup       (or python followup.py)
    python followup.py --list    who would get one now, and why others wouldn't (no browser)

Off unless config.json's outreach.followup.enabled is true. Who gets one: everyone data/contacted.csv records as
'sent' at least outreach.followup.after_days (6) days ago, with no reply in data/replies.csv, no answer sent (or
possibly sent) to them from the shared inbox record (inbox store.answered), no follow-up before, and not thought
to be under 18 (outreach.minor_policy). Oldest DM first, at most outreach.followup.max_per_run per run.

Each run: takes send.py's lock, checks the browser is logged in as outreach.sender_user_id (send.prepare), reads
the inbox and Message Requests through the web API for these people (replies.read_api; any reply found is saved
to data/replies.csv and that person is left alone), then for each one:

  1. Right before the send, reads their chat again through the API (direct_v2/threads/<id>/, without opening it
     or marking it seen) and skips them if they replied. A chat that can't be found or re-read is skipped too.
  2. Claude drafts the follow-up (qualify.py's client, prompts/followup.md), from the first DM and their comment;
     if AI is off or the draft fails, outreach.followup.template is used.
  3. Sends it like send.py: opens ig.me/m/<username>, checks the chat, types it, writes a 'followup_submitting'
     row, presses Send once, then records 'followup_sent' (or 'followup_uncertain', never retried, or
     'followup_failed', which may be tried again on a later run). Nobody is followed.

Follow-ups count toward outreach.daily_dm_limit (store.sent_today). Rows use the lead_key "followup:<username>",
so the review page and the first DM's held / sent logic don't change. It stops with exit code 2 on anything
account-wide (logged out, a challenge, an action block, a locked account) and after 3 failures or 2 uncertain
sends in a row, with the delays of outreach.min_delay_seconds to max_delay_seconds between sends.
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import browser
import direct
import qualify
import replies
import send
import store
from inbox import store as answers           # the shared record (repo root on sys.path via browser.py)
from send_dm import ACCOUNT_WIDE, AccountBlocked, Result

HERE = Path(__file__).resolve().parent
CONFIG = HERE / "config.json"
PROMPT = HERE / "prompts" / "followup.md"
DEFAULTS = {"enabled": False, "after_days": 6, "max_per_run": 5,
            "template": "hey {first_name}! just bumping my msg in case it got buried - still happy to help with "
                        "{topic} if you want: {booking_link}"}
MAX_FAILED_IN_A_ROW = 3
MAX_UNCERTAIN_IN_A_ROW = 2
MAX_CHARS = 600
TOOL = {
    "name": "record_followup",
    "description": "Record the one follow-up DM for this person.",
    "input_schema": {"type": "object", "properties": {"dm": {"type": "string"}}, "required": ["dm"]},
}


def settings(cfg: dict) -> dict:
    return {**DEFAULTS, **cfg["outreach"].get("followup", {})}


def lead_for(user: str, rows: list[dict], leads: dict) -> dict | None:
    """The lead our DM went to: the 'sent' row's lead, else any lead with that username."""
    key = next((r["lead_key"] for r in rows if r["status"] == "sent"), None)
    return leads.get(key) or next((l for l in leads.values() if l.get("username", "").lower() == user), None)


def why_not(user: str, at: str, rows: list[dict], lead: dict | None, replied: set[str], cfg: dict,
            now: datetime) -> str | None:
    """Why this person mustn't get a follow-up now, or None."""
    f, out = settings(cfg), cfg["outreach"]
    if datetime.fromisoformat(at) > now - timedelta(days=f["after_days"]):
        return f"DMed {at[:10]}, less than outreach.followup.after_days ({f['after_days']}) ago"
    extra = store.followup_rows(rows)
    if any(r["status"] in store.FOLLOWUP_DONE for r in extra):
        return "already followed up (one follow-up ever)"
    tries = out.get("max_attempts_per_recipient", store.MAX_ATTEMPTS)
    if len(extra) >= tries:
        return f"gave up after {len(extra)} failed follow-up attempts"
    if user in replied:
        return "they replied (data/replies.csv)"
    if answers.answered("instagram", user):
        return "we've sent them an answer (inbox)"
    if not lead or "ai" not in lead:
        return "no lead record to check their age against"
    if lead["ai"].get("likely_minor") and out.get("minor_policy") != "allow":
        return "the AI thinks they're under 18 and outreach.minor_policy doesn't allow a DM"
    return None


def candidates(cfg: dict, now: datetime | None = None) -> tuple[list[dict], list[tuple[str, str]]]:
    """([{"username", "sent_at", "lead"}] oldest DM first, [(username, why not)])."""
    now = now or datetime.now()
    hist, leads = store.history(), store.load_leads()
    replied = {r["username"].lower() for r in store.load_replies()}
    todo, skip = [], []
    for user, at in sorted(store.sent_at(hist).items(), key=lambda kv: kv[1]):
        lead = lead_for(user, hist[user], leads)
        why = why_not(user, at, hist[user], lead, replied, cfg, now)
        if why:
            skip.append((user, why))
        else:
            todo.append({"username": user, "sent_at": at, "lead": lead})
    return todo, skip


def first_dm(c: dict) -> str:
    return (c["lead"].get("approved") or {}).get("dm") or c["lead"]["ai"].get("dm") or ""


def from_template(cfg: dict, c: dict) -> str:
    lead = c["lead"]
    name = (lead["ai"].get("first_name") or qualify.first_name(lead) or "").strip().lower()
    need = (lead["ai"].get("need") or "").strip()
    topic = need if need and need.lower() != "none" and not need.startswith("(") else "college stuff"
    text = settings(cfg)["template"].format_map(qualify._Keep(qualify.values(cfg), topic=topic))
    text = text.replace("{first_name}", name) if name else text.replace(" {first_name}", "").replace("{first_name}", "")
    return text.strip()


def system_prompt(cfg: dict) -> str:
    return (PROMPT.read_text().format_map(qualify._Keep(qualify.values(cfg),
                                                        days=settings(cfg)["after_days"]))
            + "\n\nAlways answer by calling record_followup.")


def draft(cfg: dict, c: dict, client=None) -> tuple[str, str]:
    """(the follow-up text, 'ai' or 'template'). Claude with prompts/followup.md when ai.enabled, else (or if that
    fails or gives something unusable) outreach.followup.template."""
    if not cfg["ai"].get("enabled", True):
        return from_template(cfg, c), "template"
    lead = c["lead"]
    try:
        client = client or qualify.client()
        resp = client.messages.create(
            model=cfg["ai"]["model"], max_tokens=300,
            system=[{"type": "text", "text": system_prompt(cfg), "cache_control": {"type": "ephemeral"}}],
            tools=[TOOL], tool_choice={"type": "tool", "name": "record_followup"},
            messages=[{"role": "user", "content":
                       f"Their first name: {lead['ai'].get('first_name') or '(not known)'}\n"
                       f"{qualify.comment_message(lead)}\n\nOur first DM to them:\n\"\"\"\n{first_dm(c)}\n\"\"\""}])
        text = next(b for b in resp.content if b.type == "tool_use").input.get("dm", "").strip()
    except Exception as exc:                          # no key, API down, odd answer: the template will do
        print(f"    [!] Claude didn't draft it ({type(exc).__name__}: {exc}); using outreach.followup.template")
        return from_template(cfg, c), "template"
    if not text or len(text) > MAX_CHARS or re.search(r"\{[a-z_]+\}", text):
        print("    [!] Claude's draft was empty, too long or had a placeholder left; using outreach.followup.template")
        return from_template(cfg, c), "template"
    return text, "ai"


def replied_since(driver, cfg: dict, c: dict, found: dict) -> str | None:
    """Right before a send: read their chat again through the API. Returns why to skip them (they replied, or
    their chat can't be checked), else None. Any reply found is saved to data/replies.csv. Raises AccountBlocked."""
    user = c["username"]
    if user not in found:
        return "their chat wasn't in the inbox read, so a reply can't be ruled out"
    thread, box = found[user]
    if box == "request":
        return "their chat is in Message Requests"
    fresh = replies.thread(driver, cfg, thread.get("thread_id"))
    if fresh is None:
        return "couldn't read their chat again to check for a reply"
    rows, seen = replies.match([fresh], {user: c["sent_at"]})
    if user not in seen:
        return "the chat read again isn't a one-to-one chat with them"
    if rows:
        store.add_replies(rows)
        return f"they replied ({rows[-1]['snippet']!r})"
    return None


def status_for(result: Result, pressed: bool) -> str:
    if result.status == "sent":
        return "followup_sent"
    if result.status == "uncertain" or (pressed and result.status in ACCOUNT_WIDE):
        return "followup_uncertain"
    return "followup_failed"


def run(cfg: dict, send_fn=None, draft_fn=None) -> int:
    """Send this run's follow-ups. Returns the exit code (2: stopped on an account-wide problem or a streak)."""
    f, out = settings(cfg), cfg["outreach"]
    if not f["enabled"]:
        print("[+] Follow-ups are off (outreach.followup.enabled is false).")
        return 0
    send_fn = send_fn or (lambda *args, **kw: direct.send_message(*args, verify=out.get("verify_after_send", False),
                                                                  **kw))
    draft_fn = draft_fn or (lambda c: draft(cfg, c))
    todo, _ = candidates(cfg)
    if not todo:
        print("[+] Nobody is due a follow-up.")
        return 0
    cap = out["daily_dm_limit"]
    print(f"[+] {len(todo)} people due a follow-up; at most {f['max_per_run']} this run")
    with store.lock():
        if store.sent_today() >= cap:
            print(f"[+] Already sent {store.sent_today()}/{cap} DMs today; follow-ups wait for tomorrow.")
            return 0
        counts = {"followup_sent": 0, "followup_failed": 0, "followup_uncertain": 0, "skipped": 0}
        stopped, halted, streak, tried = "", False, {"status": None, "n": 0}, 0
        driver = browser.make_driver(cfg)
        try:
            send.prepare(driver, cfg)
            try:
                got = replies.read_api(driver, cfg, {c["username"]: c["sent_at"] for c in todo}, datetime.now())
            except AccountBlocked as exc:
                raise browser.Halt(f"account-wide problem while reading the inbox ({exc.kind}): {exc}")
            if got is None:
                print("[!] The inbox endpoint didn't answer, so replies can't be ruled out; no follow-ups this run.")
                return 0
            store.add_replies(got["rows"])
            for c in todo:
                user = c["username"]
                if tried >= f["max_per_run"]:
                    stopped = f"outreach.followup.max_per_run ({f['max_per_run']}) reached"
                    break
                if store.sent_today() >= cap:
                    stopped = f"hit the daily cap ({store.sent_today()}/{cap} DMs today)"
                    break
                if any(r["username"] == user for r in got["rows"]):
                    print(f"[=] @{user} replied; no follow-up")
                    counts["skipped"] += 1
                    continue
                if tried:
                    delay = random.uniform(out["min_delay_seconds"], out["max_delay_seconds"])
                    print(f"[+] Waiting {delay:.0f}s before the next one...")
                    time.sleep(delay)
                try:
                    why = replied_since(driver, cfg, c, got["threads"])
                except AccountBlocked as exc:
                    stopped, halted = f"account-wide problem ({exc.kind}) re-checking @{user}'s chat: {exc}", True
                    break
                if why:
                    print(f"[=] skipping @{user}: {why}")
                    counts["skipped"] += 1
                    continue
                text, how = draft_fn(c)
                print(f"\n[+] following up with @{user} (DMed {c['sent_at'][:10]}; {how} draft, {len(text)} chars)")
                tried += 1
                key, pressed = store.followup_key(user), []

                def before_submit():
                    store.record(user, key, "followup_submitting", "about to press Send on the follow-up")
                    pressed.append(True)

                try:
                    result = send_fn(driver, direct.chat_link(user), text, user, c["lead"].get("full_name", ""),
                                     before_submit=before_submit)
                except Exception as exc:              # a bug or a dead browser, not an Instagram answer
                    result = Result("uncertain" if pressed else "not_submitted", f"{type(exc).__name__}: {exc}")
                status = status_for(result, bool(pressed))
                if pressed or result.status not in ACCOUNT_WIDE + ("invalid",):   # those aren't their attempts
                    store.record(user, key, status, text if status == "followup_sent" else
                                 f"{result.status}: {result.reason}")
                counts[status] += 1
                if result.status in ACCOUNT_WIDE:
                    stopped, halted = f"account-wide problem ({result.status}): {result.reason}", True
                    break
                if status == "followup_sent":
                    print("    sent")
                    streak = {"status": None, "n": 0}
                    continue
                print(f"    [!] {status}: {result.reason}")
                streak = {"status": status, "n": streak["n"] + 1 if streak["status"] == status else 1}
                limit = MAX_FAILED_IN_A_ROW if status == "followup_failed" else MAX_UNCERTAIN_IN_A_ROW
                if streak["n"] >= limit:
                    stopped, halted = f"{streak['n']} follow-ups in a row {status}; check the account", True
                    break
        except KeyboardInterrupt:
            print("\n[+] Interrupted. Everything recorded so far is in data/contacted.csv.")
            return 0
        finally:
            driver.quit()
    print(f"\n[done] follow-ups sent {counts['followup_sent']}, failed {counts['followup_failed']}, uncertain "
          f"{counts['followup_uncertain']}, skipped {counts['skipped']}; {store.sent_today()}/{cap} DMs today")
    if stopped:
        print(f"[stopped] {stopped}")
    return 2 if halted else 0


def print_list(cfg: dict) -> None:
    todo, skip = candidates(cfg)
    if not settings(cfg)["enabled"]:
        print("(outreach.followup.enabled is false: nothing is sent until you turn it on)")
    for c in todo:
        print(f"  @{c['username']:<30} DMed {c['sent_at'][:16]}")
    for user, why in skip:
        print(f"  @{user:<30} [skip: {why}]")
    print(f"{len(todo)} due a follow-up, {len(skip)} not; {store.sent_today()}/{cfg['outreach']['daily_dm_limit']} "
          f"DMs sent today")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", action="store_true", dest="list_only", help="who is due one, without a browser")
    args = ap.parse_args()
    cfg = json.loads(CONFIG.read_text())
    if args.list_only:
        print_list(cfg)
        return
    try:
        sys.exit(run(cfg))
    except browser.Halt as stop:
        print(f"[stopped] {stop}")
        sys.exit(2)


if __name__ == "__main__":
    main()
