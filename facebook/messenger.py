"""messenger.py - send one Facebook Messenger message, the way send_dm.py sends one Reddit message.

Run it directly (opens the profile from config.json, sends one message, then waits for Enter):

    python messenger.py <author_key> "Test message" --name "Jane Doe"

or call it with any logged-in driver:

    from messenger import send_message
    ok = send_message(driver, "https://www.facebook.com/messages/t/1000123", "Hi Jane, ...", "Jane Doe")

send_message opens the chat, checks that it is a chat with `name`, that Facebook isn't refusing this account
or this person, and that the message isn't already in the chat; types the message, presses Send once, waits
for the message box to clear, then reloads the chat and confirms the message is in it.

Returns a send_dm.Result, truthy only once the message is confirmed in the chat, with the same statuses:
    'sent', 'recipient' (Facebook says the person isn't available), 'refused' (Facebook won't let this
    account message them and doesn't say why), 'login' / 'rate_limited' / 'restricted' (account-wide: stop),
    'invalid', 'not_submitted' (failed before Send: safe to retry), 'uncertain' (Send was pressed but the
    message couldn't be confirmed: check before any retry).
The step helpers are send_dm.py's, so a failed step prints what failed and saves <step>_failed.png and
<step>_failed_dom.txt. Facebook changes its markup often: if a step keeps failing, look at those files and
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

FB = "https://www.facebook.com"
INBOX = f"{FB}/messages/"
CHAT_URL = re.compile(r"^https://www\.facebook\.com/messages/(e2ee/)?t/[^/?#]+/?$")
VERIFY_TIMEOUT = 45   # seconds to keep reloading the chat for a sent message to show up
SETTLE = 3            # seconds to watch a freshly opened chat for refusals and earlier messages to load
# Matched only in the chat pane and in dialogs/alerts over it, never in the message box (our own draft).
# Facebook refusing this account, by send_dm.AccountBlocked kind:
LOGIN_NEEDED = r"log ?in to facebook|log into facebook|you must log in|session (has )?expired"
RATE_LIMITED = (r"temporarily blocked|we limit how often|you.re (sending|doing) .{0,20}too (fast|quickly|much)|"
                r"reached the limit (for|of) (messages|message requests)")
RESTRICTED = (r"temporarily restricted|you can.t use this feature|we suspended your account|"
              r"your account (has been|is) (restricted|locked|disabled|suspended)|confirm (your|it.s) (identity|you)")
PIN = r"enter your pin|restore (your )?(chat history|messages)|create a pin"   # end-to-end encrypted chats
ACCOUNT = (("login", LOGIN_NEEDED), ("rate_limited", RATE_LIMITED), ("restricted", RESTRICTED))
# Facebook refusing this person. As on Reddit, a refusal doesn't say whether their settings or a
# restriction on this account caused it, so it is 'refused', not blamed on either side.
UNAVAILABLE = (r"can.t message this (account|person)|you can.t message|can.t reply to this conversation|"
               r"(isn.t|not) (receiving|accepting) messages|only accepts messages from")
NO_SUCH_USER = (r"(this )?person is unavailable|(isn.t|not) available on messenger|this account is unavailable|"
                r"this content isn.t available|this page isn.t available")
FAILED = r"message not sent|couldn.t send|failed to send|not delivered|unable to send"

# Page-side helpers for the Messenger chat page.
_JS = r"""
const FBM = (() => {
  const BOX = '[contenteditable="true"], [role="textbox"], textarea, input';
  const visible = el => { const r = el.getBoundingClientRect();
    return el.isConnected && r.width > 2 && r.height > 2 && getComputedStyle(el).visibility !== 'hidden'; };
  const main = () => document.querySelector('[role="main"]') || document.body;
  // The chat pane, and whatever Facebook pops up over it.
  const roots = () => [main(), ...[...document.querySelectorAll('[role="dialog"], [role="alertdialog"], [role="alert"]')]
                                    .filter(visible)];
  // Text under root, leaving out text boxes (they hold our own draft). Messenger draws emoji as <img alt>.
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
  // Same as squash() in messenger.py: links and line breaks split text into pieces, so compare without spaces.
  const squash = s => (s || '').replace(/[\s​-‍﻿]+/g, '').toLowerCase();
  return {
    composer: () => [...main().querySelectorAll('[contenteditable="true"][role="textbox"]')].filter(visible),
    send: () => [...main().querySelectorAll('[role="button"], button')].filter(el =>
      visible(el) && /^(press enter to send|send)$/i.test((el.getAttribute('aria-label') || '').trim())),
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
return FBM[arguments[0]].apply(null, [].slice.call(arguments, 1));
"""


def js(driver, fn, *args):
    return driver.execute_script(_JS, fn, *args)


def squash(text):
    return re.sub(r"[\s​-‍﻿]+", "", text or "").lower()


# ----------------------------------------------------------------- what the page says
def check_account(driver):
    """Raise AccountBlocked at once if Facebook is refusing this account (logged out, blocked, checkpoint, or
    Messenger asking for the chat PIN, which only you can enter)."""
    url = driver.current_url
    if re.search(r"facebook\.com/(login|checkpoint)", url):
        raise AccountBlocked(f"Facebook sent this profile to {url}", "restricted" if "checkpoint" in url else "login")
    for kind, pattern in ACCOUNT:
        said = js(driver, "notice", pattern)
        if said:
            raise AccountBlocked(f"Facebook is refusing this account: {said!r}", kind)
    said = js(driver, "notice", PIN)
    if said:
        raise AccountBlocked(f"Messenger wants this account's chat PIN ({said!r}); enter it in the window, then "
                             f"run again", "login")


def check_recipient(driver, name):
    """Raise at once if the chat says the person isn't available (NoSuchUser) or can't be messaged (MessageRefused)."""
    said = js(driver, "notice", NO_SUCH_USER)
    if said:
        raise NoSuchUser(f"Facebook says {name} isn't available: {said!r}")
    said = js(driver, "notice", UNAVAILABLE)
    if said:
        raise MessageRefused(f"Facebook won't let this account message {name}: {said!r} (it doesn't say whether that "
                             f"is their settings or a restriction on this account)")


def page_says(driver, name):
    """A Result for what the chat shows now (refused, unavailable, or an account-wide problem), else None."""
    try:
        check_account(driver)
        check_recipient(driver, name)
    except (AccountBlocked, MessageRefused, NoSuchUser) as exc:
        return Result(classify(exc), f"messenger (by hand): {exc}")
    return None


def in_chat(driver, text):
    return js(driver, "has", text)


# ----------------------------------------------------------------- the steps
def composer(driver):
    """The chat's message box once there is exactly one, else None."""
    boxes = js(driver, "composer")
    return boxes[0] if len(boxes) == 1 else None


def open_chat(driver, chat_url, name):
    """Open the chat and wait for its message box. Raises (before anything is typed) if Facebook refuses the
    account or the person, opens something other than a chat, or the chat doesn't show `name`."""
    driver.get(chat_url)

    def ready(d):
        check_account(d)
        check_recipient(d, name)
        return composer(d)

    wait_for(driver, ready, "the Messenger chat's message box")
    if not CHAT_URL.match(driver.current_url.split("?")[0]):
        raise RuntimeError(f"Facebook opened {driver.current_url} instead of a chat")
    deadline = time.monotonic() + SETTLE       # refusals and earlier messages show up a moment after the box
    while time.monotonic() < deadline:
        check_account(driver)
        check_recipient(driver, name)
        time.sleep(0.3)
    if not in_chat(driver, name):
        raise RuntimeError(f"{name!r} isn't shown in this chat, so it may not be with them; not typing anything")


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


def submit(driver, box, name, about_to_click):
    """Press Send exactly once: the Send button if there is one, else Enter in the box, with no fallback
    (a second try could send twice). about_to_click() runs right before, after every check has passed.
    Returns the failure notice already on the page, so confirm() only reacts to a new one."""
    check_account(driver)
    check_recipient(driver, name)
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


def confirm(driver, failed_before, name):
    """Taken = the message box cleared with no new failure notice."""
    def outcome(d):
        check_account(d)
        check_recipient(d, name)
        failed = js(d, "notice", FAILED)
        if failed and failed != failed_before:
            raise RuntimeError(f"Messenger says the message didn't go: {failed!r}")
        box = composer(d)
        return box is not None and not squash(sdm(d, "read", box))

    wait_for(driver, outcome, "Messenger to take the message")
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


def was_sent(driver, chat_url, text, name):
    """For a held send (send.py checks these at the start of each run): True if the message is in the chat
    after reloading it, False only if the chat with `name` loaded and the message still isn't in it. Raises
    AccountBlocked if Facebook is refusing this account, RuntimeError if it can't tell (the send stays held)."""
    driver.switch_to.default_content()
    try:
        verify_in_chat(driver, chat_url, text)
        return True
    except NotInSent:
        pass
    if not CHAT_URL.match(driver.current_url.split("?")[0]) or not in_chat(driver, name):
        raise RuntimeError(f"the message isn't showing, but the page doesn't show a chat with {name!r} either")
    return False


# ----------------------------------------------------------------- by hand (send.py --by-hand)
def open_for_hand(driver, chat_url, text, name):
    """Open the chat and type the message in, leaving Send to you. A Result if Facebook refuses first or the
    message is already in the chat, else None."""
    try:
        open_chat(driver, chat_url, name)
        if in_chat(driver, text):
            return Result("sent", "messenger (by hand): the message was already in the chat")
        type_message(driver, text)
    except (AccountBlocked, MessageRefused, NoSuchUser) as exc:
        return Result(classify(exc), f"messenger (by hand): {exc}")
    except (TimeoutException, RuntimeError) as exc:
        print(f"    [!] {exc}; look at the window before sending")
    return None


def confirm_by_hand(driver, chat_url, text, name):
    """After you say you pressed Send: 'sent' if the message is in the chat after reloading it, Facebook's own
    answer if the chat shows one, else 'uncertain'."""
    said = page_says(driver, name)
    if said is not None:
        return said
    print("    reloading the chat to look for it...")
    try:
        verify_in_chat(driver, chat_url, text, timeout=30)
        return Result("sent", "messenger (by hand): confirmed in the chat")
    except AccountBlocked as exc:
        return Result(exc.kind, f"messenger (by hand): {exc}")
    except NotInSent as exc:
        return Result("uncertain", f"messenger (by hand): marked sent, but {exc} (edited before sending? then "
                                   f"resolve it by hand)")


# ----------------------------------------------------------------- the whole send
def send_message(driver, chat_url, text, name, *, before_submit=None):
    """Open the chat, check it, type the message, press Send once and confirm it in the reloaded chat.
    before_submit(): called right before Send is pressed (send.py uses it to note a send is in flight)."""
    if not CHAT_URL.match(chat_url or "") or not text.strip() or not (name or "").strip():
        print(f"[!] not sending: needs a {FB}/messages/t/<id> link, a message and the person's name")
        return Result("invalid", f"messenger: bad entry (link {chat_url!r}, name {name!r}, {len(text)} chars)")
    clicked = {"at": None}

    def about_to_click():
        if before_submit:
            before_submit()
        clicked["at"] = time.time()               # set before pressing: a press that errors may still land

    try:
        driver.switch_to.default_content()
        with step(driver, "step1_open_chat", f"opening the chat with {name}"):
            open_chat(driver, chat_url, name)
        if in_chat(driver, text):
            print("    this message is already in the chat; not sending it again")
            return Result("sent", "messenger: the message was already in the chat")
        with step(driver, "step2_type", "typing the message"):
            box = type_message(driver, text)
        with step(driver, "step3_send", "pressing Send (once)"):
            failed_before = submit(driver, box, name, about_to_click)
        with step(driver, "step4_confirm", "waiting for Messenger to take the message"):
            confirm(driver, failed_before, name)
        with step(driver, "step5_verify", "reloading the chat to look for the message"):
            verify_in_chat(driver, chat_url, text)
    except StepFailed as exc:
        if exc.kind != "error":                   # Facebook said what went wrong: not delivered
            return Result(exc.kind, f"messenger: {exc}")
        if clicked["at"] is None or isinstance(exc.__cause__, SendNotClicked):
            return Result("not_submitted", f"messenger: {exc}")
        try:                                      # it may have gone out anyway
            verify_in_chat(driver, chat_url, text, timeout=15)
            print("    ...but it is in the chat")
            return Result("sent", f"messenger: confirmed in the chat after: {exc}")
        except Exception as check_error:
            return Result("uncertain", f"messenger: Send was pressed; {exc}; chat check: {check_error}")
    print(f"[done] message to {name} is in the chat")
    return Result("sent", "messenger: confirmed in the chat")


# ----------------------------------------------------------------- run directly
if __name__ == "__main__":
    import argparse
    import json
    from pathlib import Path

    import store

    ap = argparse.ArgumentParser(description="Send one Messenger message with the profile from config.json.")
    ap.add_argument("author_key", help="numeric profile id or username, as in data/contacted.csv")
    ap.add_argument("message")
    ap.add_argument("--name", required=True, help="their name as Messenger shows it, to check the chat is theirs")
    args = ap.parse_args()
    cfg = json.loads((Path(__file__).resolve().parent / "config.json").read_text())
    chrome = browser.make_driver(cfg)
    try:
        browser.ensure_logged_in(chrome, cfg)
        result = send_message(chrome, store.messenger_url(args.author_key), args.message, args.name)
        print("RESULT:", "sent" if result else f"not sent ({result.status}) - {result.reason}")
        input("Press Enter here to close Chrome... ")
    except KeyboardInterrupt:
        pass
    finally:
        chrome.quit()
