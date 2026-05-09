"""
TikTok uploader.

Uses cookies (not a Chrome user-data-dir), so it does NOT suffer from the
profile-lock storms that Instagram/YouTube did. Hardening here is limited
to:
  - hard page-load / script timeouts on the driver
  - safe_driver_call() guards on the few commands that historically hung
  - force_close() guarantees the browser never hangs on shutdown
  - global per-upload timeout via run_with_upload_timeout()

Behavior, selectors, and ordering of UI steps are unchanged.
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from typing import Optional

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from ._browser import (
    GLOBAL_UPLOAD_TIMEOUT,
    PAGE_LOAD_TIMEOUT,
    QUIT_TIMEOUT,
    SAFE_COMMAND_TIMEOUT,
    SCRIPT_TIMEOUT,
    run_with_upload_timeout,
    safe_driver_call,
    setup_logging,
)

logger = logging.getLogger(__name__)


class _DriverHolder:
    """Minimal session-like wrapper so run_with_upload_timeout can force-close."""

    def __init__(self, log_prefix: str = ""):
        self.driver: Optional[webdriver.Chrome] = None
        self.log_prefix = log_prefix

    def force_close(self):
        d = self.driver
        self.driver = None
        if d is None:
            return
        try:
            safe_driver_call(d.quit, timeout=QUIT_TIMEOUT)
        except Exception as e:
            logger.warning(f"{self.log_prefix}[BROWSER] quit failed: {e}")


class TikTokUploader:
    """TikTok uploader (cookies-based, no Chrome profile)."""

    def __init__(self, cookies_file: Optional[str] = None, headless: bool = False):
        setup_logging("uploader.log")
        self.cookies_file = cookies_file
        self.headless = headless
        self.upload_id = uuid.uuid4().hex[:8]
        self.log_prefix = f"[TK][upload_id={self.upload_id}]"
        self._holder = _DriverHolder(self.log_prefix)
        self.driver: Optional[webdriver.Chrome] = None

    # ── driver lifecycle ───────────────────────────────────────────────────

    def setup_driver(self) -> None:
        opts = Options()
        opts.add_argument("--disable-blink-features=AutomationControlled")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--no-first-run")
        opts.add_argument("--no-default-browser-check")
        opts.add_argument("--disable-session-crashed-bubble")
        opts.add_argument("--restore-last-session=false")
        opts.add_argument("--window-size=1920,1080")
        opts.add_experimental_option("excludeSwitches", ["enable-automation"])
        opts.add_experimental_option("useAutomationExtension", False)
        if self.headless:
            opts.add_argument("--headless=new")

        d = webdriver.Chrome(options=opts)
        try:
            d.set_page_load_timeout(PAGE_LOAD_TIMEOUT)
            d.set_script_timeout(SCRIPT_TIMEOUT)
        except Exception:
            pass
        try:
            d.execute_script(
                "Object.defineProperty(navigator, 'webdriver', "
                "{get: () => undefined})"
            )
        except Exception:
            pass
        self.driver = d
        self._holder.driver = d
        logger.info(f"{self.log_prefix}[BROWSER] driver ready")

    def force_close(self) -> None:
        self._holder.force_close()
        self.driver = None
        logger.info(f"{self.log_prefix}[BROWSER] closed")

    # ── cookies ────────────────────────────────────────────────────────────

    def load_cookies(self) -> None:
        if not self.cookies_file or not os.path.exists(self.cookies_file):
            return
        try:
            safe_driver_call(
                lambda: self.driver.get("https://www.tiktok.com"),
                timeout=PAGE_LOAD_TIMEOUT + 5,
            )
        except Exception as e:
            logger.warning(f"{self.log_prefix}[COOKIES] initial nav failed: {e}")
        time.sleep(2)

        with open(self.cookies_file, "r", encoding="utf-8") as f:
            for line in f.read().split("\n"):
                if not line.strip() or line.startswith("#"):
                    continue
                parts = line.split("\t")
                if len(parts) < 7:
                    continue
                cookie = {
                    "name": parts[5],
                    "value": parts[6],
                    "domain": parts[0],
                    "path": parts[2],
                }
                try:
                    self.driver.add_cookie(cookie)
                except Exception:
                    pass

        try:
            safe_driver_call(self.driver.refresh, timeout=PAGE_LOAD_TIMEOUT + 5)
        except Exception:
            pass
        time.sleep(3)

    # ── popups ─────────────────────────────────────────────────────────────

    def handle_cancel_popup(self) -> bool:
        for selector in ("//button[text()='Cancel']", "//button[contains(text(), 'Cancel')]"):
            try:
                btn = WebDriverWait(self.driver, 3).until(
                    EC.element_to_be_clickable((By.XPATH, selector))
                )
                self.driver.execute_script(
                    "arguments[0].scrollIntoView({block:'center'});", btn
                )
                time.sleep(0.4)
                try:
                    btn.click()
                except Exception:
                    self.driver.execute_script("arguments[0].click();", btn)
                logger.info(f"{self.log_prefix}[POPUP] Cancel dismissed")
                return True
            except Exception:
                continue
        return False

    def handle_post_now_popup(self) -> bool:
        selectors = (
            "//button[text()='Post now']",
            "//button[contains(text(), 'Post now')]",
            "//button[normalize-space()='Post now']",
            "//*[@role='button' and contains(text(), 'Post now')]",
        )
        for selector in selectors:
            try:
                btn = WebDriverWait(self.driver, 5).until(
                    EC.element_to_be_clickable((By.XPATH, selector))
                )
                self.driver.execute_script(
                    "arguments[0].scrollIntoView({block:'center'});", btn
                )
                time.sleep(0.4)
                try:
                    btn.click()
                except Exception:
                    self.driver.execute_script("arguments[0].click();", btn)
                logger.info(f"{self.log_prefix}[POPUP] Post now clicked")
                return True
            except Exception:
                continue

        # JS fallback
        try:
            clicked = self.driver.execute_script(
                """
                var buttons = document.querySelectorAll('button');
                for (var i=0; i<buttons.length; i++){
                    var t = buttons[i].innerText || buttons[i].textContent;
                    if (t && t.trim() === 'Post now'){
                        buttons[i].click();
                        return true;
                    }
                }
                return false;
                """
            )
            if clicked:
                logger.info(f"{self.log_prefix}[POPUP] Post now via JS")
                return True
        except Exception:
            pass
        return False

    # ── upload page navigation ─────────────────────────────────────────────

    _UPLOAD_URLS = (
        "https://www.tiktok.com/tiktok-studio/upload",
        "https://www.tiktok.com/upload",
        "https://www.tiktok.com/creator-center/upload",
    )

    def _find_input_in_main_or_iframe(self, timeout: float):
        """Look for ``input[type=file]`` in the top doc; if not found, scan
        each iframe for the same selector. Returns the WebElement or None.

        IMPORTANT: when the input is found inside an iframe, we leave the
        driver focused on that iframe so subsequent interactions
        (Got it, description, Post) still work. We only restore default
        content if NO input was found.
        """
        end = time.time() + timeout
        # main doc
        try:
            self.driver.switch_to.default_content()
        except Exception:
            pass
        try:
            el = WebDriverWait(self.driver, max(1.0, min(8.0, end - time.time()))).until(
                EC.presence_of_element_located(
                    (By.CSS_SELECTOR, "input[type='file']")
                )
            )
            if el is not None:
                return el
        except Exception:
            pass

        # iframes
        try:
            iframes = self.driver.find_elements(By.CSS_SELECTOR, "iframe")
        except Exception:
            iframes = []
        for frame in iframes:
            if time.time() >= end:
                break
            try:
                self.driver.switch_to.default_content()
                self.driver.switch_to.frame(frame)
            except Exception:
                continue
            try:
                el = WebDriverWait(self.driver, 4).until(
                    EC.presence_of_element_located(
                        (By.CSS_SELECTOR, "input[type='file']")
                    )
                )
                if el is not None:
                    src = ""
                    try:
                        src = frame.get_attribute("src") or ""
                    except Exception:
                        pass
                    logger.info(
                        f"{self.log_prefix}[FILE] input found in iframe src={src!r}"
                    )
                    return el
            except Exception:
                continue

        try:
            self.driver.switch_to.default_content()
        except Exception:
            pass
        return None

    def _reach_upload_page_and_find_input(self):
        """Try TikTok upload URLs in order; for each, search top-doc + iframes
        for a file input. Returns the WebElement or None.

        Logs the actual URL we land on so login redirects are visible.
        """
        for url in self._UPLOAD_URLS:
            try:
                safe_driver_call(
                    lambda u=url: self.driver.get(u),
                    timeout=PAGE_LOAD_TIMEOUT + 5,
                )
            except Exception as e:
                logger.warning(f"{self.log_prefix}[NAV] get({url}) failed: {e}")
                continue
            time.sleep(4)
            try:
                current = self.driver.current_url
            except Exception:
                current = ""
            logger.info(f"{self.log_prefix}[NAV] landed at {current}")
            low = (current or "").lower()
            if "login" in low or "/signup" in low:
                logger.error(
                    f"{self.log_prefix}[NAV] redirected to login — cookies invalid"
                )
                return None
            el = self._find_input_in_main_or_iframe(timeout=20)
            if el is not None:
                return el
            logger.warning(
                f"{self.log_prefix}[FILE] input not on this page — trying next URL"
            )
        try:
            current = self.driver.current_url
        except Exception:
            current = ""
        logger.error(
            f"{self.log_prefix}[FILE] input not found on any upload URL "
            f"(last_url={current!r})"
        )
        return None

    # ── main upload ────────────────────────────────────────────────────────

    def upload(self, video_path: str, description: str = "") -> bool:
        """Upload a video. Always returns within GLOBAL_UPLOAD_TIMEOUT."""
        return run_with_upload_timeout(
            worker_fn=lambda: self._upload_inner(video_path, description),
            get_session_fn=lambda: self._holder,
            timeout_sec=GLOBAL_UPLOAD_TIMEOUT,
            log_prefix=self.log_prefix,
        )

    def _upload_inner(self, video_path: str, description: str) -> bool:
        try:
            logger.info(f"{self.log_prefix}[UPLOAD] starting")
            self.setup_driver()
            self.load_cookies()

            file_input = self._reach_upload_page_and_find_input()
            if file_input is None:
                return False
            try:
                safe_driver_call(
                    lambda: file_input.send_keys(os.path.abspath(video_path)),
                    timeout=SAFE_COMMAND_TIMEOUT,
                )
                logger.info(f"{self.log_prefix}[FILE] attached")
            except Exception as e:
                logger.error(f"{self.log_prefix}[FILE] send_keys failed: {e}")
                return False

            time.sleep(15)

            # "Got it"
            try:
                got_it = WebDriverWait(self.driver, 10).until(
                    EC.element_to_be_clickable(
                        (By.XPATH, "//button[.//div[contains(text(),'Got it')]]")
                    )
                )
                self.driver.execute_script("arguments[0].scrollIntoView(true);", got_it)
                got_it.click()
                time.sleep(2)
            except Exception:
                pass

            # Description
            if description:
                try:
                    desc_field = WebDriverWait(self.driver, 20).until(
                        EC.presence_of_element_located(
                            (
                                By.CSS_SELECTOR,
                                ".notranslate.public-DraftEditor-content, "
                                "[contenteditable='true']",
                            )
                        )
                    )
                    self.driver.execute_script(
                        "arguments[0].scrollIntoView(true);", desc_field
                    )
                    time.sleep(1)
                    desc_field.click()
                    desc_field.send_keys(Keys.CONTROL, "a")
                    desc_field.send_keys(Keys.DELETE)
                    desc_field.send_keys(description)
                    logger.info(f"{self.log_prefix}[CAPTION] written")
                except Exception as e:
                    logger.warning(f"{self.log_prefix}[CAPTION] write failed: {e}")

            self.driver.execute_script(
                "window.scrollTo(0, document.body.scrollHeight);"
            )
            time.sleep(2)

            self.handle_cancel_popup()

            # Post button
            post_selectors = (
                "//button[text()='Post']",
                "//button[contains(text(), 'Post')]",
                "//button[contains(@class, 'post')]",
            )
            clicked = False
            for selector in post_selectors:
                try:
                    btn = WebDriverWait(self.driver, 10).until(
                        EC.element_to_be_clickable((By.XPATH, selector))
                    )
                    self.driver.execute_script(
                        "arguments[0].scrollIntoView({block:'center'});", btn
                    )
                    time.sleep(1)
                    try:
                        btn.click()
                    except Exception:
                        self.handle_cancel_popup()
                        self.driver.execute_script("arguments[0].click();", btn)
                    logger.info(f"{self.log_prefix}[POST] clicked")
                    clicked = True
                    break
                except Exception:
                    continue

            if not clicked:
                self.driver.execute_script(
                    """
                    var buttons = document.querySelectorAll('button');
                    for (var i=0; i<buttons.length; i++){
                        var t = buttons[i].innerText || buttons[i].textContent;
                        if (t && t.trim() === 'Post'){
                            buttons[i].click();
                            return;
                        }
                    }
                    """
                )
                logger.info(f"{self.log_prefix}[POST] clicked via JS")

            # "Post now" confirm popup
            for _ in range(5):
                if self.handle_post_now_popup():
                    break
                time.sleep(2)

            logger.info(f"{self.log_prefix}[UPLOAD] done")
            return True
        except Exception as e:  # noqa: BLE001
            logger.exception(f"{self.log_prefix}[ERROR] {e}")
            return False
        finally:
            self.force_close()


# ─────────────────────────────────────────────────────────────────────────────
# Functional API (backward compatible)
# ─────────────────────────────────────────────────────────────────────────────

def upload_video_to_tiktok(
    video_path: str,
    description: str = "",
    cookies_file: Optional[str] = None,
    headless: bool = False,
) -> bool:
    return TikTokUploader(cookies_file=cookies_file, headless=headless).upload(
        video_path, description
    )
