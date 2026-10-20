# Instagram Commenter Outreach

Give it the link to an Instagram post or reel. It reads everyone who commented, has Claude pick out
the ones asking about college admissions, essays, scholarships or internships, and writes each of
them a short, casual DM that starts with their name. Then `send.py` sends them the way `run_loop.py`
sends the Reddit queue on `master`: no approving, and it presses Send itself.

```bash
python run.py https://www.instagram.com/p/XXXXXXXX/    # read comments, draft DMs, send them
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
| confirmed in the sent folder | confirmed in the chat after reloading it |
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
5. Waits for the message box to clear, reloads the chat, and records `sent` only once the DM is in it.

If the DM can't be confirmed, the person is recorded `uncertain` and **held**. A crash between pressing Send and recording the outcome leaves a `submitting` row,
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
runs at a time. Don't read comments while it runs: they share the Chrome profile.

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
| `comments.py` | Reads all comments and replies, using Instagram's own web endpoint inside your logged-in session. Falls back to reading the page. |
| `qualify.py` | Scores each comment with Claude and writes the DM. |
| `queue_server.py`, `queue.html` | The review page. |
| `send.py` | Approves new leads (`outreach.auto_approve`) and sends the DMs (or `--by-hand`), records and checks every attempt. |
| `direct.py` | Sends one DM and confirms it in the chat. |
| `store.py` | `data/leads.json` and the ledger in `data/contacted.csv`. |
| `data/` | `leads.json` (commenters and drafts) and `contacted.csv` (who you've DMed). Not committed. |
