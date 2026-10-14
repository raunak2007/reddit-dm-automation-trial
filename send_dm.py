"""send_dm.py - send one Reddit private message through the /message/compose/ form.

Run it directly (opens the automation profile, sends one message, then waits for Enter):

    python send_dm.py Efficient_Resolve_78 "Test message from the script" [--subject "Hello"]

or call it from your own code with any logged-in driver:

    from send_dm import send_dm, send_compose
    ok = send_dm(driver, "Efficient_Resolve_78", "Test message", subject="Hello")
    ok = send_compose(driver, "https://www.reddit.com/message/compose/?to=...&subject=...&message=...")

send_dm builds the same compose link a queue file contains and hands it to send_compose, which
opens it, checks that Reddit prefilled the recipient, title and message, clicks Send and waits
for Reddit to accept it. The older chat-UI flow (/chat/ + recipient search) is still available
as send_chat_dm.

Returns a Result that is truthy once Reddit accepts the message. On failure it prints the step
that failed, saves <step>_failed.png and <step>_failed_dom.txt, and returns a falsy Result whose
status says what Reddit's wording establishes, and no more (see classify()).

What a result does and doesn't show:
  - "Unable to message this account" and similar refusals don't say whether the recipient's settings
    or a restriction on the sending account caused them, so they are recorded as 'refused', not
    blamed on either side.
  - A refusal in the chat flow (send_chat_dm) is not evidence about the compose flow (send_compose),
    or the reverse; every Result.reason names the flow it came from.
  - Being logged in only shows the saved profile's session is valid. The scripts launch Chrome
    through undetected-chromedriver, and nothing here tests whether that changes how Reddit treats
    the account; no result should be read as evidence that it does.
Each step tries XPath, then a selector taken from the real markup, then generic fallbacks.
Requires Python 3.8+ and Selenium 4.
"""
from __future__ import annotations

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
    WebDriverException,
)
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait

TIMEOUT = 20   # seconds each wait may take
POLL = 0.3     # seconds between polls
START = r"^(start |create )?(a )?new chat$"
CONFIRM = r"^(start chat(ting)?|start|chat|create chat|next|done|continue|confirm)$"
SEND = r"^send( a)?( message| chat)?$"
FAILED = r"fail|unable|couldn.t|can.t|not sent|try again|limit"
# Reddit refusing a conversation ("Unable to message this account"). The wording doesn't say whether
# the recipient's message settings or a restriction on the sending account caused it.
UNAVAILABLE = (r"unable to (message|chat|dm)|can.?t (message|chat|dm)|cannot (message|chat|dm)|"
               r"(not|n.t) accepting (messages|chats|dms)|does ?n.t accept (messages|chats|dms)")
NO_SUCH_USER = r"invalid username|no matching users|(user|account) (does ?n.t|does not) exist|user not found|no such user"
# Reddit refusing this account rather than one recipient (rate limit, suspension, logged out).
ACCOUNT_LIMITED = (r"doing that (too much|a lot)|take a break|rate.?limit|too many (messages|requests|attempts)|"
                   r"try again in \d|(account|you).{0,25}(suspended|banned|restricted|locked)|not logged in|"
                   r"captcha|verify your (email|account)")
COMPOSE_URL = "https://www.reddit.com/message/compose/"
DEFAULT_SUBJECT = "Hello"

