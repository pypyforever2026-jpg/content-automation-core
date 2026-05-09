"""
Centralized browser session manager for all uploaders.

Designed to survive multi-day production runtime. Enforces strict timeouts,
kills orphan Chrome processes (using exact normalized path matching — never
a substring), cleans profile lock files, and detects unhealthy drivers AND
unhealthy pages early. NEVER retries forever.

Key guarantees:
  - No webdriver command can block longer than ``SAFE_COMMAND_TIMEOUT`` s.
  - No upload can run longer than ``GLOBAL_UPLOAD_TIMEOUT`` s.
  - ``BrowserSession.force_close()`` ALWAYS returns within
    ``FORCE_CLOSE_HARD_DEADLINE`` seconds, kills the chromedriver PID and
    every Chrome PID it spawned, and finally scans the system for
    survivors that match the same normalized profile path.
  - Process matching uses ``pathlib.Path.resolve() + os.path.normcase``;
    a Chrome instance whose ``--user-data-dir`` does not normalize to
    EXACTLY the session's profile path is NEVER touched.
  - ``dismiss_blocking_overlays()`` works without hardcoded class names:
    any fixed/absolute element covering >30% of the viewport with
    ``z-index > 1000`` is treated as a blocking layer.

Public API (preserved):
    BrowserSession(profile_path, log_prefix=..., headless=False, extra_args=...)
        .start()
        .navigate(url)
        .is_healthy()
        .is_page_healthy()
        .safe_click(element)
        .safe_send_keys(element, text)
        .dismiss_blocking_overlays()
        .chrome_rss_bytes()
        .check_memory_pressure(threshold_mb)
        .force_close() -> dict   (diagnostics)

    safe_driver_call(fn, *, timeout)
    run_with_upload_timeout(worker_fn, get_session_fn, timeout_sec, log_prefix)
    kill_existing_profile_processes(profile_path, log_prefix)
    cleanup_singleton_locks(profile_path, log_prefix)
    get_runtime_counters() -> dict
"""

from __future__ import annotations

import logging
import os
import pathlib
import threading
import time
from typing import Callable, Dict, Iterable, List, Optional, Set

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
# Hard limits
# ─────────────────────────────────────────────────────────────────────────────

PAGE_LOAD_TIMEOUT = 45               # driver.set_page_load_timeout
SCRIPT_TIMEOUT = 30                  # driver.set_script_timeout
SAFE_COMMAND_TIMEOUT = 25            # default thread-level timeout per call
HEALTH_CHECK_TIMEOUT = 5             # is_healthy() round-trip
QUIT_TIMEOUT = 5                     # graceful driver.quit() inside force_close
FORCE_CLOSE_HARD_DEADLINE = 15       # absolute upper bound for force_close()
KILL_GRACE_SEC = 8                   # OS wait for a killed PID to actually die
GLOBAL_UPLOAD_TIMEOUT = 15 * 60      # 15 min per upload, hard cap
MAX_DRIVER_RECREATIONS = 2           # max recreations per upload

INTERNAL_PAGE_PREFIXES = ("chrome://", "about:", "data:", "chrome-error://")

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
# Runtime counters (lightweight, lock-protected)
# ─────────────────────────────────────────────────────────────────────────────

class _RuntimeCounters:
    """Thread-safe counters for live observability. Cheap to update."""

    __slots__ = ("_lock", "active_driver_threads", "force_close_invocations",
                 "command_timeouts", "force_close_deadline_hits")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.active_driver_threads = 0
        self.force_close_invocations = 0
        self.command_timeouts = 0
        self.force_close_deadline_hits = 0

    def bump(self, name: str, delta: int = 1) -> None:
        with self._lock:
            setattr(self, name, getattr(self, name) + delta)

    def snapshot(self) -> Dict[str, int]:
        with self._lock:
            return {
                "active_driver_threads": self.active_driver_threads,
                "force_close_invocations": self.force_close_invocations,
                "command_timeouts": self.command_timeouts,
                "force_close_deadline_hits": self.force_close_deadline_hits,
            }


_COUNTERS = _RuntimeCounters()


def get_runtime_counters() -> Dict[str, int]:
    """Snapshot of live counters. Use for health monitors / dashboards."""
    return _COUNTERS.snapshot()


