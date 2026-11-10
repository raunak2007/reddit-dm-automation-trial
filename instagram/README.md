# Instagram Commenter Outreach

It finds your recent Instagram posts and reels on its own, reads everyone who commented, has Claude
pick out the ones asking about college admissions, essays, scholarships or internships, and writes
each of them a short, casual DM that starts with their name. Then `send.py` sends them the way
`run_loop.py` sends the Reddit queue on `master`: no approving, and it presses Send itself. Later,
`python run.py replies` checks who wrote back.

```bash
python run.py                                          # the daily run: find recent posts, read comments, draft DMs, send
python run.py replies                                  # who replied to their DM -> data/replies.csv
python run.py stats                                    # posts, leads, sent, held, replied
python run.py https://www.instagram.com/p/XXXXXXXX/    # the same run for one post you name
python run.py send                                     # send any drafted DMs that haven't gone out yet
```

**Every new lead at or above `ai.min_score` is sent its drafted DM** (`outreach.auto_approve`, on by
default), and **each person is followed right before their DM** (`outreach.auto_follow`). Set `auto_approve` to `false` to send only what you approve
on the review page, or use `send --by-hand` to press Enter in each chat yourself. Instagram restricts
accounts quickly for automated DMs, so keep runs small (see Limits). Many commenters on admissions posts are teenagers, and those are
never DMed by default.

## How this fits with the Reddit scripts

`send.py`, `direct.py` and `store.py` are the Instagram versions of `run_loop.py` / `open_queue.py`,
`send_dm.py` and `ledger.py` on `master`, with the same rules:

| Reddit (repo root) | Instagram (this folder) |
|---|---|
| queue `.html` you curate | every new lead (`outreach.auto_approve`), or the ones you **Approve for sending** |
| `run_loop.py` (presses Send) / `open_queue.py` (you click Send) | `send.py` / `send.py --by-hand` |
| `sender_username`, checked via `/api/me` | `outreach.sender_user_id`, checked via the `ds_user_id` cookie |
| recorded once Reddit accepts it | recorded once the message box clears (no reload) |
| `contacted.csv`: `submitting`, `uncertain` = held | `data/contacted.csv`: the same statuses |
| `python ledger.py held` / `resolve` | `python send.py held` / `resolve` |

The waits, clicks, typing, failed-step screenshots and `Result` statuses come straight from the root
`send_dm.py`, and Chrome's version from the root `chrome_version.py` (`browser.py` puts the repo
root on the import path). Instagram has no sent folder to import. Outside sends are caught when
`send.py` opens a chat that already has the same message in it, and by your **Mark sent** clicks.

## Setup (once)

```bash
cd instagram
python3.11 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # add your ANTHROPIC_API_KEY
python run.py login           # log in to Instagram in the Chrome window that opens
python run.py send            # the first time, says which account is logged in: put that id in
                              # config.json's outreach.sender_user_id
```

In `config.json`, set `instagram.my_username` to your handle so you're never in your own list, and
fill in `business.website` and `business.booking_link`.

## Daily use

```bash
python run.py          # (or python run.py auto) once or twice a day
python run.py replies  # whenever you like; it only reads
python run.py stats
```

`python run.py` with no link:

1. **Finds the posts** (`discover.py`): your recent posts and reels (`instagram.my_username`), plus
   those of any accounts in `discover.accounts` and any links in `discover.posts`. It asks Instagram's
   own web API inside the logged-in Chrome profile (`users/web_profile_info`, then `feed/user/<id>`,
   the way `comments.py` reads comments); if that doesn't answer, it reads the profile page's grid.
2. **Reads each one's comments** if it was posted within `discover.lookback_days`, newest first, up to
   `discover.max_posts` per account. Every run reads them again, so new commenters are picked up; with
   `discover.skip_unchanged` a post whose comment count hasn't changed since its last read is skipped.
   A post whose date isn't known (a `discover.posts` link, or one found only on the profile grid) is
   dated from when it was first seen.
3. Scores the new commenters and drafts their DMs (`qualify.py`), then sends (`send.py`), exactly as
   with a link.

`data/posts.json` remembers each post found: when it was posted and first seen, its comment count,
when it was last read and how often. `python run.py posts` lists it (`*` = still within the lookback).

**Exit codes**, for a scheduler: `0` done, `2` stopped on an account-wide problem (logged out, a
security check / challenge, an action block or rate limit, a locked account, or a streak of failed
sends), `1` anything else (a bad link or setting, another run already using the profile). Reading
posts or replies stops at once on the same signals as sending, and nothing is sent after a stop
while reading. Only one run uses the Chrome profile at a time.

### Replies

