"""
Keep Me Alive - service to keep free-tier hosted apps awake.

A small FastAPI app with server-rendered pages: no frontend build step and no
Streamlit runtime. Browser work lives in browser_worker.py, the schedule in
scheduler.py, persistence in storage.py.

Routes
    GET  /                      dashboard (or login page)
    POST /login, /logout        cookie session
    POST /sites                 add
    POST /sites/{id}/toggle     enable / pause
    POST /sites/{id}/delete     remove
    POST /sites/{id}/visit      visit one site now
    POST /visit-all             visit every enabled site now
    POST /settings              interval, engine, wait, timezone, screenshots
    GET  /shots/{name}          visit screenshots
    GET  /healthz               container health check
"""
import hashlib
import hmac
import logging
import os
import re
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import quote_plus, urlsplit

from fastapi import FastAPI, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from storage import (
    get_websites, add_website, remove_website, toggle_website,
    get_settings, update_settings, get_visit_history, clear_visit_history,
    refresh_cache,
)
from scheduler import (
    start_scheduler, stop_scheduler, get_scheduler_status, get_last_run, record_run,
)
from browser_worker import (
    visit_website_sync, visit_all_websites_sync, available_engines,
)
from gist_storage import is_gist_configured

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent
SHOT_DIR = BASE_DIR / "data" / "screenshots"
SHOT_KEEP = 60

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
SESSION_COOKIE = "kma_session"
SESSION_MAX_AGE = 7 * 24 * 3600   # seconds
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "").lower() in ("1", "true", "yes")

# Deriving from the password means sessions survive a restart, and changing the
# password invalidates every existing session.
_SESSION_SECRET = os.environ.get("SESSION_SECRET") or hashlib.sha256(
    ("keepmealive:" + (ADMIN_PASSWORD or "open")).encode()
).hexdigest()

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# One browser run at a time: a visit spawns a full Chromium, and stacking them
# exhausts the server's thread pool and memory.
_visit_lock = threading.Lock()

# Failed-login tracking, per client address.
LOGIN_WINDOW = 600
LOGIN_MAX_FAILURES = 8
_login_failures = {}
_login_lock = threading.Lock()


# ---------------------------------------------------------------- auth ------

def _sign(issued: str) -> str:
    return hmac.new(_SESSION_SECRET.encode(), issued.encode(), hashlib.sha256).hexdigest()[:32]


def _make_token() -> str:
    issued = str(int(time.time()))
    return f"{issued}.{_sign(issued)}"


def _token_valid(token: Optional[str]) -> bool:
    if not ADMIN_PASSWORD:
        return True          # no password configured: open access, as before
    if not token or "." not in token:
        return False
    issued, signature = token.rsplit(".", 1)
    if not hmac.compare_digest(signature, _sign(issued)):
        return False
    try:
        return int(issued) > time.time() - SESSION_MAX_AGE
    except ValueError:
        return False


def _logged_in(request: Request) -> bool:
    return _token_valid(request.cookies.get(SESSION_COOKIE))


def _password_matches(supplied: str) -> bool:
    """
    Constant-time password check that does not blow up on non-ASCII input.

    hmac.compare_digest raises TypeError for non-ASCII str, so compare bytes.
    """
    try:
        return hmac.compare_digest(
            supplied.encode("utf-8", "surrogatepass"),
            ADMIN_PASSWORD.encode("utf-8", "surrogatepass"),
        )
    except (TypeError, ValueError):
        return False


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _login_blocked_for(ip: str) -> int:
    """Seconds this client must wait, or 0 if it may try again."""
    now = time.time()
    with _login_lock:
        stamps = [t for t in _login_failures.get(ip, []) if now - t < LOGIN_WINDOW]
        if stamps:
            _login_failures[ip] = stamps
        else:
            _login_failures.pop(ip, None)
        if len(stamps) >= LOGIN_MAX_FAILURES:
            return max(1, int(LOGIN_WINDOW - (now - stamps[0])))
    return 0


