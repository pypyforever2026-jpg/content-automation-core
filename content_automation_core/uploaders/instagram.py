import os
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

WAIT_MS = 25_000


class InstagramUploader:

    def __init__(self, profile_path: str, buffer_url: str):
        self.profile_path = profile_path
        self.buffer_url = buffer_url
        self.page = None
        self.context = None
        self._playwright = None

    # ─────────────────────────────
    # Driver
    # ─────────────────────────────

    def build_driver(self):
        self._playwright = sync_playwright().start()

        self.context = self._playwright.chromium.launch_persistent_context(
            user_data_dir=self.profile_path,
            channel="chrome",
            headless=False,
            no_viewport=True,
            ignore_default_args=["--enable-automation"],
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-notifications",
                "--start-maximized",
                "--no-sandbox",
            ],
        )

        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()

        self.page.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )

    def close_driver(self):
        try:
            if self.context:
                self.context.close()
            if self._playwright:
                self._playwright.stop()
        except:
            pass

    # ─────────────────────────────
    # Helpers
    # ─────────────────────────────

    def wait_page_ready(self):
        self.page.wait_for_load_state("domcontentloaded", timeout=WAIT_MS)
        self.page.wait_for_selector("body", timeout=WAIT_MS)

    def _has_new_ui(self) -> bool:
        try:
            self.page.locator("button[aria-haspopup='menu']").first.wait_for(timeout=6000)
            return True
        except:
            return False

    # ─────────────────────────────
    # Open Composer (🔥 مهم‌ترین بخش)
    # ─────────────────────────────

    def click_insta_profile(self):

        if self._has_new_ui():
            print("🆕 New Buffer UI")

            # Create new
            create_btn = self.page.locator("button[aria-haspopup='menu']").first
            create_btn.wait_for(state="visible", timeout=WAIT_MS)
            create_btn.click()

            # menu
            self.page.locator("[role='menu']").wait_for(timeout=WAIT_MS)

            # Post
            post_item = self.page.locator("[role='menuitem']").filter(
                has_text="Post"
            ).first

            post_item.wait_for(state="visible", timeout=WAIT_MS)
            post_item.click()

        else:
            print("🕹 Old Buffer UI (fallback)")

            # انتخاب اینستاگرام (با icon یا text)
            insta = self.page.locator("[data-channel='instagram']").first

            if not insta.count():
                insta = self.page.locator("text=Instagram").first

            insta.wait_for(timeout=WAIT_MS)
            insta.hover()

            # دکمه New Post
            btn = self.page.locator("button:has-text('New')").first

            btn.wait_for(timeout=WAIT_MS)
            btn.click()

    # ─────────────────────────────
    # Actions
    # ─────────────────────────────

    def click_reels(self):
        try:
            reels = self.page.locator("#reels, text=Reels").first
            reels.wait_for(timeout=WAIT_MS)
            reels.click()
            return True
        except:
            print("⚠️ Reels not found")
            return False

    def write_caption(self, caption: str):
        box = self.page.locator("[data-testid='composer-text-area'], [contenteditable='true']").first
        box.wait_for(timeout=WAIT_MS)
        box.click()

        self.page.keyboard.press("Control+a")
        self.page.keyboard.press("Delete")
        self.page.keyboard.type(caption)

    def upload_file(self, file_path: str):
        file_input = self.page.locator("input[type='file']").first
        file_input.wait_for(timeout=WAIT_MS)
        file_input.set_input_files(file_path)

    def is_media_uploaded(self):
        try:
            self.page.locator("[data-testid='media-attachment-thumbnail']").wait_for(timeout=30000)
            return True
        except:
            return False

    def send_type_now(self):
        try:
            btn = self.page.locator("[data-testid='schedule-selector-trigger']").first
            btn.wait_for(timeout=WAIT_MS)
            btn.click()

            now = self.page.locator("text=right away").first
            now.wait_for(timeout=WAIT_MS)
            now.click()

            return True
        except:
            print("⚠️ schedule not found")
            return False

    def click_publish(self):
        try:
            btn = self.page.locator("button:has-text('Publish')").first
            btn.wait_for(timeout=WAIT_MS)
            btn.click()
            return True
        except:
            print("⚠️ publish not found")
            return False

    def wait_for_modal_close(self):
        try:
            self.page.locator("[role='dialog']").wait_for(state="hidden", timeout=20000)
            return True
        except:
            return False

    # ─────────────────────────────
    # Main
    # ─────────────────────────────

    def upload_reels(self, file_path: str, caption: str) -> bool:

        file_path = os.path.abspath(file_path)

        self.build_driver()

        try:
            self.page.goto(self.buffer_url)
            self.wait_page_ready()

            # DEBUG (خیلی مهم)
            self.page.screenshot(path="debug.png", full_page=True)

            # 1. باز کردن composer
            self.click_insta_profile()

            # 2. reels
            self.click_reels()

            # 3. caption
            if caption:
                self.write_caption(caption)

            # 4. upload
            self.upload_file(file_path)

            self.page.wait_for_timeout(5000)

            if not self.is_media_uploaded():
                print("⚠️ upload maybe failed")

            # 5. publish
            self.send_type_now()
            self.click_publish()

            self.page.wait_for_timeout(10000)

            self.wait_for_modal_close()

            return True

        finally:
            self.close_driver()


# API

def upload_instagram_reels(file_path: str, caption: str,
                           profile_path: str, buffer_url: str) -> bool:
    uploader = InstagramUploader(profile_path, buffer_url)
    return uploader.upload_reels(file_path, caption)