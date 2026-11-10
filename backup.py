"""backup.py - nightly copies of the records that stop anyone being messaged twice, kept for 30 days.

    python backup.py                 copy today's records into backups/<date>/ (daily.py does this every run)
    python backup.py list            the backups there are, and what's in the newest
    python backup.py check           compare the live records with the newest backup (shrunk / missing / unreadable)
    python backup.py restore <date> [path ...]   put files back from a backup (the current ones are kept as *.before-restore)

What's copied (when it exists), keeping its path under the repo:
    contacted.csv, queue_state/*.json                       Reddit's ledger (root scripts and reddit/)
    {reddit,facebook,instagram}/data/*.csv, *.json          sends, replies, follow-ups, leads, posts read
    inbox/data/answers.json, bookings/data/bookings.csv     answers and bookings
None of these are in git (they hold people's usernames and messages), so this folder is the only other copy:
backups/ is gitignored and stays on this Mac. A copy is made at most once per day per file content (re-running
the same day refreshes it). Backups older than KEEP_DAYS are deleted.

check() is the corruption guard daily.py runs before anything sends: a record that has fewer rows than in the
newest backup, is missing, or no longer parses is reported (and notified), because a send run working from a
damaged contacted.csv could message people twice. daily.py pauses sending for that platform until it's fixed:
restore the file, or, if the change was on purpose, run python backup.py to make today's copy the new baseline.
"""
from __future__ import annotations

import argparse
import csv
import json
import shutil
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
BACKUPS = HERE / "backups"
KEEP_DAYS = 30
PATTERNS = ("contacted.csv", "queue_state/*.json", "reddit/data/*.csv", "reddit/data/*.json",
            "facebook/data/*.csv", "facebook/data/*.json", "instagram/data/*.csv", "instagram/data/*.json",
            "inbox/data/answers.json", "bookings/data/bookings.csv")
# which platform's sending a damaged record should stop
OWNER = {"contacted.csv": "reddit", "queue_state": "reddit", "reddit": "reddit", "facebook": "facebook",
         "instagram": "instagram", "inbox": "inbox", "bookings": "bookings"}


def records() -> list[Path]:
    out = []
    for pattern in PATTERNS:
        out += [p for p in sorted(HERE.glob(pattern)) if p.is_file() and not p.name.endswith((".tmp", ".lock"))]
    return out


def rel(path: Path) -> str:
    return str(path.relative_to(HERE))


def owner(relpath: str) -> str:
    return OWNER.get(relpath.split("/", 1)[0], "other")


def rows(path: Path) -> int:
    """Records in a file: CSV data rows, or JSON list/dict entries. ValueError if it doesn't parse."""
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".csv":
        return sum(1 for _ in csv.DictReader(text.splitlines()))
    data = json.loads(text or "null")
    return len(data) if isinstance(data, (list, dict)) else 0


def days() -> list[Path]:
    return sorted(d for d in BACKUPS.glob("20??-??-??") if d.is_dir()) if BACKUPS.exists() else []


