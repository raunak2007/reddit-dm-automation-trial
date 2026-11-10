# Instagram Commenter Outreach

It finds your recent Instagram posts and reels on its own, reads everyone who commented, has Claude
pick out the ones asking about college admissions, essays, scholarships or internships, and writes
each of them a short, casual DM that starts with their name. Then `send.py` sends them the way
`run_loop.py` sends the Reddit queue on `master`: no approving, and it presses Send itself. Later,
`python run.py replies` checks who wrote back.

```bash
python run.py                                          # the daily run: find recent posts, read comments, draft DMs, send
python run.py replies                                  # who replied to their DM (inbox + Message Requests) -> data/replies.csv
python run.py stats                                    # posts, leads, sent, held, replied
python run.py https://www.instagram.com/p/XXXXXXXX/    # the same run for one post you name
python run.py send                                     # send any drafted DMs that haven't gone out yet
python run.py answer                                   # send the answers you approved in the shared inbox (../inbox)
python run.py followup                                 # one follow-up to people who never replied (off by default)
python run.py livecheck                                # read-only checks against instagram.com, for a weekly scheduler
```

`python run.py --help` (or any command with `--help`) only prints the help; nothing opens Chrome.

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
   the way `comments.py` reads comments), then the account's reels from `clips/user/` (a form POST of
   `target_user_id`, as Instagram web's Reels tab asks it, or a GET if that doesn't answer), since a reel
   shared only to the Reels tab isn't in the feed. A reel in both is kept once; reels count toward the same
   lookback and `max_posts`. If the API doesn't answer, it reads the profile page's grid and its Reels tab.
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
page), then Message Requests (`direct_v2/pending_inbox/`) for anyone whose chat wasn't in the inbox.
It never opens a chat or accepts a request, so nothing is marked seen, liked, accepted or sent. For everyone
`data/contacted.csv` records as `sent`, it finds their one-to-one chat and records each message
from them dated after our DM: username, when, a short snippet, the whole message (`text`, up to 4000
characters, line breaks kept) and the chat link, in `data/replies.csv`, once per message (Instagram's
item id), then prints the new ones and how many people have replied. The `how` column says where it was
found: `api` (inbox), `request` (Message Requests, not accepted) or `page` (the fallback below).
`contacted.csv` isn't changed. A `replies.csv` from before the `text` column is still read (its text is
the snippet) and is rewritten with the new header the next time a reply is added. Paging stops once every
DMed person's chat has been seen, the chats get older than the first DM, or after `replies.max_pages`
pages. If the inbox endpoint doesn't answer, it reads the chat list on the inbox page instead (still
without opening a chat): that gives only each chat's latest message, with an approximate time. A creator
account's General tab may not show up.

### Answers (`python run.py answer`)

The shared inbox at the repo root (`../inbox`, its record is `inbox/data/answers.json`) drafts an answer
to each reply, and you approve it on its page. `python run.py answer` sends the approved Instagram ones,
oldest approval first:

1. Takes `send.py`'s lock, then turns any entry a dead run left `sending` into `uncertain`, so nothing is
   resent blind. Checks the browser is logged in as `outreach.sender_user_id`.
2. Asks Message Requests (read only) which of these people's chats sit there. Those answers are marked
   `failed` with the reason: the request has to be accepted first, and nothing here accepts one. Accept
   it in Instagram and approve the answer again. (If that endpoint doesn't answer, `replies.csv`'s
   `how=request` decides.)
