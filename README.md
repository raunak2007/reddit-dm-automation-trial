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
- `run_loop.py` reads targets from `target_users.csv`, limits, delays and
  `subject_template` / `message_template` from `config.json`, builds a compose
  link per target, and writes every attempt to `contacted.csv`.
- `open_queue.py <queue.html>` walks a queue file's compose links. By default
  it opens each one and waits for you to click Send; with `--send` it clicks
  Send itself, within the same daily cap and delays. Progress is kept in
  `queue_state/<queue>.json`.

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
