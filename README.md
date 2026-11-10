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

## Running everything every day

```bash
venv/bin/python daily.py --list        # the jobs in schedule.json (written with defaults on first use)
venv/bin/python daily.py               # run them all now, one after another
venv/bin/python schedule.py install --at 10:00   # run daily.py every day at 10:00 (launchd)
venv/bin/python schedule.py status | run-now | uninstall
```

`daily.py` runs, in order: Reddit (find + send, then replies), Facebook (scrape + score + send, and comments if
`outreach.auto_comment` is on, then replies) and Instagram (find your recent posts + read comments + send, then
replies). Each job's output goes to `logs/<date>.log`. If a job stops on an account problem (logged out, blocked, a
checkpoint or chat PIN: exit code 2), that platform's later jobs are skipped, the others still run, and you get a
macOS notification. A crash or timeout is notified too. Turn a job off with `"enabled": false` in
`schedule.json`. The schedule runs only while you're logged in to the Mac, and a run missed during sleep runs on
wake. Set up and log in to each platform once (its README) before installing it, or that platform will notify a
failure every day.

