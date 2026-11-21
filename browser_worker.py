"""
Browser worker module for Keep Me Alive service.

Visits URLs with a real browser and reports whether the request actually
reached the origin, which is what resets a PaaS sleep timer.

Two things decide whether a visit counts, and neither is the user-agent string:

* The request has to survive the WAF/CDN in front of the app. An edge challenge
  ("Just a moment...") is answered by the CDN, never reaches the origin, and so
  never wakes the service. Detecting that and reporting it honestly is more
  useful than reporting a successful visit that did nothing.
* The browser must not advertise itself as automation. Playwright's headless
  Chromium reports ``HeadlessChrome/<ver>`` and sets ``navigator.webdriver``;
  both are removed below.

A persistent profile per host keeps challenge cookies between visits, so an
edge challenge is normally solved once per container instead of every visit.

Browser engine is configurable (``browser_engine`` setting, default chromium):
chromium keeps the most common fingerprint, firefox is the genuinely-not-Chrome
option and needs no user-agent rewriting at all.
"""
import asyncio
import logging
import os
import re
import shutil
import tempfile
import threading
import time
from functools import lru_cache
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

# Try to import playwright, handle if not installed
try:
    from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False

from storage import add_visit_record, get_settings

logger = logging.getLogger(__name__)

PROFILE_ROOT = Path(__file__).parent / "data" / "browser_profiles"

DEFAULT_ENGINE = "chromium"
SUPPORTED_ENGINES = ("chromium", "firefox")
LOCALE = "en-US"

# Page titles / body text served by edge bot protection. If any of these are
# showing, the request was answered by the CDN and the origin never saw it.
CHALLENGE_TITLES = (
    "just a moment",
    "attention required",
    "checking your browser",
    "verify you are human",
    "ddos protection by",
    "security verification",
    "access denied",
    "you have been blocked",
)
CHALLENGE_TEXT = (
    "checking if the site connection is secure",
    "verify you are human",
    "enable javascript and cookies to continue",
    "needs to review the security of your connection",
    "please wait while we verify",
    "you have been blocked",
    "why have i been blocked",
)
CHALLENGE_STATUS = (403, 429, 503)

# Both engines can end up with navigator.webdriver = true after launch:
# Playwright's Chromium sets it via automation flags, its Firefox build reports
# it through Juggler (the `dom.webdriver.enabled` pref is ignored there).
_WEBDRIVER_SCRIPT = (
    "Object.defineProperty(navigator, 'webdriver', {get: () => false});"
)

# Cached once per process: the real user agent of the installed Chromium build.
_UA_CACHE = {}
_UA_LOCK = threading.Lock()
_UA_PROBE_LOCK = threading.Lock()
_UA_CACHE_FILE = PROFILE_ROOT / ".chromium-ua"
_UA_PATTERN = re.compile(r"^Mozilla/5\.0 .*Chrome/\d+\.")

# One lock per host so two visits never fight over the same profile directory.
_HOST_LOCKS = {}
_HOST_LOCKS_GUARD = threading.Lock()


def _profile_dir(url: str) -> Path:
    """
    Per-host profile directory, so challenge cookies are reused.

    The name is sanitised and then checked to stay inside PROFILE_ROOT: a host
    like ``..`` (from a URL such as ``http://../``) would otherwise resolve to
    the data directory and let the browser write its profile there.
    """
    host = (urlsplit(url).hostname or "").lower()
    safe = re.sub(r"[^a-z0-9.-]+", "_", host).strip(".-")[:60] or "unknown"
    candidate = (PROFILE_ROOT / safe).resolve()
    try:
        candidate.relative_to(PROFILE_ROOT.resolve())
    except ValueError:
        logger.warning("Refusing profile path outside the profile root for %r", url)
        return PROFILE_ROOT / "invalid-host"
    return candidate


def _host_lock(url: str) -> threading.Lock:
    host = (urlsplit(url).hostname or "unknown").lower()
    with _HOST_LOCKS_GUARD:
        return _HOST_LOCKS.setdefault(host, threading.Lock())


def _visit_config() -> dict:
    """Read visit settings, tolerating missing/older keys."""
    try:
        settings = get_settings()
    except Exception:
        settings = {}

    engine = str(settings.get("browser_engine") or DEFAULT_ENGINE).lower()
    if engine not in SUPPORTED_ENGINES:
        engine = DEFAULT_ENGINE

    try:
        wait_seconds = int(settings.get("post_load_wait_seconds", 5))
    except (TypeError, ValueError):
        wait_seconds = 5

    return {
        "engine": engine,
        "wait_seconds": max(0, min(30, wait_seconds)),
        "timezone": str(settings.get("browser_timezone") or "UTC"),
        "navigation_timeout_ms": 60000,
        "challenge_wait_seconds": 20,
    }