# Page-side helpers. Everything here walks the page *including* open shadow roots.
_JS = r"""
const SDM = (() => {
  const SKIP = new Set(['SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE', 'HEAD', 'svg']);
  const INPUT = 'input:not([type]), input[type=""], input[type="text"], input[type="search"]';
  const BOX = 'textarea, [contenteditable]:not([contenteditable="false"]), [role="textbox"]';
  const CONTROL = 'button, a[href], label, [role="button"], [role="link"], [role="option"], ' +
                  '[role="menuitem"], [role="menuitemcheckbox"], [role="checkbox"]';
  const OPTION = '[role="option"], [role="listbox"], [role="menuitem"], [role="menuitemcheckbox"], [role="checkbox"]';
  const NO_RESULTS = /no (results|users|matches)|not found|couldn.t find|does ?n.t exist/;

  const norm = s => (s || '').replace(/\s+/g, ' ').trim().toLowerCase();
  const uniq = list => [...new Set(list)];
  const sig = el => ['name', 'id', 'placeholder', 'aria-label', 'title', 'role', 'data-testid', 'class']
    .map(a => el.getAttribute(a) || '').join(' ');
  // Whole-token username match: "bob" matches "bob" and "u/bob" but not "bob_2".
  const token = name => new RegExp('(^|[^a-z0-9_-])' + norm(name).replace(/[^a-z0-9_]/g, '\\$&') + '($|[^a-z0-9_-])');

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
  const within = (el, container) => { for (let n = el; n; n = up(n)) if (n === container) return true; return false; };
  const closest = (el, selector, max) => {
    for (let n = el, i = 0; n && i <= max; n = up(n), i++) if (n.matches(selector)) return n;
    return null;
  };
  // The custom elements whose shadow trees el sits in, outermost first (rs-app, rs-direct-chat, ...).
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
  const hasBox = el => { const r = el.getBoundingClientRect(); return r.width > 2 && r.height > 2; };
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

  // What to click for a piece of text: the control wrapping it, else the text element itself.
  function target(el) {
    const control = closest(el, CONTROL, 5);
    if (control && hasBox(control)) return control;
    for (let n = el; n; n = up(n)) if (hasBox(n)) return n;
    return el;
  }

  return {
    url: () => location.href,
    visible: el => visible(el),
    center: el => el.scrollIntoView({block: 'center', inline: 'center'}),
    read: el => el.matches('input, textarea') ? el.value : (el.innerText || ''),
    snapshot: () => { window.__sdmSeen = new WeakSet(all()); },
    state: () => {
      const els = all();
      return {
        ready: document.readyState,
        nodes: els.length,
        controls: els.filter(el => el.matches('button, a[href], input, textarea, iframe, [role="button"]') && visible(el)).length,
      };
    },

    // Elements by visible text (exact or substring), returned as the thing to click.
    text: (text, exact) => {
      const t = norm(text);
      return uniq(byText(s => exact ? s === t : s.includes(t)).map(target)).slice(0, 5);
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
    // fresh: only fields that did not exist when snapshot() was taken.
    fields: (kind, hint, skip, fresh) => {
      const selector = kind === 'input' ? INPUT : kind === 'box' ? BOX : INPUT + ', ' + BOX;
      const re = hint ? new RegExp(hint, 'i') : null;
      const seen = window.__sdmSeen;
      return all().filter(el =>
        el.matches(selector) && (el.matches('input, textarea') || el.isContentEditable) && el !== skip &&
        visible(el) && enabled(el) && (!re || re.test(sig(el))) && (!fresh || !seen || !seen.has(el)));
    },

    focused: () => {
      let a = document.activeElement;
      while (a && a.shadowRoot && a.shadowRoot.activeElement) a = a.shadowRoot.activeElement;
      return a && a.matches(INPUT) && visible(a) ? [a] : [];
    },

    // Search-result rows that show exactly `username`, top-most first.
    //   'text' -> anything with that text sitting below the recipient field (i.e. in its dropdown)
    //   'role' -> rows that are role=option / inside a listbox / checkbox rows
    //   'aria' -> controls below the field whose aria-label or title names the user
    results: (username, field, mode) => {
      const exact = token(username);
      const f = field.getBoundingClientRect();
      const below = el => { const r = box(el); return r.top >= f.top + f.height / 2 && r.left < f.right && r.right > f.left; };
      const byTop = (a, b) => box(a).top - box(b).top;
      if (mode === 'aria') {
        return all().filter(el => el !== field && el.matches(CONTROL + ', input') && visible(el) && below(el) &&
          exact.test(norm(el.getAttribute('aria-label') || el.getAttribute('title')))).sort(byTop).slice(0, 5);
      }
      const hits = byText(s => exact.test(s) && s.length < 200 && !NO_RESULTS.test(s));
      const rows = mode === 'role' ? hits.filter(el => closest(el, OPTION, 8)) : hits.filter(below);
      return uniq(rows.map(target)).sort(byTop).slice(0, 5);
    },

    // How many places (outside `skip`, the message box) currently display `needle`.
    count: (needle, skip) => {
      const n = norm(needle);
      return byText(s => s.includes(n)).filter(el => !skip || !within(el, skip)).length;
    },

    // Is `username` displayed in the conversation that owns the message box? Either inside the same
    // conversation component (rs-direct-chat / rs-room), or above the box in the same column.
    shows: (username, boxEl) => {
      const exact = token(username);
      const b = boxEl.getBoundingClientRect();
      const chain = hostChain(boxEl);
      const pane = chain.length >= 3 ? chain[chain.length - 2] : null;
      return byText(s => exact.test(s)).some(el => {
        if (within(el, boxEl)) return false;
        if (pane && hostChain(el).includes(pane)) return true;
        const r = box(el);
        return r.bottom <= b.top + 4 && r.left < b.right && r.right > b.left;
      });
    },

    // Why the result row `row` can't be picked: Reddit's "unable to message" text or a disabled flag on
    // the row or its ancestors, up to the row's own <li> / option (not the whole dropdown). '' = pickable.
    blocked: (row, pattern) => {
      const re = new RegExp(pattern, 'i');
      let disabled = false;
      for (let n = row, i = 0; n && i <= 6; n = up(n), i++) {
        if (n.disabled || n.getAttribute('aria-disabled') === 'true') disabled = true;
        for (const s of [norm(n.innerText || n.textContent), norm(n.getAttribute('aria-label'))]) {
          const m = s.match(re);
          if (m) return s.slice(m.index, m.index + 120);
        }
        if (n.matches('li, [role="option"]')) break;
      }
      return disabled ? 'the result row is disabled' : '';
    },

    // Visible text matching `pattern` anywhere except the inbox list (whose previews could say anything),
    // text fields (which hold our own message) and result rows (other users' rows; see blocked()).
    notice: pattern => {
      const re = new RegExp(pattern, 'i');
      const hit = byText(s => re.test(s) && s.length < 200)
        .find(el => !hostChain(el).some(h => h.localName === 'rs-rooms-nav') && !closest(el, BOX + ', input, li, ' + OPTION, 10));
      return hit ? norm(hit.textContent) : '';
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
    """Reddit is refusing this account (rate limit, suspension, logged out); every other send would fail too."""


def classify(exc):
    """Only what Reddit's own wording establishes: 'account' (names this account), 'recipient' (no such user),
    'refused' (refused, cause not stated) or 'error' (anything else)."""
    if isinstance(exc, AccountBlocked):
        return "account"
    if isinstance(exc, NoSuchUser):
        return "recipient"
    if isinstance(exc, MessageRefused):
        return "refused"
    text = str(exc)
    if re.search(ACCOUNT_LIMITED, text, re.I):
        return "account"
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
    status: 'sent' | 'recipient' (Reddit says no such user) | 'invalid' (bad username/subject/body)
            | 'refused' (Reddit refused without saying whether the recipient or this account is the cause)
            | 'account' (this account is blocked or rate-limited: stop) | 'error' (anything else: retry later)
    reason starts with the flow that produced it ('compose: ' or 'chat: '); a refusal in one flow says
    nothing about the other."""
    status: str
    reason: str = ""

    def __bool__(self):
        return self.status == "sent"


