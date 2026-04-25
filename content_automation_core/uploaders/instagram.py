import os
import time
import subprocess
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC


def _set_clipboard_windows(text: str) -> bool:
    """
    متن را روی clipboard ویندوز مینویسد.
    از clip.exe داخلی ویندوز استفاده میکند — بدون ctypes، بدون pip.
    BOM اضافه میکند تا clip.exe متن را Unicode بخواند (برای فارسی ضروری است).
    """
    try:
        bom = b"\xff\xfe"  # UTF-16 LE BOM
        encoded = bom + text.encode("utf-16-le")
        proc = subprocess.Popen("clip", stdin=subprocess.PIPE, shell=True)
        proc.communicate(input=encoded)
        return proc.returncode == 0
    except Exception as e:
        print(f"⚠️ clip.exe failed: {e}")
        return False


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
        """کلیک با JavaScript برای عناصری که با click() عادی کار نمیکنند"""
        self.driver.execute_script("arguments[0].scrollIntoView({block:'center'}); arguments[0].click();", element)

    def navigate_to(self, url: str, retries: int = 3, wait_sec: int = 8) -> bool:
        """
        Navigate به URL با retry — حل مشکل ماندن روی home page.
        بعد از هر get() بررسی میکند آیا URL عوض شده؛ اگر نه دوباره تلاش میکند.
        """
        expected_fragment = url.split("buffer.com")[-1]  # مثلاً /channels/abc123
        for attempt in range(1, retries + 1):
            print(f"🔗 Navigating to Buffer (attempt {attempt}/{retries})...")
            self.driver.get(url)
            time.sleep(wait_sec)
            current = self.driver.current_url
            if expected_fragment and expected_fragment in current:
                print(f"✅ Navigation successful: {current}")
                return True
            # اگه هنوز روی صفحه اشتباه هست، یه بار صبر اضافی
            print(f"⚠️ Still on wrong page: {current} — retrying...")
            time.sleep(3)
        print(f"❌ Failed to navigate to {url} after {retries} attempts")
        return False

    # ─────────────────────────────
    # Main Actions
    # ─────────────────────────────
    def upload_reels(self, file_path: str, caption: str) -> bool:
        file_path = os.path.abspath(file_path)
        self.build_driver()

        try:
            if not self.navigate_to(self.buffer_url):
                print("❌ Could not reach Buffer URL — aborting")
                return False

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
                    # چند selector مختلف امتحان میکنیم (Buffer UI ممکنه تغییر کنه)
                    caption_selectors = [
                        "[data-testid='composer-text-area']",
                        ".public-DraftEditor-content",
                        "div[contenteditable='true']",
                        "div[role='textbox']",
                    ]
                    caption_box = None
                    for sel in caption_selectors:
                        try:
                            caption_box = WebDriverWait(self.driver, 8).until(
                                EC.presence_of_element_located((By.CSS_SELECTOR, sel))
                            )
                            print(f"✅ Caption box found via: {sel}")
                            break
                        except Exception:
                            continue

                    if caption_box is None:
                        print("⚠️ Caption box not found — skipping caption")
                    else:
                        # ── کلیک برای فوکوس ──
                        caption_box.click()
                        time.sleep(0.4)

                        # ── Ctrl+A برای پاک کردن محتوای قبلی ──
                        actions = ActionChains(self.driver)
                        actions.key_down(Keys.CONTROL).send_keys('a').key_up(Keys.CONTROL).perform()
                        time.sleep(0.2)

                        # ── استراتژی ۱: clipboard + Ctrl+V ──
                        clip_ok = _set_clipboard_windows(caption)
                        time.sleep(0.4)  # صبر تا clip.exe تمام کند

                        if clip_ok:
                            actions2 = ActionChains(self.driver)
                            actions2.key_down(Keys.CONTROL).send_keys('v').key_up(Keys.CONTROL).perform()
                            time.sleep(0.5)
                            print("✅ Caption pasted via clipboard (Ctrl+V)")
                        else:
                            # ── استراتژی ۲: send_keys مستقیم (fallback) ──
                            print("⚠️ Clipboard failed — falling back to send_keys")
                            caption_box.send_keys(caption)
                            time.sleep(0.5)
                            print("✅ Caption written via send_keys")

                except Exception as e:
                    print(f"⚠️ Caption write failed: {e}")


            # ❺ باز کردن منوی Schedule و انتخاب «Now»
            try:
                # ── مرحله ۱: کلیک واقعی روی trigger (نه JS — Radix UI به event واقعی نیاز دارد)
                schedule_btn = self.wait_for_clickable(
                    By.CSS_SELECTOR, "button[data-testid='schedule-selector-trigger']"
                )
                schedule_btn.click()
                print("🖱 Schedule button clicked")

                # ── مرحله ۲: صبر تا منو واقعاً باز بشه (aria-expanded=true)
                WebDriverWait(self.driver, 10).until(
                    EC.presence_of_element_located((
                        By.CSS_SELECTOR,
                        "button[data-testid='schedule-selector-trigger'][aria-expanded='true']"
                    ))
                )
                print("✅ Schedule menu confirmed open")

                # ── مرحله ۳: صبر اضافی برای render شدن آیتمها توسط React
                time.sleep(2)

                # ── مرحله ۴: پیدا کردن div[role='menuitem'] که شامل «Now» است
                now_menuitem = WebDriverWait(self.driver, 10).until(
                    EC.presence_of_element_located((
                        By.XPATH,
                        "//div[@role='menuitem'][.//p[normalize-space(text())='Now']]"
                    ))
                )
                # کلیک واقعی اول، اگر نشد JS fallback
                try:
                    now_menuitem.click()
                except Exception:
                    self.driver.execute_script("arguments[0].click();", now_menuitem)
                print("⏱ Publish set to Now")
                time.sleep(1)

            except Exception as e:
                print(f"⚠️ Schedule/Now click failed: {e}")

            # ❻ کلیک Publish
            try:
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


# ─────────────────────────────
# Functional API
# ─────────────────────────────
def upload_instagram_reels(file_path: str, caption: str, profile_path: str, buffer_url: str) -> bool:
    uploader = InstagramUploader(profile_path, buffer_url)
    return uploader.upload_reels(file_path, caption)
