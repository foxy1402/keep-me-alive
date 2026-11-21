"""
Storage module for Keep Me Alive service.

Sites and settings are synced through the Gist; visit history is deliberately
local-only. History changes on every visit, and syncing it would create one
Gist revision per visit (thousands of revisions per month). It is pruned to the
last HISTORY_MAX_AGE_DAYS days / HISTORY_MAX_ENTRIES entries instead.

Two invariants matter here:

* Every mutation is a read-modify-write of one document, so mutations run under
  a single re-entrant lock. Otherwise a settings save can drop a visit record
  written between another thread's load and save, or resurrect a deleted site.
* A document that could not be loaded is never written back. If the Gist is
  unreachable the app runs from the local file and only resumes syncing once a
  Gist read has succeeded again, so a GitHub outage cannot overwrite real data
  with defaults.
"""
import copy
import json
import logging
import os
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

from gist_storage import (
    is_gist_configured, load_from_gist, save_to_gist, DEFAULT_DATA
)

logger = logging.getLogger(__name__)

# Local fallback path
DATA_DIR = Path(__file__).parent / "data"
DATA_FILE = DATA_DIR / "websites.json"

# Visit history is local-only, and bounded: max age and max entries.
HISTORY_MAX_AGE_DAYS = 3
HISTORY_MAX_ENTRIES = 100

# Re-entrant so _mutate() can hold it across load and save.
_data_lock = threading.RLock()

# Whether the last Gist read succeeded. While it is False we refuse to write to
# the Gist, because we do not have a trustworthy picture of its contents.
_gist_readable = False

# In-memory cache
_cache: Optional[dict] = None
_cache_loaded = False


def _ensure_local_file():
    """Ensure local data directory and file exist."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not DATA_FILE.exists():
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_DATA, f, indent=2)


def _atomic_write(path: Path, text: str):
    """Write via a temp file + rename so a kill mid-write cannot truncate data."""
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _read_local() -> Optional[dict]:
    """Read the local file. Returns None if it is missing or unreadable."""
    _ensure_local_file()
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        # Keep the damaged file for inspection instead of silently replacing it.
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        broken = DATA_FILE.with_name(f"websites.json.corrupt-{stamp}")
        try:
            os.replace(DATA_FILE, broken)
            logger.error("Local data file was corrupt (%s); moved to %s", e, broken.name)
        except OSError:
            logger.error("Local data file was corrupt (%s) and could not be moved", e)
        return None
    except OSError as e:
        logger.error("Could not read local data file: %s", e)
        return None


def _load_data() -> dict:
    """Load data from Gist (primary) or local file (fallback)."""
    global _cache, _cache_loaded, _gist_readable

    with _data_lock:
        if _cache_loaded and _cache is not None:
            return copy.deepcopy(_cache)

        if is_gist_configured():
            data = load_from_gist()
            if data:
                _gist_readable = True
                # Migrate older Gists that still carry a visit history: logs are
                # local now, and leaving the old log in the file only bloats it.
                if data.get("visit_history"):
                    logger.info(
                        "Removing visit history from Gist (%d entries); logs are local-only",
                        len(data["visit_history"]),
                    )
                    data["visit_history"] = []
                    if not save_to_gist(data):
                        logger.warning("Could not update Gist during history migration")
                # The local file is authoritative for history, which the Gist no
                # longer stores. Gist wins for sites and settings.
                local = _read_local() or {}
                data["visit_history"] = _cleanup_old_history(
                    local.get("visit_history", [])
                )[:HISTORY_MAX_ENTRIES]
                _cache = data
                _cache_loaded = True
                return copy.deepcopy(data)
            # Gist unreachable: fall back to the local copy and remember that we
            # must not write to the Gist until a read succeeds.
            _gist_readable = False
            logger.warning("Gist unreachable; using local data and pausing Gist writes")
        else:
            _gist_readable = False

        data = _read_local()
        if data is None:
            data = copy.deepcopy(DEFAULT_DATA)
        _cache = data
        _cache_loaded = True
        return copy.deepcopy(data)


def _save_data(data: dict, sync_gist: bool = True):
    """Save data locally, and to the Gist when the change belongs there."""
    global _cache

    with _data_lock:
        _cache = copy.deepcopy(data)

        if sync_gist and is_gist_configured():
            if _gist_readable:
                # Sites and settings are synced; visit history never leaves this
                # machine (one Gist revision per visit otherwise).
                gist_doc = {k: v for k, v in data.items() if k != "visit_history"}
                gist_doc["visit_history"] = []
                if not save_to_gist(gist_doc):
                    logger.warning("Could not save to Gist; local copy is up to date")
            else:
                logger.warning("Skipping Gist write: last Gist read failed")

        try:
            _ensure_local_file()
            _atomic_write(DATA_FILE, json.dumps(data, indent=2, default=str))
        except Exception as e:
            logger.error("Error saving local file: %s", e)


def _mutate(fn: Callable[[dict], object], sync_gist: bool = True):
    """Apply fn to the current document and save it, holding the lock throughout."""
    with _data_lock:
        data = _load_data()
        result = fn(data)
        _save_data(data, sync_gist=sync_gist)
        return result


def refresh_cache():
    """Force reload data from Gist."""
    global _cache, _cache_loaded, _gist_readable
    with _data_lock:
        _cache = None
        _cache_loaded = False
        _gist_readable = False
    _load_data()


# Website management functions
def get_websites() -> list:
    """Get all websites from storage."""
    data = _load_data()
    return data.get("websites", [])


def add_website(url: str, name: str = "") -> bool:
    """Add a website to the list. Returns True if added, False if already exists."""

    def apply(data: dict) -> bool:
        for site in data["websites"]:
            if site["url"].lower() == url.lower():
                return False
        data["websites"].append({
            "id": datetime.now().strftime("%Y%m%d%H%M%S%f"),
            "url": url,
            "name": name or url,
            "enabled": True,
            "added_at": datetime.now().isoformat(),
        })
        return True

    return _mutate(apply)


def remove_website(website_id: str) -> bool:
    """Remove a website by ID. Returns True if removed."""

    def apply(data: dict) -> bool:
        before = len(data["websites"])
        data["websites"] = [w for w in data["websites"] if w["id"] != website_id]
        return len(data["websites"]) < before

    return _mutate(apply)


def toggle_website(website_id: str) -> bool:
    """Toggle a website's enabled status. Returns new status."""

    def apply(data: dict) -> bool:
        for site in data["websites"]:
            if site["id"] == website_id:
                site["enabled"] = not site["enabled"]
                return site["enabled"]
        return False

    return _mutate(apply)


