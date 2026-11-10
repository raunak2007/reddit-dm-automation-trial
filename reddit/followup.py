"""followup.py - one short follow-up, ever, to people who never answered the first DM. Off by default.

Who gets one (why_not(), checked for everyone the root contacted.csv has as 'sent'; --list prints the reasons):
    followup.enabled is true (it is false until you turn it on)
    their first DM is at least followup.after_days (6) old, and at most followup.max_days (30)
    they have never replied (nothing from them in data/replies.csv)
    they haven't been answered on the inbox page (inbox.store.answered: sending / sent / uncertain)
    no follow-up has gone to them yet (data/followups.csv: anything sent, possibly sent, refused or
        'no such user' rules them out for good; three failures before Send give up on them)
    the first DM was this folder's: data/leads.json has their post and the drafted message (people messaged
        by run_loop.py from target_users.csv have neither, so they are left alone), and no post of theirs was
        flagged as likely under 18 (whatever outreach.minor_policy says)
Oldest first DM first, up to followup.max_per_run a run, within the root daily cap.

The message: Claude (ai.model, prompts/followup.md, the same client setup as qualify.py) writes it from their post
and our first message; if Claude can't (no API key, an API error, an empty or overlong answer), the
followup.template from config.json is used, with {username}, {subreddit} and the business {placeholders}.
Subject: "re: <the first message's subject>".

Right before each send it reads the newest followup.inbox_pages page(s) of the inbox again (mark=false, as
replies.py does; new replies found are recorded in data/replies.csv) and skips the person if they have replied
since the last replies run, and checks the inbox page's answers again.

Recording: data/followups.csv, not contacted.csv, so the ledger's own logic (already sent / held / attempts,
sync_reddit_sent) is untouched. A 'submitting' row is written right before Send is clicked (the crash marker), then
the result's status. store.sent_today() = ledger.sent_today(), which counts first DMs and (reading followups.csv) today's
follow-ups, is the one count checked against the root daily_limit_per_account here, in send.py, and in
run_loop.py / open_queue.py.

Like send.py: ledger.lock(), logged in as sender_username with the sent folder pulled in (send.start ->
ledger.prepare), the root config's min/max_delay_seconds between sends, and exit code 2 on anything account-wide
(logged out, rate-limited, restricted, a block page while re-reading the inbox) or a failure streak.

    python followup.py           send the follow-ups that are due
    python followup.py --list    who would get one and why anyone doesn't (no browser, no Claude)
"""
from __future__ import annotations

import argparse
import random
import sys
import time
from datetime import datetime, timedelta

import store  # first: it puts the repo root on sys.path
import browser  # noqa: E402
import ledger  # noqa: E402
import qualify  # noqa: E402
import replies  # noqa: E402
import send  # noqa: E402
from inbox import store as inbox  # noqa: E402
from send_dm import ACCOUNT_WIDE, AccountBlocked, Result, compose_link, send_compose  # noqa: E402

PLATFORM = "reddit"
MAX_CHARS = 1200                         # a drafted follow-up longer than this is replaced by the template
DEFAULT_TEMPLATE = ("Hi u/{username}, following up once on my message about your post in r/{subreddit}. If it would "
                    "still help, {offer} is open, no pressure: {booking_link}\n- {sender_first_name}")
TOOL = {
    "name": "record_followup",
    "description": "Record the follow-up private message.",
    "input_schema": {"type": "object", "properties": {"message": {"type": "string"}}, "required": ["message"]},
}


def settings(cfg: dict) -> dict:
    f = cfg.get("followup") or {}
    return {"enabled": bool(f.get("enabled", False)), "after_days": float(f.get("after_days", 6)),
            "max_days": float(f.get("max_days", 30)), "max_per_run": int(f.get("max_per_run", 5)),
            "inbox_pages": int(f.get("inbox_pages", 1)), "template": f.get("template") or DEFAULT_TEMPLATE}


def leads_by_author(leads: dict) -> dict[str, list[dict]]:
    out = {}
    for lead in leads.values():
        if lead.get("ai") and lead.get("author"):
            out.setdefault(ledger.key(lead["author"]), []).append(lead)
    return out


