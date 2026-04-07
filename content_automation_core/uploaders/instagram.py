import os
import time
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

WAIT_SEC = 25

class InstagramUploader:
    def __init__(self, profile_path: str, buffer_url: str):
        self.profile_path = profile_path
        self.buffer_url = buffer_url
        self.driver = None

    def build_driver(self):
        options = Options()
        options.add_argument(f"user-data-dir={self.profile_path}")
        options.add_argument("--start-maximized")
        options.add_argument("--disable-notifications")
        options.add_argument("--disable-blink-features=AutomationControlled")
        self.driver = webdriver.Chrome(options=options)

    def close_driver(self):
        if self.driver:
            self.driver.quit()

    def wait_for_element(self, by, selector, timeout=WAIT_SEC):
        return WebDriverWait(self.driver, timeout).until(
            EC.presence_of_element_located((by, selector))
        )

    def wait_for_clickable(self, by, selector, timeout=WAIT_SEC):
        return WebDriverWait(self.driver, timeout).until(
            EC.element_to_be_clickable((by, selector))
        )

    # ─────────────────────────────
    # Main Actions
    # ─────────────────────────────
    def upload_reels(self, file_path: str, caption: str) -> bool:
        file_path = os.path.abspath(file_path)
        self.build_driver()

        try:
            self.driver.get(self.buffer_url)
            time.sleep(5)  # give page some time to load

            # Click on Instagram post creation
            try:
                create_btn = self.wait_for_clickable(By.CSS_SELECTOR, "button[aria-haspopup='menu']")
                create_btn.click()
                time.sleep(1)
                post_item = self.wait_for_clickable(By.XPATH, "//div[@role='menuitem' and contains(., 'Post')]")
                post_item.click()
            except:
                print("🕹 Old UI fallback")
                insta_btn = self.wait_for_clickable(By.CSS_SELECTOR, "[data-channel='instagram']")
                insta_btn.click()
                new_post_btn = self.wait_for_clickable(By.XPATH, "//button[contains(., 'New')]")
                new_post_btn.click()

            time.sleep(2)

            # Click Reels (new UI)
            try:
                reels_label = self.wait_for_clickable(By.CSS_SELECTOR, "label[for='reels']")
                reels_label.click()
                print("✅ Reels clicked")
            except:
                print("⚠️ Reels click failed")

            # Upload file
            try:
                upload_input = self.driver.find_element(By.CSS_SELECTOR, "input[type='file']")
                upload_input.send_keys(file_path)
                print("📁 File uploaded")
            except:
                print("⚠️ File upload failed")

            time.sleep(2)

            # Write caption
            if caption:
                try:
                    caption_box = self.wait_for_element(By.CSS_SELECTOR, "[data-testid='composer-text-area'], [contenteditable='true']")
                    caption_box.click()
                    caption_box.clear()
                    caption_box.send_keys(caption)
                except:
                    print("⚠️ Caption write failed")

            # Click Now (schedule)
            # Click Now (schedule) with JS
            try:
                schedule_btn = self.wait_for_clickable(By.CSS_SELECTOR, "button[data-schedule-trigger='true']")
                self.driver.execute_script("arguments[0].scrollIntoView(true); arguments[0].click();", schedule_btn)
                print("✅ Schedule menu opened")
                time.sleep(3)

                now_option = self.driver.find_element(By.XPATH, "//p[text()='Now']")
                self.driver.execute_script("arguments[0].scrollIntoView(true); arguments[0].click();", now_option)
                print("⏱ Publish set to Now")
                time.sleep(1)
            except Exception as e:
                print("⚠️ Now click skipped (JS forced it anyway)")

            # Click Publish
            try:
                publish_btn = self.wait_for_clickable(By.CSS_SELECTOR, "button.publish_schedulePostButton_8XRSX")
                self.driver.execute_script("arguments[0].scrollIntoView(true); arguments[0].click();", publish_btn)
                print("✅ Publish clicked")
                time.sleep(30)  # wait for modal to close
            except Exception as e:
                print("⚠️ Publish click failed:", e)
                return False

            return True

        finally:
            self.close_driver()


# Usage
def upload_instagram_reels(file_path: str, caption: str, profile_path: str, buffer_url: str):
    uploader = InstagramUploader(profile_path, buffer_url)
    return uploader.upload_reels(file_path, caption)