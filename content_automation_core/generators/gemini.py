import os
import time
import uuid
import random
import re
import base64
from playwright.sync_api import sync_playwright


class GeminiImageGenerator:
    """
    Gemini Image Generator — Fixed Version
    - selector fallback برای دکمه Create image
    - دانلود تصویر blob: از طریق JS به‌جای request.get
    """

    def __init__(self, download_dir, chrome_profile, gemini_url, chrome_binary=None):
        self.download_dir = download_dir
        self.chrome_profile = chrome_profile
        self.gemini_url = gemini_url
        self.chrome_binary = chrome_binary

        os.makedirs(download_dir, exist_ok=True)

    # ──────────────────────────────────────────
    # رفتارهای انسانی
    # ──────────────────────────────────────────

    def human_sleep(self, a=0.3, b=1.2):
        time.sleep(random.uniform(a, b))

    def human_type(self, element, text):
        for ch in text:
            if random.random() < 0.02:
                wrong = random.choice("qwertyuiopasdfghjklzxcvbnm")
                element.type(wrong)
                time.sleep(random.uniform(0.05, 0.15))
                element.press("Backspace")

            if ch == "\n":
                element.press("Shift+Enter")
            else:
                element.type(ch)

            time.sleep(abs(random.gauss(0.12, 0.05)))

            if random.random() < 0.05:
                time.sleep(random.uniform(0.3, 0.8))

    def human_mouse_move(self, page, selector):
        box = page.locator(selector).bounding_box()
        if not box:
            return
        x, y = box["x"], box["y"]
        for _ in range(20):
            page.mouse.move(
                x + random.randint(-5, 5),
                y + random.randint(-5, 5),
                steps=random.randint(2, 5)
            )
            time.sleep(random.uniform(0.02, 0.08))

    def human_click(self, page, selector):
        page.locator(selector).hover()
        time.sleep(random.uniform(0.2, 1.0))
        page.locator(selector).click(delay=random.randint(80, 220))

    # ──────────────────────────────────────────
    # فایلنام یکتا
    # ──────────────────────────────────────────

    def unique_filename(self, prompt_text: str) -> str:
        safe = re.sub(r'[^a-zA-Z0-9_-]+', '_', prompt_text)[:40]
        return f"{safe}_{int(time.time())}_{uuid.uuid4().hex[:6]}.jpg"

    # ──────────────────────────────────────────
    # دانلود تصویر — پشتیبانی از blob و http
    # ──────────────────────────────────────────

    def download_image(self, page, context, image_url: str, filename: str) -> str:
        save_path = os.path.join(self.download_dir, filename)

        if image_url.startswith("blob:"):
            print("📦 Blob URL detected — using JS to extract...")
            b64_data = page.evaluate("""async (url) => {
                const response = await fetch(url);
                const blob = await response.blob();
                return new Promise(resolve => {
                    const reader = new FileReader();
                    reader.onloadend = () => resolve(reader.result);
                    reader.readAsDataURL(blob);
                });
            }""", image_url)
            # b64_data = "data:image/jpeg;base64,/9j/..."
            raw = b64_data.split(",", 1)[1]
            with open(save_path, "wb") as f:
                f.write(base64.b64decode(raw))
        else:
            response = context.request.get(image_url)
            with open(save_path, "wb") as f:
                f.write(response.body())

        print("✅ Image downloaded:", save_path)
        return save_path

    # ──────────────────────────────────────────
    # پیدا کردن دکمه Create image با fallback
    # ──────────────────────────────────────────

    def _find_create_image_btn(self, page):
        """
        چند selector مختلف امتحان می‌کند تا دکمه Create image را پیدا کند.
        """
        selectors = [
            "button[role='menuitemcheckbox']:has-text('Create image')",
            "button[role='option']:has-text('Create image')",
            "[role='menuitem']:has-text('Create image')",
            "button:has-text('Create image')",
            "[aria-label='Create image']",
        ]
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                loc.wait_for(state="visible", timeout=3000)
                print(f"✅ Found 'Create image' via: {sel}")
                return loc
            except Exception:
                continue

        raise RuntimeError("❌ 'Create image' button not found in any known selector")

    # ──────────────────────────────────────────
    # اجرای اصلی
    # ──────────────────────────────────────────

    def generate(self, prompt_text: str) -> str:

        with sync_playwright() as p:

            context = p.chromium.launch_persistent_context(
                user_data_dir=self.chrome_profile,
                headless=False,
                channel="chrome",
                args=[
                    "--disable-blink-features=AutomationControlled",
                    f"--window-size={random.randint(1100, 1500)},{random.randint(700, 900)}"
                ]
            )

            page = context.pages[0] if context.pages else context.new_page()

            # ❶ باز کردن Gemini
            page.goto(self.gemini_url)
            self.human_sleep(8, 12)

            # ──────────────────────────────────
            # انتخاب Image Tool
            # ──────────────────────────────────
            try:
                page.locator(
                    "//button[contains(@class,'toolbox-drawer-item-deselect-button')]"
                    "//mat-icon[@data-mat-icon-name='close']"
                ).wait_for(timeout=3000)
                print("Image tool already selected.")

            except Exception:
                print("Selecting Image tool...")

                tools_btn = page.get_by_role("button", name="Tools")
                tools_btn.hover()
                time.sleep(1)
                tools_btn.click()
                time.sleep(1)

                # ✅ پیدا کردن دکمه با fallback
                create_img_btn = self._find_create_image_btn(page)
                create_img_btn.hover()
                time.sleep(1)
                create_img_btn.click()
                time.sleep(2)

            # ──────────────────────────────────
            # ❷ نوشتن پرامپت
            # ──────────────────────────────────
            textarea = page.locator("div.ql-editor")
            textarea.click()
            self.human_sleep(0.5, 1.0)

            self.human_type(textarea, prompt_text)
            self.human_sleep(0.7, 1.5)

            self.human_click(page, "button[aria-label='Send message']")

            # صبر برای تولید تصویر
            time.sleep(70)

            # ──────────────────────────────────
            # ❸ پیدا کردن آخرین پیام کاربر
            # ──────────────────────────────────
            user_messages = page.locator("span.user-query-bubble-with-background")
            last_user_msg = user_messages.nth(user_messages.count() - 1)

            # ──────────────────────────────────
            # ❹ بلاک پاسخ AI بعد از پیام ما
            # ──────────────────────────────────
            next_ai_block = last_user_msg.locator(
                "xpath=ancestor::user-query/following::div"
                "[contains(@class,'response-container-content')][1]"
            )

            img_element = next_ai_block.locator("single-image img")
            image_url = img_element.get_attribute("src")

            print("🔗 Image URL:", image_url)

            filename = self.unique_filename(prompt_text)

            # ✅ ارسال page به download_image برای پشتیبانی از blob
            saved_path = self.download_image(page, context, image_url, filename)

            context.close()
            return saved_path


# ──────────────────────────────────────────
# Functional API
# ──────────────────────────────────────────

def generate_gemini_image(prompt_text, download_dir, chrome_profile, gemini_url):
    generator = GeminiImageGenerator(
        download_dir=download_dir,
        chrome_profile=chrome_profile,
        gemini_url=gemini_url
    )
    return generator.generate(prompt_text)
