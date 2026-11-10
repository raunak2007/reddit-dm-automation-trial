# Reddit Lead Finder

Finds recent Reddit posts from students and parents who need help with college admissions, essays,
scholarships or financial aid. Claude scores each post and drafts a subject and a personalized message, and
`send.py` sends them through the **root** Reddit sender (`send_dm.py` + `ledger.py`), the same code
`run_loop.py` uses: no approving, and it presses Send itself. `replies.py` then watches the inbox for answers,
`answer.py` sends the answers you approve on the inbox page (`inbox/` at the repo root), and `followup.py` (off by
default) sends one follow-up to people who never replied.

```
scrape.py ──► data/leads.json ──► qualify.py (Claude) ──► send.py ──────────► ledger.send_loop ──► Reddit
 (Reddit's JSON listings,      (score, minor flag,      (picks who's eligible,      (root sender; records
  logged-in Chrome profile)     subject + message)       writes data/queue.html)     every attempt in the
                                                                                      root contacted.csv)
replies.py: reads /message/inbox.json ──► data/replies.csv (replies from people in contacted.csv, full text)
answer.py:  inbox/data/answers.json (approved on the inbox page) ──► send_dm.send_compose ──► Reddit
followup.py: contacted.csv + leads.json - replies - answers ──► Claude ──► send_compose ──► data/followups.csv
livecheck:  tests/test_live.py, read-only, for a weekly scheduler
```

**Every eligible lead at or above `ai.min_score` is sent its drafted message** (`outreach.auto_approve`, on by
default). Set `auto_approve` to `false` to only write `data/queue.html`, look it over, and send it with the root
`open_queue.py`. Most people posting in r/ApplyingToCollege and r/chanceme are high school students, and authors
the AI thinks are under 18 are never messaged by default.

## How this fits with the root scripts

There is one record of who has been messaged: the root `contacted.csv`. `run_loop.py`, `open_queue.py` and
this folder's `send.py` all write to it and all read it, so:

- the daily cap (`daily_limit_per_account` in the root `config.json`) counts sends from every Reddit script;
- each person is messaged once, ever, whichever script got there first;
- held (`uncertain` / `submitting`) sends hold the person everywhere until settled (`python ledger.py held`);
- one sending run at a time across all of them (`ledger.lock()`).

| Setting | Where |
|---|---|
| `sender_username`, `daily_limit_per_account`, `min/max_delay_seconds`, `verify_after_send`, `max_attempts_per_recipient` | root `config.json` (shared with `run_loop.py`) |
| `business`, `search`, `ai`, `outreach`, `answers`, `followup`, `browser` | `reddit/config.json` (this folder only) |

The Chrome profile is the root one, `accounts/account_1/chrome_profile` (`browser.profile`), launched the way
`run_loop.py` launches it. Chrome won't let two processes share a profile, so don't scrape, send or read replies
while `run_loop.py` or `open_queue.py` has it open.

## Setup

Uses the root Python 3.11 venv.

```bash
venv/bin/pip install -r reddit/requirements.txt      # adds anthropic and python-dotenv
cp reddit/.env.example reddit/.env                    # then put your ANTHROPIC_API_KEY in it
venv/bin/python reddit/run.py login                   # log in to Reddit once in the window (skip if the
                                                      # root scripts already use this profile)
```

The root `config.json` needs `"sender_username"`; a send run without it stops and says which account the
profile is logged in as.

## Make it yours

1. **`config.json` → `business`**: company name, website, booking link, your first name, services, offer.
2. **`config.json` → `search`**:
   - `subreddits`: where to look (`ApplyingToCollege`, `collegeresults`, `scholarships`, `chanceme`,
     `CollegeEssays` by default).
   - `keywords`: each is searched inside every subreddit, newest first.
   - `read_new_feeds`: also read each subreddit's newest posts in full (Claude sorts them out).
   - `lookback_days` (7): older posts are neither kept nor messaged.
   - `max_pages_per_listing` (3 pages of 100), `max_requests_per_run` (80): how much one run reads.
   - `skip_authors`, `skip_nsfw`.
