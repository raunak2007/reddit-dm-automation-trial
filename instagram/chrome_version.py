"""chrome_version.py - detect the installed Chrome major version for undetected_chromedriver.

Pass the result as uc.Chrome(..., version_main=chrome_major()) so the downloaded
chromedriver always matches whatever Chrome is installed, instead of a hardcoded number.
Returns None if Chrome can't be found; uc then falls back to the latest driver.
"""
import re
import shutil
import subprocess
import sys
from pathlib import Path

CANDIDATES = {
    "darwin": ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
               str(Path.home() / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome")],
    "linux": ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser"],
    "win32": [r"C:\Program Files\Google\Chrome\Application\chrome.exe",
              r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"],
}


def chrome_major():
    if sys.platform == "win32":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Google\Chrome\BLBeacon") as key:
                return int(winreg.QueryValueEx(key, "version")[0].split(".")[0])
        except (OSError, ValueError):
            return None
    for candidate in CANDIDATES.get(sys.platform, CANDIDATES["linux"]):
        exe = candidate if Path(candidate).exists() else shutil.which(candidate)
        if not exe:
            continue
        try:
            out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=10).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        m = re.search(r"(\d+)\.\d+\.\d+", out)
        if m:
            return int(m.group(1))
    return None


if __name__ == "__main__":
    print(chrome_major())
