"""direct.py - send one Instagram DM, the way send_dm.py sends one Reddit message.

Run it directly (opens the profile from config.json, sends one message, then waits for Enter):

    python direct.py <username> "Test message" [--name "Jane Doe"]

or call it with any logged-in driver:

    from direct import send_message
    ok = send_message(driver, "https://ig.me/m/jane.doe", "hey jane! ...", "jane.doe", "Jane Doe")

send_message opens the chat (ig.me/m/<username>, which lands on instagram.com/direct/t/<thread>/), checks that
it is a chat with that person, that Instagram isn't refusing this account or them, and that the message isn't
already in the chat; types the message, presses Send once, waits for the message box to clear, then reloads
the chat and confirms the message is in it. It doesn't follow anyone: follow() does that, and send.py
calls it before each DM (outreach.auto_follow).

Returns a send_dm.Result, truthy only once the message is confirmed in the chat, with the same statuses:
    'sent', 'recipient' (Instagram says there is no such account), 'refused' (Instagram won't let this account
    message them and doesn't say why), 'login' / 'rate_limited' / 'restricted' (account-wide: stop),
    'invalid', 'not_submitted' (failed before Send: safe to retry), 'uncertain' (Send was pressed but the
    message couldn't be confirmed: check before any retry).
The step helpers are send_dm.py's, so a failed step prints what failed and saves <step>_failed.png and
<step>_failed_dom.txt. Instagram changes its markup often: if a step keeps failing, look at those files and
at _JS and the patterns below.
"""
from __future__ import annotations

import re
import time

import browser  # noqa: F401  (puts the repo root, with send_dm.py, on sys.path)
from selenium.common.exceptions import (ElementClickInterceptedException, ElementNotInteractableException,
                                        MoveTargetOutOfBoundsException, TimeoutException)
from selenium.webdriver.common.keys import Keys
from send_dm import (AccountBlocked, MessageRefused, NoSuchUser, NotInSent, Result, SendNotClicked, StepFailed,
                     classify, click, js as sdm, pause, step, type_text, wait_for)

IG = "https://www.instagram.com"
INBOX = f"{IG}/direct/inbox/"
OPEN_URL = re.compile(r"^https://ig\.me/m/[A-Za-z0-9._]+/?$")
CHAT_URL = re.compile(r"^https://www\.instagram\.com/direct/t/[^/?#]+/?$")
VERIFY_TIMEOUT = 45   # seconds to keep reloading the chat for a sent message to show up
SETTLE = 3            # seconds to watch a freshly opened chat for refusals and earlier messages to load
# Matched only in the page's main area and in dialogs/alerts over it, never in the message box (our own draft).
# Instagram refusing this account, by send_dm.AccountBlocked kind:
LOGIN_NEEDED = r"log ?in to instagram|log in to continue|you.ve been logged out|session (has )?expired"
RATE_LIMITED = (r"try again later.{0,40}(restrict|protect)|we restrict certain activity|action blocked|"
                r"please wait a few minutes|you.re sending messages too (fast|quickly)")
RESTRICTED = (r"your account (has been|is|was) (temporarily )?(locked|disabled|suspended|restricted)|"
              r"we suspended your account|confirm (it.s you|your identity)|suspicious (login )?activity")
ACCOUNT = (("login", LOGIN_NEEDED), ("rate_limited", RATE_LIMITED), ("restricted", RESTRICTED))
# Instagram refusing this person. As on Reddit, a refusal doesn't say whether their settings or a
# restriction on this account caused it, so it is 'refused', not blamed on either side.
UNAVAILABLE = (r"can.t message this account|can.t (send|receive) (messages|your message)|you can.t message|"
               r"not everyone can message this account|only (allows|accepts) messages from")
NO_SUCH_USER = r"sorry, this page isn.t available|user not found|this account (doesn.t exist|isn.t available)"
FAILED = r"failed to send|message (failed|not sent)|couldn.t send|not delivered|unable to send"
NOT_NOW = r"^not now$"   # "Turn on notifications" and similar dialogs on the inbox

