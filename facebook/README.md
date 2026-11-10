# Facebook Lead Finder

Finds recent Facebook posts from students and parents who need help with college admissions,
essays, scholarships, financial aid or internships. Claude scores each post and drafts a
personalized message pointing them to your booking link. Then `send.py` sends them the way
`run_loop.py` sends the Reddit queue on `master`: no approving, and it presses Send itself.

```
scrape.py ──► data/leads.json ──► qualify.py (Claude) ──► send.py ──► Messenger
 (your logged-in            (score, flags,            (approves new     data/contacted.csv
  Facebook session)          DM + comment drafts)      leads, sends)     (who's been contacted)

queue_server.py (optional): look at leads, edit drafts or skip people before a send
comment.py (outreach.auto_comment): posts the public comment on leads that won't get a DM
replies.py: looks in each DMed chat for a reply ──► data/replies.csv ──► ../inbox (drafts answers you approve)
answer.py: sends the answers you approved on the inbox page into each chat
followup.py (outreach.followup.enabled): one short follow-up to people who never replied
livecheck.py: read-only checks on the real site, for a weekly scheduler
```

**Every new lead at or above `ai.min_score` is sent its drafted DM** (`outreach.auto_approve`, on
by default). Set `auto_approve` to `false` to send only what you approve in the queue, or use
`send --by-hand` to press Enter in each chat yourself. Facebook restricts accounts that send
automated messages to people they aren't friends with, so keep runs small (see Guardrails). Many people posting about
admissions are under 18, and those authors are never DMed by default.

## How this fits with the Reddit scripts

`send.py`, `messenger.py` and `store.py` are the Facebook versions of `run_loop.py` / `open_queue.py`,
`send_dm.py` and `ledger.py` on `master`, with the same rules:

| Reddit (repo root) | Facebook (this folder) |
|---|---|
| queue `.html` you curate | every new lead (`outreach.auto_approve`), or the ones you **Approve for sending** |
| `run_loop.py` (presses Send) / `open_queue.py` (you click Send) | `send.py` / `send.py --by-hand` |
| `sender_username`, checked via `/api/me` | `outreach.sender_user_id`, checked via the `c_user` cookie |
| recorded once Reddit accepts it | recorded once the message box clears (no reload) |
| `contacted.csv`: `submitting`, `uncertain` = held | `data/contacted.csv`: the same statuses |
| `python ledger.py held` / `resolve` | `python send.py held` / `resolve` |

The waits, clicks, typing, failed-step screenshots and `Result` statuses come straight from the root
`send_dm.py`, and Chrome's version from the root `chrome_version.py` (`browser.py` puts the repo
root on the import path). Reddit has a sent folder to import; Messenger doesn't. Outside sends are
caught when `send.py` opens a chat that already has the same message in it, and by your
**Mark sent** clicks.

## Setup

Uses the same Python 3.11 setup as the Reddit scripts on `master`.

```bash
cd facebook
python3.11 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # then put your ANTHROPIC_API_KEY in it
python run.py login         # opens Chrome: log in to Facebook once; the session is saved
python run.py send          # the first time, says which account is logged in: put that id in
                            # config.json's outreach.sender_user_id
```

If Messenger asks for your chat PIN (end-to-end encrypted chats), enter it in that Chrome window once.

To use your everyday Chrome profile instead of a separate one, set `browser.user_data_dir` in
`config.json` (on a Mac: `~/Library/Application Support/Google/Chrome`) and quit Chrome before
running.

## Make it yours

1. **`config.json` → `business`**: your company name, website, booking link, your first name,
   your services, and the offer (for example, a free 20-minute call).
2. **`config.json` → `search`**:
   - `keywords`: the phrases to search for.
   - `groups`: URLs of Facebook groups you've joined (college parents groups, class-of-2027
     groups, scholarship groups…). Groups are usually where the best leads are.
   - `lookback_days`: defaults to 14.
3. **`prompts/qualify.md`**: who counts as a lead, and how to score them.
4. **`prompts/message.md`**: the tone, length and structure of the DM and the public comment.
   This is the file to edit when you want to change how the messages read.

