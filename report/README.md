# Updates away from the Mac (`report/`)

`daily.py` ends with a macOS notification. This folder sends the same news somewhere you can see from your
phone: a short **email digest** and a **Google Sheet**. Both only read files already on disk (through
`outcomes.py`, `logs/` and `insights/reports/`). Neither ever opens a site or messages anyone.

```bash
venv/bin/python report/run.py email --dry-run   # print the digest (plain text + HTML), send nothing
venv/bin/python report/run.py email             # send it
venv/bin/python report/run.py email --weekly    # include the weekly section on a day that isn't Monday
venv/bin/python report/run.py sheet --dry-run   # print the People and Daily rows, touch nothing
venv/bin/python report/run.py sheet             # sync the sheet
venv/bin/python -m unittest discover report/tests   # offline tests (fake SMTP, fake Sheets API)
```

Exit code 0 on success, 1 on any problem (with a one-line reason).

## Running it every day

In `report/config.json`, set `"enabled": true` under `email` and/or `sheet`. `daily.py` then runs
`report/run.py email` and `report/run.py sheet` right after its summary. If one fails you get a macOS
notification ("Outreach: report email failed") and its output is in `logs/<date>.log`, but the run's exit code
doesn't change. `daily.py --only reddit` skips the reports; add `report` (`--only reddit,report`) to keep them.
`enabled` only matters to `daily.py`. Running `report/run.py` by hand always works.

## What the email has

- The subject line: `Outreach <date>: N DMs, N follow-ups, N new replies`, plus `[needs you]` when a platform is
  paused or stopped on an account problem.
- Today and all-time numbers per platform (DMs, follow-ups, comments, replies, booked, and contacted / replied /
  reply rate / answered / booked).
- New replies today: platform, first name (Facebook) or username (Reddit, Instagram), the time, a snippet, and a
  link to the chat. Snippets are cut to 120 characters, with email addresses and phone numbers replaced by
  `[email]` / `[phone]`. No full names or whole messages, so the email stays light if it's forwarded.
  `email.max_replies` (20) caps the list.
- Answers waiting for you in the inbox (to review, uncertain, approved but not sent yet).
- Anything paused (`logs/paused.json`) or stopped, failed or skipped in today's last `daily.py` run (`logs/<date>.log`).
- **Mondays:** the newest `insights/reports/*.md` headline line and its per-platform numbers.

## Email setup (Gmail)

1. Turn on 2-Step Verification for the Google account that will send the mail (myaccount.google.com, then
   Security). Gmail only issues app passwords once it's on.
2. Create an app password at https://myaccount.google.com/apppasswords. Name it "outreach digest" and copy the
   16-character password.
3. Put it in `report/.env` (gitignored):

   ```
   SMTP_PASSWORD=abcd efgh ijkl mnop
   ```

4. Fill in `report/config.json`:

   | key | meaning |
   |---|---|
   | `email.enabled` | `true` to send it after every `daily.py` run (default `false`) |
   | `email.to` | list of addresses to send to |
   | `email.from` | the sender address (for Gmail, the account itself) |
   | `email.smtp_host` / `email.smtp_port` | `smtp.gmail.com` / `587` (STARTTLS) |
   | `email.username` | the SMTP login (for Gmail, the account's address; defaults to `from`) |
   | `email.max_replies` | most replies listed (20) |

5. `venv/bin/python report/run.py email --dry-run`, then without `--dry-run` to check one arrives.

Other providers work too: put their SMTP host, a STARTTLS port (usually 587), username and password in the
same places.

## Google Sheet setup (service account)

1. Install the one extra dependency (google-auth, which signs in as the service account; the requests library it
   uses is already in the venv):

   ```bash
   venv/bin/pip install -r report/requirements.txt
   ```

2. In the Google Cloud console (https://console.cloud.google.com), create or pick a project, then go to **APIs &
   Services > Library** and enable the **Google Sheets API**.
3. Go to **IAM & Admin > Service accounts**, then **Create service account** (any name, e.g. `outreach-sheet`, no
   roles needed). Open it, go to **Keys > Add key > Create new key > JSON**, and save the downloaded file as
   `report/service_account.json`. Every `*.json` in this folder except `config.json` is gitignored. Keep the
   file private, because it's a login.
4. Create a Google Sheet (or use an existing one). Click **Share** and add the service account's email (the
   `client_email` in the JSON, like `outreach-sheet@<project>.iam.gserviceaccount.com`) as an **Editor**. The
   service account can only see sheets shared with it.
5. Copy the spreadsheet ID from the sheet's URL, `https://docs.google.com/spreadsheets/d/<ID>/edit`, into
   `report/config.json`:

   | key | meaning |
   |---|---|
   | `sheet.enabled` | `true` to sync after every `daily.py` run (default `false`) |
   | `sheet.spreadsheet_id` | the `<ID>` above |
   | `sheet.credentials_file` | the service-account JSON, relative to `report/` (default `service_account.json`) or absolute |

6. `venv/bin/python report/run.py sheet --dry-run`, then without `--dry-run`. The tabs are created if missing.

A 403 or 404 almost always means the sheet isn't shared with the service account's email, or the Sheets API
isn't enabled for its project.

### What the sheet has

- **People** (rewritten in full each sync): platform, person, name, first DM, follow-up, replied at, replies,
  answered, booked, score, source, last status. One row per person contacted (DM, follow-up or comment). `name`
  is a first name for Facebook and the handle elsewhere. Held sends that never went out are left out.
- **Daily** (upserted by date + platform): date, platform, DMs, follow-ups, comments, replies, booked, contacted
  to date, replied to date, reply rate to date. Every day with activity is recomputed from the records, and a
  row for each platform is added for today. Rows for other days that are already in the sheet are kept, so
  history survives a record being cleaned up.

Don't add columns to these two tabs; each sync rewrites them. To chart or add notes, use another tab that
refers to them (`=People!A:L`).

## Files

- `run.py` is the command line. `digest.py` builds and sends the email (stdlib `smtplib`). `sheet.py` handles
  the rows and the Sheets REST calls (google-auth's `AuthorizedSession`, imported only for a real sync).
- `config.json` holds the settings, `.env` holds `SMTP_PASSWORD`, and `service_account.json` is the Google key
  (the last two are gitignored).
- `tests/test_report.py` is offline: temp data files, a fake `smtplib.SMTP`, a fake Sheets HTTP session.
