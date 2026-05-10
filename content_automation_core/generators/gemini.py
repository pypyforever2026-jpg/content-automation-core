import logging
import os
import random
import re
import time
import uuid

from playwright.sync_api import sync_playwright

logger = logging.getLogger(__name__)


class GeminiImageGenerator:
    """Drive Gemini image generation in Chrome via Playwright (persistent profile)."""

    def __init__(self, download_dir, chrome_profile, gemini_url, chrome_binary=None):
        self.download_dir = download_dir
        self.chrome_profile = chrome_profile
        self.gemini_url = gemini_url
        self.chrome_binary = chrome_binary

        os.makedirs(download_dir, exist_ok=True)

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
                steps=random.randint(2, 5),
            )
            time.sleep(random.uniform(0.02, 0.08))

    def human_click(self, page, selector):
        page.locator(selector).hover()
        time.sleep(random.uniform(0.2, 1.0))
        page.locator(selector).click(delay=random.randint(80, 220))

    def unique_filename(self, prompt_text: str) -> str:
        safe = re.sub(r"[^a-zA-Z0-9_-]+", "_", prompt_text)[:40]
        return f"{safe}_{int(time.time())}_{uuid.uuid4().hex[:6]}.jpg"

    def download_image(self, context, image_url: str, filename: str, img_locator=None):
        save_path = os.path.join(self.download_dir, filename)

        if image_url.startswith("blob:") and img_locator is not None:
            logger.info("Blob URL: saving via element screenshot")
            img_locator.screenshot(path=save_path)
        else:
            response = context.request.get(image_url)
            with open(save_path, "wb") as f:
                f.write(response.body())

        logger.info("Image downloaded: %s", save_path)
        return save_path

    def generate(self, prompt_text: str) -> str:

        with sync_playwright() as p:

            context = p.chromium.launch_persistent_context(
                user_data_dir=self.chrome_profile,
                headless=False,
                channel="chrome",
                args=[
                    "--disable-blink-features=AutomationControlled",
                    f"--window-size={random.randint(1100, 1500)},{random.randint(700, 900)}",
                ],
            )

            page = context.pages[0] if context.pages else context.new_page()

            page.goto(self.gemini_url)
            self.human_sleep(8, 12)

            try:
                page.locator(
                    "//button[contains(@class,'toolbox-drawer-item-deselect-button')]"
                    "//mat-icon[@data-mat-icon-name='close']"
                ).wait_for(timeout=3000)
                logger.debug("Image tool already selected")
            except Exception:
                logger.debug("Selecting Image tool")

                tools_btn = page.get_by_role("button", name="Tools")
                tools_btn.hover()
                time.sleep(1)
                tools_btn.click()
                time.sleep(1)

                create_img_btn = page.locator("button[role='menuitemcheckbox']").filter(
                    has_text="Create image"
                )
                create_img_btn.hover()
                time.sleep(1)
                create_img_btn.click()
                time.sleep(2)

            textarea = page.locator("div.ql-editor")
            textarea.click()
            self.human_sleep(0.5, 1.0)

            self.human_type(textarea, prompt_text)
            self.human_sleep(0.7, 1.5)

            self.human_click(page, "button[aria-label='Send message']")

            time.sleep(70)

            user_messages = page.locator("span.user-query-bubble-with-background")
            last_user_msg = user_messages.nth(user_messages.count() - 1)

            next_ai_block = last_user_msg.locator(
                "xpath=ancestor::user-query/following::div[contains(@class,'response-container-content')][1]"
            )

            img_element = next_ai_block.locator("single-image img")
            image_url = img_element.get_attribute("src")

            logger.debug("Image URL: %s", image_url)

            filename = self.unique_filename(prompt_text)
            save_path = os.path.join(self.download_dir, filename)

            img_element.hover()
            time.sleep(1.5)

            download_btn = next_ai_block.locator(
                "button[data-test-id='download-generated-image-button']"
            )
            download_btn.wait_for(state="visible", timeout=10_000)

            with page.expect_download(timeout=60_000) as dl_info:
                download_btn.click()

            dl = dl_info.value
            dl.save_as(save_path)
            logger.info("Image downloaded: %s", save_path)

            context.close()
            return save_path


def generate_gemini_image(prompt_text, download_dir, chrome_profile, gemini_url):
    generator = GeminiImageGenerator(
        download_dir=download_dir,
        chrome_profile=chrome_profile,
        gemini_url=gemini_url,
    )
    return generator.generate(prompt_text)