def _result(success: bool, response_time_ms: float, error: str = "",
            screenshot: Optional[bytes] = None, status_code: Optional[int] = None,
            final_url: str = "", title: str = "", blocked: bool = False,
            engine: str = "") -> dict:
    """Uniform visit result; richer than the old (success, ms, error, shot) tuple."""
    return {
        "success": success,
        "response_time_ms": response_time_ms,
        "error": error,
        "screenshot": screenshot,
        "status_code": status_code,
        "final_url": final_url,
        "title": title,
        "blocked": blocked,
        "engine": engine,
    }


class _Session:
    """A context plus whatever else has to be closed with it."""

    def __init__(self, context, browser=None, tmp_dir=None, label=""):
        self.context = context
        self.browser = browser
        self.tmp_dir = tmp_dir
        self.label = label

    async def close(self):
        try:
            await self.context.close()
        except Exception:
            pass
        if self.browser is not None:
            try:
                await self.browser.close()
            except Exception:
                pass
        if self.tmp_dir:
            shutil.rmtree(self.tmp_dir, ignore_errors=True)


async def _resolve_chromium_user_agent(p) -> Optional[str]:
    """
    Return the real Chrome user agent for the installed build.

    Playwright's Chromium reports ``HeadlessChrome/<version>``. Rewriting only
    that token keeps the platform, version and client hints consistent, which a
    hand-written UA string does not (the browser also sends sec-ch-ua with its
    true version and the true platform).

    The value is cached in memory and on disk, because the image only ships the
    full Chromium build (``--no-shell``) and launching a probe is not free.
    """
    with _UA_LOCK:
        cached = _UA_CACHE.get("chromium")
    if cached:
        return cached

    # Single flight: concurrent first visits would otherwise each launch a probe
    # and race on the cache file, and a half-written value would poison every
    # later visit in this process.
    with _UA_PROBE_LOCK:
        with _UA_LOCK:
            cached = _UA_CACHE.get("chromium")
        if cached:
            return cached

        raw = None
        for _attempt in range(2):
            try:
                browser = await p.chromium.launch(
                    headless=True, channel="chromium", args=["--no-sandbox"]
                )
                try:
                    page = await browser.new_page()
                    raw = await page.evaluate("navigator.userAgent")
                finally:
                    await browser.close()
                break
            except Exception as exc:
                logger.warning("Chromium UA probe failed: %s", exc)

        if raw:
            # HeadlessChrome/153.0.8010.12 -> Chrome/153.0.0.0 (Chrome's reduced form)
            ua = re.sub(r"HeadlessChrome/(\d+)[\d.]*", r"Chrome/\1.0.0.0", raw)
        else:
            ua = _read_cached_ua()
            if ua:
                logger.warning("Reusing cached Chromium user agent from %s", _UA_CACHE_FILE)

        if not ua or not _UA_PATTERN.match(ua):
            return None

        with _UA_LOCK:
            _UA_CACHE["chromium"] = ua
        _write_cached_ua(ua)
        return ua


def _read_cached_ua() -> Optional[str]:
    try:
        value = _UA_CACHE_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


def _write_cached_ua(ua: str):
    """Atomically replace the cached UA so a kill never leaves a partial value."""
    try:
        _UA_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _UA_CACHE_FILE.with_name(_UA_CACHE_FILE.name + ".tmp")
        tmp.write_text(ua, encoding="utf-8")
        os.replace(tmp, _UA_CACHE_FILE)
    except OSError:
        pass
    return ua


async def _harden(context):
    """Remove the automation flag from a context; never fatal on its own."""
    try:
        await context.add_init_script(_WEBDRIVER_SCRIPT)
    except Exception as exc:
        logger.warning("Could not apply webdriver init script: %s", exc)