3. For each one still `approved`: opens `ig.me/m/<person>`, checks it is their chat, that the answer isn't
   already in it, types it, marks the entry `sending`, presses Send once, then marks it `sent`
   (`uncertain` if Send was pressed and that couldn't be confirmed, `failed` if it never got to Send).

Nobody is followed, `contacted.csv` isn't touched, and answers don't count toward `daily_dm_limit`. At most
`answers.max_per_run` (10) per run, with the usual `min_delay_seconds`..`max_delay_seconds` between them. It
stops with exit code 2 on anything account-wide (the entry stays `approved` if Send wasn't pressed), and
after 3 failed or 2 uncertain answers in a row.

### Follow-ups (`python run.py followup`)

Off until you set `outreach.followup.enabled` to `true`. Then each run sends **one** follow-up, ever, to
each person DMed at least `outreach.followup.after_days` (6) days ago who hasn't replied
(`data/replies.csv`), hasn't been sent an answer from the inbox, hasn't had a follow-up, and isn't thought
to be under 18. `python followup.py --list` shows who is due one and why others aren't (no browser).

- It reads the inbox and Message Requests through the API first (replies found are saved and those people
  skipped), and **right before each send** reads that person's chat again (`direct_v2/threads/<id>/`,
  without opening it or marking it seen). A new reply, or a chat that can't be found or re-read, means no
  follow-up.
- Claude drafts it (`prompts/followup.md`: casual, short, referencing the first DM), with
  `outreach.followup.template` used when AI is off or the draft fails.
- It sends like `send.py` (no follow): `followup_submitting` before the press, then `followup_sent`,
  `followup_uncertain` (never retried) or `followup_failed` (may be tried again later). These rows have the
  lead key `followup:<username>`, so the review page and the first DM's held / sent logic are unchanged.
- Follow-ups count toward `outreach.daily_dm_limit`: `store.sent_today()` counts each person's first DM and
  their follow-up, so `send.py` and the review page see them too. At most `outreach.followup.max_per_run`
  per run; exit code 2 on anything account-wide, and after 3 failed or 2 uncertain in a row.

### Live check (`python run.py livecheck`)

For a weekly scheduler: runs `tests/test_live.py` against the real instagram.com with `IG_LIVE=1`, taking
the profile and chat to check from config.json's `livecheck` section (not from `IG_LIVE_*` variables in
your shell). `IG_LIVE_FOLLOW` is never passed on, and it runs with `IG_LIVE_READONLY=1`, so nothing is
typed, followed or pressed (not even "Not Now" on a notifications dialog). It takes `send.py`'s lock.
Exit code `0` if the checks pass, `2` if the failures show an account problem (logged out, wrong account,
a challenge, an action block), `1` for anything else (Chrome didn't start, the markup changed, a timeout).

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
| `answers.max_per_run` | 10 | Approved answers sent per `run.py answer`. |
| `outreach.followup.enabled` | `false` | Turn on `run.py followup`. |
| `outreach.followup.after_days` | 6 | Days after our DM before a follow-up. |
| `outreach.followup.max_per_run` | 5 | Follow-ups per run (they also count toward `daily_dm_limit`). |
| `outreach.followup.template` | (a bump line) | The follow-up when Claude can't draft one: `{first_name}`, `{topic}`, `{booking_link}`, ... |
| `livecheck.profile` | `""` | A profile whose Follow button `livecheck` finds (never pressed). Empty: skipped. |
| `livecheck.chat` | `""` | A username whose chat `livecheck` opens (nothing typed). Empty: skipped. |
| `livecheck.timeout_minutes` | 10 | Give up on the live checks after this long (exit 1). |

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

## API mode (a professional account, optional)

Everything above is **browser mode**, the default (`"mode": "browser"` in config.json), and it's unchanged. It's
what a personal account like @ameer.husss uses. **API mode** (`"mode": "api"`) uses Meta's official Instagram API
(Instagram API with Instagram Login, graph.instagram.com) instead of Chrome, and only works for a
**professional (Business or Creator) account**. Every command refuses (exit code 2) if `/me` says the account
isn't professional, or isn't `instagram.my_username`.

### Setting it up

1. **Make the account professional:** in the Instagram app, Settings → Account type and tools → Switch to a
   professional account (Business or Creator).
2. **Create a Meta app:** at developers.facebook.com, Create app → use case "Manage messaging & content on
   Instagram" (the *Instagram API with Instagram Login* product). Under Instagram → API setup with Instagram
   login, add the permissions `instagram_business_basic`, `instagram_business_manage_comments` and
   `instagram_business_manage_messages`.
3. **Add the account as an Instagram Tester:** App roles → Roles → Add people → Instagram Tester, then accept
   the invite in Instagram (Settings → Website permissions → Apps and websites → Tester invites).
4. **Set the app Live.** App Review isn't needed while it only uses your own accounts (the ones with a role on
   the app).
