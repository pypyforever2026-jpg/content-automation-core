# import os
# import time
# from pathlib import Path
# from selenium import webdriver
# from selenium.webdriver.chrome.service import Service
# from selenium.webdriver.chrome.options import Options
# from selenium.webdriver.common.by import By
# from selenium.webdriver.support.ui import WebDriverWait
# from selenium.webdriver.support import expected_conditions as EC
# from selenium.common.exceptions import (
#     TimeoutException, NoSuchElementException, StaleElementReferenceException,
#     ElementClickInterceptedException
# )
# from selenium.webdriver.common.action_chains import ActionChains
# from selenium.webdriver.common.keys import Keys
# from webdriver_manager.chrome import ChromeDriverManager


# WAIT_SEC = 25


# class InstagramUploader:
#     """
#     Instagram uploader for Buffer.com
#     - الگوریتم و سلکتورها ۱۰۰٪ مطابق نسخه اصلی
#     - فقط پروفایل‌ها و URLها داینامیک شده‌اند
#     """

#     def __init__(self, profile_path: str, buffer_url: str):
#         """
#         profile_path: مسیر پروفایل کروم
#         buffer_url: URL کانال Buffer
#         """
#         self.profile_path = profile_path
#         self.buffer_url = buffer_url
#         self.channel_id = self.extract_channel_id(buffer_url)
#         self.driver = None

#     # -----------------------------
#     # Driver Setup
#     # -----------------------------
#     def build_driver(self):
#         chrome_opts = Options()
#         chrome_opts.add_argument(f"--user-data-dir={self.profile_path}")
#         chrome_opts.add_argument("--disable-notifications")
#         chrome_opts.add_argument("--start-maximized")
#         chrome_opts.add_argument("--disable-features=PrivacySandboxAdsAPIs")
#         chrome_opts.add_argument("--disable-blink-features=AutomationControlled")

#         service = Service(ChromeDriverManager().install())
#         driver = webdriver.Chrome(service=service, options=chrome_opts)

#         driver.execute_cdp_cmd(
#             "Page.addScriptToEvaluateOnNewDocument",
#             {"source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"}
#         )

#         self.driver = driver

#     # -----------------------------
#     # Helpers
#     # -----------------------------
#     @staticmethod
#     def extract_channel_id(url: str) -> str:
#         return url.rstrip("/").split("/")[-1]

#     @staticmethod
#     def detect_media_type(file_path: str) -> str:
#         image_exts = {'.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp', '.tiff', '.svg', '.heic'}
#         video_exts = {'.mp4', '.mov', '.avi', '.mkv', '.flv', '.wmv', '.webm', '.3gp', '.mpeg', '.mpg', '.m4v', '.ts'}

#         ext = Path(file_path).suffix.lower()
#         if ext in image_exts:
#             return 'image'
#         if ext in video_exts:
#             return 'video'
#         return 'unknown'

#     def wait_page_ready(self):
#         WebDriverWait(self.driver, WAIT_SEC).until(
#             lambda d: d.execute_script("return document.readyState") == "complete"
#         )
#         WebDriverWait(self.driver, WAIT_SEC).until(
#             EC.presence_of_element_located((By.CSS_SELECTOR, "[data-testid='channels-page'], body"))
#         )

#     # -----------------------------
#     # UI Actions (همان الگوریتم اصلی)
#     # -----------------------------
#     def click_insta_profile(self):
#         wait = WebDriverWait(self.driver, 20)
#         container = wait.until(EC.presence_of_element_located((By.ID, self.channel_id)))
#         ActionChains(self.driver).move_to_element(container).perform()

#         btn = wait.until(
#             EC.element_to_be_clickable((By.XPATH, f'//*[@id="{self.channel_id}"]/button[2]'))
#         )
#         btn.click()

#     def click_reels(self):
#         wait = WebDriverWait(self.driver, 20)
#         reels_input = wait.until(EC.presence_of_element_located((By.ID, "reels")))

