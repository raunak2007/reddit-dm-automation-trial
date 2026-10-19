"""send_dm.py - send one Reddit private message through the /message/compose/ form.

Run it directly (opens the automation profile, sends one message, then waits for Enter):

    python send_dm.py Efficient_Resolve_78 "Test message from the script" [--subject "Hello"]

or call it from your own code with any logged-in driver:

    from send_dm import send_dm, send_compose
    ok = send_dm(driver, "Efficient_Resolve_78", "Test message", subject="Hello")
    ok = send_compose(driver, "https://www.reddit.com/message/compose/?to=...&subject=...&message=...")

send_dm builds the same compose link a queue file contains and hands it to send_compose, which
opens it, checks that Reddit prefilled the recipient, title and message, clicks Send once, waits
for Reddit to accept it and confirms it in the sent folder. Nothing here uses /chat/ or searches
for recipients: the recipient comes from the link's ?to=, exactly as the queue page has it.

Returns a Result that is truthy once Reddit accepts the message. On failure it prints the step
that failed, saves <step>_failed.png and <step>_failed_dom.txt, and returns a falsy Result whose
status says what Reddit's wording establishes, and no more (see classify()).

What a result does and doesn't show:
  - "Unable to message this account" and similar refusals don't say whether the recipient's settings
    or a restriction on the sending account caused them, so they are recorded as 'refused', not
    blamed on either side.
  - Being logged in only shows the saved profile's session is valid. The scripts launch Chrome
    through undetected-chromedriver, and nothing here tests whether that changes how Reddit treats
    the account; no result should be read as evidence that it does.
Fields are found by their name attribute in the real markup, including inside shadow roots.
Requires Python 3.8+ and Selenium 4.
"""
from __future__ import annotations

import json
import random
import re
import time
import urllib.parse
from contextlib import contextmanager
from dataclasses import dataclass

from selenium.common.exceptions import (
    ElementClickInterceptedException,
    ElementNotInteractableException,
    JavascriptException,
    MoveTargetOutOfBoundsException,
    NoSuchElementException,
    StaleElementReferenceException,
    TimeoutException,
)
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait

TIMEOUT = 20   # seconds each wait may take
POLL = 0.3     # seconds between polls
SEND = r"^send( a)?( message)?$"
FAILED = r"fail|unable|couldn.t|can.t|not sent|try again|limit"
# Reddit refusing a conversation ("Unable to message this account"). The wording doesn't say whether
# the recipient's message settings or a restriction on the sending account caused it.
UNAVAILABLE = (r"unable to (message|chat|dm)|can.?t (message|chat|dm)|cannot (message|chat|dm)|"
               r"(not|n.t) accepting (messages|chats|dms)|does ?n.t accept (messages|chats|dms)")
NO_SUCH_USER = r"invalid username|no matching users|(user|account) (does ?n.t|does not) exist|user not found|no such user"
# Reddit refusing this account rather than one recipient, by kind.
LOGIN_NEEDED = r"not logged in|log in to (continue|send|message)|sign in to (continue|send|message)|session (has )?expired"
RATE_LIMITED = (r"doing that (too much|a lot)|take a break|rate.?limit|too many (messages|requests|attempts)|"
                r"try again in \d")
RESTRICTED = r"(account|you).{0,25}(suspended|banned|restricted|locked)|captcha|verify your (email|account)"
ACCOUNT_LIMITED = f"{LOGIN_NEEDED}|{RATE_LIMITED}|{RESTRICTED}"
ACCOUNT_WIDE = ("login", "rate_limited", "restricted")     # Result statuses that stop every further send
COMPOSE_URL = "https://www.reddit.com/message/compose/"
SENT_URL = "https://old.reddit.com/message/sent/.json?limit=100&raw_json=1"
ME_URL = "https://old.reddit.com/api/me.json"
VERIFY_TIMEOUT = 45   # seconds to wait for a submitted message to show up in the sent folder
RECIPIENT_SETTLE = 3  # seconds to watch for Reddit refusing the To field before Send is clicked
DEFAULT_SUBJECT = "Hello"

