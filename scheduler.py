"""
Scheduler module for Keep Me Alive service.
Uses APScheduler for background cron-like job scheduling.
"""
import random
import threading
import logging
from datetime import datetime, timedelta
from typing import Callable, Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from storage import get_settings, get_websites
from browser_worker import visit_all_websites_sync

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Global scheduler instance
_scheduler: Optional[BackgroundScheduler] = None
_scheduler_lock = threading.Lock()
_last_run_time: Optional[datetime] = None
_next_run_time: Optional[datetime] = None
_is_running = False


def _get_random_interval() -> int:
    """Get a random interval within configured range."""
    settings = get_settings()
    min_interval = settings.get("interval_min", 10)
    max_interval = settings.get("interval_max", 14)
    return random.randint(min_interval, max_interval)


def _run_visits():
    """Execute website visits. Called by scheduler."""
    global _last_run_time, _next_run_time, _is_running
    
    _is_running = True
    _last_run_time = datetime.now()
    
    try:
        websites = get_websites()
        enabled_sites = [w for w in websites if w.get("enabled", True)]
        
        if enabled_sites:
            logger.info(f"Running scheduled visits for {len(enabled_sites)} websites")
            results = visit_all_websites_sync(enabled_sites)
            
            success_count = sum(1 for r in results if r["success"])
            logger.info(f"Completed: {success_count}/{len(results)} successful")
        else:
            logger.info("No enabled websites to visit")
            
    except Exception as e:
        logger.error(f"Error during scheduled visits: {e}")
    finally:
        _is_running = False
        # Schedule next run with new random interval
        _reschedule_with_random_interval()


def _reschedule_with_random_interval():
    """Reschedule the job with a new random interval."""
    global _scheduler, _next_run_time
    
    if _scheduler is None:
        return
    
    try:
        # Remove existing job if present
        existing_jobs = _scheduler.get_jobs()
        for job in existing_jobs:
            if job.id == "keep_alive_job":
                _scheduler.remove_job("keep_alive_job")
                break
        
        # Add job with new random interval
        interval_minutes = _get_random_interval()
        _next_run_time = datetime.now() + timedelta(minutes=interval_minutes)
        
        _scheduler.add_job(
            _run_visits,
            trigger=IntervalTrigger(minutes=interval_minutes),
            id="keep_alive_job",
            name="Keep Alive Website Visits",
            replace_existing=True,
            next_run_time=_next_run_time
        )
        
        logger.info(f"Next visit scheduled in {interval_minutes} minutes")
        
    except Exception as e:
        logger.error(f"Error rescheduling: {e}")


def start_scheduler() -> bool:
    """Start the background scheduler. Returns True if started successfully."""
    global _scheduler, _next_run_time
    
    with _scheduler_lock:
        if _scheduler is not None and _scheduler.running:
            logger.info("Scheduler already running")
            return True
        
        try:
            _scheduler = BackgroundScheduler(daemon=True)
            _scheduler.start()
            
            # Schedule first run
            interval_minutes = _get_random_interval()
            _next_run_time = datetime.now() + timedelta(minutes=interval_minutes)
            
            _scheduler.add_job(
                _run_visits,
                trigger=IntervalTrigger(minutes=interval_minutes),
                id="keep_alive_job",
                name="Keep Alive Website Visits",
                next_run_time=_next_run_time
            )
            
            logger.info(f"Scheduler started. First run in {interval_minutes} minutes")
            return True
            
        except Exception as e:
            logger.error(f"Failed to start scheduler: {e}")
            return False


def stop_scheduler() -> bool:
    """Stop the background scheduler."""
    global _scheduler, _next_run_time
    
    with _scheduler_lock:
        if _scheduler is None:
            return True
        
        try:
            _scheduler.shutdown(wait=False)
            _scheduler = None
            _next_run_time = None
            logger.info("Scheduler stopped")
            return True
            
        except Exception as e:
            logger.error(f"Failed to stop scheduler: {e}")
            return False


def is_scheduler_running() -> bool:
    """Check if scheduler is running."""
    return _scheduler is not None and _scheduler.running


def get_scheduler_status() -> dict:
    """Get current scheduler status."""
    return {
        "running": is_scheduler_running(),
        "last_run": _last_run_time.isoformat() if _last_run_time else None,
        "next_run": _next_run_time.isoformat() if _next_run_time else None,
        "is_visiting": _is_running
    }


def trigger_immediate_run():
    """Trigger an immediate visit run (manual trigger)."""
    global _is_running
    
    if _is_running:
        logger.warning("A visit run is already in progress")
        return False
    
    # Run in a separate thread to not block
    thread = threading.Thread(target=_run_visits, daemon=True)
    thread.start()
    return True
