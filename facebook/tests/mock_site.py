"""mock_site.py - a local stand-in for the Messenger pages send.py works on, and a headless Chrome to open it in.

MockSite serves pages on 127.0.0.1 from site.pages ({path: html, or a function of the path returning html}).
A chat page made by chat_page() behaves like Messenger where messenger.py depends on it: one contenteditable
message box, a "Press enter to send" button, Enter sends, the box clears, and the message is POSTed back to the
site, which keeps it (site.sent) and shows it when the chat is reloaded. Each message is a [role=row] with a
screen-reader heading ("You sent" / their name) and its bubble on our side (right) or theirs (left), which is
what messenger.thread reads. A post page made by post_page() is a Facebook post for comment.py: the post's
[role=article], its comments as nested articles (each with a reply box), the post's own "Write a comment" box,
other posts on the page with boxes of their own, and Enter posting the comment back to the site. It is only as
faithful as the markup copied into it: when the real page changes, copy the new markup from a
step*_failed_dom.txt / comment*_failed_dom.txt into here.
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
[role=button]{{display:inline-block;width:40px;height:30px;border:1px solid #999}}
#thread{{width:600px}} [role=row]{{display:flex;margin:4px 0}} [role=row].me{{justify-content:flex-end}}
[role=row].them{{justify-content:flex-start}} [role=row].mid{{justify-content:center}}
.bubble{{max-width:300px;padding:6px;border:1px solid #ccc}}
.sr{{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0)}}</style></head><body>
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


def row(name, who, text, labels=True):
    """One message row: who is 'me', 'them' or 'mid' (a centred date or notice, with no heading)."""
    head = {"me": "You sent", "them": name}.get(who, "") if labels else ""
    return (f'<div role="row" class="{who}">' + (f'<h5 class="sr">{html.escape(head)}</h5>' if head else "") +
            f'<div class="bubble"><div dir="auto">{html.escape(text)}</div></div></div>')


def chat_page(site, name, *, notice="", draft="", dialog="", mode="ok", before=(), after=(), labels=True):
    """A Messenger chat with `name` (a function of the path, so it shows what site.sent holds for it): the rows in
    before ((who, text) pairs), then our sent messages, then the rows in after (their replies). labels=False drops
    the "You sent" / name headings, so only the side a bubble is on tells who wrote it.
    dialog: text for a role=dialog over the chat (the PIN prompt, a block); mode='fail': Send shows
    "Message not sent" and nothing reaches the site."""
    def render(path):
        msgs = [*before, *(("me", m) for m in site.sent.get(path, [])), *after]
        thread = "".join(row(name, who, text, labels) for who, text in msgs)
        return PAGE.format(name=html.escape(name), thread=thread, notice=html.escape(notice),
                           draft=html.escape(draft), mode=mode,
                           dialog=f'<div role="dialog" aria-modal="true"><div>{html.escape(dialog)}</div>'
                                  f'<div role="button">Continue</div></div>' if dialog else "")
    return render


POST = """<!doctype html><html><head><meta charset="utf-8"><title>Facebook</title>
<style>body{{font:14px sans-serif}} [contenteditable]{{min-height:20px;width:400px;border:1px solid #999}}
[role=button]{{display:inline-block;margin:2px;border:1px solid #999}}</style></head><body>
<div role="banner"><a href="/">Facebook</a> <a href="/messages/">Messenger</a></div>
<div role="main">
{before}
<div class="wrap">
<div role="article" aria-label="Post by {author}">
  <h2><a href="/jane.doe">{author}</a></h2>
  <div data-ad-rendering-role="story_message"><div dir="auto">{text}</div></div>
  <div><span role="button">Like</span><span role="button" aria-label="Leave a comment">Comment</span><span role="button">Share</span></div>
  <div class="comments">{comments}</div>
  <div>{notice}</div>
  {inside}
</div>
{outside}
</div>
{after}
</div>
{dialog}
<script>
const MODE = "{mode}";
function say(role, t) {{
  const a = document.createElement('div');
  a.setAttribute('role', role);
  a.textContent = t;
  document.body.appendChild(a);
}}
async function post(box, where) {{
  const t = box.innerText.trim();
  if (!t) return;
  if (MODE === 'fail' && where === '') return say('alert', "Couldn't post your comment. Try again.");
  if (MODE === 'block' && where === '') return say('dialog', "You're Temporarily Blocked. We limit how often you can post, comment or do other things.");
  box.innerText = '';
  if (where === '') {{
    const c = document.createElement('div');
    c.setAttribute('role', 'article');
    c.setAttribute('aria-label', 'Comment by Sam Sender');
    c.innerHTML = '<a href="/sam">Sam Sender</a><div dir="auto"></div>';
    c.lastChild.textContent = t;
    document.querySelector('.comments').appendChild(c);
  }}
  await fetch(location.pathname + where, {{method: 'POST', body: t}});
}}
for (const box of document.querySelectorAll('[contenteditable=true]'))
  box.addEventListener('keydown', e => {{ if (e.key === 'Enter' && !e.shiftKey) {{ e.preventDefault(); post(box, box.dataset.to || ''); }} }});
</script></body></html>"""


def comment_html(who, text, reply_label="Reply to {who}\u2026"):
    """Someone's comment, a nested article with its own reply box (open, as after clicking Reply)."""
    label = html.escape(reply_label.format(who=who))
    return (f'<div role="article" aria-label="Comment by {html.escape(who)}"><a href="/x">{html.escape(who)}</a>'
            f'<div dir="auto">{html.escape(text)}</div><span role="button">Reply</span>'
            f'<div contenteditable="true" role="textbox" aria-label="{label}" data-to="/reply"></div></div>')


def other_post(n, author, text):
    return (f'<div class="wrap"><div role="article" aria-label="Post by {html.escape(author)}"><h2><a href="/o{n}">'
            f'{html.escape(author)}</a></h2><div dir="auto">{html.escape(text)}</div></div>'
            f'<div contenteditable="true" role="textbox" aria-label="Write a comment\u2026" data-to="/other{n}"></div></div>')


def post_page(site, author, text, *, comments=(), box="inside", notice="", dialog="", mode="ok", others=(),
              reply_label="Reply to {who}\u2026"):
    """A Facebook post by `author` (a function of the path: comments posted to it so far, from site.sent, show as
    Sam Sender's). comments: (who, text) of other people's comments, each with an open reply box labelled
    reply_label (POSTs to <path>/reply). box: where the post's own "Write a comment" box is, 'inside' the article,
    'outside' (right after it, in the same wrapper) or 'none'. others: (author, text) of other posts on the page,
    the first one before this post and the rest after, each with its own comment box (POSTs to <path>/other<n>).
    mode='fail': Enter shows "Couldn't post your comment"; 'block': Enter shows a "Temporarily Blocked" dialog.
    Neither reaches the site."""
    def render(path):
        own = '<div contenteditable="true" role="textbox" aria-label="Write a comment\u2026"></div>'
        cs = [comment_html(w, t, reply_label) for w, t in comments]
        cs += [comment_html("Sam Sender", m, reply_label) for m in site.sent.get(path, [])]
        rest = [other_post(n, a, t) for n, (a, t) in enumerate(others, 1)]
        return POST.format(author=html.escape(author), text=html.escape(text), comments="".join(cs),
                           notice=html.escape(notice), mode=mode, inside=own if box == "inside" else "",
                           outside=own if box == "outside" else "", before="".join(rest[:1]), after="".join(rest[1:]),
                           dialog=f'<div role="dialog"><div>{html.escape(dialog)}</div><div role="button">OK</div></div>'
                                  if dialog else "")
    return render