# Page-side helpers. Everything here walks the page *including* open shadow roots.
_JS = r"""
const SDM = (() => {
  const SKIP = new Set(['SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE', 'HEAD', 'svg']);
  const INPUT = 'input:not([type]), input[type=""], input[type="text"], input[type="search"]';
  const BOX = 'textarea, [contenteditable]:not([contenteditable="false"]), [role="textbox"]';

  const norm = s => (s || '').replace(/\s+/g, ' ').trim().toLowerCase();
  const uniq = list => [...new Set(list)];
  const sig = el => ['name', 'id', 'placeholder', 'aria-label', 'title', 'role', 'data-testid', 'class']
    .map(a => el.getAttribute(a) || '').join(' ');

  // Every element in document order, descending into open shadow roots.
  function all() {
    const out = [];
    (function visit(node) {
      if (node.nodeType === 1) {
        if (SKIP.has(node.tagName)) return;
        out.push(node);
        if (node.shadowRoot) visit(node.shadowRoot);
      }
      for (const child of node.children) visit(child);
    })(document);
    return out;
  }

  // Parent in the rendered tree: follows slots and crosses shadow-root boundaries.
  const up = el => el.assignedSlot || el.parentElement ||
                   (el.getRootNode() instanceof ShadowRoot ? el.getRootNode().host : null);
  const closest = (el, selector, max) => {
    for (let n = el, i = 0; n && i <= max; n = up(n), i++) if (n.matches(selector)) return n;
    return null;
  };
  // The custom elements whose shadow trees el sits in, outermost first (faceplate-text-input, ...).
  const hostChain = el => {
    const chain = [];
    for (let r = el.getRootNode(); r instanceof ShadowRoot; r = r.host.getRootNode()) chain.unshift(r.host);
    return chain;
  };

  function box(el) {
    let r = el.getBoundingClientRect();
    if (r.width < 1 && r.height < 1 && getComputedStyle(el).display === 'contents') {
      const range = document.createRange();           // display:contents wrappers have no box of their own
      range.selectNodeContents(el);
      r = range.getBoundingClientRect();
    }
    return r;
  }
  const visible = el => {
    const r = box(el);                                 // > 2px also rules out screen-reader-only text
    return el.isConnected && r.width > 2 && r.height > 2 && getComputedStyle(el).visibility !== 'hidden';
  };
  const enabled = el => !el.disabled && !el.readOnly && el.getAttribute('aria-disabled') !== 'true';

  // Tightest visible elements whose text passes `test`.
  function byText(test) {
    return all().filter(el => {
      if (!test(norm(el.textContent))) return false;
      for (const child of el.children) if (test(norm(child.textContent)) && visible(child)) return false;
      return visible(el);
    });
  }

  return {
    center: el => el.scrollIntoView({block: 'center', inline: 'center'}),
    read: el => el.matches('input, textarea') ? el.value : (el.innerText || ''),
    state: () => {
      const els = all();
      return {
        ready: document.readyState,
        nodes: els.length,
        controls: els.filter(el => el.matches('button, a[href], input, textarea, iframe, [role="button"]') && visible(el)).length,
      };
    },

    // Deep CSS match. pattern (optional regex) must match the element's text or aria-label;
    // host (optional tag name) keeps only elements rendered inside that custom element.
    css: (selector, pattern, host) => {
      const re = pattern ? new RegExp(pattern, 'i') : null;
      return all().filter(el => el.matches(selector) && visible(el) && enabled(el) &&
        (!host || hostChain(el).some(h => h.localName === host)) &&
        (!re || re.test(norm(el.textContent)) || re.test(norm(el.getAttribute('aria-label'))))).slice(0, 5);
    },

    // Visible, enabled text fields. kind: 'input' (single line) | 'box' (textarea/contenteditable) | 'any'.
    // hint: regex tested against name/id/placeholder/aria-label/role/class.
    fields: (kind, hint) => {
      const selector = kind === 'input' ? INPUT : kind === 'box' ? BOX : INPUT + ', ' + BOX;
      const re = hint ? new RegExp(hint, 'i') : null;
      return all().filter(el =>
        el.matches(selector) && (el.matches('input, textarea') || el.isContentEditable) &&
        visible(el) && enabled(el) && (!re || re.test(sig(el))));
    },

    // Visible text matching `pattern` anywhere except text fields (which hold our own message), else a
    // visible element's aria-label (Reddit puts "Unable to message this account" / "Invalid username" there).
    notice: pattern => {
      const re = new RegExp(pattern, 'i');
      const hit = byText(s => re.test(s) && s.length < 200).find(el => !closest(el, BOX + ', input', 10));
      if (hit) return norm(hit.textContent);
      const labelled = all().find(el => el.hasAttribute('aria-label') && re.test(norm(el.getAttribute('aria-label'))) &&
                                        visible(el));
      return labelled ? norm(labelled.getAttribute('aria-label')) : '';
    },

    alerts: () => uniq(all()
      .filter(el => el.matches('[role="alert"], [role="status"], [aria-live="assertive"], [class*="toast" i], [class*="error" i]') && visible(el))
      .map(el => norm(el.textContent)).filter(t => t && t.length < 200)),

    // Last-resort text entry: set the value and fire the events UI frameworks listen for.
    set: (el, text) => {
      el.focus();
      if (el.matches('input, textarea')) {
        const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
        Object.getOwnPropertyDescriptor(proto, 'value').set.call(el, text);
        el.dispatchEvent(new InputEvent('input', {bubbles: true, composed: true, inputType: 'insertText', data: text}));
        el.dispatchEvent(new Event('change', {bubbles: true}));
      } else {
        document.execCommand('selectAll');
        document.execCommand(text ? 'insertText' : 'delete', false, text);
      }
    },

    // Debug listing of every visible control and the chain of shadow hosts it lives under.
    dump: () => all()
      .filter(el => el.matches('button, a[href], input, textarea, [contenteditable], [role], [aria-label]') && visible(el))
      .slice(0, 400)
      .map(el => {
        const attrs = ['id', 'name', 'type', 'role', 'aria-label', 'placeholder', 'contenteditable', 'href']
          .filter(a => el.hasAttribute(a)).map(a => a + '="' + el.getAttribute(a).slice(0, 50) + '"').join(' ');
        return (hostChain(el).map(h => h.localName).join(' >> ') || '(light DOM)') + '  <' + el.localName +
               (attrs ? ' ' + attrs : '') + '>  ' + JSON.stringify(norm(el.textContent).slice(0, 50));
      }).join('\n'),
  };
})();
return SDM[arguments[0]].apply(null, [].slice.call(arguments, 1));
"""