def check_recipient(driver, username, row=None, page=True):
    """Raise at once if u/username's result row or the page shows a refusal (MessageRefused) or says
    there is no such user (NoSuchUser)."""
    row = row is not None and _live(row)
    if page:
        missing = js(driver, "notice", NO_SUCH_USER)
        if missing:
            raise NoSuchUser(f"Reddit says there is no user u/{username}: {missing!r}")
    why = (row and js(driver, "blocked", row, UNAVAILABLE)) or (page and js(driver, "notice", UNAVAILABLE))
    if why:
        raise MessageRefused(f"Reddit refused a conversation with u/{username}: {why!r} (it doesn't say whether "
                             "that is their message settings or a restriction on this account)")


def check_account(driver):
    """Raise AccountBlocked at once if the page says this account is rate-limited, suspended or logged out."""
    why = js(driver, "notice", ACCOUNT_LIMITED)
    if why:
        raise AccountBlocked(f"Reddit is refusing this account: {why!r}")


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


def _needle(message):
    """A plain chunk of the message to look for in the thread (no line breaks or emoji,
    which the page may render differently)."""
    chunks = re.split(r"[\r\n]|[^\u0000-\uffff]", message)
    return max(chunks, key=len).strip()[:40].strip()


def _ci(attr, word):
    """XPath 1.0 has no lower-case(): case-insensitive 'attribute contains word'."""
    return f"contains(translate(@{attr}, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '{word}')"


