"""daily.py - run every platform's outreach once, one after another, the way you would by hand.

    python daily.py              run today's jobs from schedule.json
    python daily.py --only reddit,instagram
    python daily.py --list       show the jobs (and which run today) and stop
    python daily.py --dry-run    the same as --list
    python daily.py --resume facebook     un-pause a platform without waiting for its live check to pass

schedule.py installs this as a daily launchd job; it is also fine to run it yourself. Each job is a command
run in its own folder with this repo's venv Python (so each folder's .env and config.json apply), one at a
time, since they drive Chrome and two at once would fight over a profile. Output goes to logs/<date>.log.

Jobs with "weekly": "<day>" run only on that weekday. A job with "role": "livecheck" is that platform's
read-only check against the real site (it types and sends nothing). It runs before the platform's other jobs
on its weekly day, and every day while the platform is paused. If it fails, the platform is paused (logs/paused.json):
its jobs are skipped, every day, until a live check passes again or you --resume it, because a site that
changed its pages is when a script misbehaves.

A job that exits 2 stopped on something account-wide (logged out, blocked, a checkpoint or PIN prompt): the
rest of that platform's jobs are skipped today, the other platforms still run, and you get a macOS
notification, since only you can fix those. Any other failure (a crash, a timeout) is notified too. At the
end: a summary notification (outcomes.summary(): today's DMs, follow-ups, replies, bookings, answers waiting
for you), also written to logs/summary-<date>.txt, and failure screenshots / page dumps older than
cleanup_days are deleted.
Exit code: 2 if any job stopped on an account problem or a platform is paused, 1 if any failed otherwise, else 0.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import date, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG = HERE / "schedule.json"
LOGS = HERE / "logs"
PAUSED = LOGS / "paused.json"
PYTHON = HERE / "venv" / "bin" / "python"
NEEDS_YOU = 2                       # exit code the platforms use for "stopped on an account-wide problem"
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
CLEANUP_GLOBS = ("step*_failed.png", "step*_failed_dom.txt", "debug/**/*.png", "debug/**/*.txt", "debug/**/*.html")
CLEANUP_SKIP = ("venv", "applytwin", "accounts", ".git", "node_modules")


def _platform(name, d, weekly="monday"):
    run = (lambda *a: [f"{name}/run.py", *a]) if d == "." else (lambda *a: ["run.py", *a])
    return [
        {"name": name, "dir": d, "run": run("livecheck"), "role": "livecheck", "weekly": weekly, "enabled": True},
        {"name": name, "dir": d, "run": run(), "enabled": True},
        {"name": name, "dir": d, "run": run("replies"), "enabled": True},
        {"name": name, "dir": d, "run": run("answer"), "enabled": True},
        {"name": name, "dir": d, "run": run("followup"), "enabled": True},
    ]


DEFAULT = {
    "time": "10:00",
    "timeout_minutes": 120,
    "cleanup_days": 14,
    "jobs": [
        *_platform("reddit", "."),
        *_platform("facebook", "facebook"),
        *_platform("instagram", "instagram"),
        {"name": "inbox", "dir": ".", "run": ["inbox/run.py", "draft"], "enabled": True},
        {"name": "bookings", "dir": ".", "run": ["bookings/run.py", "calendly"], "enabled": False},
        {"name": "insights", "dir": ".", "run": ["insights/run.py"], "weekly": "monday", "enabled": True},
    ],
}


def load():
    """schedule.json, written with the defaults the first time."""
    if not CONFIG.exists():
        CONFIG.write_text(json.dumps(DEFAULT, indent=2) + "\n")
    return json.loads(CONFIG.read_text())


def paused() -> dict:
    try:
        return json.loads(PAUSED.read_text()) if PAUSED.exists() else {}
    except ValueError:
        return {}


def set_paused(name, why=None):
    state = paused()
    if why:
        state[name] = {"since": datetime.now().isoformat(timespec="seconds"), "why": why}
    else:
        state.pop(name, None)
    LOGS.mkdir(exist_ok=True)
    PAUSED.write_text(json.dumps(state, indent=1) + "\n")


def notify(title, text):
    """A macOS notification; on anything else (or if it fails) just print."""
    print(f"[notify] {title}: {text}")
    if sys.platform == "darwin":
        script = f"display notification {json.dumps(text[:240])} with title {json.dumps(title)}"
        subprocess.run(["osascript", "-e", script], check=False, capture_output=True)


def label(job):
    return f"{job['name']}: {' '.join(job['run'])}"


def due(job, today: str, is_paused: bool) -> bool:
    """Whether this job runs today. A paused platform's live check runs every day."""
    if not job.get("enabled", True):
        return False
    if job.get("role") == "livecheck" and is_paused:
        return True
    return not job.get("weekly") or job["weekly"].lower() == today