# ----------------------------------------------------------------- plumbing
class MessageRefused(RuntimeError):
    """Reddit refused to let this account message the recipient. Waiting won't change it, but the refusal
    doesn't say why: it may be the recipient's settings or a restriction on the sending account."""


class NoSuchUser(RuntimeError):
    """Reddit says the recipient doesn't exist."""


class AccountBlocked(RuntimeError):
    """Reddit is refusing this account; every other send would fail too.
    kind: 'login' (logged out / wrong account), 'rate_limited' or 'restricted' (suspended, captcha, ...)."""

    def __init__(self, message, kind=None):
        super().__init__(message)
        self.kind = kind or _account_kind(message)


class SendNotClicked(RuntimeError):
    """The Send click was blocked before the browser dispatched it, so nothing was submitted."""


class NotInSent(RuntimeError):
    """Send was clicked, but the message never showed up in this account's sent folder."""


def _account_kind(text):
    if re.search(LOGIN_NEEDED, text, re.I):
        return "login"
    if re.search(RATE_LIMITED, text, re.I):
        return "rate_limited"
    return "restricted"


def classify(exc):
    """Only what Reddit's own wording establishes: 'login' / 'rate_limited' / 'restricted' (this account),
    'recipient' (no such user), 'refused' (refused, cause not stated) or 'error' (anything else)."""
    if isinstance(exc, AccountBlocked):
        return exc.kind
    if isinstance(exc, NoSuchUser):
        return "recipient"
    if isinstance(exc, MessageRefused):
        return "refused"
    text = str(exc)
    if re.search(ACCOUNT_LIMITED, text, re.I):
        return _account_kind(text)
    if re.search(NO_SUCH_USER, text, re.I):
        return "recipient"
    if re.search(UNAVAILABLE, text, re.I):
        return "refused"
    return "error"


