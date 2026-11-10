"""daily.py - run every platform's outreach once, one after another, the way you would by hand.

    python daily.py              run every enabled job in schedule.json
    python daily.py --only reddit,instagram
    python daily.py --list       show the jobs and stop
    python daily.py --dry-run    print what would run, run nothing

schedule.py installs this as a daily launchd job; it is also fine to run it yourself. Each job is a command
run in its own folder with this repo's venv Python (so each folder's .env and config.json apply), one at a
time, since they drive Chrome and two at once would fight over a profile. Output goes to
logs/<date>.log. A job that exits 2 stopped on something account-wide (logged out, blocked, a checkpoint or
PIN prompt): the rest of that platform's jobs are skipped, the other platforms still run, and you get a
macOS notification, since only you can fix those. Any other failure (a crash, a timeout) is notified too.
Exit code: 2 if any job stopped on an account problem, 1 if any failed otherwise, else 0.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG = HERE / "schedule.json"
LOGS = HERE / "logs"
PYTHON = HERE / "venv" / "bin" / "python"
NEEDS_YOU = 2                       # exit code the platforms use for "stopped on an account-wide problem"
DEFAULT = {
    "time": "10:00",
    "timeout_minutes": 120,
    "jobs": [
        {"name": "reddit", "dir": ".", "run": ["reddit/run.py"], "enabled": True},
        {"name": "reddit", "dir": ".", "run": ["reddit/run.py", "replies"], "enabled": True},
        {"name": "facebook", "dir": "facebook", "run": ["run.py"], "enabled": True},
        {"name": "facebook", "dir": "facebook", "run": ["run.py", "replies"], "enabled": True},
        {"name": "instagram", "dir": "instagram", "run": ["run.py"], "enabled": True},
        {"name": "instagram", "dir": "instagram", "run": ["run.py", "replies"], "enabled": True},
    ],
}


def load():
    """schedule.json, written with the defaults the first time."""
    if not CONFIG.exists():
        CONFIG.write_text(json.dumps(DEFAULT, indent=2) + "\n")
    return json.loads(CONFIG.read_text())


def notify(title, text):
    """A macOS notification; on anything else (or if it fails) just print."""
    print(f"[notify] {title}: {text}")
    if sys.platform == "darwin":
        script = f"display notification {json.dumps(text[:200])} with title {json.dumps(title)}"
        subprocess.run(["osascript", "-e", script], check=False, capture_output=True)


def label(job):
    return f"{job['name']}: {' '.join(job['run'])}"


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


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", help="comma-separated job names to run (e.g. reddit,facebook)")
    ap.add_argument("--list", action="store_true", help="show the jobs and stop")
    ap.add_argument("--dry-run", action="store_true", help="print what would run, run nothing")
    args = ap.parse_args()
    cfg = load()
    only = {n.strip() for n in args.only.split(",")} if args.only else None
    jobs = [j for j in cfg["jobs"] if j.get("enabled", True) and (only is None or j["name"] in only)]
    if args.list or args.dry_run:
        for j in cfg["jobs"]:
            on = j.get("enabled", True) and (only is None or j["name"] in only)
            print(f"  [{'x' if on else ' '}] {label(j):<40} in {j['dir']}")
        return 0
    if not jobs:
        print("Nothing enabled in schedule.json.")
        return 0

    LOGS.mkdir(exist_ok=True)
    path = LOGS / f"{datetime.now():%Y-%m-%d}.log"
    timeout = cfg.get("timeout_minutes", 120) * 60
    stopped, failed = set(), []
    with open(path, "a") as log:
        log.write(f"\n########## daily.py {datetime.now().isoformat(timespec='seconds')}\n")
        for job in jobs:
            if job["name"] in stopped:
                log.write(f"\n===== skipped {label(job)}: {job['name']} stopped on an account problem earlier\n")
                continue
            print(f"[+] {label(job)}")
            code = run_job(job, log, timeout)
            if code == NEEDS_YOU:
                stopped.add(job["name"])
                notify(f"Outreach: {job['name']} needs you",
                       f"{label(job)} stopped on an account problem (logged out, blocked or a checkpoint). "
                       f"See logs/{path.name}.")
            elif code != 0:
                failed.append(job)
                notify(f"Outreach: {job['name']} failed",
                       f"{label(job)} {'timed out' if code is None else f'exited {code}'}. See logs/{path.name}.")
    print(f"[done] log: {path}")
    return NEEDS_YOU if stopped else 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