def _record_login_failure(ip: str):
    with _login_lock:
        _login_failures.setdefault(ip, []).append(time.time())
        if len(_login_failures) > 512:
            # Keep the map bounded; the entries are only used for throttling.
            for key in list(_login_failures):
                if not _login_failures.get(key):
                    _login_failures.pop(key, None)


def _clear_login_failures(ip: str):
    with _login_lock:
        _login_failures.pop(ip, None)


def _guard(request: Request) -> Optional[RedirectResponse]:
    if _logged_in(request):
        return None
    return RedirectResponse("/login", status_code=303)


def _set_session(response, value: str, max_age: int):
    response.set_cookie(
        SESSION_COOKIE, value,
        max_age=max_age, httponly=True, samesite="lax",
        secure=COOKIE_SECURE, path="/",
    )
    return response


# ------------------------------------------------------------ screenshots ---

def _prune_shots():
    try:
        files = sorted(SHOT_DIR.glob("*.png"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return
    for stale in files[SHOT_KEEP:]:
        try:
            stale.unlink()
        except OSError:
            pass


def _save_screenshot(site_id, data: bytes) -> Optional[str]:
    """Persist a PNG and return the name it can be served under."""
    if not data:
        return None
    try:
        SHOT_DIR.mkdir(parents=True, exist_ok=True)
        safe_id = re.sub(r"[^A-Za-z0-9]", "", str(site_id))[:24] or "site"
        name = f"{safe_id}-{int(time.time())}.png"
        (SHOT_DIR / name).write_bytes(data)
        _prune_shots()
        return name
    except OSError:
        return None


# ---------------------------------------------------------------- visits ----

def _attach_screenshot(result: dict, site_id) -> dict:
    """Move screenshot bytes to disk so the template can link to them."""
    data = result.pop("screenshot", None)
    name = _save_screenshot(site_id, data) if data else None
    result["screenshot_url"] = f"/shots/{name}" if name else None
    return result


def _visit_one(site: dict) -> dict:
    screenshots = bool(get_settings().get("screenshots_enabled", False))
    result = visit_website_sync(site["url"], screenshots)
    result["name"] = site.get("name", site["url"])
    return _attach_screenshot(result, site.get("id", "site"))


def _visit_all() -> list:
    sites = [s for s in get_websites() if s.get("enabled", True)]
    if not sites:
        return []
    results = visit_all_websites_sync(sites)
    by_url = {s["url"]: s for s in sites}
    for result in results:
        site = by_url.get(result["url"], {})
        _attach_screenshot(result, site.get("id", "site"))
    return results


def _remember_run(results: list):
    record_run(results)


def _run_guarded(fn):
    """
    Run one browser job, refusing to stack a second one.

    Returns None when another run already holds the slot.
    """
    if not _visit_lock.acquire(blocking=False):
        return None
    try:
        return fn()
    finally:
        _visit_lock.release()


# ------------------------------------------------------------- rendering ----

def _success_rate(history: list):
    cutoff = datetime.now() - timedelta(hours=24)
    recent = []
    for record in history:
        try:
            if datetime.fromisoformat(record.get("timestamp", "")) > cutoff:
                recent.append(record)
        except (ValueError, TypeError):
            continue
    if not recent:
        return "--", 0
    ok = sum(1 for record in recent if record.get("success"))
    return f"{ok / len(recent) * 100:.0f}%", len(recent)


def _next_run_text(status: dict) -> str:
    if not status.get("running"):
        return "stopped"
    if status.get("next_run"):
        try:
            delta = datetime.fromisoformat(status["next_run"]) - datetime.now()
            return f"{max(0, delta.total_seconds() / 60):.0f} min"
        except ValueError:
            return "soon"
    return "soon"


def _dashboard(request: Request, msg: str = "") -> HTMLResponse:
    settings = get_settings()
    sites = get_websites()
    history = get_visit_history(50)
    status = get_scheduler_status()
    rate, checks = _success_rate(history)
    engines = available_engines()

    # A stored engine that this image does not ship would fail every visit, and
    # the select box cannot show it, so say so instead of silently rewriting it.
    engine_warning = ""
    configured_engine = str(settings.get("browser_engine") or "chromium")
    if configured_engine not in engines:
        engine_warning = (
            "%s is selected but not installed in this image (%s available). "
            "Visits will fail until you pick an installed engine or rebuild with "
            "--build-arg BROWSER_ENGINES=\"chromium firefox\"."
            % (configured_engine, ", ".join(engines) or "none")
        )

    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "sites": sites,
            "total": len(sites),
            "active": sum(1 for s in sites if s.get("enabled", True)),
            "rate": rate,
            "checks": checks,
            "next_run": _next_run_text(status),
            "running": bool(status.get("running")),
            "visiting": bool(status.get("is_visiting")) or _visit_lock.locked(),
            "history": history,
            "settings": settings,
            "engines": engines,
            "engine_warning": engine_warning,
            "gist": is_gist_configured(),
            "last_run": get_last_run(),
            "msg": msg,
            "has_password": bool(ADMIN_PASSWORD),
        },
    )