3. **`config.json` → `ai`**: `model`, `min_score` (60), `max_posts_per_run` (300, newest first).
4. **`config.json` → `outreach`**:
   - `auto_approve` (true): send without review.
   - `minor_policy` (`"skip"`): `"allow"` is the only other value; anything else skips them. There's no
     public-comment path here, so unlike facebook/ there is no `comment_only`.
   - `subject` (`"About your post"`): used for every message. The queue format expects one subject per queue
     (`open_queue.parse_queue` refuses entries whose subject differs from the rest). Leave it `""` to use
     Claude's drafted subject per lead; `send.py` still sends those, but `open_queue.py` would refuse most of
     the queue.
5. **`config.json` → `answers`**: `max_per_run` (10 approved answers a run), `subject` (`"re: About your post"`,
   used when the reply's own subject isn't known, e.g. for comment replies).
6. **`config.json` → `followup`** (see below): `enabled` (`false`), `after_days` (6), `max_days` (30),
   `max_per_run` (5), `inbox_pages` (1: inbox pages re-read before each send), `template` (the fallback message,
   with `{username}`, `{subreddit}` and the `business` placeholders).
7. **`config.json` → `browser`**: `profile` (`account_1`), `min/max_request_delay_seconds` (3–7 s between
   requests).
8. **`prompts/qualify.md`** (who is a lead, scoring, the minor rules), **`prompts/message.md`** (how the
   message and subject read) and **`prompts/followup.md`** (the follow-up). `{booking_link}` etc. are filled from `business`. `python reddit/run.py qualify
   --dry-run` shows the exact prompt; `--rescore` redrafts every lead after you change one.

## Daily use

```bash
venv/bin/python reddit/run.py                       # scrape -> qualify -> send
venv/bin/python reddit/run.py scrape --limit-requests 2    # a quick test
venv/bin/python reddit/run.py qualify [--rescore | --dry-run]
venv/bin/python reddit/run.py send --list           # who would be sent and why anyone isn't (no browser)
venv/bin/python reddit/run.py send                  # write data/queue.html and send it
venv/bin/python reddit/run.py replies [--pages 3]   # record replies in data/replies.csv
venv/bin/python reddit/run.py answer                # send the answers approved on the inbox page
venv/bin/python reddit/run.py followup [--list]     # one follow-up to who never replied (followup.enabled)
venv/bin/python reddit/run.py livecheck             # read-only live check (weekly): exit 0 ok, 2 account, 1 other
venv/bin/python reddit/run.py stats
```

Each script also runs on its own (`python reddit/scrape.py`, `qualify.py`, `send.py [--list]`, `replies.py`,
`answer.py`, `followup.py [--list]`).

**By hand instead**: with `auto_approve` false, `send` only writes `data/queue.html`. Open it to read every
message, then `venv/bin/python open_queue.py reddit/data/queue.html --no-checkmarks` (you press Send) or add
`--send`. `--no-checkmarks` is right as long as you send through `open_queue.py`, which records each send in
`contacted.csv` itself; if you ticked the page's **Sent** boxes while sending from it in another browser, import
them instead: `python ledger.py import-checkmarks reddit/data/queue.html -` (the page's key carries the time it
was written, so ticks from an older page never count for a new one).

**Live check**: `venv/bin/python reddit/run.py livecheck` once a week from a scheduler (close the other Reddit
scripts first; it shares the profile). Or by hand: `REDDIT_LIVE=1 venv/bin/python -m unittest discover reddit/tests -p test_live.py -v`.

**Exit codes**: 2 when a run halted on an account-wide problem (logged out, wrong account, rate-limited,
restricted, a block page) or a streak of failures (`ledger.report`), so a scheduler can notify; 0 otherwise
(1 for setup problems such as a missing `sender_username`). If the scrape halts, the full run still scores what
it has (that only talks to Claude) but sends nothing.

## What each step does

