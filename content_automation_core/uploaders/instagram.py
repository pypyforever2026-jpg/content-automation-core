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

    def _is_login_page(self, current_url: str) -> bool:
        """تشخیص ریدایرکت به صفحه login بافر/گوگل."""
        if not current_url:
            return False
        login_markers = (
            "login.buffer.com",
            "account.buffer.com",
            "/login",
            "accounts.google.com",
            "signin",
        )
        url_lc = current_url.lower()
        return any(marker in url_lc for marker in login_markers)

    def _wait_for_buffer_ready(self, timeout: int = 25) -> bool:
        """
        منتظر می‌مانیم تا shell بافر واقعاً لود شود (دکمه Create یا منوی sidebar).
        صرفِ تغییر URL کافی نیست؛ خیلی وقت‌ها URL درست است ولی صفحه هنوز سفید است.
        """
        ready_selectors = [
            (By.CSS_SELECTOR, "button[aria-haspopup='menu']"),
            (By.CSS_SELECTOR, "[data-channel='instagram']"),
            (By.XPATH, "//button[contains(., 'Create')]"),
            (By.XPATH, "//button[contains(., 'New Post')]"),
        ]
        end_time = time.time() + timeout
        while time.time() < end_time:
            for by, sel in ready_selectors:
                try:
                    el = self.driver.find_element(by, sel)
                    if el.is_displayed():
                        return True
                except Exception:
                    continue
            time.sleep(0.5)
        return False

    def navigate_to(self, url: str, retries: int = 3, initial_wait: int = 5) -> bool:
        """
        Navigate به URL با retry.

        هر تلاش:
          1. driver.get(url)
          2. صبر initial_wait ثانیه تا redirect/session cookie اعمال شود
          3. بررسی login redirect (fail fast)
          4. _wait_for_buffer_ready با timeout=40s — React SPA بافر روی cold-browser
             می‌تواند ۲۰-۳۰ ثانیه طول بکشد و باید صبر کافی داشته باشیم.

        مهم: هیچ driver.refresh() بین تلاش‌ها نیست. refresh حین لود SPA
        را interrupt می‌کند و باعث می‌شود همان چرخه تکرار شود.
        """
        if "buffer.com" in url:
            expected_fragment = url.split("buffer.com", 1)[-1].rstrip("/")
        else:
            expected_fragment = ""

        for attempt in range(1, retries + 1):
            print(f"🔗 Navigating to Buffer (attempt {attempt}/{retries})...")
            try:
                self.driver.get(url)
            except Exception as e:
                print(f"⚠️ driver.get failed: {e}")
                time.sleep(5)
                continue

            # کوچکترین صبر اولیه تا redirect کامل شود
            time.sleep(initial_wait)
            current = self.driver.current_url or ""

            # ریدایرکت به login → نشانه‌ی session منقضی شده
            if self._is_login_page(current):
                print(
                    "❌ Redirected to login page — Chrome profile session expired. "
                    f"current_url={current}"
                )
                return False

            url_ok = (not expected_fragment) or (expected_fragment in current)

            # صبر سخاوتمندانه برای hydrate شدن React (بدون هیچ refresh)
            page_ready = self._wait_for_buffer_ready(timeout=40)

            # URL ممکن است بعد از redirect عوض شده باشد؛ مجدداً بخوانیم
            current = self.driver.current_url or ""
            if self._is_login_page(current):
                print(f"❌ Login redirect detected after wait → {current}")
                return False
            url_ok = (not expected_fragment) or (expected_fragment in current)

            if url_ok and page_ready:
                print(f"✅ Navigation successful: {current}")
                return True

            print(
                f"⚠️ Attempt {attempt} failed (url_ok={url_ok}, page_ready={page_ready}, "
                f"current={current}) — retrying with fresh get..."
            )
            time.sleep(3)

        print(f"❌ Failed to navigate to {url} after {retries} attempts")
        return False

    # ─────────────────────────────
    # Composer
    # ─────────────────────────────
    def _open_composer(self) -> bool:
        """
        تلاش برای باز کردن composer (Create new → Post). دو مسیر UI جدید و قدیم
        را امتحان می‌کند و True/False برمی‌گرداند تا فراخواننده بتواند retry کند.
        """
        # New UI
        try:
            create_btn = self.wait_for_clickable(
                By.CSS_SELECTOR, "button[aria-haspopup='menu']", timeout=15
            )
            create_btn.click()
            time.sleep(1)
            post_item = self.wait_for_clickable(
                By.XPATH, "//div[@role='menuitem' and contains(., 'Post')]", timeout=10
            )
            post_item.click()
            print("✅ New UI: Create new → Post")
            return True
        except Exception as e:
            print(f"🕹 New UI failed ({e}) — trying old UI fallback")

        # Old UI fallback
        try:
            insta_btn = self.wait_for_clickable(
                By.CSS_SELECTOR, "[data-channel='instagram']", timeout=10
            )
            insta_btn.click()
            new_post_btn = self.wait_for_clickable(
                By.XPATH, "//button[contains(., 'New')]", timeout=10
            )
            new_post_btn.click()
            print("✅ Old UI: Instagram → New")
            return True
        except Exception as e:
            print(f"❌ Old UI fallback also failed: {e}")
            return False

    # ─────────────────────────────
    # Main Actions
    # ─────────────────────────────
    def upload_reels(self, file_path: str, caption: str, max_relaunch: int = 2) -> bool:
        """
        Upload یک Reel به Buffer.

        max_relaunch: اگر navigate یا open_composer شکست بخورد، driver کاملاً
        بسته و دوباره باز می‌شود (تا max_relaunch بار). این مهم‌ترین لایه‌ی
        مقاومت در برابر باگ «به URL نمی‌رود» است.
        """
        file_path = os.path.abspath(file_path)

        for relaunch_attempt in range(max_relaunch + 1):
            self.build_driver()
            try:
                if not self.navigate_to(self.buffer_url):
                    print(
                        f"❌ Could not reach Buffer URL "
                        f"(relaunch {relaunch_attempt}/{max_relaunch})"
                    )
                    self.close_driver()
                    if relaunch_attempt < max_relaunch:
                        print("🔄 Relaunching browser from scratch...")
                        time.sleep(3)
                        continue
                    return False

                if not self._open_composer():
                    print(
                        f"❌ Could not open composer "
                        f"(relaunch {relaunch_attempt}/{max_relaunch})"
                    )
                    self.close_driver()
                    if relaunch_attempt < max_relaunch:
                        print("🔄 Relaunching browser from scratch...")
                        time.sleep(3)
                        continue
                    return False

                # navigate + composer هر دو اوکی → از حلقه‌ی relaunch خارج شو
                break
            except Exception as e:
                print(f"⚠️ Unexpected error during navigate/composer: {e}")
                self.close_driver()
                if relaunch_attempt < max_relaunch:
                    print("🔄 Relaunching browser from scratch...")
                    time.sleep(3)
                    continue
                return False

        try:
            time.sleep(2)

            # ❷ انتخاب Reels
            try:
                reels_label = self.wait_for_clickable(By.CSS_SELECTOR, "label[for='reels']")
                reels_label.click()
                print("✅ Reels clicked")
            except Exception:
                print("⚠️ Reels click failed")

            # ❸ آپلود فایل (با wait تا input واقعاً ظاهر بشود)
            try:
                upload_input = WebDriverWait(self.driver, 20).until(
                    EC.presence_of_element_located(
                        (By.CSS_SELECTOR, "input[type='file']")
                    )
                )
                upload_input.send_keys(file_path)
                print("📁 File uploaded")
                time.sleep(3)
            except Exception as e:
                print(f"⚠️ File upload failed: {e}")
                return False

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

                if not publish_btn:
                    print("⚠️ Publish button not found")
                    return False

                self.js_click(publish_btn)
                print("✅ Publish clicked — verifying...")

            except Exception as e:
                print(f"⚠️ Publish click failed: {e}")
                return False

            # ❼ تأیید واقعی publish — به جای sleep(30) خام، بسته شدن modal یا
            # ظهور toast/state موفقیت را چک می‌کنیم.
            return self._verify_publish_succeeded(timeout=60)

        finally:
            self.close_driver()

    def _verify_publish_succeeded(self, timeout: int = 60) -> bool:
        """
        بعد از کلیک Publish، چک می‌کند که آیا ارسال واقعاً انجام شده یا نه.

        موفقیت یعنی یکی از این‌ها:
          - Modal/Composer بسته شده (دکمه publish دیگر در DOM نیست/visible نیست).
          - Toast یا متنی شامل «Sharing now», «Post added», «posted», «scheduled» پیدا شد.

        اگر هیچ‌کدام تو timeout اتفاق نیفتد False برمی‌گرداند تا تماس‌گیرنده
        بفهمد publish انگار شکست خورده.
        """
        success_text_xpath = (
            "//*[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', "
            "'abcdefghijklmnopqrstuvwxyz'), 'sharing now') or "
            "contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', "
            "'abcdefghijklmnopqrstuvwxyz'), 'post added') or "
            "contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', "
            "'abcdefghijklmnopqrstuvwxyz'), 'has been posted') or "
            "contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', "
            "'abcdefghijklmnopqrstuvwxyz'), 'successfully posted') or "
            "contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', "
            "'abcdefghijklmnopqrstuvwxyz'), 'queued')]"
        )

        publish_btn_selectors = [
            (By.XPATH, "//button[normalize-space(text())='Share Now']"),
            (By.XPATH, "//button[normalize-space(text())='Publish Now']"),
            (By.CSS_SELECTOR, "button[data-testid='publish-button']"),
        ]

        end_time = time.time() + timeout
        last_log = 0.0
        while time.time() < end_time:
            # 1) دکمه Publish دیگر visible نیست → modal بسته شده
            publish_visible = False
            for by, sel in publish_btn_selectors:
                try:
                    el = self.driver.find_element(by, sel)
                    if el.is_displayed():
                        publish_visible = True
                        break
                except Exception:
                    continue
            if not publish_visible:
                print("✅ Publish modal closed — upload confirmed")
                # یک کم صبر می‌کنیم تا اگر toast هست هم لود شود
                time.sleep(3)
                return True

            # 2) toast موفقیت ظاهر شد
            try:
                el = self.driver.find_element(By.XPATH, success_text_xpath)
                if el.is_displayed():
                    print(f"✅ Publish success toast detected: '{el.text[:80]}'")
                    return True
            except Exception:
                pass

            now = time.time()
            if now - last_log > 5:
                print("⏳ Waiting for Buffer to confirm publish...")
                last_log = now
            time.sleep(1)

        print("❌ Publish click did not produce a confirmed success within timeout")
        return False


# ─────────────────────────────
# Functional API
# ─────────────────────────────
def upload_instagram_reels(file_path: str, caption: str, profile_path: str, buffer_url: str) -> bool:
    uploader = InstagramUploader(profile_path, buffer_url)
    return uploader.upload_reels(file_path, caption)
