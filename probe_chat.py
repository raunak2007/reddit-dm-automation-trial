"""probe_chat.py - read-only look at Reddit chat's DOM. It clicks, types and sends nothing.

Save next to send_dm.py (~/reddit-dm-automation-trial/) and run:
    python probe_chat.py          # Chrome stays open 60 s
    python probe_chat.py 900      # ...or 15 min, e.g. while you run the console snippets
"""
import sys
import time
from pathlib import Path

import undetected_chromedriver as uc
from selenium.common.exceptions import WebDriverException

from send_dm import js, wait_until_loaded

HERE = Path(__file__).resolve().parent
PROFILE = HERE / "accounts" / "account_1" / "chrome_profile"    # the only profile this script opens
URL = "https://www.reddit.com/chat/"
DUMP_FILE = HERE / "chat_dom_dump.txt"
HOLD_SECONDS = int(sys.argv[1]) if len(sys.argv) > 1 else 60


def main():
    if not PROFILE.is_dir():
        sys.exit(f"Profile folder not found: {PROFILE}\n"
                 "Not starting Chrome, so no other profile gets used or created.")

    options = uc.ChromeOptions()
    options.add_argument(f"--user-data-dir={PROFILE}")
    try:
        driver = uc.Chrome(options=options, version_main=154)
    except Exception as exc:
        sys.exit(f"Chrome did not start: {exc}\n"
                 "If another Chrome window already has this profile open, close that window yourself and rerun.")

    try:
        driver.get(URL)
        try:
            wait_until_loaded(driver)
        except Exception as exc:
            reason = getattr(exc, "msg", None) or exc
            print(f"note: the page did not settle cleanly ({type(exc).__name__}: {reason}); dumping what is there")
        print("url:  ", driver.current_url)
        print("title:", driver.title)

        frames = js(driver, "css", "iframe", None)
        print("state (top document):", js(driver, "state"))
        sections = [f"### top document: {driver.current_url}\n{js(driver, 'dump')}"]
        for number, frame in enumerate(frames, 1):
            src = frame.get_attribute("src") or "(no src)"
            try:
                driver.switch_to.frame(frame)
                print(f"state (iframe {number}, {src}):", js(driver, "state"))
                sections.append(f"### iframe {number}: {src}\n{js(driver, 'dump')}")
            except WebDriverException as exc:
                sections.append(f"### iframe {number}: {src}\n(could not read it: {exc.msg})")
            finally:
                driver.switch_to.default_content()

        DUMP_FILE.write_text("\n\n".join(sections) + "\n", encoding="utf-8")
        print(f"wrote {DUMP_FILE}")

        print(f"Chrome stays open for {HOLD_SECONDS}s so you can inspect it (Ctrl+C here to finish early).")
        time.sleep(HOLD_SECONDS)       # deliberate hold for manual inspection, not a page wait
    except KeyboardInterrupt:
        print("stopping early")
    finally:
        driver.quit()                  # closes only the Chrome this script started


if __name__ == "__main__":
    main()