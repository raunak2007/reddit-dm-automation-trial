"""send.py - send the drafted messages to the leads qualify.py found, through the root Reddit sender.

Which leads go out (why_not(), checked for every lead; the first reason found is printed by --list):
    scored, relevant and at or above ai.min_score, with a drafted message
    posted within search.lookback_days (a lead the daily cap held back isn't messaged weeks later)
    not likely under 18, unless outreach.minor_policy is "allow" (the default "skip" never messages them:
        Reddit has no public-comment path here)
    a valid username that isn't this account
    nothing in the root contacted.csv against them (ledger.skip_reason: already sent, no such user, held,
        given up on). Someone with several posts is messaged once, about their best-scored one.
Best score first, then newest.

Every run writes those leads to data/queue.html in the format open_queue.parse_queue reads (one <li class=c> per
person with its compose link, <pre> body and Sent checkbox), so a person can read what would go out, or send it by
hand: python open_queue.py reddit/data/queue.html --no-checkmarks (open_queue.py records every send itself, so the
page's checkboxes only need importing if you ticked them while sending from the page in another browser; see
ledger.py import-checkmarks).

With outreach.auto_approve (the default) they are then sent the way run_loop.py sends a queue, with the same code:
ledger.lock() (one sending run at a time across every Reddit script), ledger.prepare() (logged in as the root
config.json's sender_username, this account's sent folder pulled into contacted.csv), ledger.send_loop() (the
daily cap daily_limit_per_account counted across every run (this folder's follow-ups, data/followups.csv,
are taken off it first), delays, a 'submitting' row before Send, stops on
account-wide problems and failure streaks) and ledger.report(). With auto_approve false nothing is sent: review
the queue and send it yourself.

The subject is outreach.subject for everyone when set (the queue format expects one subject for every entry);
leave it empty to use Claude's drafted subject per lead, in which case open_queue.py will refuse the queue's
entries with a different subject from the rest, though this script still sends them.

    python send.py              write the queue and send it (auto_approve)
    python send.py --list       who would be sent and why anyone isn't (no browser)

Returns / exits 2 when the run halted on an account-wide problem or a streak (ledger.report), 0 otherwise.
"""
from __future__ import annotations

import argparse
import html
import re
import sys
from datetime import datetime, timedelta

import browser
import ledger
import store
from send_dm import AccountBlocked, compose_link

USERNAME = re.compile(r"[A-Za-z0-9_-]+")


def dm_allowed(lead: dict, cfg: dict) -> bool:
    return not lead["ai"]["likely_minor"] or cfg["outreach"].get("minor_policy", "skip") == "allow"


def subject_for(lead: dict, cfg: dict) -> str:
    return (cfg["outreach"].get("subject") or lead["ai"].get("subject") or "").strip()[:100]


def why_not(lead: dict, cfg: dict, cfg_root: dict, sender: str | None = None, now: datetime | None = None):
    """Why this lead must not be messaged now, or None."""
    a = lead.get("ai")
    if not a:
        return "not scored yet"
    if not a.get("relevant") or a.get("score", 0) < cfg["ai"]["min_score"]:
        return f"score {a.get('score', 0)} (below ai.min_score {cfg['ai']['min_score']} or not relevant)"
    if not dm_allowed(lead, cfg):
        return "the AI thinks the author is under 18 and outreach.minor_policy doesn't allow a DM"
    if not (a.get("message") or "").strip():
        return "no drafted message"
    if not subject_for(lead, cfg):
        return "no subject (set outreach.subject)"
    cutoff = (now or datetime.now()) - timedelta(days=cfg["search"]["lookback_days"])
    if float(lead.get("created_utc") or 0) < cutoff.timestamp():
        return f"posted more than {cfg['search']['lookback_days']} days ago"
    author = lead.get("author") or ""
    if not USERNAME.fullmatch(author):
        return f"not a valid username: {author!r}"
    if sender and ledger.key(author) == ledger.key(sender):
        return "that's this account"
    return ledger.skip_reason(author, cfg_root.get("max_attempts_per_recipient", ledger.MAX_ATTEMPTS))