async def _open_chromium(p, profile_dir: Path, tmp_dir: Optional[str],
                         user_agent: Optional[str], timezone: str) -> _Session:
    """
    Open a Chromium context from the full browser build.

    Only ``channel="chromium"`` is used: the stripped headless shell reports no
    plugins, no ``window.chrome`` and a ``HeadlessChrome`` user agent, so
    falling back to it would silently undo the point of this module.
    """
    common = {
        "headless": True,
        "channel": "chromium",
        "ignore_default_args": ["--enable-automation"],
        "args": [
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--disable-blink-features=AutomationControlled",
            "--no-first-run",
            "--no-default-browser-check",
        ],
        "viewport": {"width": 1440, "height": 900},
        "locale": LOCALE,
        "timezone_id": timezone,
        # A kept-alive page has no business writing files into the container.
        "accept_downloads": False,
    }
    if user_agent:
        common["user_agent"] = user_agent

    user_data_dir, tmp = (tmp_dir, tmp_dir) if tmp_dir else (str(profile_dir), None)
    try:
        context = await p.chromium.launch_persistent_context(user_data_dir=user_data_dir, **common)
    except Exception as exc:
        raise RuntimeError(
            "Chromium could not start (%s). If the browser build is missing, "
            "rebuild the image with `playwright install --no-shell chromium`." % exc
        ) from exc

    await _harden(context)
    return _Session(context, tmp_dir=tmp, label="chromium")


async def _open_firefox(p, profile_dir: Path, tmp_dir: Optional[str],
                        timezone: str) -> _Session:
    """Open a Firefox context. Its UA is already honest, so nothing is rewritten."""
    common = {
        "headless": True,
        "viewport": {"width": 1440, "height": 900},
        "locale": LOCALE,
        "timezone_id": timezone,
        "accept_downloads": False,
    }

    attempts = []
    if tmp_dir:
        attempts.append(("temp-profile", {"user_data_dir": tmp_dir}, tmp_dir))
    else:
        attempts.append(("persistent", {"user_data_dir": str(profile_dir)}, None))

    for label, targeted, tmp in attempts:
        try:
            context = await p.firefox.launch_persistent_context(**targeted, **common)
        except Exception as exc:
            logger.warning("Firefox %s failed: %s", label, exc)
            continue
        await _harden(context)
        return _Session(context, tmp_dir=tmp, label=f"firefox-{label}")

    browser = await p.firefox.launch(headless=True)
    try:
        context = await browser.new_context(**common)
    except Exception:
        await browser.close()
        raise
    await _harden(context)
    return _Session(context, browser=browser, tmp_dir=tmp_dir, label="firefox-fallback")


async def _interstitial_reason(page) -> Optional[str]:
    """
    Return a reason string if the current page is a bot-protection interstitial.

    Only page-level signals are checked here; a bare 4xx status is handled
    separately so a genuine app error does not look like a challenge.
    """
    try:
        title = (await page.title() or "").strip().lower()
    except Exception:
        title = ""
    for marker in CHALLENGE_TITLES:
        if marker in title:
            return "challenge page %r" % title[:60]

    try:
        has_cf_script = await page.evaluate(
            """!!document.querySelector('script[src*="/cdn-cgi/challenge-platform"]')"""
        )
    except Exception:
        has_cf_script = False
    if has_cf_script:
        return "Cloudflare challenge script loaded"

    try:
        text = await page.evaluate(
            "document.body ? document.body.innerText.slice(0, 1500) : ''"
        )
    except Exception:
        text = ""
    text = (text or "").lower()
    for marker in CHALLENGE_TEXT:
        if marker in text:
            return "challenge text %r" % marker

    return None


def _waf_signature(headers: dict) -> Optional[str]:
    """
    Name the CDN/WAF behind a response, if one is visible in the headers.

    Used to decide whether a bare 4xx/5xx is worth calling a block: without one
    of these it is more likely a genuine response from the app itself.
    """
    lowered = {str(k).lower(): str(v) for k, v in (headers or {}).items()}

    if "cf-mitigated" in lowered or "cf-ray" in lowered:
        return "Cloudflare edge"
    if "cloudflare" in lowered.get("server", "").lower():
        return "Cloudflare edge"
    if "fastly" in lowered.get("x-served-by", "").lower():
        return "Fastly edge"
    if "x-datadome" in lowered or "datadome" in lowered.get("x-datadome-request", ""):
        return "DataDome"
    if "x-akamai-transformed" in lowered or "akamai" in lowered.get("server", "").lower():
        return "Akamai"
    return None


async def _wait_for_challenge(page, timeout_seconds: int,
                              status_code: Optional[int],
                              headers: dict) -> Optional[str]:
    """
    Wait for an interstitial to clear, then report what is still blocking.

    Cloudflare cookies often make this resolve in one or two seconds, so the
    wait is bounded and stops as soon as the real page arrives. A bare error
    status only counts as a block when a CDN/WAF is visible in the headers, so
    an app's own 403 is not misreported as an edge challenge.
    """
    reason = await _interstitial_reason(page)

    if reason:
        logger.info("Challenge detected: %s — waiting up to %ss", reason, timeout_seconds)
        deadline = time.monotonic() + timeout_seconds
        while reason and time.monotonic() < deadline:
            await asyncio.sleep(1.5)
            reason = await _interstitial_reason(page)

    if reason:
        return reason

    mitigated = (headers or {}).get("cf-mitigated", "")
    if mitigated in ("challenge", "block"):
        return "Cloudflare cf-mitigated:%s" % mitigated

    if status_code in CHALLENGE_STATUS:
        edge = _waf_signature(headers)
        if edge:
            return "HTTP %s from the %s" % (status_code, edge)

    return None