#         try:
#             wait.until(EC.element_to_be_clickable((By.ID, "reels"))).click()
#         except:
#             self.driver.execute_script("arguments[0].click();", reels_input)

#         return self.driver.find_element(By.ID, "reels").is_selected()

#     def write_caption(self, caption):
#         wait = WebDriverWait(self.driver, 20)
#         box = wait.until(
#             EC.presence_of_element_located((By.CSS_SELECTOR, "div[role='textbox'][data-testid='composer-text-area']"))
#         )
#         box.click()
#         box.send_keys(Keys.CONTROL + "a")
#         box.send_keys(Keys.DELETE)
#         box.send_keys(caption)

#     def upload_file(self, file_path):
#         wait = WebDriverWait(self.driver, 20)
#         try:
#             file_input = wait.until(
#                 EC.presence_of_element_located((By.CSS_SELECTOR, "input[name='file-upload-input'][type='file']"))
#             )
#             self.driver.execute_script("arguments[0].scrollIntoView(true);", file_input)
#             file_input.send_keys(file_path)
#             return True
#         except:
#             return False

#     def is_media_uploaded(self):
#         wait = WebDriverWait(self.driver, 30)
#         try:
#             wait.until(
#                 EC.presence_of_element_located(
#                     (By.CSS_SELECTOR, "button[data-testid='media-attachment-thumbnail']")
#                 )
#             )
#             return True
#         except:
#             return False

#     def send_type_now(self):
#         wait = WebDriverWait(self.driver, 20)
#         try:
#             schedule_btn = wait.until(
#                 EC.element_to_be_clickable((By.CSS_SELECTOR, "button[data-testid='schedule-selector-trigger']"))
#             )
#             schedule_btn.click()

#             publish_now = wait.until(
#                 EC.element_to_be_clickable(
#                     (By.XPATH, "//small[normalize-space()='Publish your post right away.']/..")
#                 )
#             )
#             publish_now.click()
#             return True
#         except:
#             return False

#     def click_publish(self):
#         wait = WebDriverWait(self.driver, 20)
#         try:
#             btn = wait.until(
#                 EC.element_to_be_clickable((By.XPATH, "//button[normalize-space()='Publish Now']"))
#             )
#             btn.click()
#             return True
#         except:
#             return False

#     def wait_for_modal_close(self):
#         wait = WebDriverWait(self.driver, 20)
#         try:
#             wait.until(EC.invisibility_of_element_located((By.CSS_SELECTOR, "div[role='dialog']")))
#             return True
#         except:
#             return False

#     # -----------------------------
#     # Main Upload Function
#     # -----------------------------
#     def upload_reels(self, file_path: str, caption: str):
#         file_path = os.path.abspath(file_path)
#         media_type = self.detect_media_type(file_path)

#         self.build_driver()
#         self.driver.get(self.buffer_url)
#         self.wait_page_ready()

#         self.driver.execute_script("window.scrollTo(0, 0);")
#         self.click_insta_profile()
#         self.click_reels()

#         if caption:
#             self.write_caption(caption)

#         self.upload_file(file_path)

#         time.sleep(20 if media_type == "image" else 60)

#         if not self.is_media_uploaded():
#             print("⚠️ Media upload failed")

#         self.send_type_now()
#         self.click_publish()

#         time.sleep(30 if media_type == "image" else 60)

#         self.wait_for_modal_close()
#         time.sleep(5)
#         self.driver.quit()

#         return True


# # -----------------------------
# # Functional API
# # -----------------------------
# def upload_instagram_reels(file_path, caption, profile_path, buffer_url):
#     uploader = InstagramUploader(profile_path=profile_path, buffer_url=buffer_url)
#     return uploader.upload_reels(file_path, caption)

import os
import time
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError


WAIT_MS = 25_000   # 25 ثانیه (معادل WAIT_SEC=25 در سلنیوم)