def xp(driver, expr):
    """Plain XPath lookup. Light DOM only: it cannot see inside shadow roots."""
    return [el for el in driver.find_elements(By.XPATH, expr) if el.is_displayed()]


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


def first_match(finders):
    """Try each (label, finder) in order; return (element, label) for the first that finds anything."""
    for label, find in finders:
        found = find()
        if found:
            return found[0], label
    return None


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
        if i:                                            # line break: Shift+Enter (plain Enter would send)
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


# ----------------------------------------------------------------- step 1
def wait_until_loaded(driver, ready=None):
    """Wait for the page to render, then to stop changing. ready(driver) -> truthy once the controls
    the next step needs are on the page; as soon as it is, the page counts as loaded."""
    def rendered(d):
        url = d.current_url
        if re.search(r"reddit\.com/(login|register)", url):
            raise AccountBlocked(f"not logged in: redirected to {url}")
        state = js(d, "state")
        return state["ready"] == "complete" and state["controls"] > 0

    wait_for(driver, rendered, "the page to render")
    if ready and ready(driver):
        return

    quiet = {"nodes": -1, "polls": 0}

    def settled(d):
        if ready and ready(d):
            return True
        nodes = js(d, "state")["nodes"]
        quiet["polls"] = quiet["polls"] + 1 if nodes == quiet["nodes"] else 0
        quiet["nodes"] = nodes
        return quiet["polls"] >= 3                       # nothing added or removed for ~1s

    try:
        wait_for(driver, settled, "the page to stop rendering", timeout=10)
    except TimeoutException:
        print("    note: page still changing after 10s; carrying on")


# ----------------------------------------------------------------- step 2
# Real markup: the pill is <a class="... button-primary ..." href="/room/create"> inside
# rs-app >> rs-welcome-screen; the header icon is <a aria-label="Create new chat" href="/room/create">.
def start_chat_finders(d):
    return [
        ("xpath text", lambda: xp(d, "//*[self::button or self::a or @role='button'][normalize-space()='Start new chat']")),
        ("exact: welcome-screen pill", lambda: js(d, "css", 'a[href$="/room/create"]', None, "rs-welcome-screen")),
        ("text inside shadow DOM", lambda: js(d, "text", "Start new chat", True)),
        ("role", lambda: js(d, "css", 'button, a[href], [role="button"], [role="link"]', START)),
        ("aria-label: header icon", lambda: js(d, "css", 'a[aria-label="Create new chat"], a[href$="/room/create"], '
                                                        '[aria-label*="new chat" i], [aria-label*="start chat" i], '
                                                        '[aria-label*="create chat" i]', None)),
    ]