- **scrape.py** reads `/r/<sub>/new.json` and `/r/<sub>/search.json?q=<kw>&restrict_sr=on&sort=new` on
  old.reddit.com through the logged-in browser, page by page until it reaches posts older than `lookback_days`.
  Posts are merged into `data/leads.json` by post id (`t3_…`); a post edited since it was scored is scored
  again. Deleted authors, AutoModerator, this account, stickied, moderator, removed and NSFW posts are left out.
  It first checks the profile is logged in (as `sender_username`, when set).
- **qualify.py** sends each new post to Claude with the two prompts and a `record_lead` tool (same SDK use as
  facebook/: cached system prompt, forced tool call, `ai.model`). Posts by anyone `contacted.csv` already rules
  out aren't sent to Claude.
- **send.py** picks the leads that are scored, relevant, at or above `min_score`, within `lookback_days`, not
  likely minors (unless `minor_policy` is `allow`), not this account and clear in `contacted.csv`
  (`ledger.skip_reason`), one per person (their best-scored post), best score first. It builds each compose link
  with `send_dm.compose_link`, writes `data/queue.html`, checks it parses cleanly with `open_queue.parse_queue`,
  then `ledger.lock()` → `ledger.prepare()` (sender check, sent folder pulled in) → `ledger.send_loop()` →
  `ledger.report()`.
- **replies.py** reads `/message/inbox/.json?mark=false` (up to `--pages` pages of 100, including messages
  nested in a thread) and keeps anything written by someone `contacted.csv` has as `sent`, after their first
  send: private messages, comment and post replies, mentions. Each goes into `data/replies.csv` once (by
  message id) with username, time, a snippet, the full `text` (up to 4000 characters, for the inbox page's
  drafts) and a permalink. A `replies.csv` from before the `text` column is read with the snippet as its text and
  rewritten with the new header on the next write. It never marks anything read, never sends, and never changes
  `contacted.csv`.
