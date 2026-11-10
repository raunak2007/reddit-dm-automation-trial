"""comment.py - post the drafted public comment on leads that won't get a DM, the way send.py sends DMs.

    python comment.py                     post comments now (python run.py comment; python run.py runs it after
                                          sending when outreach.auto_comment is true)
    python comment.py --list              which leads would get a comment, and why the others won't (no browser)
    python comment.py held                comments whose outcome is unknown (the next run looks for them)
    python comment.py resolve <post_key> --commented | --not-commented     settle one by hand

Which leads: the comment is the alternative to a DM, so it goes only where send.py won't send one: authors the AI
thinks are under 18 (outreach.minor_policy "comment_only"), and only with outreach.auto_comment_minors (off by
default: a public reply to a likely minor is left to you, in the queue), and leads with no profile link to message.
Each must be at or above ai.min_score, have a drafted comment (your edit in the queue, if you made one) and a link
to the post, and still be in "To review": a post you skipped, marked or commented on by hand is left alone, and so
is any author who has been DMed, commented on, held, or has a DM approved and waiting, on any of their posts. As
with "Mark commented", a comment counts as contacting that author: they're never DMed or commented on again.

For each one, best score first: opens the post, finds that post (the article showing the author's name and the
start of the post's text) and its own comment box (a "Write a comment" box that isn't a reply box inside someone
else's comment), checks the comment isn't already there, types it (line breaks as Shift+Enter), writes a
'comment_submitting' row to data/contacted.csv, presses Enter in the box once, and records 'commented' as soon as
the box clears with no error (the queue's "Mark commented" writes the same status). If something goes wrong after
Enter and the comment can't be seen on the post, it is 'comment_uncertain' and HELD: never posted again until it is
found on the post (the next run looks, outreach.auto_resolve) or you settle it by hand. A held comment is never
cleared automatically, because Facebook doesn't always show every comment on a post: not seeing it isn't proof.

The run stops (exit code 2) on anything account-wide (logged out, checkpoint, "temporarily blocked", "we limit how
often you can comment") and on streaks of failures, closed posts or uncertain comments. outreach.daily_comment_limit
(default 10) counts every comment today, from here or "Mark commented", separately from the DM cap; it waits
outreach.comment_min_delay_seconds to comment_max_delay_seconds between comments, and takes the same lock as
send.py (they share the Chrome profile).
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import browser
import messenger
import send
import store
from queue_server import dm_allowed, status_of
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.keys import Keys
from send_dm import ACCOUNT_WIDE, AccountBlocked, Result, StepFailed, pause, step, type_text, wait_for
from send_dm import js as sdm

HERE = Path(__file__).resolve().parent
CONFIG = HERE / "config.json"
POST_URL = re.compile(r"^https://(www|web|m)\.facebook\.com/\S+$")
SETTLE = 2               # seconds to watch a freshly opened post for a block dialog
SNIPPET = 40             # characters of the post's text used to tell it from other posts on the page
DAILY_LIMIT = 10
MAX_FAILS_IN_A_ROW = 3
MAX_CLOSED_IN_A_ROW = 3  # posts in a row that won't take a comment: more likely this account than the posts
MAX_UNCERTAIN_IN_A_ROW = 2
STREAKS = {
    "not_submitted": (MAX_FAILS_IN_A_ROW, "failures before posting in a row; check the account and the saved "
                                          "comment*_failed files"),
    "closed": (MAX_CLOSED_IN_A_ROW, "posts in a row didn't take a comment from this account; that can be a "
                                    "restriction on the account"),
    "uncertain": (MAX_UNCERTAIN_IN_A_ROW, "comments in a row couldn't be confirmed (python comment.py held)"),
}
# Result status -> the contacted.csv status it is recorded as.
RECORD = {"commented": "commented", "closed": "comment_closed", "not_submitted": "comment_not_submitted",
          "uncertain": "comment_uncertain", **{k: "comment_stopped" for k in ACCOUNT_WIDE}}
NOTES = {"closed": "the post doesn't take comments from this account; won't retry it",
         "not_submitted": "failed before Enter was pressed; will retry on a later run",
         "uncertain": "Enter was pressed but the comment couldn't be confirmed; HELD",
         "invalid": "bad entry; not posted"}
# The post saying it won't take a comment (only read when it has no comment box).
CLOSED = (r"commenting (has been |is )?(turned off|disabled|limited)|comments? (are|have been) (turned off|disabled|"
          r"limited)|only (members|admins|certain people|people [^.]{0,40}) can comment|join (the |this )?group to "
          r"(comment|participate)|you can.t comment")
FAILED = r"couldn.t post|could not post|unable to post|failed to post|comment (failed|wasn.t posted|couldn.t be posted)"

# Page-side helpers for a Facebook post. A post is a top-level [role=article]; its comments are [role=article]s
# inside it (aria-label "Comment by ..."), and each comment's reply box lives inside that comment.
_JS = r"""
const FBP = (() => {
  const BOX = '[contenteditable="true"], [role="textbox"], textarea, input';
  const OWN_BOX = /^(write a (public )?comment|comment as |leave a comment|add a comment|write an answer)/i;
  const visible = el => { const r = el.getBoundingClientRect();
    return el.isConnected && r.width > 2 && r.height > 2 && getComputedStyle(el).visibility !== 'hidden'; };
  const squash = s => (s || '').replace(/[\s​-‍﻿]+/g, '').toLowerCase();
  function text(root, sep) {                     // as in messenger.py: no text boxes, emoji from <img alt>
    const out = [];
    const walk = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT, {acceptNode: n =>
      n.nodeType === 1 && (n.matches(BOX) || /^(SCRIPT|STYLE|NOSCRIPT|TEMPLATE)$/.test(n.tagName))
        ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT});
    for (let n = walk.nextNode(); n; n = walk.nextNode()) {
      if (n.nodeType === 3) out.push(n.nodeValue);
      else if (n.tagName === 'IMG' && n.alt) out.push(n.alt);
    }
    return out.join(sep);
  }
  const label = el => (el.getAttribute('aria-label') || el.getAttribute('aria-placeholder') ||
                       el.getAttribute('placeholder') || '').trim();
  const outer = a => !(a.parentElement && a.parentElement.closest('[role="article"]'));
  const isComment = a => !outer(a) || /^(comment|reply) by /i.test(a.getAttribute('aria-label') || '');
  // Inside someone's comment (or reply): any article around el is nested in another one, or labelled as a comment.
  const inComment = el => { for (let a = el.closest('[role="article"]'); a; a = a.parentElement && a.parentElement.closest('[role="article"]'))
                              if (isComment(a)) return true;
                            return false; };
  const posts = () => [...document.querySelectorAll('[role="article"]')].filter(a => outer(a) && !isComment(a));
  const boxes = scope => [...scope.querySelectorAll('[contenteditable="true"]')].filter(b =>
    visible(b) && OWN_BOX.test(label(b)) && !/repl(y|ies)/i.test(label(b)) && !inComment(b));
  // The post by `author` starting with `snippet`, and its own comment box: inside the post, or in the nearest
  // container around it that holds no other post (some layouts put the box just after the article).
  function locate(author, snippet) {
    const all = posts();
    const fits = el => { const t = squash(text(el, '')); return t.includes(squash(author)) && t.includes(squash(snippet)); };
    const hits = all.filter(fits);
    if (hits.length > 1) return {why: `${hits.length} posts on the page look like this one`};
    if (!hits.length) return {why: all.length ? `none of the ${all.length} posts on the page is this one` : 'no post on the page'};
    for (let el = hits[0]; el && el !== document.documentElement; el = el.parentElement) {
      if (el !== hits[0] && all.some(p => p !== hits[0] && el.contains(p))) break;
      const found = boxes(el);
      if (found.length === 1) return {post: hits[0], scope: el, box: found[0]};
      if (found.length > 1) return {post: hits[0], scope: el, why: `${found.length} comment boxes for this post`};
    }
    return {post: hits[0], scope: hits[0], why: 'no comment box for this post'};
  }
  // Dialogs and alerts that aren't the post itself (a post can open as a dialog over the feed).
  const overlays = () => [...document.querySelectorAll('[role="dialog"], [role="alertdialog"], [role="alert"]')]
    .filter(d => visible(d) && !d.querySelector('[role="article"]') && !d.querySelector('[contenteditable="true"]'));
  return {
    find: (author, snippet, closed) => {
      const r = locate(author, snippet);
      let said = '';
      if (!r.box && r.scope) {
        const re = new RegExp(closed, 'i');
        for (const t of [text(r.scope, ' '), ...overlays().map(d => text(d, ' '))]) {
          const m = re.exec(t.replace(/\s+/g, ' '));
          if (m) { said = m[0]; break; }
        }
      }
      return {box: r.box || null, why: r.why || '', found: !!r.post, closed: said};
    },
    has: (author, snippet, want) => {
      const r = locate(author, snippet);
      return !!(r.scope || r.post) && squash(text(r.scope || r.post, '')).includes(squash(want));
    },
    notice: pattern => {
      const re = new RegExp(pattern, 'i');
      for (const d of overlays()) {
        const t = text(d, ' ').replace(/\s+/g, ' ');
        const m = re.exec(t);
        if (m) return t.slice(Math.max(0, m.index - 40), m.index + m[0].length + 40).trim();
      }
      return '';
    },
    // A password field (never on a logged-in page), or an email field where the post should be.
    login: () => [...document.querySelectorAll('input[name="pass"]')].some(visible) ||
                 ([...document.querySelectorAll('input[name="email"]')].some(visible) && !posts().length),
  };
})();
return FBP[arguments[0]].apply(null, [].slice.call(arguments, 1));
"""


def js(driver, fn, *args):
    return driver.execute_script(_JS, fn, *args)


def load_config():
    return json.loads(CONFIG.read_text())


@dataclass
class Posted(Result):
    """send_dm.Result for one comment. status: 'commented', 'closed' (the post won't take one), 'not_submitted',
    'uncertain', 'invalid', or 'login' / 'rate_limited' / 'restricted' (account-wide, before Enter). stop: the
    account-wide kind Facebook showed after Enter was pressed (the comment is then 'commented' or 'uncertain'
    and the run stops too)."""
    stop: str = ""

    def __bool__(self):
        return self.status == "commented"


# ----------------------------------------------------------------- the page
def check_account(driver):
    """Raise AccountBlocked if Facebook is refusing this account: sent to the login page or a checkpoint, a login
    form instead of the post, or a block/restriction dialog. The post's own text and its comments are never
    read for this (people write "blocked" in posts); only dialogs and alerts over the page are."""
    url = driver.current_url
    if re.search(r"facebook\.com/(login|checkpoint)", url):
        raise AccountBlocked(f"Facebook sent this profile to {url}", "restricted" if "checkpoint" in url else "login")
    if js(driver, "login"):
        raise AccountBlocked("Facebook is showing its login form instead of the post", "login")
    for kind, pattern in messenger.ACCOUNT:
        said = js(driver, "notice", pattern)
        if said:
            raise AccountBlocked(f"Facebook is refusing this account: {said!r}", kind)


class PostClosed(RuntimeError):
    """The post doesn't take comments from this account (turned off, members only, ...)."""


def find_box(driver, author, snippet):
    """The post's own comment box. Raises PostClosed if the post says it takes no comments, RuntimeError if the
    post or its box can't be told apart from the rest of the page (nothing is typed then)."""
    r = js(driver, "find", author, snippet, CLOSED)
    if r["box"] is not None:
        return r["box"]
    if r["closed"]:
        raise PostClosed(f"the post says {r['closed']!r}")
    raise RuntimeError(r["why"] or "no comment box")


def open_post(driver, url, author, snippet):
    """Open the post and wait for its own comment box. Raises before anything is typed if Facebook refuses the
    account, the post takes no comments, or the box can't be found."""
    driver.get(url)
    last = {}

    def ready(d):
        check_account(d)
        try:
            return find_box(d, author, snippet)
        except PostClosed:
            raise
        except RuntimeError as exc:
            last["why"] = str(exc)
            return None

    try:
        wait_for(driver, ready, "the post and its comment box")
    except TimeoutException as exc:
        raise RuntimeError(f"{last.get('why') or exc}; not typing anything") from None
    deadline = time.monotonic() + SETTLE
    while time.monotonic() < deadline:
        check_account(driver)
        time.sleep(0.3)
    return find_box(driver, author, snippet)


def on_post(driver, author, snippet, text):
    return js(driver, "has", author, snippet, text)


def type_comment(driver, author, snippet, text):
    box = find_box(driver, author, snippet)
    pause()
    type_text(driver, box, text)
    have = sdm(driver, "read", box)
    if messenger.squash(have) != messenger.squash(text):
        raise RuntimeError(f"the comment box shows {have[:80]!r}, not the drafted comment; not posting")
    return box


def submit(driver, box, about_to_press):
    """Press Enter in the post's comment box exactly once (Enter posts a Facebook comment; there is no fallback,
    since a second try could post twice). Returns the failure notice already showing, so confirm() only reacts to a
    new one."""
    check_account(driver)
    failed_before = js(driver, "notice", FAILED)
    pause(0.3, 0.8)
    about_to_press()
    box.send_keys(Keys.ENTER)
    return failed_before


def confirm(driver, author, snippet, failed_before):
    """Taken = the post's comment box is empty again with no new failure notice."""
    def outcome(d):
        check_account(d)
        failed = js(d, "notice", FAILED)
        if failed and failed != failed_before:
            raise RuntimeError(f"Facebook says the comment didn't post: {failed!r}")
        r = js(d, "find", author, snippet, CLOSED)
        return r["box"] is not None and not messenger.squash(sdm(d, "read", r["box"]))

    wait_for(driver, outcome, "Facebook to take the comment")


def was_posted(driver, url, text, author, snippet):
    """For a held comment: True if it is on the post after reloading it. Raises RuntimeError if it can't be seen
    (it stays held: not seeing it isn't proof, Facebook doesn't always show every comment), AccountBlocked if
    Facebook is refusing this account."""
    driver.switch_to.default_content()
    driver.get(url)
    try:
        wait_for(driver, lambda d: check_account(d) or on_post(d, author, snippet, text), "the comment on the post",
                 timeout=15)
        return True
    except TimeoutException:
        raise RuntimeError("the comment isn't showing on the post") from None


def post_comment(driver, url, text, author, snippet, *, before_submit=None):
    """Open the post, find its own comment box, type the comment, press Enter once and wait for Facebook to take it.
    before_submit(): called right before Enter is pressed (comment.py writes its 'comment_submitting' row there)."""
    if not POST_URL.match(url or "") or not text.strip() or not (author or "").strip():
        return Posted("invalid", f"comment: bad entry (link {url!r}, author {author!r}, {len(text)} chars)")
    pressed = {"at": None}

    def about_to_press():
        if before_submit:
            before_submit()
        pressed["at"] = time.time()

    try:
        driver.switch_to.default_content()
        with step(driver, "comment1_open_post", f"opening {author}'s post"):
            open_post(driver, url, author, snippet)
        if on_post(driver, author, snippet, text):
            print("    this comment is already on the post; not posting it again")
            return Posted("commented", "comment: already on the post")
        with step(driver, "comment2_type", "typing the comment"):
            box = type_comment(driver, author, snippet, text)
        with step(driver, "comment3_post", "pressing Enter (once)"):
            failed_before = submit(driver, box, about_to_press)
        with step(driver, "comment4_confirm", "waiting for Facebook to take the comment"):
            confirm(driver, author, snippet, failed_before)
    except StepFailed as exc:
        cause = exc.__cause__
        if pressed["at"] is None:
            if isinstance(cause, PostClosed):
                return Posted("closed", f"comment: {exc}")
            return Posted(exc.kind if exc.kind in ACCOUNT_WIDE else "not_submitted", f"comment: {exc}")
        stop = exc.kind if exc.kind in ACCOUNT_WIDE else ""
        try:                                      # Enter was pressed: it may be on the post anyway
            if on_post(driver, author, snippet, text):
                return Posted("commented", f"comment: on the post after: {exc}", stop)
        except Exception:
            pass
        return Posted("uncertain", f"comment: Enter was pressed; {exc}", stop)
    print(f"[done] commented on {author}'s post")
    return Posted("commented", "comment: Enter pressed and Facebook took it (not looked for on the post)")


# ----------------------------------------------------------------- which leads
def comment_text(lead):
    return (lead.get("edited", {}).get("comment") or lead.get("ai", {}).get("comment") or "").strip()


def snippet_of(lead):
    return re.sub(r"\s+", " ", lead.get("text") or "").strip()[:SNIPPET]


def akey_of(lead):
    return lead.get("author_key") or f"post:{lead['post_key']}"      # as the queue records a lead with no profile


def why_not(lead, cfg, handled=None, hist=None):
    """Why this lead must not get an automatic comment now (checked again right before each), or None."""
    out, a = cfg["outreach"], lead.get("ai")
    handled = store.handled_posts() if handled is None else handled
    hist = store.history() if hist is None else hist
    if not a:
        return "not scored yet"
    if not a["relevant"] or a["score"] < cfg["ai"]["min_score"]:
        return "below ai.min_score"
    if a["likely_minor"] and out["minor_policy"] == "skip":
        return "the AI thinks the author is under 18 and outreach.minor_policy is skip"
    if a["likely_minor"] and not out.get("auto_comment_minors", False):
        return "the AI thinks the author is under 18; outreach.auto_comment_minors is off (comment from the queue)"
    if dm_allowed(lead, cfg) and lead.get("author_key"):
        return "can be DMed: send.py handles this lead"
    if not lead.get("post_url"):
        return "no link to the post"
    if not comment_text(lead):
        return "no drafted comment"
    last = handled.get(lead["post_key"])
    if last in store.COMMENT_HELD:
        return (f"HELD: a comment may already be on the post (python comment.py resolve {lead['post_key']} "
                f"--commented | --not-commented)")
    status = status_of(lead, cfg, handled, hist)
    if status != "todo":
        return f"not in To review ({status})"
    akey = akey_of(lead)
    if akey in store.messaged_authors(hist):
        return "already contacted (DMed, commented on or held)"
    if any(handled.get(r["post_key"]) in store.WAITING for r in hist.get(akey, [])):
        return "a DM to them is approved and waiting for send.py"
    tries = [r for r in hist.get(akey, []) if r["post_key"] == lead["post_key"] and r["status"] in store.COMMENT_ATTEMPTS]
    if len(tries) >= out.get("max_attempts_per_recipient", store.MAX_ATTEMPTS):
        return f"gave up after {len(tries)} attempts"
    return None


def candidates(cfg):
    """Every lead that may get an automatic comment now, best score first."""
    handled, hist = store.handled_posts(), store.history()
    leads = [l for l in store.load_leads().values() if not why_not(l, cfg, handled, hist)]
    return sorted(leads, key=lambda l: -l["ai"]["score"])


# ----------------------------------------------------------------- the run
def prepare(driver, cfg):
    """Start of a run: logged in as outreach.sender_user_id and not blocked. Exits on any problem."""
    me = send.check_sender(driver, cfg)
    try:
        check_account(driver)
    except AccountBlocked as exc:
        raise browser.Halt(f"Not commenting ({exc.kind}): {exc}")
    print(f"[+] commenting as Facebook user {me}")
    return me


def submitted_text(post_key):
    """The comment text written in the post's last 'comment_submitting' row (what was typed before Enter)."""
    rows = [r for r in store._rows() if r["post_key"] == post_key and r["status"] == "comment_submitting"]
    return rows[-1]["note"] if rows else ""


def resolve_held(driver, check=None):
    """Start of a run (outreach.auto_resolve): look for every held comment on its post. Found -> 'commented';
    anything else -> still held (only you can say it isn't there: python comment.py resolve ... --not-commented)."""
    check = check or was_posted
    leads = store.load_leads()
    for post_key, row in store.held_comments().items():
        lead, text = leads.get(post_key), submitted_text(post_key)
        if not lead or not lead.get("post_url") or not text:
            print(f"[=] the comment on {post_key} is held, but its post or text isn't known; check it by hand: "
                  f"python comment.py resolve {post_key} --commented | --not-commented")
            continue
        name = lead.get("author") or row["author"]
        print(f"[+] looking for the held comment on {name}'s post")
        try:
            check(driver, lead["post_url"], text, name, snippet_of(lead))
        except AccountBlocked as exc:
            print(f"    [!] stopped checking held comments ({exc.kind}): {exc}")
            return
        except Exception as exc:
            print(f"    [!] not found ({exc}); still held. If it isn't on the post: python comment.py resolve "
                  f"{post_key} --not-commented")
            continue
        store.resolve_comment(post_key, True, how="automatically")


def new_counts():
    counts = {s: 0 for s in ("commented", "closed", "not_submitted", "uncertain", "invalid", "skipped")}
    counts.update(stopped="", halted=False)
    return counts


def comment_loop(driver, leads, cfg, post=None):
    """Comment on each eligible lead within outreach.daily_comment_limit and the delays. Returns a count per status,
    plus 'stopped' (why it ended early) and 'halted' (True for anything but the daily cap)."""
    out = cfg["outreach"]
    post = post or post_comment
    cap = out.get("daily_comment_limit", DAILY_LIMIT)
    lo = out.get("comment_min_delay_seconds", out["min_delay_seconds"])
    hi = out.get("comment_max_delay_seconds", out["max_delay_seconds"])
    counts, streak = new_counts(), {"status": None, "n": 0}
    for i, lead in enumerate(leads):
        done_today = store.commented_today()
        if done_today >= cap:
            counts["stopped"] = f"hit the daily comment cap ({done_today}/{cap} today)"
            break
        why = why_not(lead, cfg)
        if why:
            print(f"[=] skipping {lead.get('author') or lead['post_key']}: {why}")
            counts["skipped"] += 1
            continue

        text, akey, name = comment_text(lead), akey_of(lead), lead.get("author") or ""
        print(f"\n[+] comment on {name or lead['post_key']}'s post ({done_today + 1}/{cap} today)")
        submitted = []

        def before_submit():
            store.record(akey, name, lead["post_key"], "comment_submitting", text)
            submitted.append(True)

        try:
            result = post(driver, lead["post_url"], text, name, snippet_of(lead), before_submit=before_submit)
        except Exception as exc:                          # a bug or a dead browser, not a Facebook answer
            result = Posted("uncertain" if submitted else "not_submitted", f"{type(exc).__name__}: {exc}")
        if result.status != "invalid" or submitted:
            status = RECORD.get(result.status, "comment_uncertain" if submitted else "comment_not_submitted")
            note = f"{result.status}: {result.reason}" if status == "comment_stopped" else result.reason
            store.record(akey, name, lead["post_key"], status, note)

        stop = result.status if result.status in ACCOUNT_WIDE else getattr(result, "stop", "")
        if result.status not in ACCOUNT_WIDE:
            counts[result.status] = counts.get(result.status, 0) + 1
        if stop:
            counts["stopped"], counts["halted"] = f"account-wide problem ({stop}): {result.reason}", True
            break
        if result.status != "commented":
            print(f"    [!] {NOTES.get(result.status, result.status)}: {result.reason}")
        if result.status == "invalid":
            continue
        streak = ({"status": result.status, "n": streak["n"] + 1} if result.status == streak["status"]
                  else {"status": result.status, "n": 1})
        limit, why = STREAKS.get(result.status, (None, ""))
        if limit and streak["n"] >= limit:
            counts["stopped"], counts["halted"] = f"{streak['n']} {why}", True
            break

        if i + 1 < len(leads) and store.commented_today() < cap:
            delay = random.uniform(lo, hi)
            print(f"[+] Waiting {delay:.0f}s before the next one...")
            time.sleep(delay)
    return counts


def report(counts):
    """Print a run summary; returns the exit code (2 if the run halted on an account or streak problem)."""
    print(f"\n[done] commented {counts['commented']}, uncertain {counts['uncertain']}, closed {counts['closed']}, "
          f"failed before posting {counts['not_submitted']}, bad entries {counts['invalid']}, skipped "
          f"{counts['skipped']}; {store.commented_today()} comments today in total")
    if counts["stopped"]:
        print(f"[stopped] {counts['stopped']}")
    waiting = store.held_comments()
    if waiting:
        print(f"[!] {len(waiting)} comment(s) HELD with an uncertain outcome: python comment.py held")
    return 2 if counts["halted"] else 0


def print_list(cfg):
    handled, hist = store.handled_posts(), store.history()
    leads = sorted((l for l in store.load_leads().values() if l.get("ai") and l["ai"]["relevant"]
                    and l["ai"]["score"] >= cfg["ai"]["min_score"]), key=lambda l: -l["ai"]["score"])
    n = 0
    for lead in leads:
        why = why_not(lead, cfg, handled, hist)
        n += not why
        print(f"  {lead.get('author') or '?':<30} score {lead['ai']['score']:>3}  {len(comment_text(lead))} chars"
              + (f"  [skip: {why}]" if why else "  [will comment]"))
    print(f"{n} to comment on; {store.commented_today()}/{cfg['outreach'].get('daily_comment_limit', DAILY_LIMIT)} "
          f"comments today")


def run(cfg):
    out = cfg["outreach"]
    leads = candidates(cfg)
    resolving = out.get("auto_resolve", True) and bool(store.held_comments())
    if not leads and not resolving:
        print("[+] No leads to comment on.")
        return 0
    print(f"[+] {len(leads)} leads to comment on" + (f", {len(store.held_comments())} held to check" if resolving else ""))
    with store.lock():
        cap = out.get("daily_comment_limit", DAILY_LIMIT)
        if store.commented_today() >= cap and not resolving:
            print(f"[+] Already {store.commented_today()}/{cap} comments today. Nothing to do until tomorrow.")
            return 0
        driver = browser.make_driver(cfg)
        try:
            prepare(driver, cfg)
            if resolving:
                resolve_held(driver)
                leads = candidates(cfg)
            return report(comment_loop(driver, leads, cfg))
        except KeyboardInterrupt:
            print("\n[+] Interrupted. Everything recorded so far is in data/contacted.csv.")
            return 0
        finally:
            driver.quit()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", nargs="?", default="run", choices=["run", "held", "resolve"])
    ap.add_argument("post_key", nargs="?", help="for resolve: the post_key from python comment.py held")
    ap.add_argument("--list", action="store_true", dest="list_only")
    which = ap.add_mutually_exclusive_group()
    which.add_argument("--commented", action="store_true", help="resolve: it is on the post")
    which.add_argument("--not-commented", action="store_true", help="resolve: it isn't on the post, eligible again")
    args = ap.parse_args()
    cfg = load_config()
    if args.cmd == "held":
        waiting = store.held_comments()
        for post_key, r in waiting.items():
            print(f"{post_key}  {r['author']}  {r['status']}  {r['timestamp']}  {r['note'][:80]}")
        print(f"{len(waiting)} held. The next python comment.py looks for each on its post; to settle one by hand: "
              f"python comment.py resolve <post_key> --commented | --not-commented" if waiting else "Nothing held.")
    elif args.cmd == "resolve":
        if not args.post_key or not (args.commented or args.not_commented):
            ap.error("resolve needs a post_key and --commented or --not-commented")
        store.resolve_comment(args.post_key, args.commented)
    elif args.list_only:
        print_list(cfg)
    else:
        sys.exit(run(cfg))


if __name__ == "__main__":
    browser.halting(main)