def locate_in_any_frame(driver, finders):
    """Run the finders in the top document, then inside each iframe. The driver is left
    switched into whichever document matched, so the later steps look in the same place."""
    driver.switch_to.default_content()
    hit = first_match(finders(driver))
    if hit:
        return hit
    for frame in js(driver, "css", "iframe", None):
        try:
            driver.switch_to.frame(frame)
            hit = first_match(finders(driver))
            if hit:
                return hit
        except WebDriverException:
            pass
        driver.switch_to.default_content()
    return None


# The new-chat form counts as open when its username input is on the page. Only specific finders here:
# recipient_finders' generic fallbacks ("any visible text input") would also match the site search box.
def new_chat_finders(d):
    return [
        ("exact: #user-search-input", lambda: js(d, "css", "input#user-search-input", None)),
        ("text input in the new-chat form", lambda: js(d, "css", 'input:not([type]), input[type="text"], '
                                                                 'input[type="search"]', None, "rs-room-creation")),
        ("xpath attribute", lambda: recipient_finders(d)[0][1]()),
    ]


def new_chat_open(driver):
    return locate_in_any_frame(driver, new_chat_finders)


def chat_page_ready(driver):
    """The chat app has drawn what step 2 needs: the new-chat form, or a button that opens it."""
    return new_chat_open(driver) or locate_in_any_frame(driver, start_chat_finders)


def click_start_chat(driver):
    js(driver, "snapshot")                               # lets step 3 tell which inputs are new
    already = new_chat_open(driver)
    if already:
        print(f"    the new-chat form is already open (found its username input via {already[1]})")
        return
    _, via = wait_for(driver, lambda d: locate_in_any_frame(d, start_chat_finders), 'the "Start new chat" button')
    print(f"    found via {via}")
    url = js(driver, "url")
    fields = len(js(driver, "fields", "any", None, None, False))
    methods = ("native", "actions", "js")
    state = {"clicks": 0, "at": float("-inf"), "el": None}

    def opened(d):
        if first_match(new_chat_finders(d)):              # same document the button was found in
            return True
        if state["clicks"]:
            live = _live(state["el"])
            gone = live is None or not js(d, "visible", live)
            if gone or js(d, "url") != url or len(js(d, "fields", "any", None, None, False)) > fields:
                return True
        # First click, or the previous click did nothing: click again with the next method.
        if state["clicks"] < len(methods) and time.monotonic() - state["at"] > 4:
            hit = first_match(start_chat_finders(d))
            if hit:
                state["el"] = hit[0]
                pause(0.2, 0.6)
                click(d, hit[0], methods[state["clicks"]:])
                state["clicks"] += 1
                state["at"] = time.monotonic()
        return False

    wait_for(driver, opened, "the new-chat panel's username input to appear after the click")


# ----------------------------------------------------------------- step 3
# Real markup: <input type="text" id="user-search-input" aria-label="Begin typing to search for users.">
# inside rs-app >> rs-room-creation >> rs-direct-chat-creation >> rs-users-multiselect.
def recipient_finders(d):
    attrs = " or ".join(_ci(a, "user") for a in ("placeholder", "aria-label", "name"))
    return [
        ("xpath attribute", lambda: xp(d, f"//input[{attrs}]")),
        ("exact: #user-search-input", lambda: js(d, "css", "input#user-search-input", None)),
        ("role", lambda: js(d, "fields", "input", r"combobox|searchbox", None, True)),
        ("aria-label / placeholder", lambda: js(d, "fields", "input", r"user|recipient|people|member|invite", None, False)),
        ("new input since the click", lambda: js(d, "fields", "input", None, None, True)),
        ("focused input", lambda: js(d, "focused")),
        ("any visible text input", lambda: js(d, "fields", "input", None, None, False)),
    ]