5. **Get a long-lived token:** in API setup with Instagram login, "Generate token" for the account (it's
   long-lived, about 60 days), or exchange a short-lived one with
   `GET https://graph.instagram.com/access_token?grant_type=ig_exchange_token&client_secret=...&access_token=...`.
   Put it in `instagram/.env` as `IG_ACCESS_TOKEN=...`.
6. In config.json set `"mode": "api"` and `instagram.my_username` to that account's handle, then run
   `python run.py livecheck`.

The token is refreshed automatically once it's 30 days old
(`graph.instagram.com/refresh_access_token`). The new token and its dates go in `data/api_token.json`, which is
gitignored and readable only by you, and the token is never printed. Every run warns when it has under 14 days
left. A new token pasted into `.env` replaces the stored one.

### What each command does in API mode

| Command | API mode |
|---|---|
| `python run.py` | Discover: the account's own posts and reels (`/me/media`) within `discover.lookback_days`, capped at 7 (it says so when it lowers it). Their comments (`/{media}/comments`) go into the same `leads.json` shape, with `comment_id` (the commenter's newest comment) and `user_id`. Qualify: unchanged. Send: see below. `discover.accounts` / `discover.posts` aren't read. |
| `python run.py send` | A **private reply** to the lead's comment (`POST /{ig-user-id}/messages {"recipient": {"comment_id": ...}}`). `send.py`'s own loop: a `submitting` row first, the daily cap, the delays, the streak stops. Nobody is followed. Instagram's "already sent" counts as `sent`. A closed 7-day window is `expired` (final, never retried). A rate limit or a dead token stops the run with exit code 2. No answer or a 5xx is `uncertain` and held. |
| `python run.py replies` | `/me/conversations`: their messages after our reply go into `replies.csv` with `text`, `item_id` = the message id, `thread_id` = the conversation id, `user_id` = their IGSID, `how=graph`, and `reply_by`: their latest message + 24 hours. |
| `python run.py answer` | Approved inbox answers, sent as a DM to their IGSID (`{"recipient": {"id": ...}}`), **only inside the 24-hour window**. Outside it, the entry is marked `failed`: "the 24-hour window closed on <time>". The same `sending` / `sent` / `failed` / `uncertain` rules as browser mode. The inbox page shows "answer by <time>", in red under 3 hours. |
| `python run.py followup` | Refused (exit 0): Meta allows no message to someone who hasn't written back. |
| `python run.py livecheck` | No browser. It checks that the token works, that `/me` is a professional account matching `instagram.my_username`, and that one media, comments and conversations read each work. Exit `0` passed, `2` bad token, missing permission, or the wrong / a non-professional account, `1` anything else. |

### Limits

- **7 days:** a private reply is only possible within 7 days of the comment.
- **One reply per comment.** One DM per person, as in browser mode.
- **The 24-hour window:** after they write, you can answer for 24 hours from their latest message.
- **No follow-ups:** nothing can go to someone who hasn't replied.
- **About 750 private replies per hour** per account (keep `daily_dm_limit` far below that anyway).

| Key | Default | What it does |
|---|---|---|
| `mode` | `"browser"` | `"api"` for API mode. |
| `api.graph_version` | `"v23.0"` | The Graph API version. |
| `api.ig_user_id` | `""` | Optional: the account's id. `/me` gives it; if set, it must match. |
| `api.max_media_pages` | 4 | Pages of `/me/media` (25 each) to read at most. |
| `IG_ACCESS_TOKEN` (in `.env`) | | The long-lived token. |

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
- `tests/test_discover.py` also covers the reels endpoint: reels missing from the feed, kept once, paged to
  the lookback, the GET fallback, `max_posts`, and only read-only endpoints ever POSTed to.