# ─────────────────────────────────────────────────────────────────────────────
# Path normalization — the foundation of safe process matching
# ─────────────────────────────────────────────────────────────────────────────

def _norm_path(p: Optional[str]) -> str:
    """
    Canonicalize a filesystem path for comparison.

    Steps applied (in order):
      1. strip surrounding quotes/whitespace
      2. abspath (handle relative paths)
      3. Path.resolve() — handles symlinks, ``..``, mixed slashes; works
         on non-existent paths since Python 3.6 (strict=False is the default).
      4. normcase — lowercase on Windows so ``C:\\X`` == ``c:\\x``.

    Returns ``""`` for empty / unparseable input. Never raises.

    Examples (Windows-flavored, case insensitive):
      _norm_path("C:/Users/Foo/Profile") == _norm_path("c:\\users\\foo\\profile\\")
      _norm_path('"C:/My Path/p"') == _norm_path("C:/My Path/p")
    """
    if not p or not isinstance(p, str):
        return ""
    raw = p.strip().strip('"').strip("'")
    if not raw:
        return ""
    try:
        absp = os.path.abspath(raw)
    except (TypeError, ValueError, OSError):
        return ""
    try:
        resolved = str(pathlib.Path(absp).resolve(strict=False))
    except (OSError, RuntimeError, ValueError):
        resolved = absp
    return os.path.normcase(resolved)


def _extract_user_data_dir(cmdline: Optional[List[str]]) -> Optional[str]:
    """
    Extract the value of ``--user-data-dir`` from a command-line list.

    Handles all of:
      ['chrome.exe', '--user-data-dir=C:/path/profile', ...]
      ['chrome.exe', '--user-data-dir', 'C:/path/profile', ...]
      ['chrome.exe', '--user-data-dir="C:\\path with spaces\\p"', ...]

    Returns the raw string value (NOT normalized).
    """
    if not cmdline:
        return None
    n = len(cmdline)
    for i, arg in enumerate(cmdline):
        if not isinstance(arg, str):
            continue
        a = arg.lstrip()
        if a.startswith("--user-data-dir="):
            value = a[len("--user-data-dir="):].strip().strip('"').strip("'")
            return value or None
        if a == "--user-data-dir" and i + 1 < n:
            nxt = cmdline[i + 1]
            if isinstance(nxt, str):
                value = nxt.strip().strip('"').strip("'")
                return value or None
    return None


# Sanity assertions on the normalizer — no doctests (don't run by default).
assert _norm_path("") == ""
assert _norm_path(None) == ""  # type: ignore[arg-type]
assert _norm_path("C:/x") == _norm_path("C:\\x")
assert _norm_path("C:/x/y/../y") == _norm_path("C:/x/y")
assert _extract_user_data_dir(["chrome", "--user-data-dir=/p"]) == "/p"
assert _extract_user_data_dir(["chrome", "--user-data-dir", "/p"]) == "/p"
assert _extract_user_data_dir(["chrome", '--user-data-dir="/p with space"']) \
    == "/p with space"
assert _extract_user_data_dir(["chrome", "--no-sandbox"]) is None


# ─────────────────────────────────────────────────────────────────────────────
# Process discovery + safe killing
# ─────────────────────────────────────────────────────────────────────────────

def _proc_matches_profile(proc: psutil.Process, profile_norm: str) -> bool:
    """True iff *proc* was started with ``--user-data-dir`` == profile_norm.

    Match is on the FULLY NORMALIZED path (resolve + normcase). A Chrome
    instance whose user-data-dir is just a sibling/parent of profile_norm
    is NOT matched. This prevents collateral kills.
    """
    if not profile_norm:
        return False
    try:
        cmdline = proc.cmdline() or []
    except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
        return False
    raw = _extract_user_data_dir(cmdline)
    if not raw:
        return False
    return _norm_path(raw) == profile_norm


def _find_profile_processes(profile_path: str) -> List[psutil.Process]:
    """List Chrome / chromedriver processes whose --user-data-dir EXACTLY
    matches *profile_path* after normalization."""
    profile_norm = _norm_path(profile_path)
    if not profile_norm:
        return []
    found: List[psutil.Process] = []
    for proc in psutil.process_iter(attrs=("name",)):
        try:
            name = (proc.info.get("name") or "").lower()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if name not in CHROME_PROCESS_NAMES:
            continue
        if _proc_matches_profile(proc, profile_norm):
            found.append(proc)
    return found


