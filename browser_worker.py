"""
Browser worker module for Keep Me Alive service.
Uses Playwright to visit websites with a real browser.
Works on Linux (Streamlit Cloud, Render.com).
"""
import asyncio
import time
from datetime import datetime
from pathlib import Path
from typing import Tuple

# Try to import playwright, handle if not installed
try:
    from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False

from storage import add_visit_record, get_settings

# Screenshots directory
SCREENSHOTS_DIR = Path(__file__).parent / "data" / "screenshots"


async def visit_website(url: str, take_screenshot: bool = False) -> Tuple[bool, float, str, str]:
    """
    Visit a website using Playwright browser.
    
    Args:
        url: The URL to visit
        take_screenshot: Whether to capture a screenshot
    
    Returns:
        Tuple of (success, response_time_ms, error_message, screenshot_path)
    """
    if not PLAYWRIGHT_AVAILABLE:
        return False, 0, "Playwright not installed. Run: playwright install chromium", ""
    
    start_time = time.time()
    screenshot_path = ""
    
    try:
        async with async_playwright() as p:
            # Launch browser in headless mode
            browser = await p.chromium.launch(
                headless=True,
                args=['--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu']
            )
            
            # Create a new context with realistic viewport
            context = await browser.new_context(
                viewport={'width': 1920, 'height': 1080},
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
            )
            
            page = await context.new_page()
            
            # Navigate to URL with timeout
            await page.goto(url, wait_until='domcontentloaded', timeout=30000)
            
            # Wait a bit to simulate real browsing
            await asyncio.sleep(2)
            
            response_time = (time.time() - start_time) * 1000  # Convert to ms
            
            # Take screenshot if enabled
            if take_screenshot:
                SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                safe_url = url.replace("https://", "").replace("http://", "").replace("/", "_")[:50]
                screenshot_path = str(SCREENSHOTS_DIR / f"{safe_url}_{timestamp}.png")
                await page.screenshot(path=screenshot_path)
            
            await browser.close()
            
            # Record successful visit
            add_visit_record(url, True, response_time, "", screenshot_path)
            
            return True, response_time, "", screenshot_path
            
    except PlaywrightTimeout:
        response_time = (time.time() - start_time) * 1000
        error_msg = "Timeout: Page took too long to load"
        add_visit_record(url, False, response_time, error_msg)
        return False, response_time, error_msg, ""
        
    except Exception as e:
        response_time = (time.time() - start_time) * 1000
        error_msg = str(e)
        add_visit_record(url, False, response_time, error_msg)
        return False, response_time, error_msg, ""


def visit_website_sync(url: str, take_screenshot: bool = False) -> Tuple[bool, float, str, str]:
    """Synchronous wrapper for visit_website."""
    return asyncio.run(visit_website(url, take_screenshot))


async def visit_all_websites(websites: list) -> list:
    """Visit all enabled websites."""
    settings = get_settings()
    take_screenshots = settings.get("screenshots_enabled", False)
    
    results = []
    for site in websites:
        if site.get("enabled", True):
            success, response_time, error, screenshot = await visit_website(
                site["url"], 
                take_screenshots
            )
            results.append({
                "url": site["url"],
                "name": site.get("name", site["url"]),
                "success": success,
                "response_time_ms": response_time,
                "error": error,
                "screenshot": screenshot
            })
    
    return results


def visit_all_websites_sync(websites: list) -> list:
    """Synchronous wrapper for visit_all_websites."""
    return asyncio.run(visit_all_websites(websites))


def check_playwright_installed() -> bool:
    """Check if Playwright and browser are installed."""
    if not PLAYWRIGHT_AVAILABLE:
        return False
    
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            browser.close()
        return True
    except Exception:
        return False