`python run.py replies` reads your Direct inbox through the web API (`direct_v2/inbox/`, page by
page). It never opens a chat, so nothing is marked seen, liked or sent. For everyone
`data/contacted.csv` records as `sent`, it finds their one-to-one chat and records each message
from them dated after our DM: username, when, a short snippet and the chat link, in
`data/replies.csv`, once per message (Instagram's item id), then prints the new ones and how many
people have replied. `contacted.csv` isn't changed. Paging stops once every DMed person's chat has
been seen, the chats get older than the first DM, or after `replies.max_pages` pages. If the
endpoint doesn't answer, it reads the chat list on the inbox page instead (still without opening a
chat): that gives only each chat's latest message, with an approximate time. Chats sitting in
Message Requests (or a creator account's General tab) may not show up.

### Settings

| Key | Default | What it does |
|---|---|---|
| `discover.lookback_days` | 7 | Read a post while it is this many days old or newer. |
| `discover.max_posts` | 6 | At most this many recent posts per account per run. |
| `discover.accounts` | `[]` | Other usernames whose recent posts to read too. |
| `discover.posts` | `[]` | Post or reel links to watch (dated from when first seen; no `max_posts` cap). |
| `discover.skip_unchanged` | `true` | Skip a post whose comment count hasn't changed since it was last read. |
| `replies.max_pages` | 10 | Inbox pages (20 chats each) to read at most. |
| `replies.messages_per_chat` | 20 | Latest messages of each chat the inbox returns. |

## Write your message

`message_template` in `config.json` is your message:

```
hey {first_name}! saw your comment about {topic} - we're Aristo Scholars, we help students with
college apps, essays and scholarships. happy to hop on a free call and give you some pointers if
that'd help: {booking_link}
```

- `{first_name}`: their first name, taken from their display name or an obvious username. When
  there's no clear name, the message starts with "hey!".
- `{topic}`: what *their* comment was actually about ("the essay stuff", "finding scholarships"),
  so "saw your comment about…" is always true.
- `{booking_link}`, `{website}` and `{business_name}` are filled in from `config.json`.

The style rules (casual, lowercase-friendly, short, no corporate wording) are in
`prompts/message.md`. Edit them freely. `python run.py preview` prints exactly what Claude gets, and
`python run.py redraft` rewrites every unsent DM after you change something.

With `"ai": {"enabled": false}`, every commenter gets the template as is, with `{topic}` = "college
stuff". There's no filtering then, so "congrats!" and emoji-only comments show up too, and nothing
checks for minors.

## The review page

It opens at http://127.0.0.1:8766. Each commenter shows their comment, a score and the draft DM,
which you can edit.

1. **Open profile (follow)**: opens their profile (optional: `send.py` follows them itself before the DM).
2. **Approve for sending**: keeps the DM exactly as it is now; `python run.py send` sends it. It moves
   to the **Approved** tab, where **Unapprove** takes it back until it's sent.

With `outreach.auto_approve` on, you don't need this page: each send run approves every lead still in
**To send** with its draft (your edit, if you made one). To keep someone out, **Skip** or **Not a lead**
them, or **Unapprove** them, before the next run.

Or send it yourself: **Copy DM & open chat** copies the DM and opens a chat; paste it, send it, then
**Mark sent**. Either way that person is never offered again, from any post.

**Skip** hides them for this post. **Not a lead** hides them and records that the AI got it wrong.

## Sending

```bash
python run.py send            # approve new leads and press Send for each
python run.py send --by-hand  # ...or each chat opens with the DM typed in and you press Enter
python send.py --list         # what's approved and why any would be skipped
python send.py held           # sends that couldn't be confirmed (the next run checks them)
python send.py resolve <username> --sent | --not-sent
```

For each approved commenter, best score first, `send.py`:

1. Opens their profile and presses **Follow**, unless the account already follows them or has a
   request pending (`outreach.auto_follow`, on by default; set it to `false` to never follow). If the
   follow fails the DM still goes; if Instagram blocks the action, the run stops as below.
2. Opens `ig.me/m/<username>` and checks it landed on a chat showing their username or name, that
   Instagram isn't refusing the account or them, and that the DM isn't already there (if it is,
   it's recorded as sent and not sent again).
3. Types the approved text. Line breaks use Shift+Enter, so nothing goes out early.
4. Writes a `submitting` row to `data/contacted.csv`, then presses Send **once**.
5. Waits for the message box to clear with no error, and records `sent` right away. It doesn't reload
   the chat to look for the DM (the way ApplyTwin logs "Applied" right after Submit). `--by-hand` trusts
   your `[s]ent` the same way. Set `outreach.verify_after_send` to `true` to reload and check first, as before.

If something goes wrong after Send is pressed and a quick look can't find the DM, the person is recorded `uncertain` and **held**. A crash between pressing Send and recording the outcome leaves a `submitting` row,
which counts as held too. Nobody held is messaged again until their chat is checked, and the next run
does that itself (`outreach.auto_resolve`, on by default): before sending anything it reloads each held
chat; if the message is there it's recorded `sent`, if the chat loaded and it isn't, it's `cleared` and
sent again in that run, and if the check can't tell (the chat didn't load, or doesn't show them) it
stays held for the run after. `python send.py resolve <username> --sent | --not-sent` still settles one by hand.

The run stops (exit code 2) on anything account-wide: logged out, wrong account, a challenge, an
action block ("Try Again Later"), or a locked account. It also stops after 3 failures before Send in
a row, 3 refusals from different people in a row, or 2 unconfirmed sends in a row. Someone refused or
failed is retried on later runs, up to `outreach.max_attempts_per_recipient` (3). It waits
`outreach.min_delay_seconds` to `max_delay_seconds` (60–180 s) between sends, and only one `send.py`
runs at a time. Don't read comments while it runs: they share the Chrome profile (`python run.py`
and `run.py replies` refuse to start while `send.py` runs).

## Tests

```bash
cd instagram
../venv/bin/python -m unittest discover tests          # ~50 s, nothing touches instagram.com
IG_LIVE=1 IG_LIVE_PROFILE=<username> ../venv/bin/python -m unittest tests/test_live.py -v
```

- `tests/test_stops.py`: what a run does on a logout, challenge or action block, including one during
  the follow (no DM goes out, exit 2), and what the next run does after (follows and messages the same
  person, never presses Send twice for a message already in the chat, settles held sends).
- `tests/test_discover.py`: finding posts from fake API answers: the media feed, the lookback and
  `max_posts`, pinned posts, `data/posts.json` across runs, the profile-grid fallback, and `python run.py`
  exiting 2 (and sending nothing) when Instagram refuses the account.
- `tests/test_replies.py`: fake inbox answers: matching chats to the people sent a DM, only their messages
  after our send counting, dedupe by item id, paging, the chat-list fallback, and `stats`' replied count.
- `tests/test_mock_site.py`: `direct.py`'s real page code in headless Chrome against mock profiles and chats.
  This covers the Follow button lookup: header Follow, not "Suggested for you"; Following / Requested left
  alone; both block popups; the notifications dialog. Also the two page fallbacks (profile grid, inbox list).
- `tests/test_live.py`: off unless `IG_LIVE=1`. Checks login, the sender id and the inbox. With
  `IG_LIVE_PROFILE` it finds the Follow button on that profile without pressing it. `IG_LIVE_FOLLOW=1` makes
  it really press Follow, so use a test account. `IG_LIVE_CHAT=<username>` also opens one chat. It never types
  a message. Failures save their screenshots and page dumps to `debug/live/`.

## Limits it enforces

- `outreach.daily_dm_limit` (default 20), counted across all runs from `data/contacted.csv`: `send.py`
  sends, **Mark sent** clicks, and held sends from today. Both the review page and `send.py`
  enforce it. Instagram puts DMs to people who
  don't follow you into Message Requests, and a burst of them is what triggers action blocks. Stay
  around 20–30 a day, fewer on a new account.
- One DM per person, ever. That includes held sends.
- Commenters who seem to be under 18 are hidden (`outreach.minor_policy: "skip"`).
- Comment reading stops at once if Instagram rate-limits or logs out the profile.

## Files

| File | What it does |
|---|---|
| `run.py` | The commands above. |
| `discover.py` | Finds the recent posts to read (web API, else the profile grid) and remembers them in `data/posts.json`. |
| `comments.py` | Reads all comments and replies, using Instagram's own web endpoint inside your logged-in session. Falls back to reading the page. |
| `replies.py` | Reads the inbox (web API, else the chat list) for replies to our DMs, into `data/replies.csv`. |
| `qualify.py` | Scores each comment with Claude and writes the DM. |
| `queue_server.py`, `queue.html` | The review page. |
| `send.py` | Approves new leads (`outreach.auto_approve`) and sends the DMs (or `--by-hand`), records and checks every attempt. |
| `direct.py` | Sends one DM (and with `verify_after_send`, confirms it in the chat). |
| `store.py` | `data/leads.json` and the ledger in `data/contacted.csv`. |
| `data/` | `leads.json` (commenters and drafts), `contacted.csv` (who you've DMed), `posts.json` (posts found and read) and `replies.csv` (who wrote back). Not committed. |
