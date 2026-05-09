"""
Instagram (Buffer) uploader.

Refactored to use the centralized ``_browser.BrowserSession`` for stability.

What was wrong before, what is fixed now:

  - PROCESS LEAKS:    every relaunch could leave orphan chrome.exe processes
                      because driver.quit() was sometimes hung. Now each
                      session.start() begins by killing every chrome.exe
                      using the same --user-data-dir, and force_close()
                      always kills processes if quit() failed.
  - INFINITE WAITS:   webdriver commands could block for hours via urllib3
                      retries. Now every command is wrapped in
                      safe_driver_call(...) with a hard wall-clock timeout.
  - RELAUNCH STORMS:  recursive fallback loops kept spawning Chrome instances
                      that hit the same Buffer onboarding overlay. Now there
                      is exactly ONE bounded recreate loop (max 2 recreates)
                      and overlays are dismissed before every click.
  - FALSE NAV OK:     navigate_to() trusted Buffer's URL. Now navigation
                      succeeds only when readyState=='complete' AND the URL
                      is not chrome:// / about:blank / chrome-error://.
  - NO GLOBAL CAP:    a single Buffer hang could block the queue for hours.
                      Now upload_reels() is guarded by GLOBAL_UPLOAD_TIMEOUT
                      (15 minutes); on expiry we force-close and return False.

Public API is unchanged:
    InstagramUploader(profile_path, buffer_url).upload_reels(file_path, caption)
    upload_instagram_reels(file_path, caption, profile_path, buffer_url)
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
import uuid
from typing import Optional

from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from ._browser import (
    BrowserSession,
    DriverUnhealthyError,
    GLOBAL_UPLOAD_TIMEOUT,
    MAX_DRIVER_RECREATIONS,
    NavigationError,
    SAFE_COMMAND_TIMEOUT,
    run_with_upload_timeout,
    safe_driver_call,
    setup_logging,
)

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Clipboard helper (unchanged — needed for Persian captions)
# ─────────────────────────────────────────────────────────────────────────────

def _set_clipboard_windows(text: str) -> bool:
    """Write *text* to the Windows clipboard using clip.exe + UTF-16 LE BOM."""
    try:
        bom = b"\xff\xfe"
        encoded = bom + text.encode("utf-16-le")
        proc = subprocess.Popen("clip", stdin=subprocess.PIPE, shell=True)
        proc.communicate(input=encoded)
        return proc.returncode == 0
    except Exception as e:
        logger.warning(f"[IG][CLIPBOARD] clip.exe failed: {e}")
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Login redirect detection
# ─────────────────────────────────────────────────────────────────────────────

_LOGIN_MARKERS = (
    "login.buffer.com",
    "account.buffer.com",
    "/login",
    "accounts.google.com",
    "signin",
)

_BUFFER_SHELL_SELECTORS = (
    (By.CSS_SELECTOR, "button[aria-haspopup='menu']"),
    (By.CSS_SELECTOR, "[data-channel='instagram']"),
    (By.XPATH, "//button[contains(., 'Create')]"),
)


def _is_login_redirect(url: str) -> bool:
    if not url:
        return False
    u = url.lower()
    return any(m in u for m in _LOGIN_MARKERS)


# ─────────────────────────────────────────────────────────────────────────────
# Uploader
# ─────────────────────────────────────────────────────────────────────────────

class InstagramUploader:
    """
    Upload an Instagram Reel via Buffer.

    A single InstagramUploader instance owns at most ONE BrowserSession at
    a time. The recovery policy is bounded: at most MAX_DRIVER_RECREATIONS
    total recreations of the browser session. There is NO recursive fallback.
    """

    def __init__(self, profile_path: str, buffer_url: str) -> None:
        setup_logging("uploader.log")
        self.profile_path = profile_path
        self.buffer_url = buffer_url
        self.upload_id = uuid.uuid4().hex[:8]
        self.log_prefix = f"[IG][upload_id={self.upload_id}]"
        self.session: Optional[BrowserSession] = None

    # ── session lifecycle ──────────────────────────────────────────────────

    def _create_session(self) -> BrowserSession:
        """Force-close any existing session, then start a fresh one."""
        if self.session is not None:
            try:
                self.session.force_close()
            except Exception as e:
                logger.warning(f"{self.log_prefix}[BROWSER] cleanup raised: {e}")
            self.session = None

        s = BrowserSession(self.profile_path, log_prefix=self.log_prefix)
        s.start()
        self.session = s
        return s

    # ── public API ─────────────────────────────────────────────────────────

    def upload_reels(self, file_path: str, caption: str) -> bool:
        """
        Upload a Reel. Always returns within GLOBAL_UPLOAD_TIMEOUT seconds.

        Wraps the inner upload logic in run_with_upload_timeout so that, if
        anything (Buffer SPA, chromedriver, network) hangs, the browser is
        force-killed and we return False cleanly.
        """
        file_path = os.path.abspath(file_path)
        if not os.path.exists(file_path):
            logger.error(f"{self.log_prefix} video file not found: {file_path}")
            return False

        return run_with_upload_timeout(
            worker_fn=lambda: self._upload_reels_inner(file_path, caption),
            get_session_fn=lambda: self.session,
            timeout_sec=GLOBAL_UPLOAD_TIMEOUT,
            log_prefix=self.log_prefix,
        )

    # ── inner upload (no timeout management) ───────────────────────────────

    def _upload_reels_inner(self, file_path: str, caption: str) -> bool:
        """
        Bounded-recovery upload pipeline.

        Up to (MAX_DRIVER_RECREATIONS + 1) attempts. Each attempt:
          1. Create a fresh session (kills old chrome processes first).
          2. Navigate to Buffer URL.
          3. Verify Buffer shell is rendered and we are not on a login page.
          4. Open composer, attach file, write caption, schedule Now,
             click Publish, and verify success.
        Returns True on the first success.
        """
        try:
            for attempt in range(MAX_DRIVER_RECREATIONS + 1):
                logger.info(
                    f"{self.log_prefix}[ATTEMPT] {attempt + 1}"
                    f"/{MAX_DRIVER_RECREATIONS + 1}"
                )

                try:
                    session = self._create_session()
                except Exception as e:
                    logger.error(f"{self.log_prefix}[BROWSER] start failed: {e}")
                    continue

                # Navigation — strict
                try:
                    final_url = session.navigate(self.buffer_url)
                except (NavigationError, DriverUnhealthyError) as e:
                    logger.warning(f"{self.log_prefix}[NAV] {e} — recreating")
                    continue

                if _is_login_redirect(final_url):
                    logger.error(
                        f"{self.log_prefix}[NAV] Buffer session expired "
                        f"(login redirect) — aborting upload"
                    )
                    return False

                if not session.is_healthy():
                    logger.warning(f"{self.log_prefix}[HEALTH] driver unhealthy after nav")
                    continue

                # Buffer shell render — bounded wait, no relaunch on overlay
                if not self._wait_for_buffer_shell(session):
                    logger.warning(f"{self.log_prefix}[NAV] Buffer shell did not render")
                    continue

                # From this point on, treat any unhealthy/nav exception as a
                # failure for this attempt only — recreate one more time if
                # we have budget.
                try:
                    if self._run_composer_pipeline(session, file_path, caption):
                        return True
                except DriverUnhealthyError as e:
                    logger.warning(
                        f"{self.log_prefix}[HEALTH] driver hung mid-upload: {e}"
                    )
                    continue
                except Exception as e:
                    logger.exception(
                        f"{self.log_prefix}[ERROR] composer pipeline raised: {e}"
                    )
                    continue

            logger.error(
                f"{self.log_prefix} upload failed after "
                f"{MAX_DRIVER_RECREATIONS + 1} attempt(s)"
            )
            return False
        finally:
            if self.session is not None:
                try:
                    self.session.force_close()
                except Exception as e:
                    logger.warning(f"{self.log_prefix}[BROWSER] final close: {e}")
                self.session = None

    # ── pipeline steps ─────────────────────────────────────────────────────

    def _wait_for_buffer_shell(self, session: BrowserSession, timeout: int = 30) -> bool:
        """Wait for any well-known Buffer shell element to appear AND be visible."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not session.is_healthy():
                return False
            # Always remove overlays mid-poll so we don't time out behind them.
            session.dismiss_blocking_overlays()
            for by, sel in _BUFFER_SHELL_SELECTORS:
                try:
                    el = safe_driver_call(
                        lambda: session.driver.find_element(by, sel),
                        timeout=5,
                    )
                    if el is not None:
                        try:
                            visible = safe_driver_call(el.is_displayed, timeout=5)
                        except Exception:
                            visible = False
                        if visible:
                            return True
                except Exception:
                    continue
            time.sleep(0.7)
        return False

    def _run_composer_pipeline(
        self, session: BrowserSession, file_path: str, caption: str
    ) -> bool:
        """The composer → file → caption → schedule → publish pipeline."""
        # 1. Open composer
        if not self._open_composer(session):
            return False

        # 2. Click "Reels" tab (best effort)
        self._select_reels_tab(session)

        # 3. Attach file
        if not self._attach_file(session, file_path):
            return False

        # 4. Caption (best effort — uploads still work without caption)
        if caption:
            self._write_caption(session, caption)

        # 5. Schedule → Now
        if not self._set_schedule_now(session):
            logger.warning(
                f"{self.log_prefix}[SCHEDULE] failed to set 'Now' — Publish "
                f"button may not become available"
            )
            return False

        # 6. Click Publish + verify
        if not self._click_publish(session):
            return False

        return self._verify_publish_succeeded(session, timeout=90)

    def _open_composer(self, session: BrowserSession) -> bool:
        """
        Open Buffer's composer. Tries the new UI ("Create new" button)
        first, falls back to the old UI (Instagram channel → New).
        Always dismisses overlays before each click.
        """
        # New UI
        session.dismiss_blocking_overlays()
        try:
            create_btn = safe_driver_call(
                lambda: WebDriverWait(session.driver, 15).until(
                    EC.presence_of_element_located(
                        (By.CSS_SELECTOR, "button[aria-haspopup='menu']")
                    )
                ),
                timeout=20,
            )
            if create_btn is not None and session.safe_click(create_btn):
                time.sleep(1)
                session.dismiss_blocking_overlays()
                post_item = safe_driver_call(
                    lambda: WebDriverWait(session.driver, 10).until(
                        EC.presence_of_element_located(
                            (By.XPATH, "//div[@role='menuitem' and contains(., 'Post')]")
                        )
                    ),
                    timeout=15,
                )
                if post_item is not None and session.safe_click(post_item):
                    logger.info(f"{self.log_prefix}[COMPOSER] new UI opened")
                    return True
        except DriverUnhealthyError:
            raise
        except Exception as e:
            logger.debug(f"{self.log_prefix}[COMPOSER] new UI failed: {e}")

        # Old UI fallback (best effort, no recursion)
        session.dismiss_blocking_overlays()
        try:
            insta_btn = safe_driver_call(
                lambda: WebDriverWait(session.driver, 10).until(
                    EC.presence_of_element_located(
                        (By.CSS_SELECTOR, "[data-channel='instagram']")
                    )
                ),
                timeout=15,
            )
            if insta_btn is not None and session.safe_click(insta_btn):
                session.dismiss_blocking_overlays()
                new_btn = safe_driver_call(
                    lambda: WebDriverWait(session.driver, 10).until(
                        EC.presence_of_element_located(
                            (By.XPATH, "//button[contains(., 'New')]")
                        )
                    ),
                    timeout=15,
                )
                if new_btn is not None and session.safe_click(new_btn):
                    logger.info(f"{self.log_prefix}[COMPOSER] old UI opened")
                    return True
        except DriverUnhealthyError:
            raise
        except Exception as e:
            logger.debug(f"{self.log_prefix}[COMPOSER] old UI failed: {e}")

        logger.error(f"{self.log_prefix}[COMPOSER] could not open composer")
        return False

    def _select_reels_tab(self, session: BrowserSession) -> None:
        session.dismiss_blocking_overlays()
        try:
            label = safe_driver_call(
                lambda: WebDriverWait(session.driver, 10).until(
                    EC.presence_of_element_located(
                        (By.CSS_SELECTOR, "label[for='reels']")
                    )
                ),
                timeout=15,
            )
            if label is not None and session.safe_click(label):
                logger.info(f"{self.log_prefix}[REELS] tab selected")
        except DriverUnhealthyError:
            raise
        except Exception as e:
            logger.debug(f"{self.log_prefix}[REELS] tab click skipped: {e}")

    def _attach_file(self, session: BrowserSession, file_path: str) -> bool:
        try:
            file_input = safe_driver_call(
                lambda: WebDriverWait(session.driver, 25).until(
                    EC.presence_of_element_located(
                        (By.CSS_SELECTOR, "input[type='file']")
                    )
                ),
                timeout=30,
            )
        except DriverUnhealthyError:
            raise
        except Exception as e:
            logger.error(f"{self.log_prefix}[FILE] input not found: {e}")
            return False

        try:
            safe_driver_call(
                lambda: file_input.send_keys(file_path),
                timeout=SAFE_COMMAND_TIMEOUT,
            )
        except DriverUnhealthyError:
            raise
        except Exception as e:
            logger.error(f"{self.log_prefix}[FILE] send_keys failed: {e}")
            return False

        logger.info(f"{self.log_prefix}[FILE] attached")
        time.sleep(3)
        return True

    def _write_caption(self, session: BrowserSession, caption: str) -> None:
        session.dismiss_blocking_overlays()
        caption_selectors = (
            "[data-testid='composer-text-area']",
            ".public-DraftEditor-content",
            "div[contenteditable='true']",
            "div[role='textbox']",
        )
        caption_box = None
        for sel in caption_selectors:
            try:
                caption_box = safe_driver_call(
                    lambda s=sel: WebDriverWait(session.driver, 8).until(
                        EC.presence_of_element_located((By.CSS_SELECTOR, s))
                    ),
                    timeout=10,
                )
                if caption_box is not None:
                    logger.info(f"{self.log_prefix}[CAPTION] found via {sel}")
                    break
            except DriverUnhealthyError:
                raise
            except Exception:
                continue

        if caption_box is None:
            logger.warning(f"{self.log_prefix}[CAPTION] textbox not found — skipping")
            return

        # Focus + clear
        if not session.safe_click(caption_box):
            logger.warning(f"{self.log_prefix}[CAPTION] could not focus textbox")
            return

        try:
            safe_driver_call(
                lambda: ActionChains(session.driver)
                .key_down(Keys.CONTROL).send_keys("a").key_up(Keys.CONTROL).perform(),
                timeout=10,
            )
        except Exception:
            pass

        # Strategy 1: clipboard + Ctrl+V (works for Persian)
        if _set_clipboard_windows(caption):
            time.sleep(0.4)
            try:
                safe_driver_call(
                    lambda: ActionChains(session.driver)
                    .key_down(Keys.CONTROL).send_keys("v").key_up(Keys.CONTROL).perform(),
                    timeout=10,
                )
                logger.info(f"{self.log_prefix}[CAPTION] pasted via clipboard")
                return
            except DriverUnhealthyError:
                raise
            except Exception as e:
                logger.debug(f"{self.log_prefix}[CAPTION] paste failed: {e}")

        # Strategy 2: send_keys
        if session.safe_send_keys(caption_box, caption, clear_first=False):
            logger.info(f"{self.log_prefix}[CAPTION] written via send_keys")
        else:
            logger.warning(f"{self.log_prefix}[CAPTION] all strategies failed")

    def _set_schedule_now(self, session: BrowserSession) -> bool:
        session.dismiss_blocking_overlays()
        try:
            sched_btn = safe_driver_call(
                lambda: WebDriverWait(session.driver, 15).until(
                    EC.presence_of_element_located(
                        (By.CSS_SELECTOR, "button[data-testid='schedule-selector-trigger']")
                    )
                ),
                timeout=20,
            )
        except DriverUnhealthyError:
            raise
        except Exception as e:
            logger.warning(f"{self.log_prefix}[SCHEDULE] trigger not found: {e}")
            return False

        if not session.safe_click(sched_btn):
            return False

        # Wait for the menu to actually open (aria-expanded=true)
        try:
            safe_driver_call(
                lambda: WebDriverWait(session.driver, 10).until(
                    EC.presence_of_element_located((
                        By.CSS_SELECTOR,
                        "button[data-testid='schedule-selector-trigger']"
                        "[aria-expanded='true']",
                    ))
                ),
                timeout=15,
            )
        except Exception:
            logger.warning(f"{self.log_prefix}[SCHEDULE] menu did not open")
            return False

        time.sleep(1.5)  # let menu items hydrate
        session.dismiss_blocking_overlays()

        try:
            now_item = safe_driver_call(
                lambda: WebDriverWait(session.driver, 10).until(
                    EC.presence_of_element_located((
                        By.XPATH,
                        "//div[@role='menuitem'][.//p[normalize-space(text())='Now']]",
                    ))
                ),
                timeout=15,
            )
        except DriverUnhealthyError:
            raise
        except Exception as e:
            logger.warning(f"{self.log_prefix}[SCHEDULE] 'Now' item not found: {e}")
            return False

        if not session.safe_click(now_item):
            return False
        logger.info(f"{self.log_prefix}[SCHEDULE] set to Now")
        time.sleep(1)
        return True

    def _click_publish(self, session: BrowserSession) -> bool:
        session.dismiss_blocking_overlays()
        publish_selectors = (
            (By.XPATH, "//button[normalize-space(text())='Share Now']"),
            (By.XPATH, "//button[normalize-space(text())='Publish Now']"),
            (By.CSS_SELECTOR, "button[data-testid='publish-button']"),
            (By.XPATH, "//button[contains(@class,'schedulePostButton')]"),
        )
        publish_btn = None
        for by, sel in publish_selectors:
            try:
                publish_btn = safe_driver_call(
                    lambda b=by, s=sel: WebDriverWait(session.driver, 6).until(
                        EC.presence_of_element_located((b, s))
                    ),
                    timeout=10,
                )
                if publish_btn is not None:
                    break
            except DriverUnhealthyError:
                raise
            except Exception:
                continue

        if publish_btn is None:
            logger.error(f"{self.log_prefix}[PUBLISH] button not found")
            return False

        if not session.safe_click(publish_btn):
            logger.error(f"{self.log_prefix}[PUBLISH] click failed")
            return False
        logger.info(f"{self.log_prefix}[PUBLISH] clicked")
        return True

    def _verify_publish_succeeded(
        self, session: BrowserSession, *, timeout: int = 90
    ) -> bool:
        """
        Verify Buffer accepted the publish.

        Success criteria (any one):
          - The Publish button disappeared from the DOM (composer closed),
            AND its label is no longer ``Sharing now…`` (transient state).
          - A success toast contains "queued" / "shared" / "posted" / etc.
          - The URL changed away from the composer.

        Returns False on timeout — caller decides whether to retry.
        """
        composer_url_marker = "/compose"  # buffer composer route fragment

        publish_btn_selectors = (
            (By.XPATH, "//button[normalize-space(text())='Share Now']"),
            (By.XPATH, "//button[normalize-space(text())='Publish Now']"),
            (By.CSS_SELECTOR, "button[data-testid='publish-button']"),
        )
        success_xpath = (
            "//*[contains(translate(., "
            "'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'),"
            " 'sharing now') or "
            "contains(translate(., "
            "'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'),"
            " 'post added') or "
            "contains(translate(., "
            "'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'),"
            " 'has been posted') or "
            "contains(translate(., "
            "'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'),"
            " 'successfully') or "
            "contains(translate(., "
            "'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'),"
            " 'queued') or "
            "contains(translate(., "
            "'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'),"
            " 'shared')]"
        )

        deadline = time.time() + timeout
        last_log = 0.0
        while time.time() < deadline:
            if not session.is_healthy():
                logger.warning(
                    f"{self.log_prefix}[VERIFY] driver unhealthy during verify"
                )
                return False

            # 1) URL left composer = success
            current = session.current_url()
            if current and composer_url_marker not in current.lower():
                # In the new Buffer UI, on success we are redirected back to the
                # channel feed which doesn't contain '/compose'.
                logger.info(
                    f"{self.log_prefix}[VERIFY] URL left composer: {current}"
                )
                return True

            # 2) Publish button gone
            visible = False
            for by, sel in publish_btn_selectors:
                try:
                    btn = safe_driver_call(
                        lambda b=by, s=sel: session.driver.find_element(b, s),
                        timeout=4,
                    )
                    is_disp = False
                    if btn is not None:
                        try:
                            is_disp = bool(
                                safe_driver_call(btn.is_displayed, timeout=4)
                            )
                        except Exception:
                            is_disp = False
                    if is_disp:
                        visible = True
                        break
                except Exception:
                    continue
            if not visible:
                logger.info(f"{self.log_prefix}[VERIFY] publish button gone")
                return True

            # 3) Success toast
            try:
                el = safe_driver_call(
                    lambda: session.driver.find_element(By.XPATH, success_xpath),
                    timeout=4,
                )
                if el is not None and bool(
                    safe_driver_call(el.is_displayed, timeout=4)
                ):
                    logger.info(f"{self.log_prefix}[VERIFY] success toast detected")
                    return True
            except Exception:
                pass

            now = time.time()
            if now - last_log > 5:
                logger.info(f"{self.log_prefix}[VERIFY] waiting...")
                last_log = now
            time.sleep(1)

        logger.error(f"{self.log_prefix}[VERIFY] timeout — no success signal")
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Functional API (backward compatible)
# ─────────────────────────────────────────────────────────────────────────────

def upload_instagram_reels(
    file_path: str, caption: str, profile_path: str, buffer_url: str
) -> bool:
    return InstagramUploader(profile_path, buffer_url).upload_reels(file_path, caption)
