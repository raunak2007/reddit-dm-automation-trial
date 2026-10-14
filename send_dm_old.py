import undetected_chromedriver as uc
from chrome_version import chrome_major
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import time
import random
import os
import json

def load_config():
    with open("config.json") as f:
        return json.load(f)

def send_dm(account_id, target_username, message):
    profile_dir = os.path.abspath(f"accounts/{account_id}/chrome_profile")
    os.makedirs(profile_dir, exist_ok=True)

    for lock in ["SingletonLock", "SingletonSocket", "SingletonCookie"]:
        p = os.path.join(profile_dir, lock)
        if os.path.exists(p):
            os.remove(p)

    options = uc.ChromeOptions()
    options.add_argument(f"--user-data-dir={profile_dir}")
    options.add_argument("--no-first-run")
    options.add_argument("--no-default-browser-check")

    driver = uc.Chrome(options=options, version_main=chrome_major())
    wait = WebDriverWait(driver, 20)

    try:
        print("[+] Opening chat...")
        driver.get("https://www.reddit.com/chat/")
        time.sleep(random.uniform(4, 7))

        if "login" in driver.current_url:
            print("[!] Not logged in. Profile session missing. Aborting.")
            return False

        print("[+] Clicking 'Start new chat'...")
        start_btn = wait.until(EC.element_to_be_clickable(
            (By.XPATH, "//button[contains(., 'Start new chat')]")
        ))
        start_btn.click()
        time.sleep(random.uniform(1.5, 3))

        print(f"[+] Typing username: {target_username}")
        user_input = wait.until(EC.presence_of_element_located(
            (By.CSS_SELECTOR, "input[placeholder*='username' i], input[placeholder*='Search' i]")
        ))
        user_input.click()
        user_input.send_keys(target_username)
        time.sleep(random.uniform(2, 4))

        print("[+] Selecting first result...")
        first_result = wait.until(EC.element_to_be_clickable(
            (By.XPATH, "//*[contains(@class, 'result') or contains(@role, 'option')][1]")
        ))
        first_result.click()
        time.sleep(random.uniform(1.5, 3))

        print("[+] Typing message...")
        msg_box = wait.until(EC.presence_of_element_located(
            (By.CSS_SELECTOR, "textarea, div[contenteditable='true']")
        ))
        msg_box.click()
        msg_box.send_keys(message)
        time.sleep(random.uniform(1, 2))

        print("[+] Sending (Enter)...")
        msg_box.send_keys(Keys.ENTER)
        time.sleep(random.uniform(2, 4))

        driver.save_screenshot(f"accounts/{account_id}/after_send_{int(time.time())}.png")
        print("[+] Sent. Screenshot saved.")
        return True

    except Exception as e:
        print(f"[!] Error: {e}")
        driver.save_screenshot(f"accounts/{account_id}/error_{int(time.time())}.png")
        return False
    finally:
        time.sleep(3)
        driver.quit()

if __name__ == "__main__":
    cfg = load_config()
    send_dm("account_1", "your_test_username_here", "test message from trial")
