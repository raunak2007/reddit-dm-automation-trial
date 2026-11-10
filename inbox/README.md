# inbox: answering people who replied

The reply watchers (`reddit/`, `facebook/`, `instagram/` `run.py replies`) list who wrote back in each platform's
`data/replies.csv`. This folder drafts an answer to each with Claude and gives you one page to edit and approve
them. It never sends anything: each platform's `run.py answer` (and the daily run) sends what you approved.

    python inbox/run.py                    draft new answers, then open the page
    python inbox/run.py draft [--limit N] [--dry-run]
    python inbox/run.py serve [--port 8767] [--no-open]
    python inbox/run.py stats

Put `ANTHROPIC_API_KEY=...` in `inbox/.env` (the repo root's, `facebook/`'s, `instagram/`'s or `reddit/`'s `.env`
also work). Install with `pip install -r inbox/requirements.txt` (already in the platform requirements).

## Drafting (`draft.py`)

- Every reply with no entry in `inbox/data/answers.json` is drafted once. Several replies from one person are one
  entry, keyed on the newest reply's id; all their ids go in the entry's `covers`, so none is drafted again. If
  that person already has an untouched draft, it's folded into the new one and dismissed as replaced (an edited or
  approved one is left alone).
- The full message (`text` column) is used when the replies.csv has one, else `snippet`.
- Our original DM comes from the platform's `leads.json` (approved message, else edited, else drafted), found
  through its `contacted.csv` (Reddit: the root `contacted.csv`).
- A clear "no thanks" or "stop messaging me" is caught without asking Claude; Claude can flag one too. Those are
  shown as **suggest: dismiss** with no pitch. They stay `drafted` until you choose, so `store.answered()` doesn't
  count them.
- The instructions are in `prompts/answer.md`, filled from `config.json`'s `business` block. Edit either to change
  the tone or the facts Claude may use. It must never invent prices, results or credentials.

## Auto-approve (off by default)

`auto_approve.enabled: true` lets `draft` approve one narrow kind of answer without you: they only want to book
("yes, I'd like the call", "how do I book?", "send me the link"), and the answer just gives the booking link. The
platform's `run.py answer` sends it like any other approved entry. It is marked `approved_by: "auto"` (the page
sets `"you"`), shows an **auto-approved** badge, and can be unapproved until a sender picks it up.

It stays inactive until you've reviewed `min_reviewed` drafts yourself (approved, edited or dismissed on the page;
auto-approvals don't count). `draft` and `stats` say why it's inactive. An entry is auto-approved only if all of
these hold, and otherwise it stays `drafted` with the reason in `auto_skip`:

- Claude's intent is in `intents` (default `booking`), with no decline or stop suggestion.
- The lead says they're not a likely minor. A missing lead, or one whose check didn't run, counts as a minor.
- The answer is non-empty, at most `max_answer_words` words, contains the booking link (UTM-tagged or not) and
  has no question mark outside URLs.
- Their message is at most `max_message_words` words, and any question in it is about booking.
- It's their first answer: nothing else for them is approved, sending, sent or uncertain.
- Fewer than `max_per_day` auto-approvals today (unapproved ones still count).

## The page (`server.py`, http://127.0.0.1:8767)

Tabs: **To review** (drafted, failed), **Approved** (approved, sending), **Sent**, **Dismissed / uncertain**.
Each card shows the platform, the name, what they wrote, our original DM and an editable answer (saved as you type),
with Approve / Dismiss / Re-draft (asks Claude again, with an optional note) / Open chat. Approved ones can be
unapproved, dismissed ones restored, and uncertain ones (a send that may have gone out) settled with
"Mark sent" or "Mark not sent (back to approved)" after you check the chat.

The server enforces this through `store.update(expect=...)`: text and status change only in `store.EDITABLE`
statuses, and nothing touches an entry that is sending or sent. It listens on 127.0.0.1 only and refuses
cross-origin requests and other Host names.

## config.json

    business            name, website, booking_link, sender_first_name, services, offer (copied from facebook/)
    ai.model            the Claude model
    ai.max_drafts_per_run   cap per draft run (--limit overrides)
    ai.max_tokens       per answer
    server.port         the page's port (8767)
    auto_approve.enabled            false: off unless you turn it on
    auto_approve.intents            ["booking"]: Claude intents that may be auto-approved
    auto_approve.min_reviewed       30: drafts you must review yourself first
    auto_approve.max_per_day        5
    auto_approve.max_answer_words   60
    auto_approve.max_message_words  40

## Tests

    venv/bin/python -m unittest discover inbox/tests

Offline: Claude is mocked and every data path points at a temp dir.