def backup(today: str | None = None, skip: set = frozenset()) -> tuple[Path, int, int]:
    """Copy every record into backups/<today>/; drop backups older than KEEP_DAYS. Returns (folder, copied, pruned).
    skip: paths (as in check()) not to copy, so a damaged record never becomes the baseline; daily.py passes
    check()'s. A record that no longer parses is never copied either."""
    today = today or date.today().isoformat()
    dest = BACKUPS / today
    copied = 0
    for path in records():
        if rel(path) in skip:
            print(f"[!] {rel(path)} looks damaged; keeping the last good backup of it")
            continue
        try:
            rows(path)
        except (ValueError, UnicodeDecodeError):
            print(f"[!] {rel(path)} doesn't parse; not backing it up over a good copy")
            continue
        target = dest / rel(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        copied += 1
    cutoff = (date.fromisoformat(today) - timedelta(days=KEEP_DAYS)).isoformat()
    pruned = 0
    for d in days():
        if d.name < cutoff:
            shutil.rmtree(d)
            pruned += 1
    return dest, copied, pruned


def check() -> list[dict]:
    """Problems with the live records against the newest backup: [{path, platform, problem}]. Growing is normal;
    shrinking, vanishing or not parsing isn't. The newest backup is the baseline, so `python backup.py` after a
    change you meant accepts it."""
    ds = days()
    if not ds:
        return []
    base = ds[-1]
    problems = []
    seen = {}
    for d in ds:                                    # each file's newest copy (a skipped file keeps an older one)
        for saved in d.rglob("*"):
            if saved.is_file():
                seen[str(saved.relative_to(d))] = (d, saved)
    for relpath, (base, saved) in sorted(seen.items()):
        live = HERE / relpath
        try:
            before = rows(saved)
        except (ValueError, UnicodeDecodeError):
            continue
        if not live.exists():
            if before:
                problems.append({"path": relpath, "platform": owner(relpath),
                                 "problem": f"missing (had {before} records in the {base.name} backup)"})
            continue
        try:
            now = rows(live)
        except (ValueError, UnicodeDecodeError) as exc:
            problems.append({"path": relpath, "platform": owner(relpath), "problem": f"doesn't parse: {exc}"})
            continue
        # leads/posts files can legitimately be pruned; the send records can't
        shrink_matters = relpath.endswith(("contacted.csv", "followups.csv", "replies.csv", "answers.json",
                                           "bookings.csv"))
        if shrink_matters and now < before:
            problems.append({"path": relpath, "platform": owner(relpath),
                             "problem": f"shrank from {before} to {now} records since the {base.name} backup"})
    return problems


def restore(day: str, paths: list[str]) -> list[str]:
    src = BACKUPS / day
    if not src.is_dir():
        raise SystemExit(f"No backup for {day}: python backup.py list")
    chosen = paths or [str(p.relative_to(src)) for p in src.rglob("*") if p.is_file()]
    done = []
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    for relpath in chosen:
        saved = src / relpath
        if not saved.is_file():
            print(f"[!] {relpath} isn't in the {day} backup")
            continue
        live = HERE / relpath
        if live.exists():
            shutil.copy2(live, live.with_name(f"{live.name}.before-restore-{stamp}"))
        live.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(saved, live)
        done.append(relpath)
    return done


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", nargs="?", default="backup", choices=["backup", "list", "check", "restore"])
    ap.add_argument("day", nargs="?", help="restore: the backup date (YYYY-MM-DD)")
    ap.add_argument("paths", nargs="*", help="restore: only these files (paths as in the backup)")
    args = ap.parse_args()
    if args.cmd == "backup":
        dest, copied, pruned = backup()
        print(f"[+] {copied} records copied to {dest}" + (f"; {pruned} old backups deleted" if pruned else ""))
    elif args.cmd == "list":
        ds = days()
        print(f"{len(ds)} backups in {BACKUPS}" + (f", {ds[0].name} .. {ds[-1].name}" if ds else ""))
        if ds:
            for p in sorted(x for x in ds[-1].rglob("*") if x.is_file()):
                try:
                    n = rows(p)
                except (ValueError, UnicodeDecodeError):
                    n = "?"
                print(f"  {str(p.relative_to(ds[-1])):<44} {n} records")
    elif args.cmd == "check":
        problems = check()
        for p in problems:
            print(f"[!] {p['path']} ({p['platform']}): {p['problem']}")
        print("[+] records look fine against the newest backup" if not problems else
              "Fix: python backup.py restore <date> <path>, or if the change was on purpose: python backup.py")
        return 1 if problems else 0
    else:
        if not args.day:
            ap.error("restore needs a date: python backup.py list")
        for relpath in restore(args.day, args.paths):
            print(f"[+] restored {relpath} from {args.day}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
