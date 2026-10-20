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

## Instagram commenter outreach (`instagram/`)

`instagram/` reads everyone who commented on an Instagram post or reel. Claude picks out the ones asking
about admissions and writes each a short, casual DM. You approve DMs on a local review page, and
`instagram/send.py` sends the approved ones. It follows the same rules as the Reddit scripts here: sender
check, one send per person, confirmation after sending, held uncertain sends, a daily cap, and stops on
account-wide problems. It shares this folder's `chrome_version.py` and `send_dm.py` helpers. Setup and
use: [instagram/README.md](instagram/README.md).