class StepFailed(RuntimeError):
    """A step failed; the message names the step and the reason, kind is classify() of the cause."""

    def __init__(self, message, kind="error"):
        super().__init__(message)
        self.kind = kind


@dataclass
class Result:
    """What happened to one send. Truthy only when the message was sent.
    status:
      'sent'           confirmed: the message is in this account's sent folder, to that recipient
      'recipient'      Reddit says there is no such user
      'refused'        Reddit refused without saying whether the recipient or this account is the cause
      'login'          not logged in, or logged in as the wrong account            } account-wide:
      'rate_limited'   Reddit says this account is sending too much                } stop sending
      'restricted'     suspended, locked, captcha, ...                             }
      'invalid'        bad entry (username, subject, body or link); nothing was opened or submitted
      'not_submitted'  failed before Send was clicked; safe to retry
      'uncertain'      Send was clicked but the message couldn't be confirmed; it may have gone out,
                       so it must be checked before any retry
    reason starts with 'compose: ' (the flow that produced it)."""
    status: str
    reason: str = ""

    def __bool__(self):
        return self.status == "sent"


def check_recipient(driver, username):
    """Raise at once if the page shows a refusal (MessageRefused) or says there is no such user (NoSuchUser)."""
    missing = js(driver, "notice", NO_SUCH_USER)
    if missing:
        raise NoSuchUser(f"Reddit says there is no user u/{username}: {missing!r}")
    why = js(driver, "notice", UNAVAILABLE)
    if why:
        raise MessageRefused(f"Reddit refused a conversation with u/{username}: {why!r} (it doesn't say whether "
                             "that is their message settings or a restriction on this account)")


def check_account(driver):
    """Raise AccountBlocked at once if the page says this account is rate-limited, suspended or logged out."""
    why = js(driver, "notice", ACCOUNT_LIMITED)
    if why:
        raise AccountBlocked(f"Reddit is refusing this account: {why!r}", _account_kind(why))


def reddit_json(driver, url):
    """Open one of Reddit's .json URLs in the logged-in browser and parse it. RuntimeError if the page
    isn't JSON (logged out, blocked, or an error page)."""
    driver.get(url)
    text = driver.execute_script("return document.body ? document.body.innerText : ''")
    try:
        return json.loads(text)
    except ValueError:
        raise RuntimeError(f"{url} did not return JSON (logged out or blocked?): {text[:120]!r}") from None


def active_sender(driver):
    """Username of the account this browser profile is logged in as. AccountBlocked('login') if none."""
    try:
        name = (reddit_json(driver, ME_URL).get("data") or {}).get("name")
    except RuntimeError as exc:
        raise AccountBlocked(f"couldn't tell which account is logged in: {exc}", "login") from None
    if not name:
        raise AccountBlocked("not logged in: Reddit's /api/me has no username", "login")
    return name


def verify_sender(driver, expected):
    """Stop (AccountBlocked 'login') unless the profile is logged in as `expected`. Returns the name."""
    name = active_sender(driver)
    if name.lower() != expected.strip().lower().removeprefix("u/"):
        raise AccountBlocked(f"logged in as u/{name}, but config.json's sender_username is u/{expected}", "login")
    return name