# ------------------------------------------------------------------ app -----

@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Starting the scheduler reads settings, which can block on a Gist request
    # (10s timeout). Do it off the startup path so /healthz answers immediately.
    threading.Thread(
        target=start_scheduler, name="kma-scheduler-start", daemon=True
    ).start()
    yield
    stop_scheduler()


app = FastAPI(title="Keep Me Alive", lifespan=lifespan, docs_url=None, redoc_url=None)


@app.middleware("http")
async def same_origin_guard(request: Request, call_next):
    """
    Reject state-changing requests started by another origin.

    SameSite=Lax already withholds the session cookie on cross-site POSTs. This
    additionally covers another service on the same hostname (any port), which
    same-site cookies do not distinguish.
    """
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin")
        host = request.headers.get("host", "")
        if origin and host:
            origin_host = urlsplit(origin).netloc
            if origin_host and origin_host.lower() != host.lower():
                logger.warning(
                    "Refused cross-origin %s from %s (host %s)",
                    request.method, origin, host,
                )
                return HTMLResponse("Cross-origin request refused", status_code=403)
    return await call_next(request)


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
def index(request: Request, msg: str = ""):
    if not _logged_in(request):
        return RedirectResponse("/login", status_code=303)
    return _dashboard(request, msg)


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request, error: str = ""):
    if _logged_in(request):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        request, "login.html",
        {"error": error, "has_password": bool(ADMIN_PASSWORD)},
    )


@app.post("/login")
def login(request: Request, password: str = Form("")):
    ip = _client_ip(request)

    # A throttled client only gets past this with the correct password, so the
    # admin cannot be locked out of their own tool (for example by an attacker
    # sharing a proxy's client address), while guessing stays throttled.
    wait = _login_blocked_for(ip)
    if wait and not (ADMIN_PASSWORD and _password_matches(password)):
        logger.warning("Login throttled for %s (%ss left)", ip, wait)
        return RedirectResponse(
            f"/login?error=Too+many+attempts.+Try+again+in+{wait}+seconds",
            status_code=303,
        )

    if not ADMIN_PASSWORD or _password_matches(password):
        _clear_login_failures(ip)
        response = RedirectResponse("/", status_code=303)
        return _set_session(response, _make_token(), SESSION_MAX_AGE)

    _record_login_failure(ip)
    logger.warning("Failed admin login from %s", ip)
    return RedirectResponse("/login?error=Wrong+password", status_code=303)


@app.post("/logout")
def logout():
    response = RedirectResponse("/login", status_code=303)
    return _set_session(response, "", 0)


@app.post("/sites")
def create_site(request: Request, url: str = Form(""), name: str = Form("")):
    if (redirect := _guard(request)):
        return redirect
    url = url.strip()
    if not url:
        return RedirectResponse("/?msg=URL+is+required", status_code=303)
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    added = add_website(url, name.strip())
    message = "Site added" if added else "That URL is already in the list"
    return RedirectResponse(f"/?msg={quote_plus(message)}", status_code=303)


