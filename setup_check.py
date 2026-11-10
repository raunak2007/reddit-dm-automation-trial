"""setup_check.py - one checklist of what each platform still needs before it can run on its own.

    python setup_check.py           check everything from the files here (opens no browser, sends nothing)
    python setup_check.py --live    also run each platform's read-only live check (opens Chrome; types nothing)
    python setup_check.py --only facebook,inbox

Each line is  OK / TODO (it won't work, or isn't safe to turn on, until you do this) / NOTE (optional or
worth knowing), with the exact thing to do. Exit code 1 if anything is TODO, else 0.

What it looks at, per platform: Python packages and Chrome; the Claude key (.env); the Chrome profile you log in
to once; the sender account in config.json; the business details the messages use; where it searches; the
live-check settings and when a live check last passed (logs/livecheck.json, written by daily.py and by
--live); features switched on before they're safe (follow-ups, automatic comments, automatic answers need a
passing live check first); and for the whole setup: schedule installed, platforms paused, backups, email /
Sheet updates, booking import. It never prints a secret, only whether it's set.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
PYTHON = HERE / "venv" / "bin" / "python"
LIVECHECKS = HERE / "logs" / "livecheck.json"
PAUSED = HERE / "logs" / "paused.json"
PLIST = Path.home() / "Library" / "LaunchAgents" / "com.outreach.daily.plist"
LIVE_FRESH_DAYS = 8              # a weekly check plus a day's slack
PACKAGES = ("anthropic", "dotenv", "selenium", "undetected_chromedriver")
PLACEHOLDERS = ("example.com", "your ", "yourname", "<", "todo", "xxx")
SECTIONS = ("general", "reddit", "facebook", "instagram", "inbox", "results")


class Checklist:
    def __init__(self):
        self.items: list[tuple[str, str, str]] = []

    def add(self, section, level, text):
        self.items.append((section, level, text))

    def ok(self, section, text):
        self.add(section, "OK", text)

    def todo(self, section, text):
        self.add(section, "TODO", text)

    def note(self, section, text):
        self.add(section, "NOTE", text)

    def print(self, sections):
        for s in sections:
            mine = [i for i in self.items if i[0] == s]
            if not mine:
                continue
            todo = sum(1 for i in mine if i[1] == "TODO")
            print(f"\n{s.upper()}" + (f"  ({todo} to do)" if todo else "  (ready)"))
            for _, level, text in mine:
                print(f"  {level:<4}  {text}")
        n = sum(1 for i in self.items if i[1] == "TODO" and i[0] in sections)
        print(f"\n{n} thing(s) to do." if n else "\nEverything checked is ready.")
        return 1 if n else 0


# ----------------------------------------------------------------- helpers
def load(path: Path) -> dict:
    try:
        return json.loads(path.read_text()) if path.exists() else {}
    except ValueError as exc:
        return {"__error__": str(exc)}


def env_has(name: str, *files: Path) -> bool:
    """Whether a variable is set in the environment or in any of these .env files (value never read out)."""
    if os.environ.get(name, "").strip():
        return True
    for f in files:
        if f.exists():
            for line in f.read_text(errors="ignore").splitlines():
                k, _, v = line.partition("=")
                if k.strip().removeprefix("export ").strip() == name and v.strip().strip("'\""):
                    return True
    return False


def placeholder(value) -> bool:
    v = str(value or "").strip().lower()
    return not v or any(p in v for p in PLACEHOLDERS)


def profile_dir(platform: str, cfg: dict) -> Path:
    b = cfg.get("browser", {})
    if b.get("user_data_dir"):
        return Path(b["user_data_dir"]).expanduser()
    base = HERE if platform == "reddit" else HERE / platform
    return base / "accounts" / b.get("profile", "account_1") / "chrome_profile"


def livechecks() -> dict:
    return load(LIVECHECKS)


def record_livecheck(platform: str, code):
    data = livechecks()
    now = datetime.now().isoformat(timespec="seconds")
    entry = data.get(platform, {})
    entry.update(last_at=now, last_code=code)
    if code == 0:
        entry["passed_at"] = now
    data[platform] = entry
    LIVECHECKS.parent.mkdir(exist_ok=True)
    LIVECHECKS.write_text(json.dumps(data, indent=1) + "\n")


def live_passed(platform: str) -> datetime | None:
    at = livechecks().get(platform, {}).get("passed_at")
    try:
        when = datetime.fromisoformat(at) if at else None
    except ValueError:
        return None
    return when if when and datetime.now() - when <= timedelta(days=LIVE_FRESH_DAYS) else None


def gated(c, section, what, platform, setting):
    """A feature that is only safe once the live check passes."""
    when = live_passed(platform)
    if when:
        c.ok(section, f"{what} is on, and the live check passed {when:%Y-%m-%d}")
    else:
        c.todo(section, f"{what} is on ({setting}) but no live check has passed in the last {LIVE_FRESH_DAYS} days. "
                        f"Run: python setup_check.py --live --only {platform}  (or turn it off until it passes)")


def live_status(c, section, platform, settings_ok):
    entry = livechecks().get(platform, {})
    when = live_passed(platform)
    if when:
        c.ok(section, f"live check passed {when:%Y-%m-%d %H:%M}")
    elif entry.get("last_at"):
        c.todo(section, f"the last live check ({entry['last_at'][:16]}) failed with exit {entry.get('last_code')}: "
                        f"see the log, fix, then python setup_check.py --live --only {platform}")
    elif settings_ok:
        c.todo(section, f"no live check has run yet: python setup_check.py --live --only {platform}")
    else:
        c.note(section, "no live check has run yet (finish the TODOs above first)")
    if platform in load(PAUSED):
        c.todo(section, f"PAUSED by daily.py: {load(PAUSED)[platform].get('why')}. It resumes when a live check "
                        f"passes, or: python daily.py --resume {platform}")


def business(c, section, cfg, tags_itself=False):
    b = cfg.get("business", {})
    bad = [k for k in ("name", "website", "booking_link", "sender_first_name") if placeholder(b.get(k))]
    if bad:
        c.todo(section, f"fill in config.json business: {', '.join(bad)}")
    else:
        c.ok(section, f"business details set (booking link {b['booking_link']})")
        if "utm_source" not in b["booking_link"] and not tags_itself:
            c.note(section, "the booking link has no utm_source, so bookings can't be traced to this platform "
                            "(python bookings/run.py tag <link> <platform>)")


def key(c, section, where: Path, *more: Path):
    if env_has("ANTHROPIC_API_KEY", where / ".env", *more):
        c.ok(section, "Claude key set")
    else:
        c.todo(section, f"put ANTHROPIC_API_KEY=... in {where.relative_to(HERE) / '.env' if where != HERE else '.env'} "
                        f"(Claude scores leads and drafts messages)")


def profile(c, section, platform, cfg, login_cmd):
    d = profile_dir(platform, cfg)
    if d.is_dir() and any(d.iterdir()):
        c.ok(section, f"Chrome profile exists ({d.relative_to(HERE) if d.is_relative_to(HERE) else d}); the live "
                      f"check confirms it's still logged in")
    else:
        c.todo(section, f"log in once: {login_cmd}")


# ----------------------------------------------------------------- sections
def general(c):
    s = "general"
    if not PYTHON.exists():
        c.todo(s, "create the venv: python3.11 -m venv venv")
    missing = [p for p in PACKAGES if importlib.util.find_spec(p) is None]
    if missing:
        c.todo(s, f"install packages ({', '.join(missing)} missing): venv/bin/pip install -r facebook/requirements.txt "
                  f"-r instagram/requirements.txt -r reddit/requirements.txt")
    else:
        c.ok(s, "Python packages installed")
    try:
        sys.path.insert(0, str(HERE))
        from chrome_version import chrome_major
        c.ok(s, f"Google Chrome {chrome_major()} found")
    except Exception as exc:
        c.todo(s, f"Google Chrome not found ({type(exc).__name__}): install it")
    if sys.platform == "darwin":
        if PLIST.exists():
            c.ok(s, "daily schedule installed (python schedule.py status)")
        else:
            c.note(s, "daily schedule not installed: once the TODOs below are done, python schedule.py install --at 10:00")
    try:
        import backup
        ds = backup.days()
        problems = backup.check()
        for p in problems:
            c.todo(s, f"record {p['path']} {p['problem']}: python backup.py restore <date> {p['path']} "
                      f"(or python backup.py if that change was on purpose)")
        if ds and not problems:
            c.ok(s, f"backups: {len(ds)} days, newest {ds[-1].name}")
        elif not ds:
            c.note(s, "no backups yet (daily.py makes one every run; or python backup.py)")
    except Exception as exc:
        c.note(s, f"couldn't check backups ({exc})")


def reddit(c):
    s, root, cfg = "reddit", load(HERE / "config.json"), load(HERE / "reddit" / "config.json")
    if "__error__" in root or "__error__" in cfg:
        c.todo(s, f"a config file doesn't parse: {root.get('__error__') or cfg.get('__error__')}")
        return
    profile(c, s, "reddit", cfg, "python reddit/run.py login")
    if root.get("sender_username"):
        c.ok(s, f"sends as u/{root['sender_username']}")
    else:
        c.todo(s, 'set "sender_username" in config.json (the Reddit account that sends; python reddit/run.py send '
                  'prints which account the profile is logged in as)')
    key(c, s, HERE / "reddit")
    business(c, s, cfg)
    subs = cfg.get("search", {}).get("subreddits") or []
    (c.ok if subs else c.todo)(s, f"{len(subs)} subreddits to search" if subs else "add search.subreddits to reddit/config.json")
    live_status(c, s, "reddit", bool(root.get("sender_username")))
    if cfg.get("followup", {}).get("enabled"):
        gated(c, s, "follow-ups", "reddit", "followup.enabled")
    else:
        c.note(s, "follow-ups are off (followup.enabled); turn on after a live check passes")


def platform_checks(c, platform):
    s, cfg = platform, load(HERE / platform / "config.json")
    if "__error__" in cfg:
        c.todo(s, f"{platform}/config.json doesn't parse: {cfg['__error__']}")
        return
    out, live = cfg.get("outreach", {}), cfg.get("livecheck", {})
    if platform == "instagram" and cfg.get("mode", "browser") == "api":
        return instagram_api(c, cfg)
    profile(c, s, platform, cfg, f"cd {platform} && python run.py login")
    if str(out.get("sender_user_id") or "").strip():
        c.ok(s, f"sends as user id {out['sender_user_id']}")
    else:
        c.todo(s, f"set outreach.sender_user_id in {platform}/config.json (cd {platform} && python run.py send prints "
                  f"the id the profile is logged in as)")
    key(c, s, HERE / platform)
    business(c, s, cfg)
    if platform == "facebook":
        kw, groups = cfg.get("search", {}).get("keywords") or [], cfg.get("search", {}).get("groups") or []
        (c.ok if kw else c.todo)(s, f"{len(kw)} search keywords" if kw else "add search.keywords to facebook/config.json")
        if not groups:
            c.note(s, "no search.groups: groups you've joined are usually where the best leads are")
        need = [k for k in ("chat", "name") if not str(live.get(k) or "").strip()]
        if need:
            c.todo(s, "fill in livecheck.chat (an author_key whose chat has a reply from them) and livecheck.name "
                      "(their name as Messenger shows it): the live check needs a chat to read who wrote what")
        else:
            c.ok(s, "live-check chat set")
        settings_ok = not need
        if out.get("auto_comment"):
            if not str(live.get("post") or "").strip():
                c.todo(s, "outreach.auto_comment is on but livecheck.post is empty: the live check can't confirm it finds "
                          "the comment box. Set livecheck.post / post_author / post_text, or turn auto_comment off")
            gated(c, s, "automatic comments", platform, "outreach.auto_comment")
        else:
            c.note(s, "automatic comments are off (outreach.auto_comment)")
    else:
        ig = cfg.get("instagram", {})
        (c.ok if ig.get("my_username") else c.todo)(
            s, f"reads posts of @{ig['my_username']}" if ig.get("my_username") else "set instagram.my_username in instagram/config.json")
        if not str(live.get("profile") or "").strip():
            c.todo(s, "fill in livecheck.profile (any public profile username) so the live check can look at a Follow "
                      "button; livecheck.chat (a username you've messaged) also checks a chat")
            settings_ok = False
        else:
            c.ok(s, "live-check profile set" + ("" if live.get("chat") else " (add livecheck.chat to check a chat too)"))
            settings_ok = True
    live_status(c, s, platform, settings_ok)
    fu = out.get("followup") or cfg.get("followup") or {}
    if fu.get("enabled"):
        gated(c, s, "follow-ups", platform, "outreach.followup.enabled")
    else:
        c.note(s, "follow-ups are off (outreach.followup.enabled); turn on after a live check passes")


def instagram_api(c, cfg):
    """Instagram in API mode (config "mode": "api"): the official API instead of a browser."""
    s, out = "instagram", cfg.get("outreach", {})
    c.ok(s, "API mode: comments, first DMs, answers and replies go through Meta's official API (no browser)")
    stored = HERE / "instagram" / "data" / "api_token.json"
    if env_has("IG_ACCESS_TOKEN", HERE / "instagram" / ".env") or stored.exists():
        c.ok(s, "Instagram access token set" + (" (refreshed automatically)" if stored.exists() else ""))
    else:
        c.todo(s, "put IG_ACCESS_TOKEN=... in instagram/.env: a long-lived token from your Meta app (instagram/README.md, "
                  "API mode). The account must be Business or Creator: a personal account can't use the API")
    ig = cfg.get("instagram", {})
    (c.ok if ig.get("my_username") else c.todo)(
        s, f"API account @{ig['my_username']} (must be the professional account the token belongs to)"
        if ig.get("my_username") else "set instagram.my_username to the professional account's handle")
    if str(out.get("sender_user_id") or "").strip() == "" and not cfg.get("api", {}).get("ig_user_id"):
        c.note(s, "the account id is read from the token (/me); the live check confirms it's the right, professional account")
    key(c, s, HERE / "instagram")
    business(c, s, cfg)
    live_status(c, s, "instagram", True)
    c.note(s, "API mode limits: first DMs only within 7 days of the comment, one per comment; answers only within 24h "
              "of their last message (the inbox shows 'answer by'); no follow-ups")


def inbox(c):
    s, cfg = "inbox", load(HERE / "inbox" / "config.json")
    if env_has("ANTHROPIC_API_KEY", HERE / "inbox" / ".env", HERE / ".env", *(HERE / p / ".env" for p in ("facebook", "instagram", "reddit"))):
        c.ok(s, "Claude key found for drafting answers")
    else:
        c.todo(s, "put ANTHROPIC_API_KEY=... in inbox/.env (or any platform's .env) to draft answers")
    business(c, s, cfg, tags_itself=True)            # draft.py tags the link per platform and person
    auto = cfg.get("auto_approve", {})
    if auto.get("enabled"):
        c.note(s, f"automatic answers are ON for intents {auto.get('intents')} (up to {auto.get('max_per_day')}/day, "
                  f"only after {auto.get('min_reviewed')} drafts you reviewed): python inbox/run.py stats shows the gate")
    else:
        c.note(s, "answers wait for your approval at http://127.0.0.1:8767 (python inbox/run.py)")
    try:
        sys.path.insert(0, str(HERE))
        import outcomes
        w = outcomes.answers_waiting()
        if w["to_review"] or w["uncertain"]:
            c.note(s, f"{w['to_review']} answers to review, {w['uncertain']} uncertain: python inbox/run.py")
    except Exception:
        pass


def results(c):
    s = "results"
    sched = load(HERE / "schedule.json")
    jobs = {j["name"]: j for j in sched.get("jobs", [])}
    if jobs.get("bookings", {}).get("enabled"):
        (c.ok if env_has("CALENDLY_TOKEN", HERE / "bookings" / ".env") else c.todo)(
            s, "Calendly import on, token set" if env_has("CALENDLY_TOKEN", HERE / "bookings" / ".env")
            else "the bookings job is on but bookings/.env has no CALENDLY_TOKEN")
    else:
        c.note(s, "booking import is off: python bookings/run.py import <export.csv> now and then, or with Calendly: "
                  "CALENDLY_TOKEN in bookings/.env and the bookings job enabled in schedule.json")
    rep = load(HERE / "report" / "config.json")
    if rep:
        email, sheet = rep.get("email", {}), rep.get("sheet", {})
        if email.get("enabled"):
            missing = [k for k in ("to", "from", "smtp_host", "username") if not email.get(k)]
            if not env_has("SMTP_PASSWORD", HERE / "report" / ".env"):
                missing.append("SMTP_PASSWORD in report/.env")
            (c.todo if missing else c.ok)(s, f"email digest: missing {', '.join(missing)}" if missing
                                          else f"email digest to {', '.join(email['to']) if isinstance(email['to'], list) else email['to']}")
        else:
            c.note(s, "email digest off (report/config.json email.enabled)")
        if sheet.get("enabled"):
            cred = HERE / "report" / str(sheet.get("credentials_file") or "")
            problems = [p for p, bad in (("spreadsheet_id", not sheet.get("spreadsheet_id")),
                                         (f"credentials file {cred.name}", not cred.is_file()),
                                         ("the google-auth package (venv/bin/pip install -r report/requirements.txt)",
                                          importlib.util.find_spec("google") is None
                                          or importlib.util.find_spec("google.auth") is None)) if bad]
            (c.todo if problems else c.ok)(s, f"Google Sheet sync: missing {', '.join(problems)}" if problems
                                           else "Google Sheet sync configured")
        else:
            c.note(s, "Google Sheet sync off (report/config.json sheet.enabled)")


def run_live(platform) -> int:
    cmd = [str(PYTHON), "reddit/run.py" if platform == "reddit" else "run.py", "livecheck"]
    cwd = HERE if platform == "reddit" else HERE / platform
    print(f"[+] {platform}: running the read-only live check (Chrome opens; nothing is typed or sent)...")
    try:
        code = subprocess.run(cmd, cwd=cwd, timeout=20 * 60).returncode
    except subprocess.TimeoutExpired:
        code = None
    record_livecheck(platform, code)
    print(f"    {platform}: {'passed' if code == 0 else f'failed (exit {code})'}")
    return code


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--live", action="store_true", help="also run each platform's read-only live check (opens Chrome)")
    ap.add_argument("--only", help=f"comma-separated: {', '.join(SECTIONS)}")
    args = ap.parse_args()
    sections = [x.strip() for x in args.only.split(",")] if args.only else list(SECTIONS)
    if args.live:
        for platform in ("reddit", "facebook", "instagram"):
            if platform in sections:
                run_live(platform)
    c = Checklist()
    checks = {"general": general, "reddit": reddit, "facebook": lambda c: platform_checks(c, "facebook"),
              "instagram": lambda c: platform_checks(c, "instagram"), "inbox": inbox, "results": results}
    for name in sections:
        if name not in checks:
            ap.error(f"unknown section {name!r}; choose from {', '.join(SECTIONS)}")
        checks[name](c)
    return c.print(sections)


if __name__ == "__main__":
    sys.exit(main())
