"""
Storage module for Keep Me Alive service.
Uses GitHub Gist for persistence (cloud) with local fallback.
"""
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Optional
import threading

from gist_storage import (
    is_gist_configured, load_from_gist, save_to_gist, DEFAULT_DATA
)

# Local fallback path
DATA_DIR = Path(__file__).parent / "data"
DATA_FILE = DATA_DIR / "websites.json"

# Thread lock for operations
_data_lock = threading.Lock()

# In-memory cache
_cache: Optional[dict] = None
_cache_loaded = False


def _ensure_local_file():
    """Ensure local data directory and file exist."""
    DATA_DIR.mkdir(exist_ok=True)
    if not DATA_FILE.exists():
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_DATA, f, indent=2)


def _load_data() -> dict:
    """Load data from Gist (primary) or local file (fallback)."""
    global _cache, _cache_loaded
    
    with _data_lock:
        # Return cache if already loaded
        if _cache_loaded and _cache is not None:
            return _cache.copy()
        
        # Try Gist first
        if is_gist_configured():
            data = load_from_gist()
            if data:
                _cache = data
                _cache_loaded = True
                return data.copy()
        
        # Fallback to local file
        _ensure_local_file()
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                _cache = data
                _cache_loaded = True
                return data.copy()
        except Exception:
            _cache = DEFAULT_DATA.copy()
            _cache_loaded = True
            return DEFAULT_DATA.copy()


def _save_data(data: dict):
    """Save data to Gist (primary) and local file (backup)."""
    global _cache
    
    with _data_lock:
        _cache = data.copy()
        
        # Save to Gist if configured
        if is_gist_configured():
            save_to_gist(data)
        
        # Always save local backup
        _ensure_local_file()
        try:
            with open(DATA_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, default=str)
        except Exception as e:
            print(f"Error saving local file: {e}")


def refresh_cache():
    """Force reload data from Gist."""
    global _cache, _cache_loaded
    with _data_lock:
        _cache = None
        _cache_loaded = False
    _load_data()


# Website management functions
def get_websites() -> list:
    """Get all websites from storage."""
    data = _load_data()
    return data.get("websites", [])


def add_website(url: str, name: str = "") -> bool:
    """Add a website to the list. Returns True if added, False if already exists."""
    data = _load_data()
    
    # Check if URL already exists
    for site in data["websites"]:
        if site["url"].lower() == url.lower():
            return False
    
    # Add new website
    website = {
        "id": datetime.now().strftime("%Y%m%d%H%M%S%f"),
        "url": url,
        "name": name or url,
        "enabled": True,
        "added_at": datetime.now().isoformat()
    }
    data["websites"].append(website)
    _save_data(data)
    return True


def remove_website(website_id: str) -> bool:
    """Remove a website by ID. Returns True if removed."""
    data = _load_data()
    original_count = len(data["websites"])
    data["websites"] = [w for w in data["websites"] if w["id"] != website_id]
    
    if len(data["websites"]) < original_count:
        _save_data(data)
        return True
    return False


def toggle_website(website_id: str) -> bool:
    """Toggle a website's enabled status. Returns new status."""
    data = _load_data()
    for website in data["websites"]:
        if website["id"] == website_id:
            website["enabled"] = not website["enabled"]
            _save_data(data)
            return website["enabled"]
    return False


# Settings management functions
def get_settings() -> dict:
    """Get current settings."""
    data = _load_data()
    return data.get("settings", DEFAULT_DATA["settings"].copy())


def update_settings(interval_min: int = None, interval_max: int = None, 
                   screenshots_enabled: bool = None):
    """Update settings. Only updates provided values."""
    data = _load_data()
    settings = data.get("settings", DEFAULT_DATA["settings"].copy())
    
    if interval_min is not None:
        settings["interval_min"] = interval_min
    if interval_max is not None:
        settings["interval_max"] = interval_max
    if screenshots_enabled is not None:
        settings["screenshots_enabled"] = screenshots_enabled
    
    data["settings"] = settings
    _save_data(data)


# Visit history functions
def add_visit_record(url: str, success: bool, response_time: float = 0, 
                     error_message: str = "", screenshot_path: str = ""):
    """Add a visit record to history."""
    data = _load_data()
    
    record = {
        "url": url,
        "timestamp": datetime.now().isoformat(),
        "success": success,
        "response_time_ms": round(response_time, 2),
        "error_message": error_message,
        "screenshot_path": screenshot_path
    }
    
    # Keep only last 50 records to save Gist space
    history = data.get("visit_history", [])
    history.insert(0, record)
    data["visit_history"] = history[:50]
    
    _save_data(data)


def get_visit_history(limit: int = 50) -> list:
    """Get recent visit history."""
    data = _load_data()
    history = data.get("visit_history", [])
    return history[:limit]


def clear_visit_history():
    """Clear all visit history."""
    data = _load_data()
    data["visit_history"] = []
    _save_data(data)