def find_in_sent(driver, want, since, sender=None, pages=2):
    """The sent-folder message matching want (recipient, subject, body; sent by `sender`, created at or after
    `since` - 2 min of clock slack), or None. RuntimeError if the folder can't be read."""
    url, after = SENT_URL, None
    for _ in range(pages):
        data = reddit_json(driver, url + (f"&after={after}" if after else ""))["data"]
        for child in data["children"]:
            msg = child["data"]
            if (msg.get("created_utc", 0) >= since - 120
                    and (msg.get("dest") or "").lower().removeprefix("u/") == want["to"].lower()
                    and (not sender or (msg.get("author") or "").lower() == sender.lower())
                    and _squash(msg.get("subject")) == _squash(want["subject"])
                    and _squash(msg.get("body")) == _squash(want["message"])):
                return msg
        after = data.get("after")
        if not after:
            return None
    return None


def verify_in_sent(driver, want, since, sender=None, timeout=None):
    """Wait until the message is in the sent folder; NotInSent if it isn't within `timeout` seconds
    (default VERIFY_TIMEOUT)."""
    timeout = VERIFY_TIMEOUT if timeout is None else timeout
    deadline = time.monotonic() + timeout
    while True:
        msg = find_in_sent(driver, want, since, sender)
        if msg:
            return msg
        if time.monotonic() > deadline:
            raise NotInSent(f"no message to u/{want['to']} with this subject and body in the sent folder "
                            f"after {timeout}s")
        time.sleep(5)


def js(driver, fn, *args):
    return driver.execute_script(_JS, fn, *args)


def pause(lo=0.4, hi=1.1):
    """Human-pacing delay between actions - never used to wait for the page."""
    time.sleep(random.uniform(lo, hi))


def _squash(text):
    text = re.sub(r"[\u200b\u200c\u200d\ufeff]", "", text or "")
    return re.sub(r"\s+", " ", text).strip().lower()


def _reason(exc):
    text = (getattr(exc, "msg", None) or str(exc) or "").strip()
    return text.splitlines()[0] if text else "(no message)"


def _live(el):
    """el if it is still attached to the page, else None (a stale element cannot be passed to JS)."""
    try:
        el.is_enabled()
        return el
    except (StaleElementReferenceException, NoSuchElementException):
        return None


def wait_for(driver, probe, what, timeout=TIMEOUT):
    """WebDriverWait that survives re-renders and always says what it was waiting for
    (a bare TimeoutException is what prints the empty 'Message:')."""
    last = []

    def attempt(d):
        try:
            return probe(d)
        except (StaleElementReferenceException, ElementClickInterceptedException,
                ElementNotInteractableException, MoveTargetOutOfBoundsException, JavascriptException) as exc:
            last[:] = [exc]
            return False

    try:
        return WebDriverWait(driver, timeout, poll_frequency=POLL).until(attempt)
    except TimeoutException:
        detail = f" (last error: {type(last[0]).__name__}: {_reason(last[0])})" if last else ""
        raise TimeoutException(f"timed out after {timeout}s waiting for {what}{detail}") from None


def click(driver, el, methods=("native", "actions", "js")):
    """Scroll el to mid-viewport and click it, moving on to the next method if one is blocked."""
    js(driver, "center", el)
    error = None
    for how in methods:
        try:
            if how == "native":
                el.click()
            elif how == "actions":
                ActionChains(driver).move_to_element(el).pause(0.1).click().perform()
            else:
                driver.execute_script("arguments[0].click();", el)
            return
        except (ElementClickInterceptedException, ElementNotInteractableException,
                MoveTargetOutOfBoundsException) as exc:
            error = exc
    raise error


def type_text(driver, el, text):
    """Type into el key by key, check that it landed, and fall back to setting it directly."""
    click(driver, el)                                    # focus
    if js(driver, "read", el).strip():
        js(driver, "set", el, "")                        # leftover draft from an earlier attempt
    for i, line in enumerate(text.split("\n")):
        if i:                                            # line break: Shift+Enter (safe in any text box)
            ActionChains(driver).key_down(Keys.SHIFT).send_keys(Keys.ENTER).key_up(Keys.SHIFT).perform()
        if all(ord(ch) <= 0xFFFF for ch in line):
            for ch in line:
                el.send_keys(ch)
                time.sleep(random.uniform(0.03, 0.12))   # typing cadence
        else:                                            # chromedriver cannot send_keys emoji / non-BMP text
            driver.execute_cdp_cmd("Input.insertText", {"text": line})
    if _squash(text) not in _squash(js(driver, "read", el)):
        js(driver, "set", el, text)
        if _squash(text) not in _squash(js(driver, "read", el)):
            raise RuntimeError("typed text did not land in the field")