def _kill_pid(pid: Optional[int], deadline: float, log_prefix: str = "") -> bool:
    """
    Kill *pid* (and verify). Returns True iff a live process was killed.

    Bounded by *deadline* (absolute time.time()). If the deadline is already
    past, sends the signal but does not wait.
    """
    if not pid or pid <= 0:
        return False
    try:
        proc = psutil.Process(pid)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False
    if not proc.is_running():
        return False
    try:
        proc.kill()
    except psutil.NoSuchProcess:
        return False
    except psutil.AccessDenied as e:
        logger.warning(f"{log_prefix}[BROWSER][KILL] access denied for pid={pid}: {e}")
        return False

    remaining = max(0.0, min(2.0, deadline - time.time()))
    if remaining > 0:
        try:
            proc.wait(timeout=remaining)
        except psutil.TimeoutExpired:
            pass
        except psutil.NoSuchProcess:
            pass
    return True


def _wait_pids_gone(pids: Iterable[int], deadline: float) -> bool:
    """Return True when every pid in *pids* is gone, or False on deadline."""
    pid_list = [p for p in pids if p]
    while time.time() < deadline:
        any_alive = False
        for pid in pid_list:
            try:
                proc = psutil.Process(pid)
            except psutil.NoSuchProcess:
                continue
            try:
                if proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE:
                    any_alive = True
                    break
            except psutil.NoSuchProcess:
                continue
        if not any_alive:
            return True
        time.sleep(0.15)
    return False


def kill_existing_profile_processes(profile_path: str, log_prefix: str = "") -> int:
    """
    Kill chrome.exe / chromedriver.exe processes whose ``--user-data-dir`` is
    EXACTLY equal (after normalization) to *profile_path*. Blocks up to
    KILL_GRACE_SEC seconds for the OS to clean them up.

    Returns the number actually killed.
    """
    procs = _find_profile_processes(profile_path)
    if not procs:
        return 0

    deadline = time.time() + KILL_GRACE_SEC
    for p in procs:
        try:
            p.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    while time.time() < deadline:
        alive = [p for p in procs if p.is_running()]
        if not alive:
            break
        time.sleep(0.3)

    killed = sum(1 for p in procs if not p.is_running())
    logger.info(f"{log_prefix}[CLEANUP] killed {killed}/{len(procs)} chrome processes")
    return killed


def cleanup_singleton_locks(profile_path: str, log_prefix: str = "") -> List[str]:
    """Remove leftover SingletonLock / SingletonCookie / SingletonSocket files."""
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
    for n in removed:
        logger.info(f"{log_prefix}[CLEANUP] removed {n}")
    return removed


# ─────────────────────────────────────────────────────────────────────────────
# Thread-level command wrapper
# ─────────────────────────────────────────────────────────────────────────────

def safe_driver_call(fn: Callable, *, timeout: float = SAFE_COMMAND_TIMEOUT):
    """
    Run ``fn()`` in a daemon thread; abort if it does not return within
    *timeout* seconds.

    On timeout: raises DriverUnhealthyError. The worker thread is left to
    finish on its own — it WILL exit cleanly once the caller calls
    ``BrowserSession.force_close()`` because that kills the chromedriver
    process, which makes any in-flight HTTP socket read in the worker
    thread error out. The active_driver_threads counter tracks how many
    such workers are still alive.
    """
    box = {"value": None, "error": None}
    done = threading.Event()

    def runner():
        _COUNTERS.bump("active_driver_threads", 1)
        try:
            box["value"] = fn()
        except BaseException as e:  # noqa: BLE001
            box["error"] = e
        finally:
            _COUNTERS.bump("active_driver_threads", -1)
            done.set()

    t = threading.Thread(target=runner, daemon=True)
    t.start()
    if not done.wait(timeout=timeout):
        _COUNTERS.bump("command_timeouts", 1)
        raise DriverUnhealthyError(
            f"WebDriver command exceeded {timeout}s and was abandoned"
        )
    if box["error"] is not None:
        raise box["error"]
    return box["value"]


