import os
import time
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
from selenium.common.exceptions import TimeoutException, ElementClickInterceptedException
from selenium.webdriver.common.keys import Keys


class TikTokUploader:
    """
    TikTok uploader with popup handling.
    Algorithm, selectors, and behavior are EXACTLY the same as your original script.
    """

    def __init__(self, cookies_file=None, headless=False):
        self.cookies_file = cookies_file
        self.headless = headless
        self.driver = None

    # -----------------------------
    # Driver Setup
    # -----------------------------
    def setup_driver(self):
        chrome_options = Options()
        chrome_options.add_argument('--disable-blink-features=AutomationControlled')
        chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
        chrome_options.add_experimental_option('useAutomationExtension', False)
        chrome_options.add_argument('--disable-dev-shm-usage')
        chrome_options.add_argument('--no-sandbox')

        if self.headless:
            chrome_options.add_argument("--headless=new")

        driver = webdriver.Chrome(options=chrome_options)
        driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

        self.driver = driver

    # -----------------------------
    # Load Cookies
    # -----------------------------
    def load_cookies(self):
        if not self.cookies_file or not os.path.exists(self.cookies_file):
            return

        self.driver.get("https://www.tiktok.com")
        time.sleep(2)

        with open(self.cookies_file, 'r', encoding='utf-8') as f:
            for line in f.read().split("\n"):
                if line.strip() and not line.startswith("#"):
                    parts = line.split("\t")
                    if len(parts) >= 7:
                        cookie = {
                            "name": parts[5],
                            "value": parts[6],
                            "domain": parts[0],
                            "path": parts[2],
                        }
                        try:
                            self.driver.add_cookie(cookie)
                        except:
                            pass

        self.driver.refresh()
        time.sleep(3)

    # -----------------------------
    # Popup: Cancel
    # -----------------------------
    def handle_cancel_popup(self):
        selectors = [
            "//button[text()='Cancel']",
            "//button[contains(text(), 'Cancel')]",
        ]

        for selector in selectors:
            try:
                btn = WebDriverWait(self.driver, 3).until(
                    EC.element_to_be_clickable((By.XPATH, selector))
                )
                self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
                time.sleep(0.5)
                try:
                    btn.click()
                except:
                    self.driver.execute_script("arguments[0].click();", btn)

                print("✅ پاپ‌آپ Cancel بسته شد")
                return True
            except:
                continue

        return False

    # -----------------------------
    # Popup: Post now
    # -----------------------------
    def handle_post_now_popup(self):
        selectors = [
            "//button[text()='Post now']",
            "//button[contains(text(), 'Post now')]",
            "//button[normalize-space()='Post now']",
            "//*[@role='button' and contains(text(), 'Post now')]",
        ]

        for selector in selectors:
            try:
                btn = WebDriverWait(self.driver, 5).until(
                    EC.element_to_be_clickable((By.XPATH, selector))
                )
                self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
                time.sleep(0.5)

                try:
                    btn.click()
                except:
                    self.driver.execute_script("arguments[0].click();", btn)

                print("✅ پاپ‌آپ Post now کلیک شد")
                return True

            except:
                continue

        # fallback JS
        try:
            clicked = self.driver.execute_script("""
                var buttons = document.querySelectorAll('button');
                for (var i=0; i<buttons.length; i++){
                    var t = buttons[i].innerText || buttons[i].textContent;
                    if (t && t.trim() === 'Post now'){
                        buttons[i].click();
                        return true;
                    }
                }
                return false;
            """)
            if clicked:
                print("✅ پاپ‌آپ Post now با JS کلیک شد")
                return True
        except:
            pass

        return False

    # -----------------------------
    # Main Upload Function
    # -----------------------------
    def upload(self, video_path, description=""):
        try:
            print("🚀 شروع آپلود تیک‌تاک...")

            self.setup_driver()
            print("✅ درایور آماده شد")

            self.load_cookies()
            print("✅ کوکی‌ها بارگذاری شدند")

            self.driver.get("https://www.tiktok.com/upload")
            time.sleep(5)

            # Upload file
            file_input = WebDriverWait(self.driver, 20).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "input[type='file']"))
            )
            file_input.send_keys(os.path.abspath(video_path))
            print("📹 ویدئو انتخاب شد")

            time.sleep(15)

            # Handle "Got it"
            try:
                got_it = WebDriverWait(self.driver, 10).until(
                    EC.element_to_be_clickable((By.XPATH, "//button[.//div[contains(text(),'Got it')]]"))
                )
                self.driver.execute_script("arguments[0].scrollIntoView(true);", got_it)
                got_it.click()
                print("✔️ Got it کلیک شد")
                time.sleep(2)
            except:
                pass

            # Write description
            if description:
                try:
                    desc_field = WebDriverWait(self.driver, 20).until(
                        EC.presence_of_element_located((
                            By.CSS_SELECTOR,
                            ".notranslate.public-DraftEditor-content, [contenteditable='true']"
                        ))
                    )
                    self.driver.execute_script("arguments[0].scrollIntoView(true);", desc_field)
                    time.sleep(1)

                    desc_field.click()
                    desc_field.send_keys(Keys.CONTROL, "a")
                    desc_field.send_keys(Keys.DELETE)
                    desc_field.send_keys(description)
                    print("✍️ کپشن نوشته شد")
                except Exception as e:
                    print("⚠️ خطا در نوشتن کپشن:", e)

            # Scroll
            self.driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(2)

            # Cancel popup
            self.handle_cancel_popup()

            # Click Post
            post_selectors = [
                "//button[text()='Post']",
                "//button[contains(text(), 'Post')]",
                "//button[contains(@class, 'post')]",
            ]

            clicked = False
            for selector in post_selectors:
                try:
                    btn = WebDriverWait(self.driver, 10).until(
                        EC.element_to_be_clickable((By.XPATH, selector))
                    )
                    self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
                    time.sleep(1)

                    try:
                        btn.click()
                    except:
                        self.handle_cancel_popup()
                        self.driver.execute_script("arguments[0].click();", btn)

                    print("📤 دکمه Post کلیک شد")
                    clicked = True
                    break
                except:
                    continue

            if not clicked:
                self.driver.execute_script("""
                    var buttons = document.querySelectorAll('button');
                    for (var i=0; i<buttons.length; i++){
                        var t = buttons[i].innerText || buttons[i].textContent;
                        if (t && t.trim() === 'Post'){
                            buttons[i].click();
                            return;
                        }
                    }
                """)
                print("📤 دکمه Post با JS کلیک شد")

            # Post now popup
            for _ in range(5):
                if self.handle_post_now_popup():
                    break
                time.sleep(2)

            print("🎉 آپلود کامل شد")
            return True

        except Exception as e:
            print("❌ خطای کلی:", e)
            return False

        finally:
            if self.driver:
                time.sleep(5)
                self.driver.quit()


# -----------------------------
# Simple Functional API
# -----------------------------
def upload_video_to_tiktok(video_path, description="", cookies_file=None, headless=False):
    uploader = TikTokUploader(cookies_file=cookies_file, headless=headless)
    return uploader.upload(video_path, description)
