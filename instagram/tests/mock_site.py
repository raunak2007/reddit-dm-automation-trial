"""mock_site.py - a local stand-in for the Instagram pages send.py works on, and a headless Chrome to open it in.

MockSite serves pages on 127.0.0.1 from site.pages ({path: html, or a function of the path returning html}) and
follows site.redirects ({path: path}, as ig.me/m/<user> redirects to /direct/t/<thread>/).
profile_page()  a profile whose header has a Follow button (plus Message and Options), with "Suggested for you"
                Follow buttons below it, as Instagram shows once you follow someone. Pressing the header's Follow
                turns it into Following / Requested, or pops up a block, depending on `then`.
chat_page()     a Direct chat: one contenteditable message box, a Send button, Enter sends, the box clears, and
                the message is POSTed back to the site (site.sent), which shows it when the chat is reloaded.
grid_page()     a profile whose grid links to its posts and reels (discover.py's fallback when the API is silent).
inbox_page()    the Direct inbox's chat list: one row per chat with a name and a preview "text · 2h", no links
                (replies.py's fallback when the inbox endpoint is silent).
They are only as faithful as the markup copied into them: when the real page changes, copy the new markup from a
step*_failed_dom.txt (or test_live.py's debug/live/ files) into here.
"""
from __future__ import annotations

import html
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class MockSite:
    def __init__(self):
        self.pages, self.redirects, self.sent = {}, {}, {}
        site = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                path = self.path.split("?")[0]
                if path in site.redirects:
                    self.send_response(302)
                    self.send_header("Location", site.url(site.redirects[path]))
                    self.end_headers()
                    return
                page = site.pages.get(path)
                if page is None:
                    self.send_error(404)
                    return
                body = (page(path) if callable(page) else page).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                text = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
                site.sent.setdefault(self.path.split("?")[0], []).append(text)
                self.send_response(204)
                self.end_headers()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def url(self, path):
        return self.base + path

    def reset(self):
        self.pages.clear()
        self.redirects.clear()
        self.sent.clear()

    def close(self):
        self.server.shutdown()


def headless_chrome():
    """Plain Selenium Chrome (no saved profile, so nothing is logged in to anything), headless unless SHOW=1, which
    opens a visible window so you can watch each test. Skips the tests if Chrome can't start here."""
    try:
        from selenium import webdriver
        opts = webdriver.ChromeOptions()
        if os.environ.get("SHOW") != "1":
            opts.add_argument("--headless=new")
        opts.add_argument("--window-size=1300,950")
        return webdriver.Chrome(options=opts)
    except Exception as exc:                       # no Chrome / no driver on this machine
        raise unittest.SkipTest(f"headless Chrome didn't start: {exc}")


STYLE = """<style>body{font:14px sans-serif} button,[role=button]{display:inline-block;min-width:40px;
min-height:24px;border:1px solid #999;margin:2px} [role=textbox]{min-height:20px;width:400px;border:1px solid #999}
.cover{position:fixed;inset:0;background:rgba(0,0,0,.5);z-index:9}</style>"""

NAV = """<div role="navigation"><a href="/">Home</a> <a href="/explore/">Explore</a> <a href="/direct/inbox/">Messages</a>
<a href="/me.test/">Profile</a></div>"""

SUGGESTED = """<div id="suggested"><span>Suggested for you</span>
<div><a href="/sam.p/">sam.p</a><button type="button"><div>Follow</div></button></div>
<div><a href="/alex.k/">alex.k</a><div role="button" tabindex="0">Follow</div></div></div>"""

BLOCKS = {
    "block": "Action Blocked. This action was blocked. Please try again later. We restrict certain activity to "
             "protect our community. Tell us if you think we made a mistake.",
    "limit": "Try Again Later. We limit how often you can do certain things on Instagram to protect our community. "
             "Tell us if you think we made a mistake.",
}


def profile_page(user, *, button="Follow", then="following", suggested=False, notifications=False):
    """then: what pressing the header's Follow does: 'following', 'requested' (a private account), 'block' or
    'limit' (Instagram's two block popups), or 'nothing'."""
    after = {"following": "Following", "requested": "Requested"}.get(then, "")
    popup = html.escape(BLOCKS.get(then, ""))
    dialog = ('<div class="cover" role="dialog"><h3>Turn on Notifications</h3><p>Know right away when people '
              'follow you or like and comment on your photos.</p><button>Turn On</button><button '
              'onclick="this.parentNode.remove()">Not Now</button></div>') if notifications else ""
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>@{user} • Instagram</title>{STYLE}</head><body>
{NAV}<main role="main"><div>
<header><section>
  <div><h2>{html.escape(user)}</h2>
    <div><button type="button" id="follow"><div><div>{button}</div></div></button>
      <div role="button" tabindex="0">Message</div>
      <div role="button" tabindex="0" aria-label="Options"><svg aria-label="Options" width="20" height="20"></svg></div>
    </div></div>
  <ul><li>12 posts</li><li>340 followers</li><li>210 following</li></ul>
  <span>{html.escape(user.replace('.', ' ').title())}</span>