def enter_recipient(driver, username):
    field, via = wait_for(driver, lambda d: first_match(recipient_finders(d)), "the username input")
    print(f"    found via {via}")
    pause()
    type_text(driver, field, username)
    return field


# ----------------------------------------------------------------- step 4
# Real markup: <li aria-label="<name> user found, press enter to select user" role="presentation">
#                <div role="button" tabindex="0"> ... <span class="text-body-2"><name></span> ... </div></li>
def result_finders(d, username, field):
    return [
        ("xpath text", lambda: xp(d, f"//*[@role='option'][.//text()[normalize-space()='{username}' "
                                     f"or normalize-space()='u/{username}']]")),
        ("exact: 'user found' row", lambda: js(d, "css", f'li[aria-label^="{username} user found" i] [role="button"]', None)),
        ("exact text below the input", lambda: js(d, "results", username, field, "text")),
        ("role=option", lambda: js(d, "results", username, field, "role")),
        ("aria-label", lambda: js(d, "results", username, field, "aria")),
    ]


def pick_result(driver, username, field):
    seen = {"key": None, "row": None, "no_user": None}

    def pick(d):
        anchor = _live(field) or (first_match(recipient_finders(d)) or (None,))[0]
        hit = anchor and first_match(result_finders(d, username, anchor))
        if not hit:
            seen["key"] = None
            missing = js(d, "notice", NO_SUCH_USER)      # shown while searching too, so it must stay up 2s
            if not missing:
                seen["no_user"] = None
            elif seen["no_user"] is None:
                seen["no_user"] = time.monotonic()
            elif time.monotonic() - seen["no_user"] > 2:
                raise NoSuchUser(f"Reddit found no user u/{username}: {missing!r}")
            return False
        row, via = hit
        check_recipient(d, username, row, page=False)    # a disabled row is not a match to click
        key = (row.id, row.rect["x"], row.rect["y"])
        if key != seen["key"]:                           # click only once the list has stopped shifting
            seen["key"] = key
            return False
        pause(0.2, 0.5)
        click(d, row)
        seen["row"] = row
        return via

    via = wait_for(driver, pick, f"an exact match for '{username}' in the results dropdown "
                                 "(does the user exist and accept chats?)")
    print(f"    clicked via {via}")
    pause(0.3, 0.6)
    check_recipient(driver, username, seen["row"])      # some notices only appear after the click


# ----------------------------------------------------------------- step 5
# Real markup: <textarea name="message" placeholder="Message" aria-label="Write message">
# inside rs-app >> rs-direct-chat >> rs-message-composer.
def composer_finders(d, skip):
    attrs = " or ".join(_ci(a, "message") for a in ("placeholder", "aria-label", "name"))
    return [
        ("xpath attribute", lambda: xp(d, f"//textarea[{attrs}]")),
        ("exact: textarea[name=message]", lambda: js(d, "css", 'textarea[name="message"]', None, "rs-message-composer")),
        ("role=textbox", lambda: js(d, "fields", "box", r"textbox", skip, False)),
        ("aria-label / placeholder", lambda: js(d, "fields", "box", r"message|write|reply|say something", skip, False)),
        ("any textarea / contenteditable", lambda: js(d, "fields", "box", None, skip, False)[-1:]),
    ]


# The button that turns the ticked user into an open conversation. Not captured yet, so this
# looks inside the new-chat form (rs-room-creation) by text, then by type, then by styling.
def confirm_finders(d):
    form = "rs-room-creation"
    return [
        ("button text in the new-chat form", lambda: js(d, "css", 'button, [role="button"], a[href]', CONFIRM, form)),
        ("submit button in the new-chat form", lambda: js(d, "css", 'button[type="submit"]', None, form)),
        ("primary button in the new-chat form", lambda: js(d, "css", "button.button-primary, a.button-primary", None, form)),
        ("button text anywhere", lambda: js(d, "css", 'button, [role="button"]', r"^(start chat(ting)?|create chat)$")),
    ]


