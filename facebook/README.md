# Facebook Lead Finder

Finds recent Facebook posts from students and parents who need help with college admissions,
essays, scholarships, financial aid or internships. Claude scores each post and drafts a
personalized message pointing them to your booking link. You review each one in a local queue,
approve the ones worth sending, and `send.py` sends them the way the Reddit scripts on `master`
send theirs.

```
scrape.py ──► data/leads.json ──► qualify.py (Claude) ──► queue_server.py ──► send.py ──► Messenger
 (your logged-in            (score, flags,            (review, edit,      (by hand, or     data/contacted.csv
  Facebook session)          DM + comment drafts)      approve)            --auto)          (who's been contacted)
```

**Nothing is sent that you haven't approved**, with the exact text you approved. By default
`send.py` only opens each chat with the message typed in and you press Enter. With `--auto` it
presses Send itself. Facebook restricts accounts that send automated messages to people they
aren't friends with, so keep `--auto` runs small (see Guardrails). Many people posting about
admissions are under 18, and those authors are never DMed by default.

## How this fits with the Reddit scripts

`send.py`, `messenger.py` and `store.py` are the Facebook versions of `run_loop.py` / `open_queue.py`,
`send_dm.py` and `ledger.py` on `master`, with the same rules:

| Reddit (repo root) | Facebook (this folder) |
|---|---|
| queue `.html` you curate | leads you **Approve for sending** in the review queue |
| `open_queue.py` (you click Send) / `--send` | `send.py` (you press Enter) / `--auto` |
| `sender_username`, checked via `/api/me` | `outreach.sender_user_id`, checked via the `c_user` cookie |
| confirmed in the sent folder | confirmed in the chat after reloading it |
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
python run.py                      # scrape -> score/draft -> open the review queue
python run.py scrape --limit-pages 1   # quick test on one search page
python run.py queue                # reopen the queue without scraping
python run.py send                 # send what you approved: you press Enter in each chat
python run.py send --auto          # ...or let it press Send
python run.py stats
python send.py --list              # what's approved and why any would be skipped
python send.py held                # sends that couldn't be confirmed
python send.py resolve <author_key> --sent | --not-sent
```

In the queue (http://127.0.0.1:8765):

| Button | What it does |
|---|---|
| **Approve for sending** | Keeps the draft exactly as it is now. `python run.py send` sends it. It moves to the **Approved** tab, where **Unapprove** takes it back until it's sent. |
| **Copy & open Messenger** | Copies your edited draft and opens a chat with the author. Paste it, read it once more, send. |
| **Mark sent** | Records the DM. That person is never offered again, even from other posts. |
| **Copy comment & open post** / **Mark commented** | Use a helpful public reply instead of a DM. |
| **Skip** / **Not relevant** | Hides the post. "Not relevant" also keeps a record of where the AI was wrong. |

Edits to drafts save automatically. Leads are sorted by score.

## Sending

For each approved lead, best score first, `send.py`:

1. Opens their chat and checks it shows their name, that Facebook isn't refusing the account or the
   person, and that the message isn't already there (if it is, it's recorded as sent and not sent again).
2. Types the approved text. Line breaks use Shift+Enter, so nothing goes out early.
3. Writes a `submitting` row to `data/contacted.csv`, then presses Send **once**.
4. Waits for the message box to clear, reloads the chat, and records `sent` only once the message
   is in it.

If the message can't be confirmed, the lead is recorded `uncertain` and **held**. Nobody held is
messaged again until you check the chat and run `python send.py resolve <author_key> --sent` or
`--not-sent`. A crash between pressing Send and recording the outcome leaves a `submitting` row,
which counts as held too.

The run stops (exit code 2) on anything account-wide: logged out, wrong account, checkpoint,
"temporarily blocked", or Messenger asking for the PIN. It also stops after 3 failures before Send in
a row, 3 refusals from different people in a row, or 2 unconfirmed sends in a row. Someone refused or
failed is retried on later runs, up to `outreach.max_attempts_per_recipient` (3). It waits
`outreach.min_delay_seconds` to `max_delay_seconds` (60–180 s) between sends, and only one `send.py`
runs at a time. Don't scrape while it runs: they share the Chrome profile.

## Guardrails

- **Daily DM cap** (`outreach.daily_dm_limit`, default 20), counted across every run from
  `data/contacted.csv`: `send.py` sends, **Mark sent** clicks, and held sends from today. Both the queue
  server and `send.py` enforce it. New or low-activity accounts should start around 10, especially
  with `--auto`.
- **One DM per person, ever.** That includes held sends: their other posts aren't offered.
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
