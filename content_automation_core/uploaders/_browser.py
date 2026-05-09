"""
Centralized browser session manager for all uploaders.

Designed to make Selenium-based uploaders production-stable by enforcing
strict timeouts, killing orphan Chrome processes, cleaning profile lock
files, and detecting unhealthy drivers early. NEVER retries forever — fails
fast and recovers cleanly.

Key guarantees:
- No webdriver command can block longer than ``SAFE_COMMAND_TIMEOUT`` seconds.
- No upload can run longer than ``GLOBAL_UPLOAD_TIMEOUT`` seconds.
- Each browser start is preceded by killing any chrome.exe / chromedriver.exe
  processes that share the same ``--user-data-dir`` and removing leftover
  ``Singleton{Lock,Cookie,Socket}`` files.
- When a session is force-closed, the chrome.exe processes are guaranteed
  to be killed (not just driver.quit()).

Public API:
    BrowserSession(profile_path, log_prefix=..., headless=False, extra_args=...)
        .start()
        .navigate(url)
        .is_healthy()
        .safe_click(element)
        .safe_send_keys(element, text)
        .dismiss_blocking_overlays()
        .force_close()

    run_with_upload_timeout(worker_fn, get_session_fn, timeout_sec, log_prefix)
        Runs worker_fn in a daemon thread; if it exceeds timeout, force-closes
        the current browser session so the worker unblocks.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Callable, Iterable, List, Optional

import psutil
from selenium import webdriver
from selenium.common.exceptions import (
    ElementClickInterceptedException,
    NoSuchElementException,
    StaleElementReferenceException,
    WebDriverException,
)
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Hard limits — see module docstring
# ─────────────────────────────────────────────────────────────────────────────

PAGE_LOAD_TIMEOUT = 45              # driver.set_page_load_timeout
SCRIPT_TIMEOUT = 30                 # driver.set_script_timeout
SAFE_COMMAND_TIMEOUT = 25           # default thread-level timeout per webdriver call
HEALTH_CHECK_TIMEOUT = 5            # for is_driver_healthy()
QUIT_TIMEOUT = 10                   # max wait for driver.quit()
KILL_GRACE_SEC = 8                  # max wait for OS to release a killed process
GLOBAL_UPLOAD_TIMEOUT = 15 * 60     # 15 minutes per upload, hard cap
MAX_DRIVER_RECREATIONS = 2          # i.e. up to 3 attempts (initial + 2 recreates)

INTERNAL_PAGE_PREFIXES = ("chrome://", "about:", "data:", "chrome-error://")

# Substrings that, if found in document.title, indicate the browser is in
# an unrecoverable state and should be force-closed.
CRASH_TITLE_MARKERS = (
    "Aw, Snap!",
    "Tab crashed",
    "He's dead, Jim!",
    "Restore pages",
)

CHROME_PROCESS_NAMES = {
    "chrome.exe",
    "chromedriver.exe",
    "google chrome.exe",
    "chrome",
    "chromedriver",
}


# ─────────────────────────────────────────────────────────────────────────────
# Exceptions
# ─────────────────────────────────────────────────────────────────────────────

class DriverUnhealthyError(Exception):
    """Raised when a webdriver command times out or the driver is dead."""


class NavigationError(Exception):
    """Raised when navigation lands on an internal/error/crash page."""


# ─────────────────────────────────────────────────────────────────────────────
# Process-level helpers
# ─────────────────────────────────────────────────────────────────────────────

def _normalize_path(p: str) -> str:
    return os.path.normpath(p).lower()


def _proc_uses_profile(proc: psutil.Process, profile_norm: str) -> bool:
    """Return True if proc was started with --user-data-dir=<profile_path>."""
    try:
        cmdline = proc.cmdline() or []
    except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
        return False
    for arg in cmdline:
        if not arg:
            continue
        a = arg.lower()
        if "--user-data-dir" in a or "user-data-dir=" in a:
            value = a.split("=", 1)[-1].strip().strip('"').strip("'")
            if value and _normalize_path(value) == profile_norm:
                return True
    return False


def _find_profile_processes(profile_path: str) -> List[psutil.Process]:
    """Return Chrome / chromedriver processes that own profile_path."""
    if not profile_path:
        return []
    norm = _normalize_path(profile_path)
    found: List[psutil.Process] = []
    for proc in psutil.process_iter(attrs=("name",)):
        try:
            name = (proc.info.get("name") or "").lower()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if name not in CHROME_PROCESS_NAMES:
            continue
        if _proc_uses_profile(proc, norm):
            found.append(proc)
    return found


def kill_existing_profile_processes(profile_path: str, log_prefix: str = "") -> int:
    """
    Kill any chrome.exe / chromedriver.exe processes that are using
    ``profile_path`` as ``--user-data-dir``. Blocks up to KILL_GRACE_SEC
    seconds for the OS to clean them up. Returns the count actually killed.
    """
    procs = _find_profile_processes(profile_path)
    if not procs:
        return 0

    for p in procs:
        try:
            p.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    deadline = time.time() + KILL_GRACE_SEC
    while time.time() < deadline:
        alive = [p for p in procs if p.is_running()]
        if not alive:
            break
        time.sleep(0.3)

    killed = sum(1 for p in procs if not p.is_running())
    logger.info(f"{log_prefix}[CLEANUP] killed {killed}/{len(procs)} chrome processes")
    return killed


def cleanup_singleton_locks(profile_path: str, log_prefix: str = "") -> List[str]:
    """
    Remove leftover Chrome SingletonLock / SingletonCookie / SingletonSocket
    files. These are written by Chrome on start and removed on clean exit.
    If a previous Chrome was killed (or crashed) they remain and Chrome will
    open in guest mode on the next start — which is the root cause of the
    "stuck on chrome://newtab" symptom.
    """
    if not profile_path or not os.path.isdir(profile_path):
        return []
    removed: List[str] = []
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        path = os.path.join(profile_path, name)
        try:
            if os.path.islink(path):
                os.unlink(path)
                removed.append(name)
            elif os.path.exists(path):
                os.remove(path)
                removed.append(name)
        except OSError as e:
            logger.warning(f"{log_prefix}[CLEANUP] could not remove {name}: {e}")
    if removed:
        for n in removed:
            logger.info(f"{log_prefix}[CLEANUP] removed {n}")
    return removed


# ─────────────────────────────────────────────────────────────────────────────
# Thread-level command wrapper — guarantees no webdriver call blocks forever
# ─────────────────────────────────────────────────────────────────────────────

def safe_driver_call(fn: Callable, *, timeout: float = SAFE_COMMAND_TIMEOUT):
    """
    Run ``fn()`` (typically a webdriver call) in a daemon thread and abort if
    it does not return within ``timeout`` seconds.

    On timeout: raises DriverUnhealthyError. The worker thread is left to
    finish on its own — it will exit cleanly when the caller calls
    ``BrowserSession.force_close()`` (which kills the underlying chrome and
    chromedriver processes, causing any in-flight HTTP socket read in the
    worker thread to error out).

    DO NOT use this from inside another safe_driver_call.
    """
    box = {"value": None, "error": None}
    done = threading.Event()

    def runner():
        try:
            box["value"] = fn()
        except BaseException as e:  # noqa: BLE001 — propagate everything
            box["error"] = e
        finally:
            done.set()

    t = threading.Thread(target=runner, daemon=True)
    t.start()
    if not done.wait(timeout=timeout):
        raise DriverUnhealthyError(
            f"WebDriver command exceeded {timeout}s and was abandoned"
        )
    if box["error"] is not None:
        raise box["error"]
    return box["value"]


# ─────────────────────────────────────────────────────────────────────────────
# BrowserSession — one stable Chrome session
# ─────────────────────────────────────────────────────────────────────────────

class BrowserSession:
    """
    A single Chrome session with strict timeouts, health checks, and
    guaranteed force-close. Always created via ``start()``.

    Lifecycle:
        s = BrowserSession(profile_path, log_prefix="[IG][upload_id=...]")
        s.start()                       # cleanup → spawn Chrome → set timeouts
        try:
            s.navigate("https://...")   # raises NavigationError on internal pages
            ...
        finally:
            s.force_close()             # always safe — kills processes if needed
    """

    def __init__(
        self,
        profile_path: Optional[str],
        *,
        log_prefix: str = "",
        headless: bool = False,
        extra_args: Optional[Iterable[str]] = None,
    ) -> None:
        self.profile_path = profile_path
        self.log_prefix = log_prefix
        self.headless = headless
        self.extra_args: List[str] = list(extra_args or [])
        self.driver: Optional[webdriver.Chrome] = None
        self._dead = False

    # ── lifecycle ──────────────────────────────────────────────────────────

    def start(self) -> None:
        """Cleanup any orphan processes, then spawn Chrome with hard timeouts."""
        if self.profile_path:
            kill_existing_profile_processes(self.profile_path, self.log_prefix)
            cleanup_singleton_locks(self.profile_path, self.log_prefix)
            time.sleep(1)

        opts = Options()
        if self.profile_path:
            opts.add_argument(f"--user-data-dir={self.profile_path}")
        opts.add_argument("--window-size=1920,1080")
        opts.add_argument("--no-first-run")
        opts.add_argument("--no-default-browser-check")
        opts.add_argument("--disable-session-crashed-bubble")
        opts.add_argument("--restore-last-session=false")
        opts.add_argument("--disable-blink-features=AutomationControlled")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--disable-notifications")
        opts.add_argument("--disable-infobars")
        opts.add_experimental_option("excludeSwitches", ["enable-automation"])
        opts.add_experimental_option("useAutomationExtension", False)
        if self.headless:
            opts.add_argument("--headless=new")
        for arg in self.extra_args:
            opts.add_argument(arg)

        try:
            self.driver = webdriver.Chrome(options=opts)
        except WebDriverException as e:
            self._dead = True
            logger.error(f"{self.log_prefix}[BROWSER] start failed: {e}")
            raise

        # Hard timeouts so single commands cannot block forever.
        try:
            self.driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT)
            self.driver.set_script_timeout(SCRIPT_TIMEOUT)
        except WebDriverException:
            pass

        try:
            self.driver.execute_script(
                "Object.defineProperty(navigator, 'webdriver', "
                "{get: () => undefined})"
            )
        except WebDriverException:
            pass

        logger.info(f"{self.log_prefix}[BROWSER] started")

    def force_close(self) -> None:
        """
        Close the browser with a hard cap of ~15 seconds.

        Steps:
          1. driver.quit() with QUIT_TIMEOUT cap (in a worker thread).
          2. Kill any chrome.exe / chromedriver.exe still using profile_path.
          3. Remove SingletonLock / SingletonCookie / SingletonSocket files.

        Always safe to call multiple times.
        """
        d = self.driver
        self.driver = None
        self._dead = True

        if d is not None:
            try:
                safe_driver_call(d.quit, timeout=QUIT_TIMEOUT)
            except Exception as e:
                logger.warning(f"{self.log_prefix}[BROWSER] quit failed: {e}")

        if self.profile_path:
            kill_existing_profile_processes(self.profile_path, self.log_prefix)
            cleanup_singleton_locks(self.profile_path, self.log_prefix)
        logger.info(f"{self.log_prefix}[BROWSER] force-closed")

    # ── health & state ─────────────────────────────────────────────────────

    def is_healthy(self) -> bool:
        """
        Return True only if a 5-second ``execute_script("return 1")`` round-trip
        succeeds. Anything else (timeout, exception, dead driver) returns False.
        """
        if self.driver is None or self._dead:
            return False
        try:
            ok = safe_driver_call(
                lambda: self.driver.execute_script("return 1") == 1,
                timeout=HEALTH_CHECK_TIMEOUT,
            )
            return bool(ok)
        except DriverUnhealthyError:
            logger.warning(f"{self.log_prefix}[HEALTH] driver hung — marking dead")
            self._dead = True
            return False
        except WebDriverException as e:
            logger.warning(f"{self.log_prefix}[HEALTH] {e}")
            self._dead = True
            return False

    def current_url(self) -> str:
        try:
            return safe_driver_call(
                lambda: self.driver.current_url or "", timeout=HEALTH_CHECK_TIMEOUT
            )
        except Exception:
            return ""

    def title(self) -> str:
        try:
            return safe_driver_call(
                lambda: self.driver.title or "", timeout=HEALTH_CHECK_TIMEOUT
            )
        except Exception:
            return ""

    # ── navigation ─────────────────────────────────────────────────────────

    def navigate(self, url: str) -> str:
        """
        Navigate to ``url`` and confirm we landed on a real page.

        Returns the resolved URL on success.

        Raises:
          NavigationError       — landed on chrome://, about:blank, crash page,
                                  or could not read URL.
          DriverUnhealthyError  — webdriver command itself froze.

        Caller is expected to discard this session and recreate on either error.
        """
        try:
            safe_driver_call(
                lambda: self.driver.get(url),
                timeout=PAGE_LOAD_TIMEOUT + 5,
            )
        except DriverUnhealthyError:
            raise
        except WebDriverException as e:
            # Page-load timeout or interrupted load — keep going, may have
            # partially loaded; the readyState check below decides.
            logger.warning(f"{self.log_prefix}[NAV] driver.get raised: {e}")

        # Wait for document.readyState == 'complete' (best effort)
        try:
            safe_driver_call(
                lambda: WebDriverWait(self.driver, 20).until(
                    lambda d: d.execute_script("return document.readyState")
                    == "complete"
                ),
                timeout=25,
            )
        except Exception:
            pass

        current = self.current_url()
        if not current:
            raise NavigationError("could not read current_url after get()")

        if current.startswith(INTERNAL_PAGE_PREFIXES):
            raise NavigationError(f"landed on internal page: {current}")

        title = self.title()
        for marker in CRASH_TITLE_MARKERS:
            if marker.lower() in title.lower():
                raise NavigationError(
                    f"crash/restore page detected (title={title!r})"
                )

        logger.info(f"{self.log_prefix}[NAV] arrived at {current}")
        return current

    # ── overlay handling (Buffer / react-joyride) ──────────────────────────

    _DISMISS_OVERLAYS_JS = r"""
        var removed = 0;

        // 1) Click any visible Skip / Close / Dismiss button on a tour first.
        var skipSelectors = [
            '.react-joyride__overlay button[data-action="skip"]',
            '.react-joyride__tooltip button[data-action="skip"]',
            'button[data-action="close"]',
            '[aria-label="Skip tour"]',
            '[aria-label="Close tour"]'
        ];
        for (var s = 0; s < skipSelectors.length; s++) {
            var btns = document.querySelectorAll(skipSelectors[s]);
            for (var i = 0; i < btns.length; i++) {
                try { btns[i].click(); removed++; } catch (e) {}
            }
        }

        // 2) Forcibly remove overlay containers known to break clicks.
        var overlaySelectors = [
            '.react-joyride__overlay',
            '.react-joyride__spotlight',
            '[class*="publish_overlay_"]',
            '[class*="joyride__overlay"]',
            'div[data-state="open"][class*="overlay"]'
        ];
        for (var s = 0; s < overlaySelectors.length; s++) {
            var els = document.querySelectorAll(overlaySelectors[s]);
            for (var j = 0; j < els.length; j++) {
                try {
                    els[j].style.pointerEvents = 'none';
                    els[j].style.display = 'none';
                    if (els[j].parentNode) {
                        els[j].parentNode.removeChild(els[j]);
                    }
                    removed++;
                } catch (e) {}
            }
        }
        return removed;
    """

    def dismiss_blocking_overlays(self) -> int:
        """
        Detect and dismiss known blocking overlays (react-joyride, Buffer's
        publish_overlay_*). Returns the number of overlays/buttons handled.

        This is NEVER a recovery path — it's a precondition we run before
        every click on Buffer. Failure to dismiss does not raise.
        """
        if self.driver is None or self._dead:
            return 0
        try:
            n = safe_driver_call(
                lambda: self.driver.execute_script(self._DISMISS_OVERLAYS_JS),
                timeout=HEALTH_CHECK_TIMEOUT,
            )
            n = int(n or 0)
        except Exception as e:
            logger.debug(f"{self.log_prefix}[OVERLAY] dismiss script failed: {e}")
            return 0
        if n:
            logger.info(f"{self.log_prefix}[OVERLAY] removed {n} blocking element(s)")
        return n

    # ── safe interactions ──────────────────────────────────────────────────

    def safe_click(self, element) -> bool:
        """
        Click ``element`` using a well-defined fallback ladder. Always
        dismisses overlays first. NEVER retries forever.

        Order:
          1. scroll into view + native click()
          2. on ElementClickIntercepted → dismiss_blocking_overlays + native click
          3. ActionChains move + click
          4. JS .click()

        Returns True on success.
        """
        # Always clear overlays before clicking
        self.dismiss_blocking_overlays()

        try:
            safe_driver_call(
                lambda: self.driver.execute_script(
                    "arguments[0].scrollIntoView({block:'center'});", element
                ),
                timeout=HEALTH_CHECK_TIMEOUT,
            )
        except Exception:
            pass

        # native click
        try:
            safe_driver_call(element.click, timeout=SAFE_COMMAND_TIMEOUT)
            return True
        except ElementClickInterceptedException:
            self.dismiss_blocking_overlays()
        except (StaleElementReferenceException, NoSuchElementException) as e:
            logger.debug(f"{self.log_prefix}[CLICK] stale/missing: {e}")
            return False
        except DriverUnhealthyError:
            raise
        except WebDriverException as e:
            logger.debug(f"{self.log_prefix}[CLICK] native failed: {e}")

        # ActionChains
        try:
            safe_driver_call(
                lambda: ActionChains(self.driver)
                .move_to_element(element)
                .pause(0.1)
                .click()
                .perform(),
                timeout=SAFE_COMMAND_TIMEOUT,
            )
            return True
        except DriverUnhealthyError:
            raise
        except Exception as e:
            logger.debug(f"{self.log_prefix}[CLICK] action chain failed: {e}")

        # JS click — last resort
        try:
            safe_driver_call(
                lambda: self.driver.execute_script("arguments[0].click();", element),
                timeout=SAFE_COMMAND_TIMEOUT,
            )
            return True
        except DriverUnhealthyError:
            raise
        except Exception as e:
            logger.warning(f"{self.log_prefix}[CLICK] all strategies failed: {e}")
            return False

    def safe_send_keys(self, element, text: str, *, clear_first: bool = True) -> bool:
        """
        Type ``text`` into ``element`` with overlay dismissal and JS fallback.
        Returns True on success.
        """
        if not self.safe_click(element):
            logger.debug(f"{self.log_prefix}[TYPE] could not focus element")

        if clear_first:
            try:
                safe_driver_call(
                    lambda: element.send_keys(Keys.CONTROL + "a"),
                    timeout=HEALTH_CHECK_TIMEOUT,
                )
                safe_driver_call(
                    lambda: element.send_keys(Keys.DELETE),
                    timeout=HEALTH_CHECK_TIMEOUT,
                )
            except Exception:
                pass

        # native send_keys
        try:
            safe_driver_call(
                lambda: element.send_keys(text),
                timeout=SAFE_COMMAND_TIMEOUT,
            )
            return True
        except DriverUnhealthyError:
            raise
        except Exception as e:
            logger.debug(f"{self.log_prefix}[TYPE] native failed: {e}")

        # JS fallback for inputs / textareas / contenteditable
        try:
            safe_driver_call(
                lambda: self.driver.execute_script(
                    "var el = arguments[0]; var v = arguments[1];"
                    "if ('value' in el) { el.value = v; }"
                    "else { el.textContent = v; }"
                    "el.dispatchEvent(new Event('input', {bubbles: true}));"
                    "el.dispatchEvent(new Event('change', {bubbles: true}));",
                    element, text,
                ),
                timeout=SAFE_COMMAND_TIMEOUT,
            )
            return True
        except Exception as e:
            logger.warning(f"{self.log_prefix}[TYPE] all strategies failed: {e}")
            return False


# ─────────────────────────────────────────────────────────────────────────────
# Global upload timeout wrapper
# ─────────────────────────────────────────────────────────────────────────────

def run_with_upload_timeout(
    worker_fn: Callable[[], bool],
    get_session_fn: Callable[[], Optional[BrowserSession]],
    timeout_sec: float,
    log_prefix: str = "",
) -> bool:
    """
    Run ``worker_fn`` in a daemon thread with a hard wall-clock timeout.

    On timeout, the *current* BrowserSession (returned by ``get_session_fn``)
    is force-closed. This kills its chrome.exe and chromedriver.exe
    processes, which makes any in-flight Selenium HTTP socket read inside
    the worker thread error out — the worker exits naturally a moment
    later. We give the worker up to 15 seconds to wind down and return.

    Returns whatever ``worker_fn`` returned, or False on timeout / exception.
    """
    box = {"value": False, "error": None}
    done = threading.Event()

    def runner():
        try:
            box["value"] = bool(worker_fn())
        except Exception as e:  # noqa: BLE001
            box["error"] = e
        finally:
            done.set()

    t = threading.Thread(target=runner, daemon=True)
    t.start()
    finished = done.wait(timeout=timeout_sec)

    if not finished:
        logger.error(
            f"{log_prefix}[TIMEOUT] upload exceeded {timeout_sec:.0f}s "
            f"— force-closing browser"
        )
        try:
            sess = get_session_fn()
        except Exception:
            sess = None
        if sess is not None:
            try:
                sess.force_close()
            except Exception as e:
                logger.warning(f"{log_prefix}[TIMEOUT] force-close raised: {e}")
        # Allow worker thread to unblock and exit after browser death.
        done.wait(timeout=15)
        return False

    if box["error"] is not None:
        logger.error(
            f"{log_prefix}[ERROR] worker raised {type(box['error']).__name__}: "
            f"{box['error']}"
        )
        return False

    return bool(box["value"])