class InstagramUploader:
    """
    Instagram uploader for Buffer.com — Playwright version
    - پشتیبانی از UI قدیم (channel container با ID) و UI جدید (Create new → Post)
    - الگوریتم و سلکتورهای composer مطابق نسخه اصلی
    """

    def __init__(self, profile_path: str, buffer_url: str):
        """
        profile_path : مسیر پروفایل کروم
        buffer_url   : URL کانال Buffer
        """
        self.profile_path = profile_path
        self.buffer_url = buffer_url
        self.channel_id = self.extract_channel_id(buffer_url)
        self.page = None
        self.context = None
        self._playwright = None

    # ──────────────────────────────────────────
    # Driver Setup
    # ──────────────────────────────────────────

    def build_driver(self):
        self._playwright = sync_playwright().start()

        self.context = self._playwright.chromium.launch_persistent_context(
            user_data_dir=self.profile_path,
            headless=False,
            no_viewport=True,               # پنجره با اندازه واقعی
            ignore_default_args=["--enable-automation"],
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-notifications",
                "--start-maximized",
                "--disable-features=PrivacySandboxAdsAPIs",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )

        # صفحه موجود یا جدید
        if self.context.pages:
            self.page = self.context.pages[0]
        else:
            self.page = self.context.new_page()

        # مخفی کردن webdriver flag
        self.page.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )

    def close_driver(self):
        try:
            if self.context:
                self.context.close()
            if self._playwright:
                self._playwright.stop()
        except Exception:
            pass

    # ──────────────────────────────────────────
    # Helpers
    # ──────────────────────────────────────────

    @staticmethod
    def extract_channel_id(url: str) -> str:
        return url.rstrip("/").split("/")[-1]

    @staticmethod
    def detect_media_type(file_path: str) -> str:
        image_exts = {'.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp', '.tiff', '.svg', '.heic'}
        video_exts = {'.mp4', '.mov', '.avi', '.mkv', '.flv', '.wmv', '.webm',
                      '.3gp', '.mpeg', '.mpg', '.m4v', '.ts'}
        ext = Path(file_path).suffix.lower()
        if ext in image_exts:
            return 'image'
        if ext in video_exts:
            return 'video'
        return 'unknown'

    def wait_page_ready(self):
        self.page.wait_for_load_state("domcontentloaded", timeout=WAIT_MS)
        # صبر برای body یا channels-page
        self.page.wait_for_selector(
            "[data-testid='channels-page'], body",
            timeout=WAIT_MS
        )

    def _has_new_ui(self) -> bool:
        """بررسی وجود دکمه Create new (UI جدید بافر)"""
        try:
            self.page.locator("button:has-text('Create new')").wait_for(
                state="visible", timeout=5_000
            )
            return True
        except PlaywrightTimeoutError:
            return False

    # ──────────────────────────────────────────
    # UI Actions
    # ──────────────────────────────────────────

    def click_insta_profile(self):
        """
        UI قدیم : container کانال → دکمه دوم
        UI جدید : Create new → Post
        """
        if self._has_new_ui():
            print("🆕 New Buffer UI detected")
            # کلیک Create new
            self.page.locator("button:has-text('Create new')").click()

            # صبر برای منو و کلیک روی Post
            post_item = self.page.locator("[role='menuitem']:has(label:text-is('Post'))")
            post_item.wait_for(state="visible", timeout=WAIT_MS)
            post_item.click()

        else:
            print("🕹 Old Buffer UI detected")
            # hover روی channel container
            container = self.page.locator(f"#{self.channel_id}")
            container.wait_for(state="visible", timeout=WAIT_MS)
            container.hover()

            # کلیک دکمه دوم (New Post)
            btn = self.page.locator(f"#{self.channel_id} button:nth-child(2)")
            btn.wait_for(state="visible", timeout=WAIT_MS)
            btn.click()

    def click_reels(self) -> bool:
        """انتخاب گزینه Reels"""
        try:
            reels = self.page.locator("#reels")
            reels.wait_for(state="visible", timeout=WAIT_MS)
            reels.click()
            return reels.is_checked()
        except PlaywrightTimeoutError:
            print("⚠️ Reels option not found")
            return False

    def write_caption(self, caption: str):
        """نوشتن کپشن در textbox composer"""
        box = self.page.locator("[data-testid='composer-text-area']")
        box.wait_for(state="visible", timeout=WAIT_MS)
        box.click()

        # پاک‌کردن متن قبلی
        self.page.keyboard.press("Control+a")
        self.page.keyboard.press("Delete")

        # تایپ کپشن (keyboard.type برای contenteditable بهتر از fill است)
        self.page.keyboard.type(caption)

    def upload_file(self, file_path: str) -> bool:
        """آپلود فایل رسانه"""
        try:
            file_input = self.page.locator("input[name='file-upload-input'][type='file']")
            file_input.wait_for(state="attached", timeout=WAIT_MS)
            file_input.set_input_files(file_path)
            return True
        except Exception as e:
            print(f"⚠️ File upload error: {e}")
            return False

    def is_media_uploaded(self) -> bool:
        """بررسی ظاهر شدن thumbnail رسانه"""
        try:
            self.page.locator("button[data-testid='media-attachment-thumbnail']").wait_for(
                state="visible", timeout=30_000
            )
            return True
        except PlaywrightTimeoutError:
            return False

    def send_type_now(self) -> bool:
        """تغییر زمان‌بندی به Publish Now"""
        try:
            schedule_btn = self.page.locator("button[data-testid='schedule-selector-trigger']")
            schedule_btn.wait_for(state="visible", timeout=WAIT_MS)
            schedule_btn.click()

            # کلیک روی گزینه «Publish right away»
            publish_now = self.page.locator(
                "small:text('Publish your post right away.')").locator("..")
            publish_now.wait_for(state="visible", timeout=WAIT_MS)
            publish_now.click()
            return True
        except PlaywrightTimeoutError:
            print("⚠️ Schedule selector not found")
            return False

    def click_publish(self) -> bool:
        """کلیک دکمه Publish Now نهایی"""
        try:
            btn = self.page.locator("button:text-is('Publish Now')")
            btn.wait_for(state="visible", timeout=WAIT_MS)
            btn.click()
            return True
        except PlaywrightTimeoutError:
            print("⚠️ Publish Now button not found")
            return False

    def wait_for_modal_close(self) -> bool:
        """صبر برای بسته شدن modal"""
        try:
            self.page.locator("[role='dialog']").wait_for(state="hidden", timeout=20_000)
            return True
        except PlaywrightTimeoutError:
            return False

    # ──────────────────────────────────────────
    # Main Upload Function
    # ──────────────────────────────────────────

    def upload_reels(self, file_path: str, caption: str) -> bool:
        file_path = os.path.abspath(file_path)
        media_type = self.detect_media_type(file_path)

        self.build_driver()
        try:
            self.page.goto(self.buffer_url)
            self.wait_page_ready()

            self.page.evaluate("window.scrollTo(0, 0)")

            # ❶ باز کردن composer
            self.click_insta_profile()

            # ❷ انتخاب Reels
            self.click_reels()

            # ❸ نوشتن کپشن
            if caption:
                self.write_caption(caption)

            # ❹ آپلود فایل
            self.upload_file(file_path)

            # ❺ صبر برای پردازش
            wait_upload = 20_000 if media_type == "image" else 60_000
            self.page.wait_for_timeout(wait_upload)

            if not self.is_media_uploaded():
                print("⚠️ Media upload may have failed")

            # ❻ تغییر به Publish Now و publish
            self.send_type_now()
            self.click_publish()

            # ❼ صبر برای تکمیل
            wait_after = 30_000 if media_type == "image" else 60_000
            self.page.wait_for_timeout(wait_after)

            self.wait_for_modal_close()
            self.page.wait_for_timeout(5_000)

            return True

        finally:
            self.close_driver()


# ──────────────────────────────────────────
# Functional API (همان signature قبلی)
# ──────────────────────────────────────────

def upload_instagram_reels(file_path: str, caption: str,
                           profile_path: str, buffer_url: str) -> bool:
    uploader = InstagramUploader(profile_path=profile_path, buffer_url=buffer_url)
    return uploader.upload_reels(file_path, caption)