# Page-side helpers for the Instagram Direct chat page.
_JS = r"""
const IGD = (() => {
  const BOX = '[contenteditable="true"], [role="textbox"], textarea, input';
  const visible = el => { const r = el.getBoundingClientRect();
    return el.isConnected && r.width > 2 && r.height > 2 && getComputedStyle(el).visibility !== 'hidden'; };
  const main = () => document.querySelector('[role="main"], main') || document.body;
  // The main area, and whatever Instagram pops up over it.
  const roots = () => [main(), ...[...document.querySelectorAll('[role="dialog"], [role="alertdialog"], [role="alert"]')]
                                    .filter(visible)];
  // Text under root, leaving out text boxes (they hold our own draft). Emoji may be <img alt>.
  function text(root, sep) {
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
  // Same as squash() in direct.py: links and line breaks split text into pieces, so compare without spaces.
  const squash = s => (s || '').replace(/[\s​-‍﻿]+/g, '').toLowerCase();
  const label = el => (el.getAttribute('aria-label') || el.innerText || '').trim();
  const profileButtons = re => [...(main().querySelector('header') || main()).querySelectorAll('button, [role="button"]')]
    .filter(el => visible(el) && re.test((el.innerText || el.getAttribute('aria-label') || '').trim()));
  return {
    composer: () => [...main().querySelectorAll('[contenteditable="true"][role="textbox"]')].filter(visible),
    // Instagram's Send is a role=button whose text is "Send"; some builds label it instead.
    send: () => [...main().querySelectorAll('[role="button"], button')].filter(el => visible(el) && /^send$/i.test(label(el))),
    buttons: pattern => [...document.querySelectorAll('[role="dialog"] button, [role="dialog"] [role="button"]')]
      .filter(el => visible(el) && new RegExp(pattern, 'i').test(label(el))),
    // A profile's Follow button, and whether it already says Following / Requested. Only in the profile
    // header, so the "Follow" buttons under suggested accounts don't count.
    follow: () => profileButtons(/^follow( back)?$/i),
    following: () => profileButtons(/^(following|requested)$/i).length > 0,
    has: want => squash(text(main(), '')).includes(squash(want)),
    notice: pattern => {
      const re = new RegExp(pattern, 'i');
      for (const root of roots()) {
        const t = text(root, ' ').replace(/\s+/g, ' ');
        const m = re.exec(t);
        if (m) return t.slice(Math.max(0, m.index - 40), m.index + m[0].length + 40).trim();
      }
      return '';
    },
  };
})();
return IGD[arguments[0]].apply(null, [].slice.call(arguments, 1));
"""


def js(driver, fn, *args):
    return driver.execute_script(_JS, fn, *args)


def squash(text):
    return re.sub(r"[\s​-‍﻿]+", "", text or "").lower()


def chat_link(username):
    return f"https://ig.me/m/{username}"


def dismiss_dialogs(driver):
    """Close "Turn on notifications"-style dialogs (their "Not Now" button), which sit over the chat."""
    for el in js(driver, "buttons", NOT_NOW):
        try:
            click(driver, el)
            pause()
        except Exception:
            pass


# ----------------------------------------------------------------- what the page says
def check_account(driver):
    """Raise AccountBlocked at once if Instagram is refusing this account (logged out, action blocked,
    challenge / checkpoint, locked)."""
    url = driver.current_url
    if re.search(r"instagram\.com/(accounts/login|challenge|accounts/suspended)", url):
        raise AccountBlocked(f"Instagram sent this profile to {url}", "login" if "login" in url else "restricted")
    for kind, pattern in ACCOUNT:
        said = js(driver, "notice", pattern)
        if said:
            raise AccountBlocked(f"Instagram is refusing this account: {said!r}", kind)


def check_recipient(driver, username):
    """Raise at once if the page says there is no such account (NoSuchUser) or they can't be messaged
    (MessageRefused)."""
    said = js(driver, "notice", NO_SUCH_USER)
    if said:
        raise NoSuchUser(f"Instagram says there is no account @{username}: {said!r}")
    said = js(driver, "notice", UNAVAILABLE)
    if said:
        raise MessageRefused(f"Instagram won't let this account message @{username}: {said!r} (it doesn't say "
                             f"whether that is their settings or a restriction on this account)")


def page_says(driver, username):
    """A Result for what the chat shows now (refused, no such account, or an account-wide problem), else None."""
    try:
        check_account(driver)
        check_recipient(driver, username)
    except (AccountBlocked, MessageRefused, NoSuchUser) as exc:
        return Result(classify(exc), f"direct (by hand): {exc}")
    return None


def in_chat(driver, text):
    return js(driver, "has", text)


# ----------------------------------------------------------------- the steps
def composer(driver):
    """The chat's message box once there is exactly one, else None."""
    boxes = js(driver, "composer")
    return boxes[0] if len(boxes) == 1 else None


def open_chat(driver, open_url, username, name=""):
    """Open the chat and wait for its message box. Returns the chat's own URL (instagram.com/direct/t/...).
    Raises (before anything is typed) if Instagram refuses the account or the person, opens something other
    than a chat, or the chat shows neither their username nor their name."""
    driver.get(open_url)

    def ready(d):
        check_account(d)
        check_recipient(d, username)
        return composer(d)

    wait_for(driver, ready, "the Instagram chat's message box")
    chat_url = driver.current_url.split("?")[0]
    if not CHAT_URL.match(chat_url):
        raise RuntimeError(f"Instagram opened {driver.current_url} instead of a chat")
    deadline = time.monotonic() + SETTLE       # refusals and earlier messages show up a moment after the box
    while time.monotonic() < deadline:
        check_account(driver)
        check_recipient(driver, username)
        time.sleep(0.3)
    if not (in_chat(driver, username) or (name and in_chat(driver, name))):
        raise RuntimeError(f"neither @{username} nor {name!r} is shown in this chat, so it may not be with them; "
                           f"not typing anything")
    return chat_url