@contextmanager
def step(driver, label, doing):
    print(f"[{label}] {doing}")
    try:
        yield
    except Exception as exc:
        reason = f"{type(exc).__name__}: {_reason(exc)}"
        print(f"[{label}] FAILED - {reason}")
        try:
            driver.save_screenshot(f"{label}_failed.png")
            print(f"    saved {label}_failed.png")
        except Exception as save_error:
            print(f"    could not save screenshot: {save_error}")
        try:
            with open(f"{label}_failed_dom.txt", "w", encoding="utf-8") as fh:
                fh.write(js(driver, "dump"))
            print(f"    saved {label}_failed_dom.txt")
        except Exception as save_error:
            print(f"    could not save DOM listing: {save_error}")
        raise StepFailed(f"{label}: {reason}", classify(exc)) from exc
    print(f"[{label}] ok")


def wait_until_loaded(driver, ready=None, what="the controls this step needs"):
    """Wait until the page is usable. ready(driver) -> truthy once the controls the next step needs are on
    the page: given, that is the only test (document.readyState, the URL and how much is on the page are
    ignored, since Reddit's pages can keep loading long after the form works). Without it: wait for the
    page to render, then to stop changing. what: those controls, for the timeout message."""
    def logged_in(d):
        url = d.current_url
        if re.search(r"reddit\.com/(login|register)", url):
            raise AccountBlocked(f"not logged in: redirected to {url}")

    if ready:
        wait_for(driver, lambda d: logged_in(d) or ready(d), what)
        return

    def rendered(d):
        logged_in(d)
        state = js(d, "state")
        return state["ready"] == "complete" and state["controls"] > 0

    wait_for(driver, rendered, "the page to render")

    quiet = {"nodes": -1, "polls": 0}

    def settled(d):
        nodes = js(d, "state")["nodes"]
        quiet["polls"] = quiet["polls"] + 1 if nodes == quiet["nodes"] else 0
        quiet["nodes"] = nodes
        return quiet["polls"] >= 3                       # nothing added or removed for ~1s

    try:
        wait_for(driver, settled, "the page to stop rendering", timeout=10)
    except TimeoutException:
        print("    note: page still changing after 10s; carrying on")


# ----------------------------------------------------------------- compose form
# Real markup (www.reddit.com/message/compose/): faceplate-text-input >> <input name="message-recipient-input">,
# faceplate-text-input >> <input name="message-title">, faceplate-textarea-input >> <textarea name="message-content">,
# and a light-DOM <button type="submit">Send</button>. The page prefills all three fields from ?to=&subject=&message=.
COMPOSE_FIELDS = (("to", "input", "message-recipient-input"),
                  ("subject", "input", "message-title"),
                  ("message", "box", "message-content"))


def _clean(username, message):
    """Validate; the message is returned unchanged (only a leading u/ is dropped from the username)."""
    username = re.sub(r"^/?u/", "", username.strip())
    if not re.fullmatch(r"[A-Za-z0-9_-]+", username):
        raise ValueError(f"not a valid Reddit username: {username!r}")
    if not message.strip():
        raise ValueError("message is empty")
    return username, message


def compose_link(username, message, subject=DEFAULT_SUBJECT):
    """The same link format the queue files use."""
    username, message = _clean(username, message)
    return COMPOSE_URL + "?" + urllib.parse.urlencode({"to": username, "subject": subject, "message": message},
                                                      quote_via=urllib.parse.quote)