def first_lead(mine: list[dict]) -> dict | None:
    """The post the first DM was about: send.py messages a person about their best-scored post."""
    drafted = [l for l in mine if (l["ai"].get("message") or "").strip()]
    return max(drafted, key=lambda l: (l["ai"].get("score", 0), float(l.get("created_utc") or 0)), default=None)


def why_not(person: str, first_at: datetime, cfg: dict, cfg_root: dict, mine: list[dict], replied: set,
            sender: str | None = None, now: datetime | None = None):
    """Why this person must not get a follow-up now, or None."""
    s, now = settings(cfg), now or datetime.now()
    k = ledger.key(person)
    if not send.USERNAME.fullmatch(person):
        return f"not a valid username: {person!r}"
    if sender and k == ledger.key(sender):
        return "that's this account"
    if first_at > now - timedelta(days=s["after_days"]):
        return f"first DM {first_at:%Y-%m-%d %H:%M} is less than followup.after_days ({s['after_days']:g}) old"
    if first_at < now - timedelta(days=s["max_days"]):
        return f"first DM {first_at:%Y-%m-%d} is more than followup.max_days ({s['max_days']:g}) old"
    if k in replied:
        return "they replied (data/replies.csv)"
    if inbox.answered(PLATFORM, person):
        return "already answered on the inbox page"
    rows = store.followed_up().get(k, [])
    done = [r for r in rows if r["status"] in store.FOLLOWUP_DONE]
    if done:
        return f"follow-up already {done[-1]['status']} ({done[-1]['timestamp']})"
    tries = [r for r in rows if r["status"] == "not_submitted"]
    if len(tries) >= cfg_root.get("max_attempts_per_recipient", ledger.MAX_ATTEMPTS):
        return f"gave up on the follow-up after {len(tries)} failures before Send"
    if not mine or not first_lead(mine):
        return "no first message on record in data/leads.json (not messaged by this folder)"
    if any(l["ai"].get("likely_minor") for l in mine):
        return "the AI thinks the author is under 18"
    return None


def candidates(cfg: dict, cfg_root: dict, sender: str | None = None, now: datetime | None = None):
    """(due, [(person, why not)]). due: [(person, lead, first sent at)], oldest first DM first."""
    by_author = leads_by_author(store.load_leads())
    replied = {ledger.key(r["username"]) for r in store.load_replies()}
    names = {ledger.key(r["username"]): r["username"] for r in ledger._rows()} if ledger.LOG.exists() else {}
    due, skipped = [], []
    for k, first_at in sorted(store.first_sent().items(), key=lambda kv: kv[1]):
        person = names.get(k, k)
        mine = by_author.get(k, [])
        why = why_not(person, first_at, cfg, cfg_root, mine, replied, sender, now)
        if why:
            skipped.append((person, why))
        else:
            due.append((person, first_lead(mine), first_at))
    return due, skipped


# ----------------------------------------------------------------- the message
def template_message(cfg: dict, person: str, lead: dict) -> str:
    return settings(cfg)["template"].format(**qualify.business_values(cfg), username=person,
                                            subreddit=lead.get("subreddit") or "").strip()


def prompt(cfg: dict) -> str:
    return (qualify.PROMPTS / "followup.md").read_text().format(**qualify.business_values(cfg))


def details(cfg: dict, person: str, lead: dict, first_at: datetime) -> str:
    return (f"Username: {person}\nSubreddit: r/{lead.get('subreddit') or 'unknown'}\n"
            f"First message sent: {first_at:%B %d, %Y} ({(datetime.now() - first_at).days} days ago)\n\n"
            f"Their post's title: {lead.get('title', '')}\n\nTheir post:\n\"\"\"\n"
            f"{(lead.get('text') or '(no text, title only)')[:2500]}\n\"\"\"\n\n"
            f"Our first message (subject {send.subject_for(lead, cfg)!r}):\n\"\"\"\n"
            f"{lead['ai']['message'].strip()}\n\"\"\"")