Placeholders like `{booking_link}` and `{services}` are filled in from `config.json`. To check the
exact prompt Claude will see, run `python run.py qualify --dry-run`. After you change a prompt,
run `python run.py qualify --rescore` to redraft every lead.

## Daily use

```bash
python run.py                      # scrape -> score/draft -> send (-> comment, with outreach.auto_comment)
python run.py scrape --limit-pages 1   # quick test on one search page
python run.py queue                # look at leads, edit drafts or skip people (optional)
python run.py send                 # approve new leads and press Send for each
python run.py send --by-hand       # ...or you press Enter in each chat
python run.py stats
python send.py --list              # what's approved and why any would be skipped
python send.py held                # sends that couldn't be confirmed (the next run checks them)
python send.py resolve <author_key> --sent | --not-sent
python run.py comment              post the drafted public comment on leads that won't get a DM
python comment.py --list           who would get a comment, and why the others won't
python comment.py held             comments that couldn't be confirmed
python comment.py resolve <post_key> --commented | --not-commented
python run.py replies              look for replies in every DMed chat -> data/replies.csv
python replies.py --list           who has replied
python run.py answer               send the answers you approved on the inbox page (../inbox)
python answer.py --list            approved answers waiting to go
python run.py followup             one follow-up to people who never replied (off until outreach.followup.enabled)
python followup.py --list          who would get a follow-up, and why the others won't
python run.py livecheck            read-only checks on the real site; exit 0 ok, 2 account problem, 1 other
```

In the queue (http://127.0.0.1:8765):

| Button | What it does |
|---|---|
| **Approve for sending** | Keeps the draft exactly as it is now. `python run.py send` sends it. It moves to the **Approved** tab, where **Unapprove** takes it back until it's sent. |
| **Copy & open Messenger** | Copies your edited draft and opens a chat with the author. Paste it, read it once more, send. |
| **Mark sent** | Records the DM. That person is never offered again, even from other posts. |
| **Copy comment & open post** / **Mark commented** | Use a helpful public reply instead of a DM. |
| **Skip** / **Not relevant** | Hides the post. "Not relevant" also keeps a record of where the AI was wrong. |

Edits to drafts save automatically. Leads are sorted by score. With `outreach.auto_approve` on, you
don't need the queue: each send run approves every lead still in **To review** with its draft (your
edit, if you made one). To keep someone out, **Skip** or **Not relevant** them, or **Unapprove**
them, before the next run.

## Sending

For each approved lead, best score first, `send.py`:

1. Opens their chat and checks it shows their name, that Facebook isn't refusing the account or the
   person, and that the message isn't already there (if it is, it's recorded as sent and not sent again).
2. Types the approved text. Line breaks use Shift+Enter, so nothing goes out early.
3. Writes a `submitting` row to `data/contacted.csv`, then presses Send **once**.
4. Waits for the message box to clear with no error, and records `sent` right away. It doesn't reload
   the chat to look for the message (the way ApplyTwin logs "Applied" right after Submit). `--by-hand` trusts
   your `[s]ent` the same way. Set `outreach.verify_after_send` to `true` to reload and check first, as before.

If something goes wrong after Send is pressed and a quick look can't find the message, the lead is recorded `uncertain` and **held**. A crash between pressing Send and recording the outcome leaves a `submitting` row,
which counts as held too. Nobody held is messaged again until their chat is checked, and the next run
does that itself (`outreach.auto_resolve`, on by default): before sending anything it reloads each held
chat; if the message is there it's recorded `sent`, if the chat loaded and it isn't, it's `cleared` and
sent again in that run, and if the check can't tell (the chat didn't load, or doesn't show them) it
stays held for the run after. `python send.py resolve <author_key> --sent | --not-sent` still settles one by hand.