def parse_compose_link(link):
    """{'to', 'subject', 'message'} from a /message/compose/ link."""
    parts = urllib.parse.urlparse(link)
    if not re.search(r"(^|\.)reddit\.com$", parts.netloc) or not parts.path.rstrip("/").endswith("/message/compose"):
        raise ValueError(f"not a reddit.com/message/compose/ link: {link!r}")
    query = urllib.parse.parse_qs(parts.query)
    return {key: query.get(key, [""])[0] for key, _, _ in COMPOSE_FIELDS}


def compose_form(driver):
    """{'to': el, 'subject': el, 'message': el} once all three fields are on the page, else None."""
    form = {}
    for key, kind, name in COMPOSE_FIELDS:
        found = js(driver, "fields", kind, f"^{name}$|^{name} ")
        if not found:
            return None
        form[key] = found[0]
    return form


def fill_compose(driver, want):
    """Check each prefilled field against the link. A field Reddit left empty is typed in; a field holding
    anything else stops the send (nothing has been submitted). All three are re-read before returning.
    A refused or unknown recipient (MessageRefused / NoSuchUser) stops it at once, also before Send."""
    def loaded(d):
        check_account(d)
        check_recipient(d, want["to"])
        return compose_form(d)

    form = wait_for(driver, loaded, "the compose form's To / Title / Message fields")
    for key, _, _ in COMPOSE_FIELDS:
        have = js(driver, "read", form[key])
        if _squash(have) == _squash(want[key]):
            continue
        if have.strip():
            raise RuntimeError(f"the {key} field shows {have[:80]!r}, not what the link says; not submitting")
        print(f"    {key} field is empty; typing it in")
        pause()
        type_text(driver, form[key], want[key])
    for key, _, _ in COMPOSE_FIELDS:
        if _squash(js(driver, "read", form[key])) != _squash(want[key]):
            raise RuntimeError(f"the {key} field still doesn't match the link; not submitting")
    await_recipient(driver, want["to"])
    return form


def await_recipient(driver, username):
    """Reddit checks the To field a moment after the page loads. Watch for RECIPIENT_SETTLE seconds so a
    refusal stops the send with MessageRefused / NoSuchUser instead of surfacing only after Send."""
    deadline = time.monotonic() + RECIPIENT_SETTLE
    while time.monotonic() < deadline:
        check_account(driver)
        check_recipient(driver, username)
        time.sleep(POLL)


def submit_compose(driver, form, username, about_to_click):
    """Click Send exactly once (no fallback click methods: a second click could send twice).
    about_to_click() runs immediately before the click, after every pre-submit check has passed."""
    check_recipient(driver, username)                    # the To field may only now have been validated
    alerts = js(driver, "alerts")
    send = js(driver, "css", 'button[type="submit"]', SEND) or js(driver, "css", 'button, [role="button"]', SEND)
    if not send:
        raise RuntimeError("no Send button on the compose form")
    pause(0.3, 0.8)
    about_to_click()
    try:
        click(driver, send[0], methods=("native",))
    except (ElementClickInterceptedException, ElementNotInteractableException, MoveTargetOutOfBoundsException) as exc:
        raise SendNotClicked(f"the Send click was blocked ({type(exc).__name__}); nothing was submitted") from exc
    return alerts


def confirm_compose(driver, form, alerts_before, username):
    """Sent = Reddit navigated away from the form, cleared it, or showed a non-error notice.
    A notice matching FAILED (rate limit, unknown user, ...) fails the step."""
    def outcome(d):
        check_account(d)
        check_recipient(d, username)
        said = [a for a in js(d, "alerts") if a not in alerts_before]
        errors = [a for a in said if re.search(FAILED, a)]
        if errors:
            raise RuntimeError(f"Reddit refused the message: {errors[0]!r}")
        if "/message/compose" not in d.current_url:
            return f"left the compose page for {d.current_url}"
        box = _live(form["message"])
        if box is None or not js(d, "read", box).strip():
            return "the compose form was cleared"
        if said:
            return f"the page says {said[0]!r}"
        return False

    how = wait_for(driver, outcome, "Reddit to accept the message")
    print(f"    {how}")