# Settings management functions
def get_settings() -> dict:
    """Get current settings, filled in with defaults for keys an older store lacks."""
    data = _load_data()
    settings = DEFAULT_DATA["settings"].copy()
    settings.update(data.get("settings", {}))
    return settings


def update_settings(interval_min: int = None, interval_max: int = None, 
                   screenshots_enabled: bool = None, browser_engine: str = None,
                   post_load_wait_seconds: int = None,
                   browser_timezone: str = None):
    """Update settings. Only updates provided values."""
    def apply(data: dict):
        settings = DEFAULT_DATA["settings"].copy()
        settings.update(data.get("settings", {}))

        if interval_min is not None:
            settings["interval_min"] = interval_min
        if interval_max is not None:
            settings["interval_max"] = interval_max
        if screenshots_enabled is not None:
            settings["screenshots_enabled"] = screenshots_enabled
        if browser_engine is not None:
            settings["browser_engine"] = browser_engine
        if post_load_wait_seconds is not None:
            settings["post_load_wait_seconds"] = post_load_wait_seconds
        if browser_timezone is not None:
            settings["browser_timezone"] = browser_timezone

        data["settings"] = settings

    _mutate(apply)


# Visit history functions


def _cleanup_old_history(history: list) -> list:
    """Remove history entries older than HISTORY_MAX_AGE_DAYS."""
    if not history:
        return []
    
    cutoff = datetime.now() - timedelta(days=HISTORY_MAX_AGE_DAYS)
    cleaned = []
    
    for record in history:
        try:
            timestamp = datetime.fromisoformat(record.get("timestamp", ""))
            if timestamp > cutoff:
                cleaned.append(record)
        except (ValueError, TypeError):
            # Keep records with invalid timestamps (shouldn't happen)
            cleaned.append(record)
    
    return cleaned


def add_visit_record(url: str, success: bool, response_time: float = 0, 
                     error_message: str = "", screenshot_path: str = "",
                     status_code: Optional[int] = None, final_url: str = "",
                     title: str = "", blocked: bool = False):
    """Add a visit record to local history and auto-cleanup old entries.

    Deliberately does not sync to the Gist: this runs on every visit and would
    otherwise create a Gist revision per visit.
    """
    def apply(data: dict):
        record = {
            "url": url,
            "timestamp": datetime.now().isoformat(),
            "success": success,
            "response_time_ms": round(response_time, 2),
            "error_message": error_message,
            # Diagnostics: whether the origin (not just the CDN edge) answered.
            "status_code": status_code,
            "final_url": final_url or url,
            "title": title,
            "blocked": blocked,
        }

        history = _cleanup_old_history(data.get("visit_history", []))
        history.insert(0, record)
        data["visit_history"] = history[:HISTORY_MAX_ENTRIES]

    _mutate(apply, sync_gist=False)


def get_visit_history(limit: int = 50) -> list:
    """Get recent visit history."""
    data = _load_data()
    history = data.get("visit_history", [])
    return history[:limit]


def clear_visit_history():
    """Clear all visit history (local only)."""
    _mutate(lambda data: data.__setitem__("visit_history", []), sync_gist=False)
