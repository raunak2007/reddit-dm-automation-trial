import undetected_chromedriver as uc
import time
import random
import os

def test_send_dm(account_id, target_username, message):
    profile_dir = os.path.abspath(f"accounts/{account_id}/chrome_profile")
    os.makedirs(profile_dir, exist_ok=True)

    options = uc.ChromeOptions()
    options.add_argument(f"--user-data-dir={profile_dir}")
    options.add_argument("--no-first-run")
    options.add_argument("--no-default-browser-check")

    driver = uc.Chrome(options=options, version_main=154)

    try:
        print("[+] Opening chat...")
        driver.get("https://www.reddit.com/chat/")
        time.sleep(random.uniform(4, 7))

        print(f"[+] Chat title: {driver.title}")
        driver.save_screenshot(f"accounts/{account_id}/chat_before_send.png")

        # Hold open so you can send the DM manually the first time
        print(f"[!] MANUAL STEP: Send '{message}' to u/{target_username} in this window.")
        print("    Do it by hand first. Press ENTER once the message is sent.")
        input()

        driver.save_screenshot(f"accounts/{account_id}/chat_after_send.png")
        print("[+] Screenshot saved. Check chat_after_send.png to confirm the message went through.")
        time.sleep(10)

    except Exception as e:
        print(f"[!] Error: {e}")
    finally:
        driver.quit()

if __name__ == "__main__":
    test_send_dm("account_1", "YOUR_TEST_ACCOUNT", "test message from trial")