def write_message(driver, username, message, needle, field, verify_recipient):
    nudge = {"at": float("-inf")}

    def composer(d):
        check_account(d)
        check_recipient(d, username)
        hit = first_match(composer_finders(d, _live(field)))
        if hit:
            return hit
        # No message box yet: click the form's confirm button (again every 3s if nothing happened).
        if time.monotonic() - nudge["at"] > 3:
            confirm = first_match(confirm_finders(d))
            if confirm:
                click(d, confirm[0])
                nudge["at"] = time.monotonic()
                print(f"    clicked the confirm button ({confirm[1]})")
        return False

    box, via = wait_for(driver, composer, "the message box")
    print(f"    found via {via}")
    if verify_recipient:                                 # don't type into somebody else's conversation
        wait_for(driver, lambda d: js(d, "shows", username, box),
                 f"'{username}' to be shown in the open conversation (to skip this check: "
                 "verify_recipient=False, or --no-verify on the command line)", timeout=10)
    baseline = js(driver, "count", needle, None) if needle else 0
    pause()
    type_text(driver, box, message)
    return box, baseline


# ----------------------------------------------------------------- step 6
def press_enter(driver, box, needle, baseline):
    alerts = js(driver, "alerts")
    pause(0.3, 0.8)
    box.send_keys(Keys.ENTER)

    def left_the_box(d):
        live = _live(box)
        return (live is None or not js(d, "read", live).strip()
                or bool(needle and js(d, "count", needle, live) > baseline))

    try:
        wait_for(driver, left_the_box, "the message box to clear", timeout=5)
    except TimeoutException:
        send = js(driver, "css", 'button, [role="button"]', SEND)
        if not send:
            raise RuntimeError("ENTER did not send the message and no Send button was found") from None
        print("    ENTER left the text in the box; clicking Send instead")
        click(driver, send[0])
    return alerts


# ----------------------------------------------------------------- step 7
def confirm_sent(driver, box, needle, baseline, alerts_before):
    def in_thread(d):
        live = _live(box)
        if needle:
            return js(d, "count", needle, live) > baseline
        return live is None or not js(d, "read", live).strip()    # emoji-only: nothing to text-match

    def new_alerts(d):
        return [a for a in js(d, "alerts") if a not in alerts_before]

    def in_thread_or_refused(d):
        check_account(d)
        return in_thread(d)

    try:
        wait_for(driver, in_thread_or_refused, "the message to appear in the thread")
    except TimeoutException:
        said = new_alerts(driver)
        if said:
            raise RuntimeError(f"message never appeared in the thread; the page says: {said[0]!r}") from None
        raise
    # Chat UIs often draw the bubble first and report a send error a moment later.
    try:
        errors = wait_for(driver, lambda d: [a for a in new_alerts(d) if re.search(FAILED, a)],
                          "a send error", timeout=3)
    except TimeoutException:
        return
    raise RuntimeError(f"message was drawn but the page reports: {errors[0]!r}")


# ----------------------------------------------------------------- compose form
# Real markup (www.reddit.com/message/compose/): faceplate-text-input >> <input name="message-recipient-input">,
# faceplate-text-input >> <input name="message-title">, faceplate-textarea-input >> <textarea name="message-content">,
# and a light-DOM <button type="submit">Send</button>. The page prefills all three fields from ?to=&subject=&message=.
COMPOSE_FIELDS = (("to", "input", "message-recipient-input"),
                  ("subject", "input", "message-title"),
                  ("message", "box", "message-content"))


def _clean(username, message):
    username = re.sub(r"^/?u/", "", username.strip())
    if not re.fullmatch(r"[A-Za-z0-9_-]+", username):
        raise ValueError(f"not a valid Reddit username: {username!r}")
    message = message.replace("\r\n", "\n").strip()
    if not message:
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
    """Check each prefilled field against the link and type over anything Reddit left blank or changed."""
    form = wait_for(driver, lambda d: check_account(d) or compose_form(d),
                    "the compose form's To / Title / Message fields")
    check_recipient(driver, want["to"])
    for key, _, _ in COMPOSE_FIELDS:
        have = js(driver, "read", form[key])
        if _squash(have) == _squash(want[key]):
            continue
        print(f"    {key} field shows {have!r}; typing it in")
        pause()
        type_text(driver, form[key], want[key])
    return form


