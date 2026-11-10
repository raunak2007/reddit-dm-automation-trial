# Reddit DM Automation

## Requirements

- macOS (tested) or Linux. Windows should work but is untested.
- Python 3.11 (3.12+ breaks `undetected-chromedriver` because `distutils`
  was removed).
- Google Chrome installed. The scripts detect its major version at launch
  (`chrome_version.py`), so no version number needs to be kept in sync.
- A Reddit account you can log into manually once.

## Setup

```bash
# 1. Clone or copy the repo, then:
cd reddit-dm-automation-trial

# 2. Create a Python 3.11 virtualenv
python3.11 -m venv venv
source venv/bin/activate

# 3. Install dependencies
pip install undetected-chromedriver selenium python-dotenv

# 4. Optional: confirm the Chrome version the scripts will use
python chrome_version.py
```

Sends are recorded `sent` as soon as Reddit accepts them, without looking for them in the sent folder
afterwards (the way ApplyTwin logs "Applied" right after Submit). `open_queue.py` trusts your `[s]ent` the same
way. Set `"verify_after_send": true` in `config.json` to check the sent folder first, as before.

Held sends settle themselves too: each run reads the sent folder first, so a held send that went out is
recorded `sent`, and one the folder shows never went out is cleared and sent again (`"auto_resolve": true`,
the default). A held send is only cleared once the listing reaches back past it and it's 15 minutes old.

## Reddit lead finder (`reddit/`)

`reddit/` finds recent posts in admissions subreddits (r/ApplyingToCollege, r/scholarships, ...). Claude scores
each post and drafts a message, and the posts at or above `ai.min_score` are sent through this folder's sender
(`ledger.py`). That means the same daily cap, `contacted.csv` and one-DM-per-person apply. It also writes
`reddit/data/queue.html` for `open_queue.py`, and `python reddit/run.py replies` records who wrote back. Setup
and use: [reddit/README.md](reddit/README.md).

## Facebook lead finder (`facebook/`)

`facebook/` finds recent Facebook posts from students and parents who need admissions help. Claude scores
each post and drafts a message, and `facebook/send.py` sends them through Messenger on its own, like
`run_loop.py` (no approving or pressing Send; set `outreach.auto_approve` to `false` to review first). It follows the same rules as the Reddit scripts here: sender
check, one send per person, recorded as sent once the site takes it (no checking afterwards), held uncertain sends, a daily cap, and stops on
account-wide problems. It shares this folder's `chrome_version.py` and `send_dm.py` helpers. Setup and
use: [facebook/README.md](facebook/README.md).

## Instagram commenter outreach (`instagram/`)

`instagram/` reads everyone who commented on an Instagram post or reel. Claude picks out the ones asking
about admissions and writes each a short, casual DM, and `instagram/send.py` sends them on its own, like
`run_loop.py` (no approving or pressing Send; set `outreach.auto_approve` to `false` to review first). It follows the same rules as the Reddit scripts here: sender
check, one send per person, recorded as sent once the site takes it (no checking afterwards), held uncertain sends, a daily cap, and stops on
account-wide problems. It shares this folder's `chrome_version.py` and `send_dm.py` helpers. Setup and
use: [instagram/README.md](instagram/README.md).

## Replies, answers and follow-ups (`inbox/`)

Each platform's `replies` job records who wrote back (full text). `python inbox/run.py` has Claude draft an answer
to each conversation and opens a page at http://127.0.0.1:8767 where you edit, approve or dismiss them. Nothing
is sent until you approve it. The next daily run (or `<platform> run.py answer`) sends the approved ones in
the same chat. "Stop messaging me" and clear no-thanks replies are flagged "suggest: dismiss" instead of
getting a pitch. See [inbox/README.md](inbox/README.md).

Each platform can also send **one** follow-up to people who haven't replied after `after_days` (6). It's off until
you set `followup.enabled` to `true` (Reddit: `reddit/config.json`; Facebook and Instagram: `outreach.followup`).
It skips anyone who replied, was answered or is a likely minor, re-reads their chat right before sending, and
counts toward the same daily cap as first DMs. Turn it on only after the weekly live check has passed, because it
relies on reply detection being right.

## Results: bookings and insights

- Each platform's booking link carries `utm_source=<platform>`, and links in drafted answers also carry
  `utm_content=<person>`. `python bookings/run.py import <export.csv>` (any booking tool's or form's CSV
  export) or `python bookings/run.py calendly` (with `CALENDLY_TOKEN` in `bookings/.env`; then set that job's
  `"enabled": true` in `schedule.json`) records each booking and matches it to the person it came from. The
  match uses the UTM tag first, then a username in the form answers, then an exact full name; anything
  unclear is left for `bookings/run.py link`.
- `python outcomes.py` shows contacted / followed up / replied / answered / booked per platform; `--today` is
  the daily summary.
- `python insights/run.py` (weekly) writes `insights/reports/<date>.md`: reply and booking rates by platform,
  score, category and source, with example messages and suggested prompt edits from Claude. It never edits a
  prompt; copy in what you agree with.

## Running everything every day

```bash
venv/bin/python daily.py --list        # the jobs in schedule.json (written with defaults on first use)
venv/bin/python daily.py               # run them all now, one after another
venv/bin/python schedule.py install --at 10:00   # run daily.py every day at 10:00 (launchd)
venv/bin/python schedule.py status | run-now | uninstall
```

`daily.py` runs `schedule.json`'s jobs in order. For each platform: the weekly read-only live check (Mondays),
the main run (find + score + send; Facebook comments too if `outreach.auto_comment` is on), replies, answer
(send approved answers) and followup (does nothing until enabled). Then it drafts answers to new replies, and on
Mondays writes the insights report. If a live check fails, that platform is **paused**: its jobs are skipped
and it re-checks every day until the check passes (or `daily.py --resume <platform>`), since a site that
changed its pages is when a script misbehaves. Every run ends with a summary notification (today's DMs,
follow-ups, replies, bookings, answers waiting for you; also in `logs/summary-<date>.txt`). It also deletes
failure screenshots older than 14 days. Each job's output goes to `logs/<date>.log`. If a job stops on an account problem (logged out, blocked, a
checkpoint or chat PIN: exit code 2), that platform's later jobs are skipped, the others still run, and you get a
macOS notification. A crash or timeout is notified too. Turn a job off with `"enabled": false` in
`schedule.json`. The schedule runs only while you're logged in to the Mac, and a run missed during sleep runs on
wake. Set up and log in to each platform once (its README) before installing it, or that platform will notify a
failure every day.