def draft(client, cfg: dict, person: str, lead: dict, first_at: datetime) -> tuple[str, str]:
    """(message, 'claude' | 'template: <why>')."""
    if client is None:
        return template_message(cfg, person, lead), "template: no Claude client"
    try:
        resp = client.messages.create(
            model=cfg["ai"]["model"], max_tokens=600,
            system=[{"type": "text", "text": prompt(cfg), "cache_control": {"type": "ephemeral"}}],
            tools=[TOOL], tool_choice={"type": "tool", "name": "record_followup"},
            messages=[{"role": "user", "content": details(cfg, person, lead, first_at)}])
        text = (next(b for b in resp.content if b.type == "tool_use").input.get("message") or "").strip()
    except Exception as exc:                               # API errors, a missing key, an odd response
        return template_message(cfg, person, lead), f"template: Claude failed ({type(exc).__name__}: {exc})"
    if not text or len(text) > MAX_CHARS:
        return template_message(cfg, person, lead), f"template: Claude's draft was {len(text)} characters"
    return text, "claude"


def subject_for(lead: dict, cfg: dict) -> str:
    first = send.subject_for(lead, cfg) or "About your post"
    return (first if first.lower().startswith("re:") else f"re: {first}")[:100]


# ----------------------------------------------------------------- the live check before each send
def replied_since(driver, cfg: dict, person: str, sender: str, fetch=None) -> bool:
    """Read the newest followup.inbox_pages page(s) of the inbox (mark=false) and record any new replies, as
    replies.py does. True if this person has replied (now, or in data/replies.csv already). AccountBlocked
    propagates (the run stops); a page Reddit won't serve counts as a reply, so nobody is followed up blind."""
    fetch = fetch or browser.fetch_json
    sent, known, after = store.first_sent(), {r["message_id"] for r in store.load_replies()}, None
    for _ in range(settings(cfg)["inbox_pages"]):
        browser.pause(cfg, 0.5)
        try:
            data = fetch(driver, replies.INBOX + (f"&after={after}" if after else ""))["data"]
            children = data["children"]
        except (browser.PageSkipped, KeyError, TypeError) as exc:
            print(f"    [!] the inbox couldn't be read ({exc}); not following up u/{person} this run")
            return True
        found = replies.match(children, sent, known, sender)
        if found:
            now = datetime.now().isoformat(timespec="seconds")
            store.add_replies([dict(r, recorded_at=now) for r in found])
        after = data.get("after")
        if not after or not children:
            break
    return any(ledger.key(r["username"]) == ledger.key(person) for r in store.load_replies())


# ----------------------------------------------------------------- sending
def print_list(cfg: dict, cfg_root: dict):
    due, skipped = candidates(cfg, cfg_root, cfg_root.get("sender_username"))
    if not settings(cfg)["enabled"]:
        print("[!] followup.enabled is false: nothing is sent until you turn it on. Who would be due:")
    for person, lead, first_at in due:
        print(f"  u/{person:<24} first DM {first_at:%Y-%m-%d}  r/{lead.get('subreddit', '')}")
    for person, why in skipped:
        if not why.startswith(("first DM", "follow-up already")):
            print(f"  [skip] u/{person:<24} {why}")
    print(f"\n{len(due)} due (at most {settings(cfg)['max_per_run']} a run, within the daily cap: "
          f"{store.sent_today()}/{cfg_root['daily_limit_per_account']} today); {len(skipped)} not")