</section></header>
<div id="below">{SUGGESTED if suggested else ""}</div>
<article><div>posts</div></article>
</div></main>{dialog}
<script>
const btn = document.getElementById('follow');
btn.addEventListener('click', () => setTimeout(() => {{
  if ({'true' if after else 'false'}) {{
    btn.querySelector('div div').textContent = {after!r};
    document.getElementById('below').innerHTML = {SUGGESTED!r};
  }} else if ({'true' if popup else 'false'}) {{
    const d = document.createElement('div');
    d.setAttribute('role', 'dialog');
    d.innerHTML = '<div>{popup}</div><button>Tell us</button><button>OK</button>';
    document.body.appendChild(d);
  }}
}}, 300));
</script></body></html>"""


CHAT = """<!doctype html><html><head><meta charset="utf-8"><title>Instagram • Chats</title>{style}</head><body>
{nav}<main role="main">
  <div><a href="/{user}/">{user}</a><span>{name}</span><span>Active now</span></div>
  <div id="thread">{thread}</div>
  <div>{notice}</div>
  <div role="textbox" contenteditable="true" aria-label="Message" aria-describedby="Message">{draft}</div>
  <div role="button" tabindex="0">Send</div>
</main>
{dialog}
<script>
const box = document.querySelector('[role=textbox]');
async function send() {{
  const t = box.innerText.trim();
  if (!t) return;
  const p = document.createElement('div');
  p.textContent = t;
  thread.appendChild(p);
  box.innerText = '';
  await fetch(location.pathname, {{method: 'POST', body: t}});
}}
[...document.querySelectorAll('main [role=button]')].find(b => b.textContent === 'Send').addEventListener('click', send);
box.addEventListener('keydown', e => {{ if (e.key === 'Enter' && !e.shiftKey) {{ e.preventDefault(); send(); }} }});
</script></body></html>"""


def chat_page(site, user, *, name="", notice="", draft="", dialog=""):
    """A Direct chat with @user (a function of the path, so it shows what site.sent holds for it)."""
    def render(path):
        thread = "".join(f"<div>{html.escape(m)}</div>" for m in site.sent.get(path, []))
        return CHAT.format(style=STYLE, nav=NAV, user=html.escape(user), name=html.escape(name), thread=thread,
                           notice=html.escape(notice), draft=html.escape(draft),
                           dialog=f'<div role="dialog"><div>{html.escape(dialog)}</div><button>OK</button></div>'
                                  if dialog else "")
    return render


def grid_page(user, links):
    """@user's profile with a grid of links (paths like /<user>/p/<code>/ or /reel/<code>/), each shown twice the
    way the grid's image and its hover overlay both link, plus a post link in the nav outside the main area."""
    cells = "".join(f'<div><a href="{html.escape(l)}" role="link"><img alt="Photo by {html.escape(user)}"></a>'
                    f'<a href="{html.escape(l)}"><span>12</span></a></div>' for l in links)
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>@{user} • Instagram</title>{STYLE}</head><body>
{NAV}<div role="navigation"><a href="/p/NAVPOST/">a post outside the profile</a></div><main role="main">
<header><section><h2>{html.escape(user)}</h2><button>Edit profile</button></section></header>
<div role="tablist"><a href="/{user}/">Posts</a><a href="/{user}/reels/">Reels</a><a href="/{user}/tagged/">Tagged</a></div>
<article>{cells}</article></main></body></html>"""


def inbox_page(chats):
    """The inbox list. chats: [(name, preview, age)], e.g. ("Jane Doe", "You: hey jane!", "2h")."""
    rows = "".join(f"""<div role="button" tabindex="0"><img alt="{html.escape(n)}'s profile picture" width="20">
  <div><div><span>{html.escape(n)}</span></div><div><span>{html.escape(p)}</span><span> · </span><abbr>{a}</abbr></div></div>
</div>""" for n, p, a in chats)
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>Inbox • Direct</title>{STYLE}</head><body>
{NAV}<main role="main"><div><h1>me.test</h1><div role="button" tabindex="0" aria-label="New message">New</div></div>
<div><span>Messages</span><span>Requests</span></div>
<div aria-label="Thread list" role="list" style="height:300px;overflow:auto">{rows}</div></main></body></html>"""