The run stops (exit code 2) on anything account-wide: logged out, wrong account, checkpoint,
"temporarily blocked", or Messenger asking for the PIN. It also stops after 3 failures before Send in
a row, 3 refusals from different people in a row, or 2 unconfirmed sends in a row. Someone refused or
failed is retried on later runs, up to `outreach.max_attempts_per_recipient` (3). It waits
`outreach.min_delay_seconds` to `max_delay_seconds` (60–180 s) between sends, and only one `send.py`
runs at a time. Don't scrape while it runs: they share the Chrome profile.

## Public comments (comment.py)

Off unless you turn it on: set `outreach.auto_comment` to `true` and `python run.py` runs `comment.py` after
sending (`python run.py comment` runs it on its own whenever you ask). The comment is the alternative to a DM, so it
only goes where `send.py` won't send one:

- leads with no profile link to message (the queue shows these with a note to reply from the post), and
- authors the AI thinks are under 18 (`minor_policy` `comment_only`), **only** with `outreach.auto_comment_minors`
  set to `true`. It's `false` by default: a public reply to someone who may be a minor is left to you, with
  **Copy comment & open post** in the queue.

Each lead must be at or above `ai.min_score`, have a drafted comment (your edit, if you made one) and a link to
the post, and still be in **To review**. As with **Mark commented**, a comment counts as contacting that person:
nobody who has been DMed, commented on or held (on any of their posts), or has a DM approved and waiting, gets a
comment, and nobody commented on is DMed afterwards.

For each one, `comment.py` opens the post, finds that post on the page (the article with the author's name and the
start of the post's text) and its own **Write a comment** box (not a reply box inside someone's comment, and not
another post's box), checks the comment isn't already there, types it, writes a `comment_submitting` row, presses
Enter **once**, and records `commented` (the same status **Mark commented** writes) as soon as the box clears. If
something goes wrong after Enter and the comment can't be seen on the post, it's `comment_uncertain` and held: the
next run looks for it and records it `commented` if it's there, but never clears it on its own (Facebook doesn't
always show every comment, so not seeing it isn't proof). `python comment.py resolve <post_key> --not-commented`
makes it eligible again once you've checked. A post that takes no comments (members only, turned off) is
`comment_closed` and not tried again.

It stops (exit code 2) on a logout, checkpoint, "temporarily blocked" or "we limit how often you can comment"
dialog, and after 3 failures, 3 closed posts or 2 unconfirmed comments in a row. `outreach.daily_comment_limit`
(10) counts comments from here and **Mark commented** clicks, separately from the DM cap; it waits
`comment_min_delay_seconds` to `comment_max_delay_seconds` (90–240 s) between comments.

## Replies (replies.py)

`python run.py replies` opens the chat of everyone with a `sent` DM who isn't in `data/replies.csv` yet (most recent
DM first, at most `outreach.replies_per_run`, 30, a run, `reply_min_delay_seconds` to `reply_max_delay_seconds`,
20–60 s, apart) and reads it. It never types anything. It finds our DM in the chat (the text in `leads.json`) and
counts any message **from them after it** as a reply; their messages from before our DM don't count. Who wrote a
message comes from Messenger's screen-reader label on each message row ("You sent" / their name) and, without one,
from which side the bubble is on; rows it can't place never count. Each reply is one row in `data/replies.csv`:
`author_key, author, post_key, seen_at, snippet, chat_url`. If our DM can't be found in the chat, that person is
checked again next run. Opening a chat marks their messages as seen, as it would by hand. It stops (exit code 2)
on anything account-wide (logged out, checkpoint, block, chat PIN), or after 5 chats in a row it couldn't read.

`text` holds everything they wrote after our DM (their messages joined with " / ", up to 4000 characters); `snippet`
is the first 160. A `replies.csv` from before the `text` column still reads (its rows get `text` = `snippet`) and is
rewritten with the column the next time a reply is added.

