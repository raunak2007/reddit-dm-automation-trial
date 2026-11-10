"""schedule.py - run daily.py every day on this Mac with launchd.

    python schedule.py install [--at 10:00]   start running daily.py at that time each day
    python schedule.py uninstall              stop
    python schedule.py status                 installed? when it runs, and the last log
    python schedule.py run-now                start the installed job once, right away

It installs ~/Library/LaunchAgents/<LABEL>.plist, so it runs as you, in your login session (Chrome needs
one): only while you're logged in, and a run missed while the Mac was asleep runs when it wakes. The time
comes from --at, else schedule.json's "time". launchd's own output goes to logs/launchd.log; each run's
output is in logs/<date>.log (see daily.py).
"""
from __future__ import annotations

import argparse
import os
import plistlib
import subprocess
import sys
from pathlib import Path

import daily

LABEL = "com.outreach.daily"
PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def parse_time(text):
    try:
        hour, minute = (int(x) for x in text.split(":"))
        if 0 <= hour < 24 and 0 <= minute < 60:
            return hour, minute
    except ValueError:
        pass
    raise SystemExit(f"--at wants HH:MM (24-hour), not {text!r}")


def plist(hour, minute):
    daily.LOGS.mkdir(exist_ok=True)
    return {
        "Label": LABEL,
        "ProgramArguments": [str(daily.PYTHON), str(daily.HERE / "daily.py")],
        "WorkingDirectory": str(daily.HERE),
        "StartCalendarInterval": {"Hour": hour, "Minute": minute},
        "StandardOutPath": str(daily.LOGS / "launchd.log"),
        "StandardErrorPath": str(daily.LOGS / "launchd.log"),
        "EnvironmentVariables": {"PATH": "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"},
    }


def launchctl(*args):
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def install(at):
    if sys.platform != "darwin":
        raise SystemExit("schedule.py uses launchd (macOS). Elsewhere, run daily.py from cron.")
    if not daily.PYTHON.exists():
        raise SystemExit(f"No venv Python at {daily.PYTHON}; set up the venv first (see README.md).")
    hour, minute = parse_time(at or daily.load().get("time", "10:00"))
    domain = f"gui/{os.getuid()}"
    if PLIST.exists():
        launchctl("bootout", domain, str(PLIST))
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    with open(PLIST, "wb") as f:
        plistlib.dump(plist(hour, minute), f)
    done = launchctl("bootstrap", domain, str(PLIST))
    if done.returncode != 0:
        raise SystemExit(f"launchctl bootstrap failed: {done.stderr.strip() or done.stdout.strip()}")
    print(f"[+] daily.py will run every day at {hour:02d}:{minute:02d} ({PLIST})")
    print("    jobs:")
    for j in daily.load()["jobs"]:
        print(f"      [{'x' if j.get('enabled', True) else ' '}] {daily.label(j)}")


def uninstall():
    if not PLIST.exists():
        print("Not installed.")
        return
    launchctl("bootout", f"gui/{os.getuid()}", str(PLIST))
    PLIST.unlink()
    print(f"[+] removed {PLIST}; daily.py won't run on its own any more")


def status():
    if not PLIST.exists():
        print("Not installed. python schedule.py install [--at HH:MM]")
    else:
        when = plistlib.loads(PLIST.read_bytes())["StartCalendarInterval"]
        loaded = launchctl("print", f"gui/{os.getuid()}/{LABEL}").returncode == 0
        print(f"Installed: every day at {when['Hour']:02d}:{when['Minute']:02d}, "
              f"{'loaded' if loaded else 'NOT loaded (run install again)'}")
    logs = sorted(daily.LOGS.glob("20*.log")) if daily.LOGS.exists() else []
    print(f"Last run log: {logs[-1]}" if logs else "No runs logged yet.")


def run_now():
    if not PLIST.exists():
        raise SystemExit("Not installed. python schedule.py install, or just run python daily.py")
    done = launchctl("kickstart", f"gui/{os.getuid()}/{LABEL}")
    print("[+] started" if done.returncode == 0 else f"launchctl kickstart failed: {done.stderr.strip()}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["install", "uninstall", "status", "run-now"])
    ap.add_argument("--at", help="HH:MM, 24-hour (install only)")
    args = ap.parse_args()
    {"install": lambda: install(args.at), "uninstall": uninstall, "status": status, "run-now": run_now}[args.cmd]()


if __name__ == "__main__":
    main()
