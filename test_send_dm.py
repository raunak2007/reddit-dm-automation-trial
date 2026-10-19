import undetected_chromedriver as uc
from chrome_version import chrome_major
import time
import random
import os

from send_dm import compose_link

def test_send_dm(account_id, target_username, message):
    profile_dir = os.path.abspath(f"accounts/{account_id}/chrome_profile")
    os.makedirs(profile_dir, exist_ok=True)

    options = uc.ChromeOptions()
    options.add_argument(f"--user-data-dir={profile_dir}")
    options.add_argument("--no-first-run")
    options.add_argument("--no-default-browser-check")

    driver = uc.Chrome(options=options, version_main=chrome_major())

    try:
        print("[+] Opening the compose link...")
        driver.get(compose_link(target_username, message))
        time.sleep(random.uniform(4, 7))

        print(f"[+] Compose title: {driver.title}")
        driver.save_screenshot(f"accounts/{account_id}/compose_before_send.png")

        # Hold open so you can send the DM manually the first time
        print(f"[!] MANUAL STEP: the form is prefilled for u/{target_username}. Review it and click Send.")
        print("    Do it by hand first. Press ENTER once the message is sent.")
        input()

        driver.save_screenshot(f"accounts/{account_id}/compose_after_send.png")
        print("[+] Screenshot saved. Check compose_after_send.png to confirm the message went through.")
        time.sleep(10)

    except Exception as e:
        print(f"[!] Error: {e}")
    finally:
        driver.quit()

if __name__ == "__main__":
    test_send_dm("account_1", "YOUR_TEST_ACCOUNT", "test message from trial")