def _reported_url(page_url: str, requested: str) -> str:
    """Ignore Chromium's internal error page when reporting where we landed."""
    if not page_url or page_url.startswith(("chrome-error://", "about:", "edge-error://")):
        return requested
    return page_url


async def _document_status(page, fallback: Optional[int]) -> Optional[int]:
    """
    Status of the *final* document.

    A challenge that resolves navigates again, so the first response status
    (403) is stale by the time the real page is showing.
    """
    try:
        status = await page.evaluate(
            "(performance.getEntriesByType('navigation')[0] || {}).responseStatus"
        )
        if isinstance(status, int) and status > 0:
            return status
    except Exception:
        pass
    return fallback


async def _visit_with_playwright(p, url: str, take_screenshot: bool) -> dict:
    """Visit one URL using an already-running Playwright instance."""
    config = _visit_config()
    engine = config["engine"]
    start_time = time.time()

    profile_dir = _profile_dir(url)
    lock = _host_lock(url)

    # Everything from here on runs inside try/finally: creating the temp profile
    # can itself fail (full or read-only /tmp) and must not strand the host lock.
    tmp_dir = None
    acquired = False
    session = None
    try:
        # If another visit already holds this host's profile, use a throwaway
        # one rather than failing the visit.
        acquired = lock.acquire(timeout=1.0)
        if acquired:
            try:
                profile_dir.mkdir(parents=True, exist_ok=True)
            except Exception as exc:
                logger.warning("Could not create profile dir %s: %s", profile_dir, exc)
                tmp_dir = tempfile.mkdtemp(prefix="kma-profile-")
        else:
            logger.info("Profile busy for %s, using a temporary profile", url)
            tmp_dir = tempfile.mkdtemp(prefix="kma-profile-")

        try:
            if engine == "firefox":
                session = await _open_firefox(p, profile_dir, tmp_dir, config["timezone"])
            else:
                ua = await _resolve_chromium_user_agent(p)
                if not ua:
                    raise RuntimeError(
                        "Could not determine Chromium's user agent; refusing to "
                        "visit with the default HeadlessChrome one."
                    )
                session = await _open_chromium(p, profile_dir, tmp_dir, ua, config["timezone"])
        except Exception as exc:
            message = str(exc)
            if "Executable doesn't exist" in message:
                message = (
                    "%s is not installed in this image. Rebuild with "
                    "--build-arg BROWSER_ENGINES=\"chromium firefox\", or switch "
                    "the browser engine back to chromium." % engine
                )
            response_time = (time.time() - start_time) * 1000
            _record(url, False, response_time, message, None, "", False)
            return _result(False, response_time, message, engine=engine)

        context = session.context
        page = await context.new_page()

        # Capture the main-frame response as it arrives. Chromium aborts the
        # navigation itself for error statuses (ERR_HTTP_RESPONSE_CODE_FAILURE),
        # so page.goto() raises and would otherwise hide the status code.
        last_response = {}

        def _on_response(response):
            try:
                if response.request.is_navigation_request() and response.frame == page.main_frame:
                    last_response["response"] = response
            except Exception:
                pass

        page.on("response", _on_response)

        navigation_error = ""
        try:
            response = await page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=config["navigation_timeout_ms"],
            )
        except PlaywrightTimeout:
            navigation_error = "Timeout: page did not respond in time"
            response = last_response.get("response")
        except Exception as exc:
            navigation_error = str(exc).splitlines()[0][:200]
            response = last_response.get("response")

        status_code = response.status if response is not None else None
        headers = dict(response.headers or {}) if response is not None else {}

        # Nothing came back at all: report it before trying to inspect a page
        # that never loaded.
        if status_code is None and navigation_error:
            response_time = (time.time() - start_time) * 1000
            final_url = _reported_url(page.url, url)
            _record(url, False, response_time, navigation_error, None, final_url, False)
            return _result(False, response_time, navigation_error,
                           final_url=final_url, engine=engine)

        block_reason = await _wait_for_challenge(
            page, config["challenge_wait_seconds"], status_code, headers
        )
        blocked = block_reason is not None

        # Settle only for pages that actually loaded; no reason to sleep after
        # a failed navigation.
        if config["wait_seconds"] and not navigation_error:
            await asyncio.sleep(config["wait_seconds"])

        status_code = await _document_status(page, status_code)
        final_url = _reported_url(page.url, url)
        try:
            title = "" if navigation_error else (await page.title() or "").strip()[:120]
        except Exception:
            title = ""

        screenshot = None
        if take_screenshot and not navigation_error:
            try:
                screenshot = await page.screenshot(type="png")
            except Exception as exc:
                logger.warning("Screenshot failed for %s: %s", url, exc)

        response_time = (time.time() - start_time) * 1000

        if blocked:
            message = "Blocked before the origin: " + block_reason
            success = False
        elif status_code is None:
            message = navigation_error or "No HTTP response from the page"
            success = False
        elif status_code >= 400:
            message = "HTTP %s" % status_code
            success = False
        else:
            message = ""
            success = True

        _record(url, success, response_time, message, status_code, final_url,
                blocked, title)

        logger.info(
            "Visit %s [%s] %s HTTP %s in %.0f ms%s",
            url, engine, "OK" if success else "FAILED", status_code,
            response_time, " (%s)" % message if message else "",
        )
        return _result(success, response_time, message, screenshot,
                       status_code=status_code, final_url=final_url,
                       title=title, blocked=blocked, engine=engine)

    except Exception as exc:
        response_time = (time.time() - start_time) * 1000
        message = str(exc).splitlines()[0][:200]
        _record(url, False, response_time, message, None, "", False)
        return _result(False, response_time, message, engine=engine)

    finally:
        if session is not None:
            await session.close()
        elif tmp_dir:
            # The browser never opened, so _Session never took ownership of it.
            shutil.rmtree(tmp_dir, ignore_errors=True)
        if acquired:
            lock.release()


