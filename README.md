# Reddit DM Automation — Trial

Automates sending Reddit private messages from a logged-in account to a
list of target usernames, with per-account daily caps, randomized delays
between sends, and a duplicate-contact log so the same person is never
messaged twice.

This is a **trial build**. It targets one account at a time. Multi-account
parallelism and automatic target scraping are not implemented yet.

---

## What it does

- Launches Chrome via `undetected-chromedriver` using an isolated, persistent
  profile per account (`accounts/<id>/chrome_profile/`).
- Sends through Reddit's compose form: opens a
  `https://www.reddit.com/message/compose/?to=…&subject=…&message=…` link,
  checks that Reddit prefilled To / Title / Message (retyping any field that
  doesn't match), clicks Send, and waits for Reddit to accept it.
  (`send_dm.send_compose`; the old `/chat/` flow is kept as `send_chat_dm`.)
- `run_loop.py <queue.html>` sends each queue entry's own subject and
  personalized message (taken from its `<pre>` body / compose link). Without a
  queue it falls back to `target_users.csv` plus `config.json`'s
  `subject_template` / `message_template`.
- `open_queue.py <queue.html>` walks a queue file's compose links. By default
  it opens each one and waits for you to click Send; with `--send` it sends
  them itself. Your place in the queue is kept in `queue_state/<queue>.json`.
- Both scripts share one ledger, `contacted.csv` (`ledger.py`): one row per
  attempt with its outcome. The daily cap counts today's sends across every
  run of either script. Each attempt ends as one of:
  - `sent`
  - `recipient`: Reddit won't deliver to this user (can't be messaged, no
    such user). Never retried.
  - `invalid`: bad username, subject or body. Never retried.
  - `error`: anything unexplained (timeouts, missing buttons). Retried on
    later runs, up to `max_attempts_per_recipient` (default 3).
  - `account`: Reddit is refusing the account itself (rate limit, suspension,
    logged out). The run stops at once with exit code 2, and the recipient
    is not charged an attempt.

  Three `error`s in a row also stop the run with exit code 2, since that
  usually means the account or the page changed, not the recipients.
- "Already sent" also covers messages sent outside these scripts:
  - At the start of every run, both scripts read the logged-in account's Reddit
    sent messages (`old.reddit.com/message/sent/.json`) and record everyone
    there. If that can't be read, nothing is sent.
  - Messages sent from **another account** are invisible to that check. In a
    browser logged in as that account, save the same URL as JSON, then run
    `python ledger.py import-sent page1.json [page2.json …]`.
  - A queue page's Sent checkmarks live in the localStorage of the browser that
    ticked them. Before a queue can be sent, import them once: run the console
    line that `python open_queue.py <queue.html> --list` prints in the original
    browser, then `pbpaste | python ledger.py import-checkmarks <queue.html> -`.
    Pass `--no-checkmarks` to skip this.

## What it does not do

- It does not create or buy Reddit accounts.
- It does not scrape new targets automatically.
- It does not run multiple accounts in parallel.
- It does not bypass Reddit's rate limits or spam checks. If Reddit flags the
  account, the script will simply fail at some step — it has no evasion logic.

**Use only on accounts you control, and only to message people who expect to
hear from you.** Reddit's terms prohibit unsolicited bulk messaging.

---

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