@app.post("/sites/{site_id}/toggle")
def toggle(request: Request, site_id: str):
    if (redirect := _guard(request)):
        return redirect
    toggle_website(site_id)
    return RedirectResponse("/?msg=Updated", status_code=303)


@app.post("/sites/{site_id}/delete")
def delete(request: Request, site_id: str):
    if (redirect := _guard(request)):
        return redirect
    remove_website(site_id)
    return RedirectResponse("/?msg=Site+removed", status_code=303)


@app.post("/sites/{site_id}/visit")
def visit_one(request: Request, site_id: str):
    if (redirect := _guard(request)):
        return redirect
    site = next((s for s in get_websites() if s["id"] == site_id), None)
    if site is None:
        return RedirectResponse("/?msg=Site+not+found", status_code=303)

    results = _run_guarded(lambda: [_visit_one(site)])
    if results is None:
        return RedirectResponse("/?msg=A+visit+is+already+running", status_code=303)
    _remember_run(results)
    return RedirectResponse("/?msg=Visit+finished", status_code=303)


@app.post("/visit-all")
def visit_everything(request: Request):
    if (redirect := _guard(request)):
        return redirect

    results = _run_guarded(_visit_all)
    if results is None:
        return RedirectResponse("/?msg=A+visit+is+already+running", status_code=303)
    _remember_run(results)
    return RedirectResponse("/?msg=Visit+finished", status_code=303)


@app.post("/settings")
def save_settings(
    request: Request,
    interval_min: int = Form(10),
    interval_max: int = Form(14),
    browser_engine: str = Form("chromium"),
    post_load_wait_seconds: int = Form(5),
    browser_timezone: str = Form("UTC"),
    screenshots_enabled: str = Form(""),
):
    if (redirect := _guard(request)):
        return redirect
    if interval_min > interval_max:
        return RedirectResponse("/?msg=Min+interval+must+not+exceed+Max", status_code=303)

    update_settings(
        interval_min=max(1, min(60, interval_min)),
        interval_max=max(1, min(60, interval_max)),
        screenshots_enabled=bool(screenshots_enabled),
        browser_engine=browser_engine if browser_engine in available_engines() else "chromium",
        post_load_wait_seconds=max(0, min(30, post_load_wait_seconds)),
        browser_timezone=(browser_timezone.strip() or "UTC"),
    )
    return RedirectResponse("/?msg=Settings+saved", status_code=303)


@app.post("/history/clear")
def clear_history(request: Request):
    if (redirect := _guard(request)):
        return redirect
    clear_visit_history()
    return RedirectResponse("/?msg=History+cleared", status_code=303)


@app.post("/sync")
def sync(request: Request):
    if (redirect := _guard(request)):
        return redirect
    refresh_cache()
    return RedirectResponse("/?msg=Reloaded+from+Gist", status_code=303)


@app.post("/scheduler/start")
def scheduler_start(request: Request):
    if (redirect := _guard(request)):
        return redirect
    start_scheduler()
    return RedirectResponse("/?msg=Scheduler+started", status_code=303)


@app.post("/scheduler/stop")
def scheduler_stop(request: Request):
    if (redirect := _guard(request)):
        return redirect
    stop_scheduler()
    return RedirectResponse("/?msg=Scheduler+stopped", status_code=303)


@app.get("/shots/{name}")
def shot(request: Request, name: str):
    if (redirect := _guard(request)):
        return redirect
    # Confine to the screenshot directory and to files this app could create.
    if not re.fullmatch(r"[A-Za-z0-9_-]+\.png", name):
        return HTMLResponse("Not found", status_code=404)
    path = SHOT_DIR / name
    if not path.is_file():
        return HTMLResponse("Not found", status_code=404)
    return FileResponse(path, media_type="image/png")
