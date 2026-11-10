"""report/run.py - see how outreach is going away from the Mac: a daily email digest and a Google Sheet.

    python report/run.py email [--dry-run] [--weekly]   email today's digest (--dry-run prints it, sends nothing;
                                                        --weekly adds the weekly section on any day, not just Mondays)
    python report/run.py sheet [--dry-run]              sync the People and Daily tabs (--dry-run prints the rows)

Settings: report/config.json ("email" and "sheet"; "enabled" only decides whether daily.py runs them after its
summary, by hand they always run). The SMTP password goes in report/.env as SMTP_PASSWORD (or the environment).
Setup: report/README.md. Exit code 0 on success, 1 on any error (daily.py notifies you).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import digest  # noqa: E402
import sheet  # noqa: E402

CONFIG = HERE / "config.json"
ENV = HERE / ".env"
DEFAULTS = {"email": {"enabled": False, "to": [], "from": "", "smtp_host": "smtp.gmail.com", "smtp_port": 587,
                      "username": "", "max_replies": 20},
            "sheet": {"enabled": False, "spreadsheet_id": "", "credentials_file": "service_account.json"}}


def load_config() -> dict:
    cfg = json.loads(CONFIG.read_text()) if CONFIG.exists() else {}
    return {k: {**v, **(cfg.get(k) or {})} for k, v in DEFAULTS.items()}


def env(name: str) -> str:
    """From the environment, else report/.env (KEY=value lines; quotes optional)."""
    if os.environ.get(name):
        return os.environ[name]
    if ENV.exists():
        for line in ENV.read_text().splitlines():
            k, sep, v = line.strip().partition("=")
            if sep and k.strip().removeprefix("export ").strip() == name:
                return v.strip().strip("'\"")
    return ""


def cmd_email(cfg: dict, dry_run: bool, weekly: bool) -> int:
    c = cfg["email"]
    if isinstance(c.get("to"), str):
        c["to"] = [c["to"]]
    d = digest.gather(include_weekly=True if weekly else None, max_replies=int(c.get("max_replies", 20)))
    if dry_run:
        c = {**c, "to": c["to"] or ["(to)"], "from": c["from"] or "(from)"}
    missing = [k for k in ("to", "from") if not c.get(k)]
    if missing:
        print(f"[!] set email.{' and email.'.join(missing)} in report/config.json")
        return 1
    msg = digest.message(d, c)
    if dry_run:
        print(f"Subject: {msg['Subject']}\nFrom: {msg['From']}\nTo: {msg['To']}\n")
        print(digest.render_text(d))
        print("----- HTML part -----")
        print(digest.render_html(d))
        print("[dry run] nothing sent")
        return 0
    password = env("SMTP_PASSWORD")
    if not password:
        print("[!] no SMTP_PASSWORD in report/.env (for Gmail, an app password: see report/README.md)")
        return 1
    try:
        digest.send(msg, c, password)
    except Exception as exc:            # smtplib errors, socket errors, a bad login
        print(f"[!] sending failed: {type(exc).__name__}: {exc}")
        return 1
    print(f"[+] sent \"{msg['Subject']}\" to {len(c['to'])} recipient(s)")
    return 0


def cmd_sheet(cfg: dict, dry_run: bool, http=None) -> int:
    c = cfg["sheet"]
    people, daily = sheet.people_rows(), sheet.daily_rows()
    if dry_run:
        for title, header, rows in (("People", sheet.PEOPLE_HEADER, people), ("Daily", sheet.DAILY_HEADER, daily)):
            print(f"== {title} ({len(rows)} rows)")
            for r in [header, *rows]:
                print("\t".join(str(v) for v in r))
        print("[dry run] the sheet was not touched")
        return 0
    if not c.get("spreadsheet_id"):
        print("[!] set sheet.spreadsheet_id in report/config.json")
        return 1
    try:
        if http is None:
            creds = Path(c["credentials_file"]).expanduser()
            http = sheet.session(creds if creds.is_absolute() else HERE / creds)
        n = sheet.sync(sheet.Sheets(http, c["spreadsheet_id"]), people, daily)
    except Exception as exc:            # SheetError, network errors, a bad key
        print(f"[!] sheet sync failed: {type(exc).__name__}: {exc}")
        return 1
    print(f"[+] sheet synced: People {n['people']} rows, Daily {n['daily']} rows")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("what", choices=("email", "sheet"))
    ap.add_argument("--dry-run", action="store_true", help="print instead of sending / writing")
    ap.add_argument("--weekly", action="store_true", help="email: include the weekly section today")
    args = ap.parse_args(argv)
    try:
        cfg = load_config()
    except ValueError as exc:
        print(f"[!] report/config.json isn't valid JSON: {exc}")
        return 1
    if args.what == "email":
        return cmd_email(cfg, args.dry_run, args.weekly)
    return cmd_sheet(cfg, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