# ─────────────────────────────────────────────────────────────────────────────
# Generic blocking-overlay detection
# ─────────────────────────────────────────────────────────────────────────────

# Optional hints — checked first so onboarding tours can be politely skipped.
_OVERLAY_DISMISS_HINTS_JS = r"""
    var dismissed = 0;
    var skipSelectors = [
        '.react-joyride__overlay button[data-action="skip"]',
        '.react-joyride__tooltip button[data-action="skip"]',
        'button[data-action="close"]',
        '[aria-label="Skip tour"]',
        '[aria-label="Close tour"]',
        '[aria-label="Dismiss"]',
        'button[aria-label="Close"]'
    ];
    for (var s = 0; s < skipSelectors.length; s++) {
        var btns = document.querySelectorAll(skipSelectors[s]);
        for (var i = 0; i < btns.length; i++) {
            try { btns[i].click(); dismissed++; } catch (e) {}
        }
    }
    return dismissed;
"""

# The generic detector — element-shape based, NOT class-name based.
_GENERIC_BLOCKING_OVERLAY_JS = r"""
    var vw = window.innerWidth;
    var vh = window.innerHeight;
    if (!vw || !vh) return [];
    var viewArea = vw * vh;
    var minCoverage = viewArea * 0.30;
    var detected = [];

    var nodes = document.querySelectorAll('body *');
    for (var i = 0; i < nodes.length; i++) {
        var el = nodes[i];
        // Skip our own already-disabled elements.
        if (el.dataset && el.dataset.cacOverlayHandled === '1') continue;
        var cs;
        try { cs = window.getComputedStyle(el); } catch (e) { continue; }
        if (!cs) continue;

        var pos = cs.position;
        if (pos !== 'fixed' && pos !== 'absolute') continue;
        if (cs.display === 'none' || cs.visibility === 'hidden') continue;
        if (cs.pointerEvents === 'none') continue;

        var rect;
        try { rect = el.getBoundingClientRect(); } catch (e) { continue; }
        var w = Math.min(rect.right, vw) - Math.max(rect.left, 0);
        var h = Math.min(rect.bottom, vh) - Math.max(rect.top, 0);
        if (w <= 0 || h <= 0) continue;
        var area = w * h;
        if (area < minCoverage) continue;

        var z = parseInt(cs.zIndex, 10);
        if (isNaN(z) || z <= 1000) continue;

        // Don't nuke obvious app shell containers (full-page <main>, <body>).
        var tag = el.tagName.toLowerCase();
        if (tag === 'html' || tag === 'body' || tag === 'main') continue;

        // Capture identifying info BEFORE mutating, for diagnostics.
        var info = {
            tag: tag,
            id: el.id || '',
            cls: typeof el.className === 'string' ? el.className.slice(0, 120) : '',
            z: z,
            coverage: +(area / viewArea).toFixed(2)
        };

        // Disable interaction first (cheap), then attempt removal.
        try { el.style.pointerEvents = 'none'; } catch (e) {}
        try { el.style.display = 'none'; } catch (e) {}
        try { if (el.dataset) el.dataset.cacOverlayHandled = '1'; } catch (e) {}
        try {
            if (el.parentNode) el.parentNode.removeChild(el);
        } catch (e) {}
        detected.push(info);
    }
    return detected;
"""


# ─────────────────────────────────────────────────────────────────────────────
# BrowserSession — one stable Chrome session
# ─────────────────────────────────────────────────────────────────────────────

