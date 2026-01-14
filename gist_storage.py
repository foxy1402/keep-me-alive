"""
GitHub Gist storage module for Keep Me Alive service.
Provides persistent storage using GitHub Gist API.
"""
import os
import json
import requests
from datetime import datetime
from typing import Optional
import threading

# Environment variables
GIST_TOKEN = os.environ.get("GIST_TOKEN", "")
GIST_ID = os.environ.get("GIST_ID", "")
GIST_FILENAME = "keepmealive_data.json"

# API endpoint
GIST_API_URL = "https://api.github.com/gists"

# Thread lock for API operations
_api_lock = threading.Lock()

# Default data structure
DEFAULT_DATA = {
    "websites": [],
    "settings": {
        "interval_min": 10,
        "interval_max": 14,
        "screenshots_enabled": False
    },
    "visit_history": []
}


def _get_headers() -> dict:
    """Get API headers with authentication."""
    return {
        "Authorization": f"token {GIST_TOKEN}",
        "Accept": "application/vnd.github.v3+json"
    }


def is_gist_configured() -> bool:
    """Check if Gist storage is properly configured."""
    return bool(GIST_TOKEN and GIST_ID)


def load_from_gist() -> dict:
    """Load data from GitHub Gist."""
    if not is_gist_configured():
        return DEFAULT_DATA.copy()
    
    try:
        with _api_lock:
            response = requests.get(
                f"{GIST_API_URL}/{GIST_ID}",
                headers=_get_headers(),
                timeout=10
            )
            
            if response.status_code == 200:
                gist_data = response.json()
                files = gist_data.get("files", {})
                
                if GIST_FILENAME in files:
                    content = files[GIST_FILENAME].get("content", "{}")
                    data = json.loads(content)
                    # Ensure all required keys exist
                    for key in DEFAULT_DATA:
                        if key not in data:
                            data[key] = DEFAULT_DATA[key]
                    return data
            
            return DEFAULT_DATA.copy()
            
    except Exception as e:
        print(f"Error loading from Gist: {e}")
        return DEFAULT_DATA.copy()


def save_to_gist(data: dict) -> bool:
    """Save data to GitHub Gist."""
    if not is_gist_configured():
        return False
    
    try:
        with _api_lock:
            payload = {
                "files": {
                    GIST_FILENAME: {
                        "content": json.dumps(data, indent=2, default=str)
                    }
                }
            }
            
            response = requests.patch(
                f"{GIST_API_URL}/{GIST_ID}",
                headers=_get_headers(),
                json=payload,
                timeout=10
            )
            
            return response.status_code == 200
            
    except Exception as e:
        print(f"Error saving to Gist: {e}")
        return False


def create_gist() -> Optional[str]:
    """Create a new Gist and return its ID. Requires only GIST_TOKEN."""
    if not GIST_TOKEN:
        return None
    
    try:
        payload = {
            "description": "Keep Me Alive - Website Data",
            "public": False,
            "files": {
                GIST_FILENAME: {
                    "content": json.dumps(DEFAULT_DATA, indent=2)
                }
            }
        }
        
        response = requests.post(
            GIST_API_URL,
            headers=_get_headers(),
            json=payload,
            timeout=10
        )
        
        if response.status_code == 201:
            return response.json().get("id")
        
        return None
        
    except Exception as e:
        print(f"Error creating Gist: {e}")
        return None
