"""
YouTube Studio uploader.

Refactored on top of ``_browser.BrowserSession`` for production stability.

What was wrong, what is fixed:

  - WRONG ENTRY POINT:  login() navigated to studio.youtube.com which
                        landed on the channel dashboard, where there is no
                        ``<input type='file'>`` for video upload — every
                        upload then timed out for 30s and failed. Now we
                        navigate directly to ``https://www.youtube.com/upload``
                        which always opens the upload dialog when logged in.
  - BANNED CHANNELS:    a /channel-appeal redirect was treated as "logged in"
                        and the bot wasted ~90s before timing out. Now we
                        explicitly detect the appeal URL and fail fast.
  - PROCESS LEAKS:      driver.quit() could hang for 6+ minutes; orphan
                        chrome.exe accumulated. Now BrowserSession.force_close()
                        kills processes if quit() does not finish in 10s.
  - INFINITE WAITS:     webdriver retries were unbounded. Now every command
                        runs through safe_driver_call() with a hard timeout.
  - NO GLOBAL CAP:      a single hung upload could block the queue for hours.
                        Now upload is wrapped in run_with_upload_timeout()
                        with GLOBAL_UPLOAD_TIMEOUT (15 min).

Public API is unchanged:
    YouTubeUploader(profile_path=None, headless=False)
        .login()
        .upload_video(video_path, title, description, visibility, made_for_kids)
        .close()
    upload_video_to_youtube(video_path, title, description, ...)
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from typing import Optional

from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from ._browser import (
    BrowserSession,
    DriverUnhealthyError,
    GLOBAL_UPLOAD_TIMEOUT,
    NavigationError,
    SAFE_COMMAND_TIMEOUT,
    run_with_upload_timeout,
    safe_driver_call,
)

logger = logging.getLogger(__name__)

# Single canonical upload entry point — opens the upload dialog directly.
YT_UPLOAD_URL = "https://www.youtube.com/upload"


def _setup_root_logging_once():
    """Backward-compat: previous version called logging.basicConfig() in __init__."""
    root = logging.getLogger()
    if root.handlers:
        return
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    fh = logging.FileHandler("youtube_upload.log")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root.addHandler(fh)
    root.addHandler(sh)


class YouTubeUploader:
    """
    YouTube Studio uploader.

    A single instance owns at most ONE BrowserSession. On any unrecoverable
    error it returns False — there is NO recursive relaunch.
    """

    def __init__(self, profile_path: Optional[str] = None, headless: bool = False) -> None:
        self.profile_path = profile_path or os.path.join(os.getcwd(), "chrome_profile")
        self.headless = headless
        self.upload_id = uuid.uuid4().hex[:8]
        self.log_prefix = f"[YT][upload_id={self.upload_id}]"
        self.session: Optional[BrowserSession] = None
        _setup_root_logging_once()
        # Backward-compat attribute names still expected by callers.
        self.logger = logger
        self.driver = None
        self.wait = None

    # ── Backward-compat lifecycle methods ─────────────────────────────────

    def setup_driver(self) -> None:
        """Create a fresh BrowserSession with hard timeouts."""
        if self.session is not None:
            try:
                self.session.force_close()
            except Exception:
                pass
            self.session = None

        s = BrowserSession(
            self.profile_path,
            log_prefix=self.log_prefix,
            headless=self.headless,
        )
        s.start()
        self.session = s
        self.driver = s.driver
        self.wait = WebDriverWait(s.driver, 30)
        logger.info(f"{self.log_prefix}[BROWSER] Chrome driver initialized")

    def login(self) -> bool:
        """
        Navigate to the upload page. Returns True if logged in and the upload
        dialog is reachable; False if the channel is banned or login failed.

        Detection:
          - URL contains ``accounts.google.com`` / ``signin``  → login required
          - URL contains ``/channel-appeal``                    → banned channel
          - URL on ``youtube.com/upload`` or ``studio.youtube.com``  → OK
        """
        if self.session is None:
            logger.error(f"{self.log_prefix}[LOGIN] no session — call setup_driver() first")
            return False

        try:
            current = self.session.navigate(YT_UPLOAD_URL)
        except (NavigationError, DriverUnhealthyError) as e:
            logger.error(f"{self.log_prefix}[LOGIN] navigation failed: {e}")
            return False

        # Banned channel
        if "/channel-appeal" in current:
            logger.error(
                f"{self.log_prefix}[LOGIN] channel is suspended (channel-appeal "
                f"page) — upload not possible"
            )
            return False

        # Manual sign-in needed (rare for shared profiles)
        if "accounts.google.com" in current or "signin" in current:
            logger.info(
                f"{self.log_prefix}[LOGIN] redirected to Google sign-in — "
                f"waiting up to 5 minutes for manual login"
            )
            try:
                safe_driver_call(
                    lambda: WebDriverWait(self.session.driver, 300).until(
                        EC.url_contains("youtube.com/upload")
                    ),
                    timeout=305,
                )
                logger.info(f"{self.log_prefix}[LOGIN] manual login completed")
            except Exception as e:
                logger.error(f"{self.log_prefix}[LOGIN] manual login timed out: {e}")
                return False
            current = self.session.current_url()

        # Now we should be on the upload page (or studio shell)
        if "youtube.com" not in current.lower():
            logger.error(f"{self.log_prefix}[LOGIN] unexpected URL: {current}")
            return False

        # Wait for either the file input or the studio shell to be present
        try:
            safe_driver_call(
                lambda: WebDriverWait(self.session.driver, 25).until(
                    EC.presence_of_element_located(
                        (By.XPATH, "//input[@type='file'] | //ytcp-app | //*[@id='upload-btn']")
                    )
                ),
                timeout=30,
            )
        except Exception:
            logger.warning(
                f"{self.log_prefix}[LOGIN] file input / studio shell not "
                f"detected yet — proceeding anyway"
            )

        logger.info(f"{self.log_prefix}[LOGIN] OK ({current})")
        return True

    def close(self) -> None:
        if self.session is not None:
            try:
                self.session.force_close()
            except Exception as e:
                logger.warning(f"{self.log_prefix}[BROWSER] close raised: {e}")
            self.session = None
        self.driver = None
        self.wait = None
        logger.info(f"{self.log_prefix}[BROWSER] closed")

    # ── Upload pipeline ────────────────────────────────────────────────────

    def upload_video(
        self,
        video_path: str,
        title: str,
        description: str,
        visibility: str = "public",
        made_for_kids: bool = False,
    ) -> bool:
        """Run the upload pipeline. Always returns within GLOBAL_UPLOAD_TIMEOUT."""
        if not os.path.exists(video_path):
            logger.error(f"{self.log_prefix} video file not found: {video_path}")
            return False

        return run_with_upload_timeout(
            worker_fn=lambda: self._upload_video_inner(
                video_path, title, description, visibility, made_for_kids
            ),
            get_session_fn=lambda: self.session,
            timeout_sec=GLOBAL_UPLOAD_TIMEOUT,
            log_prefix=self.log_prefix,
        )

    def _upload_video_inner(
        self,
        video_path: str,
        title: str,
        description: str,
        visibility: str,
        made_for_kids: bool,
    ) -> bool:
        try:
            if not self._upload_file(video_path):
                return False
            if not self._fill_video_details(title, description):
                return False
            if not self._set_kids_content(made_for_kids):
                return False
            if not self._navigate_upload_workflow():
                return False
            if not self._set_visibility_and_save(visibility):
                return False
            logger.info(f"{self.log_prefix}[UPLOAD] complete")
            return True
        except DriverUnhealthyError as e:
            logger.error(f"{self.log_prefix}[HEALTH] driver hung: {e}")
            return False
        except Exception as e:  # noqa: BLE001
            logger.exception(f"{self.log_prefix}[ERROR] {e}")
            return False

    # ── Step: upload file ──────────────────────────────────────────────────

    def _upload_file(self, video_path: str) -> bool:
        """
        Locate the file input and hand the video to YouTube. Then wait for
        the upload transfer to finish (up to 10 min) and the details form
        to become available.
        """
        try:
            file_input = safe_driver_call(
                lambda: WebDriverWait(self.session.driver, 30).until(
                    EC.presence_of_element_located(
                        (By.XPATH, "//input[@type='file']")
                    )
                ),
                timeout=35,
            )
        except DriverUnhealthyError:
            raise
        except Exception as e:
            logger.error(f"{self.log_prefix}[FILE] input not found: {e}")
            return False

        try:
            safe_driver_call(
                lambda: file_input.send_keys(os.path.abspath(video_path)),
                timeout=SAFE_COMMAND_TIMEOUT,
            )
        except DriverUnhealthyError:
            raise
        except Exception as e:
            logger.error(f"{self.log_prefix}[FILE] send_keys failed: {e}")
            return False
        logger.info(f"{self.log_prefix}[FILE] sent video path")

        # Wait for the upload dialog to actually open
        upload_dialog_xpath = (
            "//*[contains(text(), 'Uploading') or "
            "contains(text(), 'Upload video') or "
            "contains(text(), 'Processing') or "
            "contains(@class, 'ytcp-uploads-dialog')]"
        )
        try:
            safe_driver_call(
                lambda: WebDriverWait(self.session.driver, 30).until(
                    EC.presence_of_element_located((By.XPATH, upload_dialog_xpath))
                ),
                timeout=35,
            )
            logger.info(f"{self.log_prefix}[FILE] upload dialog opened")
        except Exception:
            logger.error(
                f"{self.log_prefix}[FILE] upload dialog did not appear — "
                f"likely on wrong page"
            )
            return False

        # Wait up to 10 minutes for transfer to finish
        logger.info(f"{self.log_prefix}[FILE] waiting for transfer...")
        try:
            safe_driver_call(
                lambda: WebDriverWait(self.session.driver, 600).until(
                    EC.invisibility_of_element_located(
                        (By.XPATH, "//*[contains(text(), 'Uploading')]")
                    )
                ),
                timeout=605,
            )
            logger.info(f"{self.log_prefix}[FILE] transfer complete")
        except DriverUnhealthyError:
            raise
        except Exception:
            logger.warning(
                f"{self.log_prefix}[FILE] 'Uploading' text still visible after 10min "
                f"— continuing anyway"
            )

        # Wait for the details form (Studio uses ytcp-social-suggestion-input +
        # contenteditable #textbox, not legacy ytcp-mention-textbox).
        details_xpath = (
            "//div[@id='textbox' and contains(@aria-label, 'Add a title')]"
            " | //ytcp-form-input-container[.//span[@id='label-text' "
            "and contains(., 'Title')]]//div[@id='textbox']"
            " | //ytcp-mention-textbox[@label='Title']"
        )
        try:
            safe_driver_call(
                lambda: WebDriverWait(self.session.driver, 30).until(
                    EC.presence_of_element_located((By.XPATH, details_xpath))
                ),
                timeout=35,
            )
            logger.info(f"{self.log_prefix}[FILE] details form ready")
        except Exception:
            logger.warning(
                f"{self.log_prefix}[FILE] details form not detected — "
                f"will retry inside _fill_video_details"
            )
        return True

    # ── Step: fill title/description ───────────────────────────────────────

    def _fill_video_details(self, title: str, description: str) -> bool:
        # Current Studio UI: ytcp-social-suggestions-textbox → div#textbox
        # (contenteditable). Legacy upload flow still has ytcp-mention-textbox.
        title_selectors = (
            "//ytcp-form-input-container[.//span[@id='label-text' "
            "and contains(., 'Title')]]//div[@id='textbox'][@contenteditable='true']",
            "//div[@id='textbox' and @role='textbox' "
            "and contains(@aria-label, 'Add a title')]",
            "//div[@id='textbox' and contains(@aria-label, 'title that describes')]",
            "//ytcp-social-suggestions-textbox//div[@id='textbox' and @aria-required='true']",
            "//ytcp-mention-textbox[@label='Title']//div[@id='textbox']",
        )

        title_field = self._wait_first_element(
            title_selectors, timeout=90, clickable=False
        )
        if title_field is None:
            logger.warning(f"{self.log_prefix}[DETAILS] title field not found")
        else:
            if self.session.safe_send_keys(title_field, title):
                logger.info(f"{self.log_prefix}[DETAILS] title filled")
            else:
                logger.warning(f"{self.log_prefix}[DETAILS] title fill failed")

        description_selectors = (
            "//ytcp-form-input-container[.//span[@id='label-text' "
            "and normalize-space(.)='Description']]"
            "//div[@id='textbox'][@contenteditable='true']",
            "//div[@id='textbox' and @role='textbox' "
            "and contains(@aria-label, 'Tell viewers about')]",
            "//ytcp-mention-textbox[@label='Description']//div[@id='textbox']",
        )
        description_field = self._wait_first_element(
            description_selectors, timeout=30, clickable=False
        )
        if description_field is None:
            logger.warning(f"{self.log_prefix}[DETAILS] description field not found")
        else:
            if self.session.safe_send_keys(description_field, description):
                logger.info(f"{self.log_prefix}[DETAILS] description filled")
        return True

    def _wait_first_element(
        self,
        xpaths,
        timeout: int,
        *,
        clickable: bool = False,
    ):
        """Try each XPath in order with a shared wall-clock budget.

        Polymer contenteditable fields often fail ``element_to_be_clickable``
        (overlays, shadow boundaries). For title/description use
        ``clickable=False`` (presence only); ``safe_send_keys`` scrolls and
        dismisses overlays before typing.
        """
        if not xpaths:
            return None
        deadline = time.time() + timeout
        ec = EC.element_to_be_clickable if clickable else EC.presence_of_element_located
        for xp in xpaths:
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            per = max(1.0, min(25.0, remaining))
            try:
                el = safe_driver_call(
                    lambda x=xp, p=per: WebDriverWait(self.session.driver, p).until(
                        ec((By.XPATH, x))
                    ),
                    timeout=per + 5,
                )
                if el is not None:
                    return el
            except DriverUnhealthyError:
                raise
            except Exception:
                continue
        return None

    # ── Step: kids content (COPPA) ─────────────────────────────────────────

    def _set_kids_content(self, made_for_kids: bool) -> bool:
        target_name = (
            "VIDEO_MADE_FOR_KIDS_MFK" if made_for_kids else "VIDEO_MADE_FOR_KIDS_NOT_MFK"
        )
        selectors = (
            f"//tp-yt-paper-radio-button[@name='{target_name}']",
            f"//paper-radio-button[@name='{target_name}']",
            f"//*[@name='{target_name}']",
        )
        for xp in selectors:
            try:
                radio = safe_driver_call(
                    lambda x=xp: WebDriverWait(self.session.driver, 10).until(
                        EC.element_to_be_clickable((By.XPATH, x))
                    ),
                    timeout=15,
                )
                if radio is not None and self.session.safe_click(radio):
                    logger.info(
                        f"{self.log_prefix}[COPPA] set made_for_kids={made_for_kids}"
                    )
                    time.sleep(1.5)
                    return True
            except DriverUnhealthyError:
                raise
            except Exception:
                continue
        logger.error(f"{self.log_prefix}[COPPA] could not set kids selection")
        return False

    # ── Step: walk through Next buttons ────────────────────────────────────

    def _navigate_upload_workflow(self) -> bool:
        """Click "Next" up to 3 times — Details → Video elements → Visibility."""
        clicked = 0
        for step in range(3):
            time.sleep(2)
            xp = (
                "//ytcp-button[@id='next-button'] | "
                "//*[@id='next-button']"
            )
            try:
                btn = safe_driver_call(
                    lambda: WebDriverWait(self.session.driver, 10).until(
                        EC.presence_of_element_located((By.XPATH, xp))
                    ),
                    timeout=15,
                )
            except DriverUnhealthyError:
                raise
            except Exception:
                logger.info(
                    f"{self.log_prefix}[WORKFLOW] no more Next buttons (step {step + 1})"
                )
                break

            try:
                disabled = safe_driver_call(
                    lambda: btn.get_attribute("disabled"), timeout=5
                )
            except Exception:
                disabled = None
            if disabled:
                logger.info(
                    f"{self.log_prefix}[WORKFLOW] Next disabled at step {step + 1} — "
                    f"waiting briefly"
                )
                time.sleep(2)
                continue

            if not self.session.safe_click(btn):
                logger.warning(
                    f"{self.log_prefix}[WORKFLOW] Next click failed at step {step + 1}"
                )
                break
            clicked += 1
            time.sleep(2)

        logger.info(f"{self.log_prefix}[WORKFLOW] clicked Next {clicked} time(s)")
        return True

    # ── Step: visibility + save/publish ────────────────────────────────────

    def _set_visibility_and_save(self, visibility: str) -> bool:
        time.sleep(2)
        target = {
            "private": "PRIVATE",
            "unlisted": "UNLISTED",
            "public": "PUBLIC",
            "scheduled": "SCHEDULED",
        }.get(visibility.lower(), "PUBLIC")

        vis_selectors = (
            f"//tp-yt-paper-radio-button[@name='{target}']",
            f"//paper-radio-button[@name='{target}']",
            f"//tp-yt-paper-radio-button[@id='{visibility.lower()}-radio-button']",
            f"//tp-yt-paper-radio-button[contains(@aria-label, "
            f"'{visibility.title()}')]",
        )

        vis_set = False
        for xp in vis_selectors:
            try:
                el = safe_driver_call(
                    lambda x=xp: WebDriverWait(self.session.driver, 6).until(
                        EC.element_to_be_clickable((By.XPATH, x))
                    ),
                    timeout=10,
                )
                if el is not None and self.session.safe_click(el):
                    logger.info(f"{self.log_prefix}[VISIBILITY] set to {visibility}")
                    vis_set = True
                    break
            except DriverUnhealthyError:
                raise
            except Exception:
                continue

        if not vis_set:
            logger.warning(f"{self.log_prefix}[VISIBILITY] could not set explicitly — proceeding")

        time.sleep(1.5)

        # Click Save / Publish
        save_selectors = (
            "//ytcp-button[@id='done-button']",
            "//*[@id='done-button']",
            "//button[normalize-space(text())='Publish']",
            "//button[normalize-space(text())='Save']",
        )
        clicked = False
        for xp in save_selectors:
            try:
                el = safe_driver_call(
                    lambda x=xp: WebDriverWait(self.session.driver, 8).until(
                        EC.element_to_be_clickable((By.XPATH, x))
                    ),
                    timeout=12,
                )
                if el is not None and self.session.safe_click(el):
                    logger.info(f"{self.log_prefix}[PUBLISH] save/publish clicked")
                    clicked = True
                    break
            except DriverUnhealthyError:
                raise
            except Exception:
                continue

        if not clicked:
            logger.error(f"{self.log_prefix}[PUBLISH] could not find save/publish button")
            return False

        time.sleep(3)
        self._handle_prechecks_warning()
        time.sleep(8)
        return True

    # ── Step: handle "Publish anyway" prechecks dialog ─────────────────────

    def _handle_prechecks_warning(self) -> bool:
        """
        If YouTube Studio shows the ``ytcp-prechecks-warning-dialog`` after
        clicking Publish, click "Publish anyway". Otherwise return False
        silently (this is the normal happy path).
        """
        deadline = time.time() + 12
        dialog_present = False
        dialog_selectors = (
            "//ytcp-prechecks-warning-dialog",
            "//*[@id='dialog-title' and contains(., 'still checking your content')]",
        )
        while time.time() < deadline:
            for xp in dialog_selectors:
                try:
                    el = safe_driver_call(
                        lambda x=xp: self.session.driver.find_element(By.XPATH, x),
                        timeout=4,
                    )
                    if el is not None and bool(
                        safe_driver_call(el.is_displayed, timeout=4)
                    ):
                        dialog_present = True
                        break
                except Exception:
                    continue
            if dialog_present:
                break
            time.sleep(0.5)

        if not dialog_present:
            logger.info(f"{self.log_prefix}[PRECHECKS] no warning dialog — OK")
            return False

        logger.warning(
            f"{self.log_prefix}[PRECHECKS] warning dialog detected — clicking Publish anyway"
        )
        publish_anyway = (
            "//ytcp-prechecks-warning-dialog//button[@aria-label='Publish anyway']",
            "//button[@aria-label='Publish anyway']",
            "//ytcp-button[contains(., 'Publish anyway')]",
        )
        for xp in publish_anyway:
            try:
                btn = safe_driver_call(
                    lambda x=xp: WebDriverWait(self.session.driver, 6).until(
                        EC.element_to_be_clickable((By.XPATH, x))
                    ),
                    timeout=10,
                )
                if btn is not None and self.session.safe_click(btn):
                    logger.info(f"{self.log_prefix}[PRECHECKS] clicked Publish anyway")
                    time.sleep(3)
                    return True
            except DriverUnhealthyError:
                raise
            except Exception:
                continue

        # JS fallback: scan the dialog for the button by aria-label/text
        try:
            clicked = safe_driver_call(
                lambda: self.session.driver.execute_script(
                    """
                    var dialog = document.querySelector('ytcp-prechecks-warning-dialog');
                    var scope = dialog || document;
                    var buttons = scope.querySelectorAll('button');
                    for (var i = 0; i < buttons.length; i++) {
                        var label = buttons[i].getAttribute('aria-label') || '';
                        var text = (buttons[i].innerText || '').trim();
                        if (label === 'Publish anyway' || text === 'Publish anyway') {
                            buttons[i].click();
                            return true;
                        }
                    }
                    return false;
                    """
                ),
                timeout=10,
            )
            if clicked:
                logger.info(f"{self.log_prefix}[PRECHECKS] clicked via JS fallback")
                time.sleep(3)
                return True
        except Exception as e:
            logger.debug(f"{self.log_prefix}[PRECHECKS] JS fallback failed: {e}")
        return False

    # ── Context manager (preserved) ────────────────────────────────────────

    def __enter__(self):
        self.setup_driver()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()


# ─────────────────────────────────────────────────────────────────────────────
# Functional API (backward compatible)
# ─────────────────────────────────────────────────────────────────────────────

def upload_video_to_youtube(
    video_path: str,
    title: str,
    description: str,
    profile_path: Optional[str] = None,
    headless: bool = False,
    visibility: str = "public",
    made_for_kids: bool = False,
) -> bool:
    """
    One-shot function-style upload. Always cleanly closes the browser, even
    on exception or timeout. Returns True/False.
    """
    uploader = YouTubeUploader(profile_path=profile_path, headless=headless)
    try:
        uploader.setup_driver()
    except Exception as e:
        logger.error(f"{uploader.log_prefix}[BROWSER] start failed: {e}")
        return False

    try:
        if not uploader.login():
            return False
        return uploader.upload_video(
            video_path, title, description, visibility, made_for_kids
        )
    except Exception as e:
        logger.exception(f"{uploader.log_prefix}[ERROR] {e}")
        return False
    finally:
        try:
            uploader.close()
        except Exception:
            pass