class BrowserSession:
    """
    A single Chrome session with strict timeouts, health checks, and
    guaranteed force-close. Always created via ``start()``.

    Thread model:
      - Selenium calls are issued from one logical "worker" thread (the
        thread inside ``run_with_upload_timeout``).
      - ``force_close()`` may be called from a *different* thread (the
        timeout watcher). It atomically transitions state to ``closing``
        so no further command on this session can race with cleanup.
    """

    _STATE_NEW = "new"
    _STATE_OPEN = "open"
    _STATE_CLOSING = "closing"
    _STATE_CLOSED = "closed"

    def __init__(
        self,
        profile_path: Optional[str],
        *,
        log_prefix: str = "",
        headless: bool = False,
        extra_args: Optional[Iterable[str]] = None,
    ) -> None:
        self.profile_path = profile_path
        self.profile_path_norm = _norm_path(profile_path) if profile_path else ""
        self.log_prefix = log_prefix
        self.headless = headless
        self.extra_args: List[str] = list(extra_args or [])
        self.driver: Optional[webdriver.Chrome] = None

        # Lifecycle / threading
        self._state_lock = threading.Lock()
        self._state = self._STATE_NEW
        self._dead = False  # backward-compat sentinel

        # PIDs captured at start() — used to guarantee cleanup on close()
        self._chromedriver_pid: Optional[int] = None
        self._chrome_pids: Set[int] = set()

        # Page-readiness tracking for is_page_healthy()
        self._first_non_complete_at: Optional[float] = None

    # ── lifecycle ──────────────────────────────────────────────────────────

    def start(self) -> None:
        """Cleanup orphans → spawn Chrome → set hard timeouts → capture PIDs."""
        with self._state_lock:
            if self._state != self._STATE_NEW:
                raise RuntimeError(f"BrowserSession already started (state={self._state})")
            self._state = self._STATE_OPEN

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
            driver = webdriver.Chrome(options=opts)
        except WebDriverException as e:
            with self._state_lock:
                self._state = self._STATE_CLOSED
                self._dead = True
            logger.error(f"{self.log_prefix}[BROWSER] start failed: {e}")
            raise

        try:
            driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT)
            driver.set_script_timeout(SCRIPT_TIMEOUT)
        except WebDriverException:
            pass
        try:
            driver.execute_script(
                "Object.defineProperty(navigator, 'webdriver', "
                "{get: () => undefined})"
            )
        except WebDriverException:
            pass

        self.driver = driver

        # Capture chromedriver PID + spawned Chrome PIDs.
        cd_pid: Optional[int] = None
        try:
            cd_pid = driver.service.process.pid  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            cd_pid = None
        self._chromedriver_pid = cd_pid

        # Give chromedriver a moment to spawn its chrome.exe children, then
        # snapshot. force_close() rescans children at close time too, so a
        # missed child here is not catastrophic.
        if cd_pid:
            time.sleep(0.5)
            try:
                cd_proc = psutil.Process(cd_pid)
                for child in cd_proc.children(recursive=True):
                    self._chrome_pids.add(child.pid)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

        logger.info(
            f"{self.log_prefix}[BROWSER] started "
            f"(chromedriver_pid={cd_pid}, chrome_pids={len(self._chrome_pids)})"
        )

    def force_close(self) -> Dict[str, object]:
        """
        Industrial-grade close. ALWAYS returns within
        ``FORCE_CLOSE_HARD_DEADLINE`` seconds.

        Returns a diagnostics dict::

            {
              "graceful_quit": bool,
              "chromedriver_killed": int,   # 0 or 1
              "chrome_killed": int,         # children + survivors
              "timeout_hit": bool,          # True if hard deadline ran out
            }

        Re-entrant: a second call after closing returns a no-op diagnostics.
        """
        deadline = time.time() + FORCE_CLOSE_HARD_DEADLINE
        diag: Dict[str, object] = {
            "graceful_quit": False,
            "chromedriver_killed": 0,
            "chrome_killed": 0,
            "timeout_hit": False,
        }

        # 1) Atomically claim closing state and capture handles.
        with self._state_lock:
            if self._state in (self._STATE_CLOSING, self._STATE_CLOSED):
                return diag
            self._state = self._STATE_CLOSING
            d = self.driver
            self.driver = None
            cd_pid = self._chromedriver_pid
            known_chrome = set(self._chrome_pids)
            self._dead = True
        _COUNTERS.bump("force_close_invocations", 1)

        # 2) Best-effort graceful quit (5s cap).
        if d is not None and time.time() < deadline:
            try:
                safe_driver_call(d.quit, timeout=min(QUIT_TIMEOUT, max(1.0, deadline - time.time())))
                diag["graceful_quit"] = True
            except DriverUnhealthyError:
                logger.warning(f"{self.log_prefix}[BROWSER][QUIT_TIMEOUT] driver.quit() exceeded 5s")
            except Exception as e:  # noqa: BLE001
                logger.warning(f"{self.log_prefix}[BROWSER][QUIT_TIMEOUT] {e}")

        # 3) Kill chromedriver PID.
        if cd_pid and time.time() < deadline:
            if _kill_pid(cd_pid, deadline, self.log_prefix):
                diag["chromedriver_killed"] = 1
                logger.info(f"{self.log_prefix}[BROWSER][KILL] chromedriver pid={cd_pid}")

        # 4) Re-snapshot current children of (now dying) chromedriver.
        chrome_to_kill: Set[int] = set(known_chrome)
        if cd_pid:
            try:
                cd_proc = psutil.Process(cd_pid)
                for child in cd_proc.children(recursive=True):
                    chrome_to_kill.add(child.pid)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

        # 5) Kill Chrome PIDs we know about.
        for pid in chrome_to_kill:
            if time.time() >= deadline:
                diag["timeout_hit"] = True
                break
            if _kill_pid(pid, deadline, self.log_prefix):
                diag["chrome_killed"] = int(diag["chrome_killed"]) + 1
                logger.info(f"{self.log_prefix}[BROWSER][KILL] chrome pid={pid}")

        # 6) Survivor scan by EXACT normalized profile path.
        if self.profile_path and time.time() < deadline:
            try:
                survivors = _find_profile_processes(self.profile_path)
            except Exception as e:  # noqa: BLE001
                survivors = []
                logger.warning(f"{self.log_prefix}[BROWSER][KILL] survivor scan failed: {e}")
            for proc in survivors:
                if time.time() >= deadline:
                    diag["timeout_hit"] = True
                    break
                if _kill_pid(proc.pid, deadline, self.log_prefix):
                    diag["chrome_killed"] = int(diag["chrome_killed"]) + 1
                    logger.info(
                        f"{self.log_prefix}[BROWSER][KILL] survivor pid={proc.pid}"
                    )

        # 7) Cleanup lock files.
        if self.profile_path:
            cleanup_singleton_locks(self.profile_path, self.log_prefix)

        # 8) Wait for OS to actually reap.
        all_pids = set(chrome_to_kill)
        if cd_pid:
            all_pids.add(cd_pid)
        if all_pids and time.time() < deadline:
            if not _wait_pids_gone(all_pids, deadline):
                diag["timeout_hit"] = True

        if diag["timeout_hit"]:
            _COUNTERS.bump("force_close_deadline_hits", 1)

        with self._state_lock:
            self._state = self._STATE_CLOSED

        logger.info(
            f"{self.log_prefix}[BROWSER][FORCE_CLOSE] "
            f"graceful={diag['graceful_quit']} "
            f"chromedriver_killed={diag['chromedriver_killed']} "
            f"chrome_killed={diag['chrome_killed']} "
            f"timeout={diag['timeout_hit']}"
        )
        return diag

    # ── state guards ───────────────────────────────────────────────────────

    def _is_open(self) -> bool:
        with self._state_lock:
            return self._state == self._STATE_OPEN and self.driver is not None

    # ── driver / page health ───────────────────────────────────────────────

    def is_healthy(self) -> bool:
        """Cheap driver-level health check. True iff driver responds in 5s."""
        if not self._is_open():
            return False
        try:
            ok = safe_driver_call(
                lambda: self.driver.execute_script("return 1") == 1,
                timeout=HEALTH_CHECK_TIMEOUT,
            )
            return bool(ok)
        except DriverUnhealthyError:
            logger.warning(f"{self.log_prefix}[HEALTH] driver hung — marking dead")
            with self._state_lock:
                self._dead = True
            return False
        except WebDriverException as e:
            logger.warning(f"{self.log_prefix}[HEALTH] {e}")
            with self._state_lock:
                self._dead = True
            return False

    def is_page_healthy(self) -> bool:
        """
        Stronger check: combines driver health with page-level signals.

        Returns False if any of:
          - driver itself is unresponsive (is_healthy() == False)
          - current_url is on an internal scheme (chrome://, about:, ...)
          - title contains a crash marker
          - DOM root (document.body) is missing
          - document.readyState has been != 'complete' for >10 seconds
        """
        if not self.is_healthy():
            return False

        url = self.current_url()
        if not url or url.startswith(INTERNAL_PAGE_PREFIXES):
            return False

        title = self.title()
        for marker in CRASH_TITLE_MARKERS:
            if marker.lower() in title.lower():
                return False

        try:
            state = safe_driver_call(
                lambda: self.driver.execute_script(
                    "return [document.readyState, !!document.body];"
                ),
                timeout=HEALTH_CHECK_TIMEOUT,
            )
        except Exception:  # noqa: BLE001
            return False
        if not state or len(state) < 2:
            return False
        ready_state, has_body = state[0], state[1]
        if not has_body:
            return False

        if ready_state != "complete":
            now = time.time()
            if self._first_non_complete_at is None:
                self._first_non_complete_at = now
            elif now - self._first_non_complete_at > 10:
                logger.warning(
                    f"{self.log_prefix}[HEALTH] readyState='{ready_state}' "
                    f"for >10s — page unhealthy"
                )
                return False
        else:
            self._first_non_complete_at = None
        return True

    def current_url(self) -> str:
        if not self._is_open():
            return ""
        try:
            return safe_driver_call(
                lambda: self.driver.current_url or "", timeout=HEALTH_CHECK_TIMEOUT
            )
        except Exception:
            return ""

    def title(self) -> str:
        if not self._is_open():
            return ""
        try:
            return safe_driver_call(
                lambda: self.driver.title or "", timeout=HEALTH_CHECK_TIMEOUT
            )
        except Exception:
            return ""

    # ── memory ─────────────────────────────────────────────────────────────

    def chrome_rss_bytes(self) -> int:
        """Total RSS in bytes for chromedriver + every Chrome child it owns."""
        total = 0
        seen: Set[int] = set()

        def add_rss(p: psutil.Process) -> None:
            nonlocal total
            if p.pid in seen:
                return
            seen.add(p.pid)
            try:
                total += p.memory_info().rss
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

        if self._chromedriver_pid:
            try:
                cd = psutil.Process(self._chromedriver_pid)
                add_rss(cd)
                for ch in cd.children(recursive=True):
                    add_rss(ch)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        for pid in list(self._chrome_pids):
            try:
                add_rss(psutil.Process(pid))
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return total

    def check_memory_pressure(self, threshold_mb: int) -> bool:
        """
        Return True if the session's combined Chrome RSS exceeds
        *threshold_mb* MB. Caller decides whether to call ``force_close()``.
        Pure observation — no side effects on the session.
        """
        rss = self.chrome_rss_bytes()
        if rss > threshold_mb * 1024 * 1024:
            logger.warning(
                f"{self.log_prefix}[BROWSER][MEMORY] "
                f"RSS={rss // (1024 * 1024)}MB > threshold={threshold_mb}MB"
            )
            return True
        return False

    # ── navigation ─────────────────────────────────────────────────────────

    def navigate(self, url: str) -> str:
        """Navigate and confirm we landed on a real page. See module docstring."""
        if not self._is_open():
            raise DriverUnhealthyError("session is not open")

        # Reset readyState tracker for the new page load.
        self._first_non_complete_at = None

        try:
            safe_driver_call(
                lambda: self.driver.get(url),
                timeout=PAGE_LOAD_TIMEOUT + 5,
            )
        except DriverUnhealthyError:
            raise
        except WebDriverException as e:
            logger.warning(f"{self.log_prefix}[NAV] driver.get raised: {e}")

        # Best-effort wait for readyState — bounded.
        try:
            safe_driver_call(
                lambda: WebDriverWait(self.driver, 20).until(
                    lambda d: d.execute_script("return document.readyState")
                    == "complete"
                ),
                timeout=25,
            )
        except Exception:  # noqa: BLE001
            pass

        # Strict success criteria via the page-health check.
        if not self.is_page_healthy():
            current = self.current_url()
            title = self.title()
            raise NavigationError(
                f"page unhealthy after get(): url={current!r}, title={title!r}"
            )

        current = self.current_url()
        logger.info(f"{self.log_prefix}[NAV] arrived at {current}")
        return current

    # ── overlay handling ───────────────────────────────────────────────────

    def dismiss_blocking_overlays(self) -> int:
        """
        Two-phase overlay handling — runs to completion, never raises.

        Phase 1 (hint-based, polite):
          Click well-known Skip / Close buttons (react-joyride etc.) so the
          host app records the dismissal in its own state. Hardcoded class
          names are HINTS only — failure here does not stop phase 2.

        Phase 2 (generic, structural):
          Find every fixed/absolute element that covers >30% of the
          viewport with z-index > 1000 AND pointer-events != 'none' AND
          tag is not html/body/main. Disable pointer events and remove it.

        Returns the total number of elements affected (phase 1 + phase 2).
        """
        if not self._is_open():
            return 0

        total = 0

        # Phase 1: optional hints.
        try:
            n1 = safe_driver_call(
                lambda: self.driver.execute_script(_OVERLAY_DISMISS_HINTS_JS),
                timeout=HEALTH_CHECK_TIMEOUT,
            )
            total += int(n1 or 0)
        except DriverUnhealthyError:
            return total
        except Exception as e:  # noqa: BLE001
            logger.debug(f"{self.log_prefix}[OVERLAY] hints script failed: {e}")

        # Phase 2: generic detector.
        try:
            detected = safe_driver_call(
                lambda: self.driver.execute_script(_GENERIC_BLOCKING_OVERLAY_JS),
                timeout=HEALTH_CHECK_TIMEOUT,
            )
        except DriverUnhealthyError:
            return total
        except Exception as e:  # noqa: BLE001
            logger.debug(f"{self.log_prefix}[OVERLAY] generic script failed: {e}")
            detected = []

        if detected:
            for info in detected:
                logger.info(
                    f"{self.log_prefix}[OVERLAY] removed "
                    f"tag={info.get('tag')} id={info.get('id')!r} "
                    f"cls={info.get('cls')!r} z={info.get('z')} "
                    f"coverage={info.get('coverage')}"
                )
            total += len(detected)
        return total

    # ── safe interactions ──────────────────────────────────────────────────

    def safe_click(self, element) -> bool:
        """Click *element* with overlay-handling and JS fallback."""
        if not self._is_open():
            return False
        self.dismiss_blocking_overlays()

        try:
            safe_driver_call(
                lambda: self.driver.execute_script(
                    "arguments[0].scrollIntoView({block:'center'});", element
                ),
                timeout=HEALTH_CHECK_TIMEOUT,
            )
        except Exception:  # noqa: BLE001
            pass

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
        except Exception as e:  # noqa: BLE001
            logger.debug(f"{self.log_prefix}[CLICK] action chain failed: {e}")

        # Last resort: dismiss overlays once more, then JS .click().
        self.dismiss_blocking_overlays()
        try:
            safe_driver_call(
                lambda: self.driver.execute_script("arguments[0].click();", element),
                timeout=SAFE_COMMAND_TIMEOUT,
            )
            return True
        except DriverUnhealthyError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning(f"{self.log_prefix}[CLICK] all strategies failed: {e}")
            return False

    def safe_send_keys(self, element, text: str, *, clear_first: bool = True) -> bool:
        """Type *text* into *element* with JS-injection fallback."""
        if not self._is_open():
            return False
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
            except Exception:  # noqa: BLE001
                pass

        try:
            safe_driver_call(
                lambda: element.send_keys(text),
                timeout=SAFE_COMMAND_TIMEOUT,
            )
            return True
        except DriverUnhealthyError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.debug(f"{self.log_prefix}[TYPE] native failed: {e}")

        # JS-injection fallback — also runs overlay dismissal once more so a
        # late-appearing overlay cannot eat the focus event.
        self.dismiss_blocking_overlays()
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
        except Exception as e:  # noqa: BLE001
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

    On timeout, the *current* BrowserSession (returned by
    ``get_session_fn``) is force-closed. This kills its chrome.exe and
    chromedriver.exe processes, which makes any in-flight Selenium socket
    read inside the worker thread error out — the worker exits naturally
    a moment later. We give the worker up to 15 seconds to wind down
    before returning.
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
        except Exception:  # noqa: BLE001
            sess = None
        if sess is not None:
            try:
                sess.force_close()
            except Exception as e:  # noqa: BLE001
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