def send_compose(driver, link, *, sender=None, before_submit=None):
    """Open a /message/compose/ link exactly as given (as found in a queue file), check the prefilled
    fields, click Send once, then confirm the message is in this account's sent folder.
    sender: expected author for that check. before_submit(): called right before Send is clicked
    (the ledger uses it to note that a submission is in flight)."""
    try:
        want = parse_compose_link(link)
        want["to"], want["message"] = _clean(want["to"], want["message"])
        if not want["subject"].strip():
            raise ValueError("compose link has no subject; Reddit requires a title")
    except ValueError as exc:
        print(f"[!] not sending: {exc}")
        return Result("invalid", f"compose: {exc}")
    clicked = {"at": None}

    def about_to_click():
        if before_submit:
            before_submit()
        clicked["at"] = time.time()                      # set before the click: a click that errors may still land

    try:
        driver.switch_to.default_content()
        with step(driver, "step1_open_compose", f"opening the compose link for u/{want['to']}"):
            driver.get(link)
            wait_until_loaded(driver, compose_form, "the compose form's To / Title / Message fields")
        with step(driver, "step2_check_form", "checking the prefilled To / Title / Message"):
            form = fill_compose(driver, want)
        with step(driver, "step3_send", 'clicking "Send" (once)'):
            alerts = submit_compose(driver, form, want["to"], about_to_click)
        with step(driver, "step4_confirm", "waiting for Reddit to accept the message"):
            confirm_compose(driver, form, alerts, want["to"])
        with step(driver, "step5_verify", "looking for the message in this account's sent folder"):
            verify_in_sent(driver, want, clicked["at"], sender)
    except StepFailed as exc:
        if exc.kind != "error":                          # Reddit said what went wrong: not delivered
            return Result(exc.kind, f"compose: {exc}")
        if clicked["at"] is None or isinstance(exc.__cause__, SendNotClicked):
            return Result("not_submitted", f"compose: {exc}")
        try:                                             # it may have gone out anyway
            if find_in_sent(driver, want, clicked["at"], sender):
                print("    ...but it is in the sent folder")
                return Result("sent", f"compose: confirmed in the sent folder after: {exc}")
        except Exception as check_error:
            exc = f"{exc}; sent folder unreadable: {check_error}"
        return Result("uncertain", f"compose: Send was clicked; {exc}")
    print(f"[done] message to {want['to']} is in the sent folder")
    return Result("sent", "compose: confirmed in the sent folder")


# ----------------------------------------------------------------- entry point
def send_dm(driver, username, message, *, subject=DEFAULT_SUBJECT, sender=None, before_submit=None):
    try:
        link = compose_link(username, message, subject)
    except ValueError as exc:
        print(f"[!] not sending: {exc}")
        return Result("invalid", f"compose: {exc}")
    return send_compose(driver, link, sender=sender, before_submit=before_submit)


# ----------------------------------------------------------------- run directly
if __name__ == "__main__":
    import argparse
    import sys
    from pathlib import Path

    import undetected_chromedriver as uc
    from chrome_version import chrome_major

    ap = argparse.ArgumentParser(description="Send one Reddit private message via /message/compose/.")
    ap.add_argument("username")
    ap.add_argument("message")
    ap.add_argument("--subject", default=DEFAULT_SUBJECT)
    args = ap.parse_args()
    profile = Path(__file__).resolve().parent / "accounts" / "account_1" / "chrome_profile"
    if not profile.is_dir():
        sys.exit(f"Profile folder not found: {profile}")

    chrome_options = uc.ChromeOptions()
    chrome_options.add_argument(f"--user-data-dir={profile}")
    chrome = uc.Chrome(options=chrome_options, version_main=chrome_major())
    try:
        chrome.set_window_size(1400, 900)
        result = send_dm(chrome, args.username, args.message, subject=args.subject)
        print("RESULT:", "sent" if result else f"not sent ({result.status}) - {result.reason}")
        input("Press Enter here to close Chrome... ")
    except KeyboardInterrupt:
        pass
    finally:
        chrome.quit()      # closes only the Chrome this script started