def type_message(driver, text):
    """Type the message into the box (send_dm.type_text: line breaks as Shift+Enter, so nothing is sent early)
    and check the box holds exactly that."""
    box = composer(driver)
    if box is None:
        raise RuntimeError("the chat's message box went away")
    pause()
    type_text(driver, box, text)
    have = sdm(driver, "read", box)
    if squash(have) != squash(text):
        raise RuntimeError(f"the message box shows {have[:80]!r}, not the approved message; not sending")
    return box


def submit(driver, box, username, about_to_click):
    """Press Send exactly once: the Send button if there is one, else Enter in the box, with no fallback
    (a second try could send twice). about_to_click() runs right before, after every check has passed.
    Returns the failure notice already on the page, so confirm() only reacts to a new one."""
    check_account(driver)
    check_recipient(driver, username)
    failed_before = js(driver, "notice", FAILED)
    buttons = js(driver, "send")
    pause(0.3, 0.8)
    about_to_click()
    try:
        if buttons:
            click(driver, buttons[0], methods=("native",))
        else:
            box.send_keys(Keys.ENTER)
    except (ElementClickInterceptedException, ElementNotInteractableException, MoveTargetOutOfBoundsException) as exc:
        raise SendNotClicked(f"pressing Send was blocked ({type(exc).__name__}); nothing was sent") from exc
    return failed_before


def confirm(driver, failed_before, username):
    """Taken = the message box cleared with no new failure notice."""
    def outcome(d):
        check_account(d)
        check_recipient(d, username)
        failed = js(d, "notice", FAILED)
        if failed and failed != failed_before:
            raise RuntimeError(f"Instagram says the message didn't go: {failed!r}")
        box = composer(d)
        return box is not None and not squash(sdm(d, "read", box))

    wait_for(driver, outcome, "Instagram to take the message")
    print("    the message box was cleared")


def verify_in_chat(driver, chat_url, text, timeout=VERIFY_TIMEOUT):
    """Reload the chat until the message is in it; NotInSent if it isn't within `timeout` seconds."""
    deadline = time.monotonic() + timeout
    while True:
        driver.get(chat_url)
        left = deadline - time.monotonic()
        try:
            wait_for(driver, lambda d: check_account(d) or in_chat(d, text), "the message to show up in the chat",
                     timeout=max(3, min(15, left)))
            return
        except TimeoutException:
            if time.monotonic() >= deadline:
                raise NotInSent(f"the message isn't in the chat after reloading it for {timeout}s") from None


def was_sent(driver, open_url, text, username, name=""):
    """For a held send (send.py checks these at the start of each run): True if the message is in the chat
    after reloading it, False only if a chat with them loaded and the message still isn't in it. Raises
    AccountBlocked if Instagram is refusing this account, RuntimeError if it can't tell (the send stays held)."""
    driver.switch_to.default_content()
    try:
        verify_in_chat(driver, open_url, text)
        return True
    except NotInSent:
        pass
    if not CHAT_URL.match(driver.current_url.split("?")[0]) or not (
            in_chat(driver, username) or (name and in_chat(driver, name))):
        raise RuntimeError(f"the message isn't showing, but the page doesn't show a chat with @{username} either")
    return False


# ----------------------------------------------------------------- following (send.py, outreach.auto_follow)
def follow(driver, username):
    """Open @username's profile and press Follow, unless this account already follows them or has asked to.
    Returns 'followed' (the button now says Following / Requested) or 'already'. Raises AccountBlocked if
    Instagram is refusing this account, NoSuchUser if the profile doesn't exist, TimeoutException otherwise."""
    driver.switch_to.default_content()
    driver.get(f"{IG}/{username}/")

    def loaded(d):
        check_account(d)
        said = js(d, "notice", NO_SUCH_USER)
        if said:
            raise NoSuchUser(f"Instagram says there is no account @{username}: {said!r}")
        return js(d, "following") or js(d, "follow")

    wait_for(driver, loaded, f"@{username}'s Follow button")
    dismiss_dialogs(driver)
    if js(driver, "following"):
        return "already"
    pause()
    click(driver, js(driver, "follow")[0], methods=("native",))
    wait_for(driver, lambda d: check_account(d) or js(d, "following"), "Instagram to take the follow")
    return "followed"


