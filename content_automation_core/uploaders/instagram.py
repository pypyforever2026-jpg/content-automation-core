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

    def js_click(self, element):
        """کلیک با JavaScript برای عناصری که با click() عادی کار نمی‌کنند"""
        self.driver.execute_script("arguments[0].scrollIntoView({block:'center'}); arguments[0].click();", element)

    # ─────────────────────────────
    # Main Actions
    # ─────────────────────────────
    def upload_reels(self, file_path: str, caption: str) -> bool:
        file_path = os.path.abspath(file_path)
        self.build_driver()

        try:
            self.driver.get(self.buffer_url)
            time.sleep(5)  # give page some time to load

            # ❶ باز کردن composer
            try:
                create_btn = self.wait_for_clickable(By.CSS_SELECTOR, "button[aria-haspopup='menu']")
                create_btn.click()
                time.sleep(1)
                post_item = self.wait_for_clickable(
                    By.XPATH, "//div[@role='menuitem' and contains(., 'Post')]"
                )
                post_item.click()
                print("✅ New UI: Create new → Post")
            except Exception:
                print("🕹 Old UI fallback")
                insta_btn = self.wait_for_clickable(By.CSS_SELECTOR, "[data-channel='instagram']")
                insta_btn.click()
                new_post_btn = self.wait_for_clickable(By.XPATH, "//button[contains(., 'New')]")
                new_post_btn.click()

            time.sleep(2)

            # ❷ انتخاب Reels
            try:
                reels_label = self.wait_for_clickable(By.CSS_SELECTOR, "label[for='reels']")
                reels_label.click()
                print("✅ Reels clicked")
            except Exception:
                print("⚠️ Reels click failed")

            # ❸ آپلود فایل
            try:
                upload_input = self.driver.find_element(By.CSS_SELECTOR, "input[type='file']")
                upload_input.send_keys(file_path)
                print("📁 File uploaded")
                time.sleep(3)
            except Exception:
                print("⚠️ File upload failed")

            # ❹ نوشتن کپشن
            if caption:
                try:
                    caption_box = self.wait_for_element(
                        By.CSS_SELECTOR,
                        "[data-testid='composer-text-area'], [contenteditable='true']"
                    )
                    caption_box.click()
                    self.driver.execute_script("arguments[0].innerText = '';", caption_box)
                    caption_box.send_keys(caption)
                    print("✅ Caption written")
                except Exception:
                    print("⚠️ Caption write failed")

            # ❺ باز کردن منوی Schedule و انتخاب «Now»
            try:
                # کلیک دکمه trigger
                schedule_btn = self.wait_for_clickable(
                    By.CSS_SELECTOR, "button[data-testid='schedule-selector-trigger']"
                )
                self.js_click(schedule_btn)
                print("✅ Schedule menu opened")

                # ────────────────────────────────────────────────
                # FIX: کلیک روی div[role='menuitem'] که شامل «Now» است
                # نه فقط روی <p> که رویداد را trigger نمی‌کند
                # ────────────────────────────────────────────────
                now_menuitem = self.wait_for_clickable(
                    By.XPATH,
                    "//div[@role='menuitem'][.//p[normalize-space(text())='Now']]",
                    timeout=10
                )
                self.js_click(now_menuitem)
                print("⏱ Publish set to Now")
                time.sleep(1)

            except Exception as e:
                print(f"⚠️ Schedule/Now click failed: {e}")

            # ❻ کلیک Publish
            try:
                # سعی می‌کنیم چند selector مختلف برای Publish امتحان کنیم
                publish_btn = None
                publish_selectors = [
                    (By.XPATH, "//button[normalize-space(text())='Share Now']"),
                    (By.XPATH, "//button[normalize-space(text())='Publish Now']"),
                    (By.XPATH, "//button[contains(@class,'schedulePostButton')]"),
                    (By.CSS_SELECTOR, "button[data-testid='publish-button']"),
                ]
                for by, sel in publish_selectors:
                    try:
                        publish_btn = self.wait_for_clickable(by, sel, timeout=5)
                        break
                    except Exception:
                        continue

                if publish_btn:
                    self.js_click(publish_btn)
                    print("✅ Publish clicked")
                    time.sleep(30)  # صبر برای بسته شدن modal
                else:
                    print("⚠️ Publish button not found")
                    return False

            except Exception as e:
                print(f"⚠️ Publish click failed: {e}")
                return False

            return True

        finally:
            self.close_driver()

#nwq
# ─────────────────────────────
# Functional API
# ─────────────────────────────
def upload_instagram_reels(file_path: str, caption: str, profile_path: str, buffer_url: str) -> bool:
    uploader = InstagramUploader(profile_path, buffer_url)
    return uploader.upload_reels(file_path, caption)