- `tests/test_replies.py`: fake inbox answers: matching chats to the people sent a DM, only their messages
  after our send counting, dedupe by item id, paging, the full `text` column (and old files without it),
  Message Requests (`how=request`, never accepted), the chat-list fallback, and `stats`' replied count.
- `tests/test_answer.py`: `run.py answer` against a temp inbox record: `sending` before the press, stuck
  entries, Message Requests, the per-run cap, account-wide stops and streaks. `test_mock_site.py` sends one
  answer through the real page code.
- `tests/test_followup.py`: who is due a follow-up, the re-check right before each send, drafting and the
  template, the daily cap counting follow-ups (in `send.py` too), one ever, and stops.
- `tests/test_apimode.py`: API mode against a fake Graph API (no network): the token (refresh, warnings, never
  printed), the professional check, discover and comments, private replies (already sent, the 7-day window, rate
  limits with exit 2, the cap), replies with `reply_by`, answers in the 24-hour window, follow-ups refused,
  livecheck, and browser mode untouched.
- `tests/test_livecheck.py`: the environment `livecheck` gives `test_live.py`, its exit codes, and that every
  script's `--help` only prints help.
- `tests/test_mock_site.py`: `direct.py`'s real page code in headless Chrome against mock profiles and chats.
  This covers the Follow button lookup: header Follow, not "Suggested for you"; Following / Requested left
  alone; both block popups; the notifications dialog. Also the two page fallbacks (profile grid, inbox list).
- `tests/test_live.py`: off unless `IG_LIVE=1`. Checks login, the sender id and the inbox. With
  `IG_LIVE_PROFILE` it finds the Follow button on that profile without pressing it. `IG_LIVE_FOLLOW=1` makes
  it really press Follow, so use a test account. `IG_LIVE_CHAT=<username>` also opens one chat. It never types
  a message. `IG_LIVE_READONLY=1` (what `run.py livecheck` uses) presses nothing at all. Failures save their
  screenshots and page dumps to `debug/live/`.

## Limits it enforces

- `outreach.daily_dm_limit` (default 20), counted across all runs from `data/contacted.csv`: `send.py`
  sends, **Mark sent** clicks, held sends from today, and follow-ups sent today. Both the review page and `send.py`
  enforce it. Instagram puts DMs to people who
  don't follow you into Message Requests, and a burst of them is what triggers action blocks. Stay
  around 20–30 a day, fewer on a new account.
- One DM per person, ever, plus at most one follow-up (off by default). That includes held sends.
- Commenters who seem to be under 18 are hidden (`outreach.minor_policy: "skip"`).
- Comment reading stops at once if Instagram rate-limits or logs out the profile.

## Files

| File | What it does |
|---|---|
| `run.py` | The commands above. |
| `discover.py` | Finds the recent posts to read (web API, else the profile grid) and remembers them in `data/posts.json`. |
| `comments.py` | Reads all comments and replies, using Instagram's own web endpoint inside your logged-in session. Falls back to reading the page. |
| `replies.py` | Reads the inbox and Message Requests (web API, else the chat list) for replies to our DMs, into `data/replies.csv`. |
| `answer.py` | `run.py answer`: sends the answers approved in the shared inbox (`../inbox/store.py`). |
| `followup.py`, `prompts/followup.md` | `run.py followup`: one follow-up to people who never replied. |
| `graph.py`, `apimode.py` | API mode: the Graph API client and token, and each command's API version. |
| `livecheck.py` | `run.py livecheck`: the read-only live checks, with an exit code for a scheduler. |
| `qualify.py` | Scores each comment with Claude and writes the DM. |
| `queue_server.py`, `queue.html` | The review page. |
| `send.py` | Approves new leads (`outreach.auto_approve`) and sends the DMs (or `--by-hand`), records and checks every attempt. |
| `direct.py` | Sends one DM (and with `verify_after_send`, confirms it in the chat). |
| `store.py` | `data/leads.json` and the ledger in `data/contacted.csv`. |
| `data/` | `leads.json` (commenters and drafts), `contacted.csv` (who you've DMed), `posts.json` (posts found and read) and `replies.csv` (who wrote back). Not committed. |