# ----------------------------------------------------------------- by hand (send.py --by-hand)
def open_for_hand(driver, open_url, text, username, name=""):
    """Open the chat and type the message in, leaving Send to you. Returns (Result or None, chat URL): a Result
    if Instagram refuses first or the message is already in the chat."""
    chat_url = open_url
    try:
        chat_url = open_chat(driver, open_url, username, name)
        if in_chat(driver, text):
            return Result("sent", "direct (by hand): the message was already in the chat"), chat_url
        type_message(driver, text)
    except (AccountBlocked, MessageRefused, NoSuchUser) as exc:
        return Result(classify(exc), f"direct (by hand): {exc}"), chat_url
    except (TimeoutException, RuntimeError) as exc:
        print(f"    [!] {exc}; look at the window before sending")
        chat_url = driver.current_url.split("?")[0] if CHAT_URL.match(driver.current_url.split("?")[0]) else open_url
    return None, chat_url


def confirm_by_hand(driver, chat_url, text, username):
    """After you say you pressed Send: 'sent' if the message is in the chat after reloading it, Instagram's own
    answer if the chat shows one, else 'uncertain'."""
    said = page_says(driver, username)
    if said is not None:
        return said
    print("    reloading the chat to look for it...")
    try:
        verify_in_chat(driver, chat_url, text, timeout=30)
        return Result("sent", "direct (by hand): confirmed in the chat")
    except AccountBlocked as exc:
        return Result(exc.kind, f"direct (by hand): {exc}")
    except NotInSent as exc:
        return Result("uncertain", f"direct (by hand): marked sent, but {exc} (edited before sending? then "
                                   f"resolve it by hand)")


# ----------------------------------------------------------------- the whole send
def send_message(driver, open_url, text, username, name="", *, before_submit=None):
    """Open the chat, check it, type the message, press Send once and confirm it in the reloaded chat.
    before_submit(): called right before Send is pressed (send.py uses it to note a send is in flight)."""
    if not OPEN_URL.match(open_url or "") or not text.strip() or not (username or "").strip():
        print("[!] not sending: needs an ig.me/m/<username> link, a message and the username")
        return Result("invalid", f"direct: bad entry (link {open_url!r}, @{username}, {len(text)} chars)")
    clicked = {"at": None}
    chat_url = open_url

    def about_to_click():
        if before_submit:
            before_submit()
        clicked["at"] = time.time()               # set before pressing: a press that errors may still land

    try:
        driver.switch_to.default_content()
        with step(driver, "step1_open_chat", f"opening the chat with @{username}"):
            chat_url = open_chat(driver, open_url, username, name)
        if in_chat(driver, text):
            print("    this message is already in the chat; not sending it again")
            return Result("sent", "direct: the message was already in the chat")
        with step(driver, "step2_type", "typing the message"):
            box = type_message(driver, text)
        with step(driver, "step3_send", "pressing Send (once)"):
            failed_before = submit(driver, box, username, about_to_click)
        with step(driver, "step4_confirm", "waiting for Instagram to take the message"):
            confirm(driver, failed_before, username)
        with step(driver, "step5_verify", "reloading the chat to look for the message"):
            verify_in_chat(driver, chat_url, text)
    except StepFailed as exc:
        if exc.kind != "error":                   # Instagram said what went wrong: not delivered
            return Result(exc.kind, f"direct: {exc}")
        if clicked["at"] is None or isinstance(exc.__cause__, SendNotClicked):
            return Result("not_submitted", f"direct: {exc}")
        try:                                      # it may have gone out anyway
            verify_in_chat(driver, chat_url, text, timeout=15)
            print("    ...but it is in the chat")
            return Result("sent", f"direct: confirmed in the chat after: {exc}")
        except Exception as check_error:
            return Result("uncertain", f"direct: Send was pressed; {exc}; chat check: {check_error}")
    print(f"[done] message to @{username} is in the chat")
    return Result("sent", "direct: confirmed in the chat")


# ----------------------------------------------------------------- run directly
if __name__ == "__main__":
    import argparse
    import json
    from pathlib import Path

    ap = argparse.ArgumentParser(description="Send one Instagram DM with the profile from config.json.")
    ap.add_argument("username")
    ap.add_argument("message")
    ap.add_argument("--name", default="", help="their display name, also accepted as proof the chat is theirs")
    args = ap.parse_args()
    cfg = json.loads((Path(__file__).resolve().parent / "config.json").read_text())
    user = args.username.lower().removeprefix("@")
    chrome = browser.make_driver(cfg)
    try:
        browser.ensure_logged_in(chrome, cfg)
        result = send_message(chrome, chat_link(user), args.message, user, args.name)
        print("RESULT:", "sent" if result else f"not sent ({result.status}) - {result.reason}")
        input("Press Enter here to close Chrome... ")
    except KeyboardInterrupt:
        pass
    finally:
        chrome.quit()