def run_job(job, log, timeout):
    """Run one job, streaming its output into the log. Returns its exit code (None on a timeout)."""
    cmd = [str(PYTHON), *job["run"]]
    log.write(f"\n===== {datetime.now():%H:%M:%S} {label(job)} (in {job['dir']})\n")
    log.flush()
    started = time.monotonic()
    try:
        done = subprocess.run(cmd, cwd=HERE / job["dir"], stdout=log, stderr=subprocess.STDOUT,
                              stdin=subprocess.DEVNULL, timeout=timeout)
        code = done.returncode
    except subprocess.TimeoutExpired:
        code = None
    log.write(f"===== exit {code} after {time.monotonic() - started:.0f}s\n")
    log.flush()
    return code


def cleanup(days: int) -> int:
    """Delete failure screenshots and page dumps older than `days` days. Returns how many."""
    cutoff, n = time.time() - days * 86400, 0
    for pattern in CLEANUP_GLOBS:
        for path in HERE.rglob(pattern):
            if any(part in CLEANUP_SKIP for part in path.relative_to(HERE).parts) or not path.is_file():
                continue
            if path.stat().st_mtime < cutoff:
                path.unlink()
                n += 1
    return n


def summary() -> str:
    try:
        import outcomes
        return outcomes.summary()
    except Exception as exc:                         # the summary must never fail the run
        return f"(no summary: {type(exc).__name__}: {exc})"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", help="comma-separated job names to run (e.g. reddit,facebook)")
    ap.add_argument("--list", action="store_true", help="show the jobs and stop")
    ap.add_argument("--dry-run", action="store_true", help="same as --list")
    ap.add_argument("--resume", metavar="PLATFORM", help="un-pause a platform and stop")
    args = ap.parse_args()
    cfg = load()
    if args.resume:
        set_paused(args.resume)
        print(f"[+] {args.resume} un-paused")
        return 0
    today = WEEKDAYS[date.today().weekday()]
    only = {n.strip() for n in args.only.split(",")} if args.only else None
    held = paused()
    picked = [j for j in cfg["jobs"] if only is None or j["name"] in only]
    if args.list or args.dry_run:
        for j in cfg["jobs"]:
            on = j in picked and due(j, today, j["name"] in held)
            when = f" ({j['weekly']}s)" if j.get("weekly") else ""
            print(f"  [{'x' if on else ' '}] {label(j) + when:<46} in {j['dir']}")
        for name, p in held.items():
            print(f"  PAUSED {name} since {p['since']}: {p['why']}")
        return 0

    LOGS.mkdir(exist_ok=True)
    path = LOGS / f"{date.today().isoformat()}.log"
    timeout = cfg.get("timeout_minutes", 120) * 60
    stopped, failed = set(), []
    with open(path, "a") as log:
        log.write(f"\n########## daily.py {datetime.now().isoformat(timespec='seconds')}\n")
        jobs = [j for j in picked if due(j, today, j["name"] in held)]
        # a platform's live check goes first among its jobs
        jobs.sort(key=lambda j: (picked.index(next(k for k in picked if k["name"] == j["name"])),
                                 j.get("role") != "livecheck"))
        for job in jobs:
            name = job["name"]
            if name in stopped:
                log.write(f"\n===== skipped {label(job)}: {name} stopped earlier today\n")
                continue
            if name in paused() and job.get("role") != "livecheck":
                log.write(f"\n===== skipped {label(job)}: {name} is paused ({paused()[name]['why']})\n")
                continue
            print(f"[+] {label(job)}")
            code = run_job(job, log, timeout)
            if job.get("role") == "livecheck":
                if code == 0:
                    if name in paused():
                        set_paused(name)
                        notify(f"Outreach: {name} resumed", "its live check passes again")
                    continue
                set_paused(name, f"live check {'timed out' if code is None else f'exited {code}'} on "
                                 f"{date.today().isoformat()}")
                stopped.add(name)
                notify(f"Outreach: {name} paused",
                       f"The read-only live check failed, so {name} won't send until it passes (it re-checks daily) "
                       f"or python daily.py --resume {name}. See logs/{path.name}.")
            elif code == NEEDS_YOU:
                stopped.add(name)
                notify(f"Outreach: {name} needs you",
                       f"{label(job)} stopped on an account problem (logged out, blocked or a checkpoint). "
                       f"See logs/{path.name}.")
            elif code != 0:
                failed.append(job)
                notify(f"Outreach: {name} failed",
                       f"{label(job)} {'timed out' if code is None else f'exited {code}'}. See logs/{path.name}.")
        removed = cleanup(cfg.get("cleanup_days", 14))
        text = summary()
        log.write(f"\n===== summary\n{text}\n===== removed {removed} old failure screenshots/dumps\n")
    (LOGS / f"summary-{date.today().isoformat()}.txt").write_text(text + "\n")
    head, _, rest = text.partition("\n")
    notify("Outreach today", head + (f" ({', '.join(rest.splitlines()[3:])})" if len(rest.splitlines()) > 3 else ""))
    print(f"[done] log: {path}")
    return NEEDS_YOU if stopped or paused() else 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
