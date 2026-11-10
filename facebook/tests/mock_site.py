"""mock_site.py - a local stand-in for the Messenger pages send.py works on, and a headless Chrome to open it in.

MockSite serves pages on 127.0.0.1 from site.pages ({path: html, or a function of the path returning html}).
A chat page made by chat_page() behaves like Messenger where messenger.py depends on it: one contenteditable
message box, a "Press enter to send" button, Enter sends, the box clears, and the message is POSTed back to the
site, which keeps it (site.sent) and shows it when the chat is reloaded. It is only as faithful as the markup
copied into it: when the real page changes, copy the new markup from a step*_failed_dom.txt into here.
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


PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>Messenger</title>
<style>body{{font:14px sans-serif}} [role=textbox]{{min-height:20px;width:400px;border:1px solid #999}}
[role=button]{{display:inline-block;width:40px;height:30px;border:1px solid #999}}</style></head><body>
<div role="navigation"><a href="/messages/">Chats</a> Marketplace</div>
<div role="main">
  <div><h2>{name}</h2><span>Active 2h ago</span></div>
  <div id="thread">{thread}</div>
  <div>{notice}</div>
  <div role="textbox" contenteditable="true" aria-label="Message" aria-placeholder="Aa">{draft}</div>
  <div role="button" aria-label="Press enter to send" tabindex="0">&#10148;</div>
</div>
{dialog}
<script>
const MODE = "{mode}";
const box = document.querySelector('[role=textbox]');
async function send() {{
  const t = box.innerText.trim();
  if (!t) return;
  const p = document.createElement('div');
  p.textContent = t;
  thread.appendChild(p);
  box.innerText = '';
  if (MODE === 'fail') {{
    const a = document.createElement('div');
    a.setAttribute('role', 'alert');
    a.textContent = 'Message not sent. Click to try again.';
    document.querySelector('[role=main]').appendChild(a);
    return;
  }}
  await fetch(location.pathname, {{method: 'POST', body: t}});
}}
document.querySelector('[aria-label="Press enter to send"]').addEventListener('click', send);
box.addEventListener('keydown', e => {{ if (e.key === 'Enter' && !e.shiftKey) {{ e.preventDefault(); send(); }} }});
</script></body></html>"""


def chat_page(site, name, *, notice="", draft="", dialog="", mode="ok"):
    """A Messenger chat with `name` (a function of the path, so it shows what site.sent holds for it).
    dialog: text for a role=dialog over the chat (the PIN prompt, a block); mode='fail': Send shows
    "Message not sent" and nothing reaches the site."""
    def render(path):
        thread = "".join(f"<div>{html.escape(m)}</div>" for m in site.sent.get(path, []))
        return PAGE.format(name=html.escape(name), thread=thread, notice=html.escape(notice),
                           draft=html.escape(draft), mode=mode,
                           dialog=f'<div role="dialog" aria-modal="true"><div>{html.escape(dialog)}</div>'
                                  f'<div role="button">Continue</div></div>' if dialog else "")
    return render
