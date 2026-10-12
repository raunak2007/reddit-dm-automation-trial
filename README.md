# Reddit DM Automation — Trial

Automates sending Reddit chat (DM) messages from a logged-in account to a
list of target usernames, with per-account daily caps, randomized delays
between sends, and a duplicate-contact log so the same person is never
messaged twice.

This is a **trial build**. It targets one account at a time. Multi-account
parallelism and automatic target scraping are not implemented yet.

---

## What it does

- Launches Chrome via `undetected-chromedriver` using an isolated, persistent
  profile per account (`accounts/<id>/chrome_profile/`).
- Drives Reddit's chat UI end-to-end: opens `/chat/`, clicks "Start new chat",
  types a recipient, selects the exact match, types a message, presses Enter,
  and confirms the message appears in the thread.
- Reads targets from `target_users.csv`, limits and delays from `config.json`,
  and writes every attempt to `contacted.csv`.

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
- Google Chrome installed, matching the version passed as `version_main` in
  the scripts (currently **154**).
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

# 4. Confirm your Chrome major version (see "Chrome version" below)
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --version
```
