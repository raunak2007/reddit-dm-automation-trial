import undetected_chromedriver as uc
from chrome_version import chrome_major
import time
import random
import os

def test_reddit_connection(account_id="account_1"):
    """
    Opens Reddit using undetected-chromedriver, using a persistent
    user-data-dir per account so sessions/cookies are isolated.
    """
    profile_dir = os.path.abspath(f"accounts/{account_id}/chrome_profile")
    os.makedirs(profile_dir, exist_ok=True)

    options = uc.ChromeOptions()
    options.add_argument(f"--user-data-dir={profile_dir}")
    options.add_argument("--no-first-run")
    options.add_argument("--no-default-browser-check")
    options.add_argument("--window-size=1200,900")

    driver = uc.Chrome(options=options, version_main=chrome_major())

    try:
        print("[+] Launching Reddit...")
        driver.get("https://www.reddit.com/")
        time.sleep(random.uniform(3, 6))

        print(f"[+] Current URL: {driver.current_url}")
        print(f"[+] Page title: {driver.title}")

        # Check whether we're logged in
        page_source = driver.page_source.lower()
        title = driver.title.lower()

        if "prove your humanity" in page_source or "prove your humanity" in title:
            print("[!] CAPTCHA / humanity challenge detected")
            print("    Solve it in the browser, then press ENTER here...")
            input()
        elif "log in" in page_source or "sign up" in page_source or "welcome to reddit" in title:
            print("[!] Not logged in. Log in manually in this window.")
            print("    Press ENTER here once you're logged in...")
            input()
        else:
            print("[+] Appears to be logged in (this only shows the saved session is valid).")

        # Try navigating to the compose page (the page the queue's links open)
        print("[+] Navigating to compose...")
        driver.get("https://www.reddit.com/message/compose/")
        time.sleep(random.uniform(4, 8))
        print(f"[+] Compose URL: {driver.current_url}")
        print(f"[+] Compose title: {driver.title}")

        # Save a screenshot for evidence
        driver.save_screenshot(f"accounts/{account_id}/compose_page.png")
        print(f"[+] Screenshot saved to accounts/{account_id}/compose_page.png")

        print("[+] Holding browser open for 60s so you can inspect it...")
        time.sleep(60)

    except Exception as e:
        print(f"[!] Error: {e}")
    finally:
        driver.quit()

if __name__ == "__main__":
    test_reddit_connection("account_1")