def submit_compose(driver, form, username):
    check_recipient(driver, username)                    # the To field may only now have been validated
    alerts = js(driver, "alerts")
    send = js(driver, "css", 'button[type="submit"]', SEND) or js(driver, "css", 'button, [role="button"]', SEND)
    if not send:
        raise RuntimeError("no Send button on the compose form")
    pause(0.3, 0.8)
    click(driver, send[0])
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


def send_compose(driver, link):
    """Open a /message/compose/ link (as found in a queue file), check the prefill, click Send."""
    try:
        want = parse_compose_link(link)
        want["to"], want["message"] = _clean(want["to"], want["message"])
        if not want["subject"].strip():
            raise ValueError("compose link has no subject; Reddit requires a title")
    except ValueError as exc:
        print(f"[!] not sending: {exc}")
        return Result("invalid", f"compose: {exc}")
    try:
        driver.switch_to.default_content()
        with step(driver, "step1_open_compose", f"opening the compose link for u/{want['to']}"):
            driver.get(link)
            wait_until_loaded(driver, compose_form)
        with step(driver, "step2_check_form", "checking the prefilled To / Title / Message"):
            form = fill_compose(driver, want)
        with step(driver, "step3_send", 'clicking "Send"'):
            alerts = submit_compose(driver, form, want["to"])
        with step(driver, "step4_confirm", "waiting for Reddit to accept the message"):
            confirm_compose(driver, form, alerts, want["to"])
    except StepFailed as exc:
        return Result(exc.kind, f"compose: {exc}")
    print(f"[done] message to {want['to']} sent")
    return Result("sent")


# ----------------------------------------------------------------- entry point
def send_dm(driver, username, message, *, subject=DEFAULT_SUBJECT):
    try:
        link = compose_link(username, message, subject)
    except ValueError as exc:
        print(f"[!] not sending: {exc}")
        return Result("invalid", f"compose: {exc}")
    return send_compose(driver, link)


# ----------------------------------------------------------------- chat-UI entry point
def send_chat_dm(driver, username, message, *, verify_recipient=True):
    """Old flow: driver must already be on https://www.reddit.com/chat/."""
    try:
        username, message = _clean(username, message)
    except ValueError as exc:
        print(f"[!] not sending: {exc}")
        return Result("invalid", f"chat: {exc}")
    needle = _needle(message)

    try:
        driver.switch_to.default_content()
        with step(driver, "step1_page_load", "waiting for the chat app to finish loading"):
            wait_until_loaded(driver, chat_page_ready)
        with step(driver, "step2_start_chat", 'clicking "Start new chat"'):
            click_start_chat(driver)
        with step(driver, "step3_recipient_input", f"typing the recipient: {username}"):
            field = enter_recipient(driver, username)
        with step(driver, "step4_select_result", "clicking the exact match in the results dropdown"):
            pick_result(driver, username, field)
        with step(driver, "step5_message_box", "typing the message"):
            box, baseline = write_message(driver, username, message, needle, field, verify_recipient)
        with step(driver, "step6_send", "pressing ENTER"):
            alerts = press_enter(driver, box, needle, baseline)
        with step(driver, "step7_confirm", "waiting for the message to show up in the thread"):
            confirm_sent(driver, box, needle, baseline, alerts)
    except StepFailed as exc:
        return Result(exc.kind, f"chat: {exc}")
    finally:
        try:
            driver.switch_to.default_content()
        except WebDriverException:
            pass
    print(f"[done] message to {username} is in the thread")
    return Result("sent")


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