def candidates(leads: dict, cfg: dict, cfg_root: dict, sender: str | None = None):
    """(to send, [(lead, why not)]). To send: [(lead, username, subject, message, compose link)], best score
    first, then newest, one per person."""
    ranked = sorted((l for l in leads.values() if l.get("ai")),
                    key=lambda l: (-l["ai"].get("score", 0), -float(l.get("created_utc") or 0)))
    out, skipped, people = [], [], set()
    for lead in ranked:
        why = why_not(lead, cfg, cfg_root, sender)
        if not why and ledger.key(lead["author"]) in people:
            why = "already in this run for a better-scored post"
        if not why:
            subject, message = subject_for(lead, cfg), lead["ai"]["message"].strip()
            try:
                link = compose_link(lead["author"], message, subject)
            except ValueError as exc:
                why = str(exc)
            else:
                people.add(ledger.key(lead["author"]))
                out.append((lead, lead["author"], subject, message, link))
                continue
        skipped.append((lead, why))
    return out, skipped


# ----------------------------------------------------------------- the queue page
_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Reddit queue</title>
<style>
body{{font:15px/1.45 -apple-system,system-ui,sans-serif;max-width:860px;margin:0 auto;padding:16px;background:#fafafa;color:#222}}
ol{{padding-left:1.4em}} li.c{{background:#fff;border:1px solid #ddd;border-radius:8px;padding:12px 14px;margin:0 0 14px}}
pre{{white-space:pre-wrap;font:inherit;background:#f4f4f4;padding:10px;border-radius:6px}}
a.b{{display:inline-block;padding:6px 12px;background:#ff4500;color:#fff;border-radius:6px;text-decoration:none}}
.m{{color:#666;font-size:13px}} label{{margin-left:12px}}
@media (prefers-color-scheme: dark){{body{{background:#161616;color:#ddd}} li.c{{background:#202020;border-color:#333}} pre{{background:#2a2a2a}} .m{{color:#999}}}}
</style></head><body>
<h1>Reddit queue</h1>
<p class="m">{count} people, written {when} by reddit/send.py. Subject: {subject}. Each person is sent once, ever
(contacted.csv). Send by hand with <code>python open_queue.py reddit/data/queue.html --no-checkmarks</code>.</p>
<ol>
{items}
</ol>
<script>
const KEY = '{key}';
let marks = {{}};
try {{ marks = JSON.parse(localStorage.getItem(KEY) || '{{}}'); }} catch (e) {{}}
for (const box of document.querySelectorAll('input[data-u]')) {{
  box.checked = !!marks[box.dataset.u];
  box.addEventListener('change', () => {{
    if (box.checked) marks[box.dataset.u] = new Date().toISOString().slice(0, 10); else delete marks[box.dataset.u];
    try {{ localStorage.setItem(KEY, JSON.stringify(marks)); }} catch (e) {{}}
  }});
}}
</script>
</body></html>
"""

_ITEM = """<li class="c">
<b>u/{user}</b> <span class="m">score {score} &middot; r/{sub} &middot; {posted} &middot; <a href="{permalink}">post</a></span>
<p class="m">{title}<br>{need}</p>
<pre>{message}</pre>
<a class="b" href="{link}" target="_blank" rel="noopener">Open compose</a>
<label><input type="checkbox" data-u="{user}"> Sent</label>
</li>"""


def write_queue(entries: list, path=None, now: datetime | None = None):
    """Write entries [(lead, username, subject, message, link)] as a queue page open_queue.parse_queue reads.
    The page's localStorage key carries the time it was written, so checkmarks from an older page are never
    mistaken for this one's (ledger.import_checkmarks files imports by that key)."""
    path = path or store.QUEUE
    now = now or datetime.now()
    e = lambda s: html.escape(str(s or ""), quote=True)  # noqa: E731
    items = "\n".join(_ITEM.format(
        user=e(user), score=e(lead["ai"].get("score")), sub=e(lead.get("subreddit")), posted=e(lead.get("created_at")),
        permalink=e(lead.get("permalink")), title=e(lead.get("title")), need=e(lead["ai"].get("need")),
        message=e(message), link=e(link)) for lead, user, subject, message, link in entries)
    subjects = sorted({subject for _, _, subject, _, _ in entries})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_PAGE.format(count=len(entries), when=f"{now:%Y-%m-%d %H:%M}", items=items,
                                 subject=e(subjects[0]) if len(subjects) == 1 else "differs per person",
                                 key=f"reddit-queue-sent-{now:%Y%m%d-%H%M%S}"), encoding="utf-8")
    return path


# ----------------------------------------------------------------- sending
def print_list(cfg: dict, cfg_root: dict):
    out, skipped = candidates(store.load_leads(), cfg, cfg_root, cfg_root.get("sender_username"))
    for lead, user, subject, message, _ in out:
        print(f"  {lead['ai']['score']:>3} u/{user:<24} r/{lead.get('subreddit', ''):<18} {subject!r} "
              f"({len(message)} chars)")
    shown = [(l, why) for l, why in skipped if not why.startswith("score")]
    for lead, why in shown:
        print(f"  [skip] u/{lead.get('author', '?'):<24} {why}")
    print(f"\n{len(out)} would be sent (up to the daily cap: {store.sent_today()}/"
          f"{cfg_root['daily_limit_per_account']} today); {len(skipped)} not, "
          f"{len(skipped) - len(shown)} of them for their score")


def run(cfg: dict, cfg_root: dict | None = None, make_driver=None, send=None) -> int:
    """Write the queue; with auto_approve, send it. Returns the exit code (2: halted on an account-wide problem)."""
    cfg_root = store.load_root_config() if cfg_root is None else cfg_root
    out, _ = candidates(store.load_leads(), cfg, cfg_root, cfg_root.get("sender_username"))
    path = write_queue(out)
    print(f"[+] {len(out)} leads to message, written to {path}")
    from open_queue import parse_queue                   # the check open_queue.py / run_loop.py would make
    bad = [q for q in parse_queue(path) if q["problem"]]
    if bad:
        print(f"[!] open_queue.py would refuse {len(bad)} of the queue's entries (see above); this run still sends "
              f"them. Set outreach.subject so every entry has the same subject.")
    if not cfg["outreach"].get("auto_approve", True):
        print("[+] outreach.auto_approve is off: nothing sent. Review the queue, then send it with\n"
              f"    python open_queue.py {path} --no-checkmarks      (or --send to press Send for each)")
        return 0
    if not out:
        return 0
    cap = cfg_root["daily_limit_per_account"]
    targets = [(user, link) for _, user, _, _, link in out]
    with ledger.lock():
        if store.sent_today() >= cap:
            print(f"[+] Already sent {store.sent_today()}/{cap} today (follow-ups included). Nothing more until "
                  f"tomorrow.")
            return 0
        loop_cfg = cfg_root                         # ledger.sent_today() already counts today's follow-ups
        driver = (make_driver or browser.make_driver)(cfg)
        try:
            sender, code = start(driver, cfg_root)
            if not sender:
                return code
            counts = ledger.send_loop(driver, targets, loop_cfg, sender, **({"send": send} if send else {}))
        finally:
            driver.quit()
    return ledger.report(counts)


def start(driver, cfg_root: dict):
    """The start of every sending run here (send, answer, followup): logged in as the root config's
    sender_username (checked before anything is synced), then ledger.prepare() (the same check, and this
    account's sent folder pulled into contacted.csv). Returns (sender, 0), or (None, exit code): 2 when Reddit is
    refusing the account (logged out, wrong account, rate-limited, restricted), 1 for a setup problem such as a
    missing sender_username."""
    if cfg_root.get("sender_username"):
        try:
            browser.check_login(driver, cfg_root)
        except AccountBlocked as exc:
            print(f"[stopped] Not sending ({exc.kind}): {exc}")
            return None, 2
    try:
        return ledger.prepare(driver, cfg_root), 0
    except SystemExit as exc:
        print(exc.code if isinstance(exc.code, str) else f"[stopped] ledger.prepare exited ({exc.code})")
        return None, 2 if exc.code == 2 or str(exc.code).startswith("Not sending (") else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", action="store_true", help="who would be sent and why anyone isn't (no browser)")
    args = ap.parse_args()
    cfg, cfg_root = store.load_config(), store.load_root_config()
    if args.list:
        return print_list(cfg, cfg_root)
    sys.exit(run(cfg, cfg_root))


if __name__ == "__main__":
    main()