- **answer.py** sends what you approved on the inbox page. The record is the root `inbox/data/answers.json`
  (`inbox/store.py`). It first turns entries a dead run left `sending` into `uncertain` (`store.stuck`), then
  takes `store.approved("reddit")`, oldest approval first, up to `answers.max_per_run`: under `ledger.lock()`,
  logged in as `sender_username` with the sent folder pulled in (the same start as `send.py`), it sends each
  entry's `text` to its `person` with `send_dm.compose_link` / `send_compose`. The subject is `re: <their
  subject>` when `replies.csv` (or the entry) has the reply as a private message, else `answers.subject`.
  `sending` is written right before Send is clicked, and only if the entry is still approved with the same text
  (an entry edited or dismissed on the page meanwhile is left alone), then `sent`, `failed` (not sent: failed
  before Send, refused, no such user, a bad entry; approve it again to retry) or `uncertain` (check the sent
  folder), with the reason. These are answers in an existing conversation, so they don't go through
  `ledger.send_loop` (its `skip_reason` would refuse everyone already messaged), aren't written to
  `contacted.csv` and don't count toward the daily cap; `answers.max_per_run` and the root delays limit them.
- **followup.py** (only with `followup.enabled`) sends one follow-up, ever, to each person whose first DM (the
  first `sent` row in `contacted.csv`) is between `after_days` and `max_days` old, who never replied
  (`replies.csv`), hasn't been answered on the inbox page (`inbox.store.answered`), hasn't had a follow-up, and
  was messaged by this folder about a post that wasn't flagged as likely under 18 (people `run_loop.py`
  messaged from `target_users.csv` have no post on record and are left alone). Oldest first DM first, up to
  `max_per_run`. Claude drafts it (`prompts/followup.md`, the same client as `qualify.py`) from their post and the
  first message; if Claude fails, `followup.template` is used. Right before each send it re-reads the newest inbox
  page(s) (`mark=false`, new replies are recorded) and skips anyone who replied since.
  Follow-ups are recorded in **`data/followups.csv`** (a `submitting` row before Send, then the outcome), not in
  `contacted.csv`, so the ledger's own sent / held / attempts logic and `sync_reddit_sent` are unchanged.
  Anything sent, possibly sent, refused or "no such user" there rules the person out for good; three failures
  before Send give up on them.
- **livecheck** (`run.py livecheck`) runs `tests/test_live.py` with `REDDIT_LIVE=1`: logged in as
  `sender_username`, the inbox JSON reads and parses, and the first `search.subreddits`' `new.json` parses with
  `scrape.parse_post`. It only opens `.json` pages: nothing is sent, posted or marked read. Exit 0 when it
  passes, 2 when it shows an account problem (logged out, wrong account, rate-limited, a block page), 1 otherwise.

## Guardrails

- **Daily cap and one DM per person, ever**, shared with every Reddit script through the root `contacted.csv`.
  Follow-ups count toward the same cap here: `send.py` and `followup.py` check `ledger.sent_today()` plus today's
  follow-ups in `data/followups.csv` (`send.py` hands `ledger.send_loop` the cap minus today's follow-ups). The
  root `run_loop.py` / `open_queue.py` don't read `followups.csv`, so they don't see follow-ups yet.
- **One follow-up per person, ever**, never to someone who replied or was answered, re-checked against the live
  inbox right before Send; off until you set `followup.enabled`.
- **Minors**: not messaged unless `outreach.minor_policy` is `"allow"`. A parent posting about their child is
  not flagged.
- **Stops on the account**: the scraper, sender and reply reader all stop (keeping what they have, exit 2) on a
  logout or wrong account, a 429 or "you're doing that too much", a 403 that isn't about one subreddit, a block
  page, or any page that isn't JSON. A private, banned or missing subreddit only skips that listing. If a run
  stops this way, look at the account and wait a day before reading again.
- **Polite pace**: 3–7 s between requests, at most `max_requests_per_run` requests a run, and the root
  config's 30–120 s between sends.
- Old posts (beyond `lookback_days`) are never messaged, even if a cap held them back earlier.
- Reddit's rules and each subreddit's rules limit unsolicited DMs; many subreddits ban them. The risk falls on
  the account, so keep `daily_limit_per_account` low, especially on a new account.

## Tests

```bash
venv/bin/python -m unittest discover reddit/tests     # well under a second; nothing touches reddit.com
```

The Claude client and the browser are fakes; the root ledger's `contacted.csv`, `queue_state/` and lock, the
root `inbox/data/answers.json` and its lock, and this folder's `data/`, point into a temp folder for each test.

- `tests/test_scrape.py`: which posts are kept, the lookback window and when paging stops, merging by post id,
  `fetch_json`'s reading of Reddit's errors, and a run stopping (keeping what it read) on a rate limit or logout.
- `tests/test_qualify.py`: what is sent to Claude, the `ai` block kept, who isn't scored, retries, `--dry-run`.
- `tests/test_send.py`: eligibility (score, minors, already contacted, held, given up, old posts, one per
  person), `data/queue.html` round-tripping through `open_queue.parse_queue` with no problems, and a send
  through the real `ledger.prepare` / `send_loop` / `report` (exit 2 on account-wide problems, shared cap).
- `tests/test_replies.py`: reply matching, dedupe by message id, `mark=false`, `contacted.csv` untouched, the
  full `text` column (capped) and old files without it.
- `tests/test_answer.py`: approved entries sent to already-contacted people, subjects, `sending` before Send then
  `sent` / `failed` / `uncertain`, stuck entries, entries edited mid-run, `max_per_run`, exit 2 on account-wide
  problems and failure streaks. The shared inbox's `answers.json` is a temp copy too.
- `tests/test_followup.py`: who is due, Claude drafts and the template fallback, the inbox re-read before Send,
  one follow-up ever, the shared daily cap (including `send.py` seeing follow-ups), exit 2 on account problems.
- `tests/test_livecheck.py`: `run.py livecheck` against a fake browser (exit 0 / 2 / 1, only `.json` reads);
  `tests/test_live.py` itself is skipped unless `REDDIT_LIVE=1`.