def _record(url, success, response_time, message, status_code, final_url,
            blocked, title=""):
    """Write a visit record; storage problems must not fail the visit itself."""
    try:
        add_visit_record(
            url, success, response_time, message or "", "",
            status_code=status_code, final_url=final_url,
            title=title, blocked=blocked,
        )
    except Exception as exc:
        logger.error("Could not save visit record for %s: %s", url, exc)


async def visit_website(url: str, take_screenshot: bool = False) -> dict:
    """
    Visit a website with a real browser.

    Returns a result dict, including whether the origin was actually reached
    (``success``) or an edge/WAF challenge intervened (``blocked``).
    """
    if not PLAYWRIGHT_AVAILABLE:
        return _result(False, 0, "Playwright not installed")

    async with async_playwright() as p:
        return await _visit_with_playwright(p, url, take_screenshot)


def visit_website_sync(url: str, take_screenshot: bool = False) -> dict:
    """Synchronous wrapper for visit_website."""
    return asyncio.run(visit_website(url, take_screenshot))


async def visit_all_websites(websites: list) -> list:
    """Visit all enabled websites, reusing one Playwright driver for the batch."""
    settings = get_settings()
    take_screenshots = settings.get("screenshots_enabled", False)
    enabled = [site for site in websites if site.get("enabled", True)]

    results = []
    if not PLAYWRIGHT_AVAILABLE:
        for site in enabled:
            results.append({
                "url": site["url"],
                "name": site.get("name", site["url"]),
                **_result(False, 0, "Playwright not installed"),
            })
        return results

    async with async_playwright() as p:
        for site in enabled:
            result = await _visit_with_playwright(p, site["url"], take_screenshots)
            results.append({
                "url": site["url"],
                "name": site.get("name", site["url"]),
                **result,
            })

    return results


def visit_all_websites_sync(websites: list) -> list:
    """Synchronous wrapper for visit_all_websites."""
    return asyncio.run(visit_all_websites(websites))


@lru_cache(maxsize=1)
def available_engines() -> tuple:
    """
    Browser engines whose build is present in this image.

    Call this from sync code only (route handlers, templates): it uses
    Playwright's sync API, which cannot run inside the visit coroutine.
    """
    if not PLAYWRIGHT_AVAILABLE:
        return ()
    found = []
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            for name in SUPPORTED_ENGINES:
                try:
                    if os.path.exists(getattr(p, name).executable_path):
                        found.append(name)
                except Exception:
                    continue
    except Exception as exc:
        logger.warning("Could not detect installed browsers: %s", exc)
    return tuple(found) or ("chromium",)


def check_playwright_installed(engine: str = DEFAULT_ENGINE) -> bool:
    """Check if Playwright and the given browser engine are installed."""
    return engine in available_engines()