Everyone in `replies.csv` is checked again on later runs, before and after you answer them (not when their latest inbox
entry is dismissed, or an answer is sending / uncertain), and gets **one row per new message** from them after our
latest message in the chat (the DM, a follow-up, an answer or anything sent by hand). Our own messages never count, a
message already recorded isn't recorded again, and each row's `seen_at` is unique for that person, since the inbox
keys Facebook replies on `author_key:seen_at`. A new row while a draft is waiting makes the next `inbox/run.py draft`
redraft with the whole conversation (it replaces an untouched draft). Within `replies_per_run`: people with no answer
sent yet (draft waiting, or none) first, then people we've answered, then DMed chats never checked.

## Answers (answer.py)

The inbox page (`../inbox`, shared with the Reddit and Instagram folders) drafts answers to replies and you approve
them there. `python run.py answer` sends the approved Facebook ones, oldest approval first, at most
`answers.max_per_run` (10) a run, `answers.min_delay_seconds` to `max_delay_seconds` apart:

1. Takes the same lock as `send.py`, then turns any entry a dead run left `sending` into `uncertain` (never resent).
2. Checks the browser is logged in as `outreach.sender_user_id` and Messenger is usable, as `send.py` does.
3. For each entry: re-reads it (you may have changed it on the page), opens the chat (the entry's `chat_url`, else
   the person's Messenger link) and checks it shows their name; if the text is already in the chat it's `sent`.
   Otherwise it types the text, marks the entry `sending`, presses Send once, and records `sent`, `failed` (not sent:
   approve it again to retry) or `uncertain` (Send was pressed but it couldn't be confirmed: look in the chat), with
   the reason. If the text was edited on the page while it was being typed, nothing is sent and it stays approved.

An account-wide problem before Send (logged out, PIN, checkpoint, block) leaves the entry `approved` for the next run
and stops with exit code 2; so do 3 failures or 2 unconfirmed sends in a row. Answers don't count toward the daily DM
cap: they go to people who wrote to you.

## Follow-ups (followup.py)

Off unless you set `outreach.followup.enabled` to `true`. `python run.py followup` sends **one** short follow-up, ever,
to each person whose DM went out at least `after_days` (6) days ago and who:

- isn't in `data/replies.csv` and hasn't been answered from the inbox,
- hasn't had a follow-up (any `followup_submitting`, `followup_sent`, `followup_uncertain` or `followup_refused` row),
- isn't someone the AI thinks is under 18 (whatever `minor_policy` says), and whose DM text is in `leads.json`.

Oldest DM first, at most `max_per_run` (5) a run. Right before each one it opens the chat and reads it: if they wrote
anything after our DM it's recorded in `data/replies.csv` and no follow-up goes; if we wrote something after it (an
answer by hand) it's skipped; if our DM can't be found in the chat, it's tried next run. Claude drafts it from
`prompts/followup.md` (short, referring back to the first DM; with `qualify.py`'s model and API key); with
`use_ai` false, or when Claude can't be reached or the draft is empty or over `max_chars` (500), `template` from
`config.json` is used. It's sent like a DM (`followup_submitting` row, Send pressed once), and recorded:

| status | meaning |
|---|---|
| `followup_sent` | Messenger took it (or it was already in the chat) |
| `followup_uncertain` | Send was pressed but it couldn't be confirmed; never sent again |
| `followup_refused` | Facebook won't let this account message them now; not retried |
| `followup_not_submitted` | failed before Send; retried on a later run |
| `followup_stopped` | account-wide problem before Send; the run stopped there, retried later |

A follow-up counts toward `outreach.daily_dm_limit` (from `send.py` too). Follow-up rows never change where a lead
stands: it stays **DM sent** in the queue, which shows the follow-up's status under it. It stops with exit code 2 on
anything account-wide and on the same streaks as `send.py`.

## Live check (livecheck.py)

`python run.py livecheck` runs `tests/test_live.py` against the real site with `FB_LIVE=1`, taking the optional
checks from `config.json`'s `livecheck` block (`chat` + `name`: open one chat you've messaged before and read who
wrote each message; `post` + `post_author` + `post_text`: open one post and find its own comment box) instead of
environment variables. It only opens and reads pages: nothing is typed, pressed, sent or recorded. Exit code 0 when
every check passed, 2 when a failure shows an account problem (logged out, wrong account, chat PIN, checkpoint,
block), 1 otherwise (markup changed, nothing checked). Meant for a weekly scheduler; it takes the same lock as
`send.py`, so it won't start while a run is using the Chrome profile.

## Tests

```bash
cd facebook
../venv/bin/python -m unittest discover tests          # ~2 min, nothing touches facebook.com
FB_LIVE=1 ../venv/bin/python -m unittest tests/test_live.py -v    # read-only checks on the real site
```

- `tests/test_stops.py`: what a run does on a logout, PIN prompt, checkpoint or block (stops before
  typing, records it, exit 2), and what the next run does after (picks up the same lead, never presses
  Send twice for a message already in the chat, settles held sends).
- `tests/test_comment.py`: which leads get a comment (minors skipped by default, never anyone already DMed or
  commented on), the daily comment cap, a block halting the run (exit 2), held comments, `run.py`'s exit codes.
- `tests/test_replies.py`: a reply after our DM counts, only our DM or their earlier message doesn't, dedupe, a block,
  the full `text` column and reading an old `replies.csv` without it.
- `tests/test_answer.py`: approved inbox answers are sent with `sending` written before the press, already-in-the-chat
  is `sent`, a chat with someone else fails before typing, blocks before / after Send, stuck entries, the cap, the lock.
- `tests/test_followup.py`: who gets a follow-up (never twice, never after a reply, an inbox answer or to a likely
  minor), the chat re-read right before it, the daily DM cap, stops, and that follow-ups leave send.py's statuses alone.
- `tests/test_livecheck.py`: livecheck's settings and exit codes, and that `test_live.py` never types, presses or records.
- `tests/test_mock_site.py`: `messenger.py`'s and `comment.py`'s real page code in headless Chrome against mock
  Messenger chats and posts (the post's own comment box, not a reply box or another post's; Enter once), and
  reading who wrote each message in a chat.
- `tests/test_live.py`: off unless `FB_LIVE=1`. Checks the real profile is logged in as the sender and the
  inbox has no PIN prompt or block. Add `FB_LIVE_CHAT=<author_key> FB_LIVE_NAME="Their Name"` to also open
  one chat (or use `python run.py livecheck` with the `livecheck` block in `config.json`). It never types anything. Run it before the first real `send.py`, and after Facebook changes
  something. Failures save their screenshots and page dumps to `debug/live/`.

## Guardrails

- **Daily DM cap** (`outreach.daily_dm_limit`, default 20), counted across every run from
  `data/contacted.csv`: `send.py` sends, **Mark sent** clicks, follow-ups, and held sends from today. Both the queue
  server and `send.py` enforce it. New or low-activity accounts should start around 10.
- **One DM per person, ever.** That includes held sends: their other posts aren't offered. Plus at most one
  follow-up (off by default), and only to people who never replied.
- **Daily comment cap** (`outreach.daily_comment_limit`, default 10), separate from the DM cap. One comment per
  person, and only to people who won't get a DM.
- **Minors** (`outreach.minor_policy`): if the AI thinks the author is under 18, the default
  `comment_only` turns off the DM and offers only the public comment. Set it to `skip` to hide
  those leads. A parent posting about their child is not flagged.
- **The scraper stops** at any checkpoint, "temporarily blocked" notice or logout, and keeps what
  it already found. If that happens, wait a day before running it again.
- It moves at human speed: random 4–9 second waits between pages and short scroll pauses.

## Notes

- Facebook changes its page markup often. If a run finds 0 posts, look at the open window and
  update the selectors in `_EXTRACT` in `scrape.py`.
- Some authors' profile links can't be read (some search layouts hide them). Those leads still
  show up, with a note to message the person from the post.
- Timestamps that Facebook scrambles are kept as "date unknown" when `keep_undated_posts` is
  `true`. Set it to `false` to drop them.
- Facebook's terms don't allow automated collection, so keep the number of pages read per day
  modest. The scraper uses your account, and the risk falls on your account.