def run(cfg: dict, cfg_root: dict | None = None, make_driver=None, send_fn=None, client=None, fetch=None) -> int:
    """Send the follow-ups that are due. Returns the exit code (2: halted on an account-wide problem or a streak,
    1: a setup problem)."""
    cfg_root = store.load_root_config() if cfg_root is None else cfg_root
    s = settings(cfg)
    if not s["enabled"]:
        print("[+] followup.enabled is false in reddit/config.json: no follow-ups sent.")
        return 0
    try:
        template_message(cfg, "someone", {"subreddit": "x"})
    except (KeyError, IndexError, ValueError) as exc:
        print(f"[!] followup.template has an unknown placeholder or a stray brace ({exc!r}); not sending.")
        return 1
    due, _ = candidates(cfg, cfg_root, cfg_root.get("sender_username"))
    if not due:
        print("[+] No follow-ups due.")
        return 0
    cap = cfg_root["daily_limit_per_account"]
    verify = cfg_root.get("verify_after_send", False)
    send_fn = send_fn or (lambda *a, **k: send_compose(*a, verify=verify, **k))
    counts = {k: 0 for k in ("sent", "uncertain", "refused", "recipient", "not_submitted", "invalid", "skipped")}
    stopped, halted, streak, tried = "", False, {"status": None, "n": 0}, 0
    with ledger.lock():
        if store.sent_today() >= cap:
            print(f"[+] Already sent {store.sent_today()}/{cap} today. No follow-ups until tomorrow.")
            return 0
        driver = (make_driver or browser.make_driver)(cfg)
        try:
            sender, code = send.start(driver, cfg_root)
            if not sender:
                return code
            due, _ = candidates(cfg, cfg_root, sender)  # again: the sent folder sync may have added people
            if client is None:
                try:
                    client = qualify.make_client()
                except Exception as exc:
                    print(f"[!] no Claude client ({type(exc).__name__}: {exc}); using followup.template")
            for person, lead, first_at in due:
                if tried >= s["max_per_run"]:
                    stopped = f"sent followup.max_per_run ({s['max_per_run']})"
                    break
                if store.sent_today() >= cap:
                    stopped = f"hit the daily cap ({store.sent_today()}/{cap} sent today, follow-ups included)"
                    break
                if tried:
                    delay = random.uniform(cfg_root["min_delay_seconds"], cfg_root["max_delay_seconds"])
                    print(f"[+] Waiting {delay:.0f}s before the next one...")
                    time.sleep(delay)
                message, how = draft(client, cfg, person, lead, first_at)
                subject = subject_for(lead, cfg)
                print(f"\n[+] follow-up to u/{person} (first DM {first_at:%Y-%m-%d}; {how})")
                try:
                    if replied_since(driver, cfg, person, sender, fetch) or inbox.answered(PLATFORM, person):
                        print(f"    [=] u/{person} has replied or been answered: no follow-up")
                        counts["skipped"] += 1
                        continue
                except AccountBlocked as exc:
                    stopped, halted = f"account-wide problem ({exc.kind}) reading the inbox: {exc}", True
                    break
                try:
                    link = compose_link(person, message, subject)
                except ValueError as exc:
                    print(f"    [!] not sent: {exc}")
                    counts["invalid"] += 1
                    continue
                tried += 1
                submitted = []

                def before_submit():
                    store.record_followup(person, "submitting", "about to click Send", post_id=lead.get("post_id"),
                                          subject=subject, message=message)
                    submitted.append(True)

                try:
                    result = send_fn(driver, link, sender=sender, before_submit=before_submit)
                except Exception as exc:                  # a bug or a dead browser, not a Reddit answer
                    result = Result("uncertain" if submitted else "not_submitted", f"{type(exc).__name__}: {exc}")
                if result.status != "invalid" or submitted:
                    store.record_followup(person, result.status, result.reason, post_id=lead.get("post_id"),
                                          subject=subject, message=message)
                if result.status in ACCOUNT_WIDE:
                    stopped, halted = f"account-wide problem ({result.status}): {result.reason}", True
                    break
                counts[result.status] += 1
                if result.status != "sent":
                    print(f"    [!] {ledger.NOTES.get(result.status, result.status)}: {result.reason}")
                if result.status == "invalid":
                    continue
                streak = ({"status": result.status, "n": streak["n"] + 1} if result.status == streak["status"]
                          else {"status": result.status, "n": 1})
                limit, why = ledger.STREAKS.get(result.status, (None, ""))
                if limit and streak["n"] >= limit:
                    stopped, halted = f"{streak['n']} {why}", True
                    break
        finally:
            driver.quit()
    print(f"\n[done] follow-ups sent {counts['sent']}, uncertain {counts['uncertain']}, refused {counts['refused']}, "
          f"no such user {counts['recipient']}, failed before submitting {counts['not_submitted']}, "
          f"skipped {counts['skipped']}; {store.sent_today()}/{cap} sent today, follow-ups included")
    if stopped:
        print(f"[stopped] {stopped}")
    return 2 if halted else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", action="store_true", help="who would get a follow-up and why anyone doesn't")
    args = ap.parse_args()
    cfg, cfg_root = store.load_config(), store.load_root_config()
    if args.list:
        return print_list(cfg, cfg_root)
    sys.exit(run(cfg, cfg_root))


if __name__ == "__main__":
    main()
