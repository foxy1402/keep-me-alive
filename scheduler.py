"""
Scheduler module for Keep Me Alive service.

Uses APScheduler for background cron-like job scheduling.

The scheduler is created once per process and adds its job before starting, so a
failure cannot leave a running-but-jobless scheduler. Intervals are clamped
because a stored min > max would otherwise raise inside the reschedule handler
and silently drop the job.
"""
import logging
import random
import threading
from datetime import datetime, timedelta
from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from storage import get_settings, get_websites
from browser_worker import visit_all_websites_sync

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

JOB_ID = "keep_alive_job"

# Global scheduler instance
_scheduler: Optional[BackgroundScheduler] = None
_scheduler_lock = threading.Lock()
_last_run_time: Optional[datetime] = None
_is_running = False

# Most recent results, shared by scheduled and manual runs so the UI has a
# single source of truth for "what happened last".
_last_run = {"at": None, "results": []}
_last_run_lock = threading.Lock()


def record_run(results: list, at: Optional[datetime] = None):
    """Remember the latest visit results (screenshots are not kept in memory)."""
    cleaned = []
    for result in results:
        item = dict(result)
        item.pop("screenshot", None)
        cleaned.append(item)

    with _last_run_lock:
        _last_run["at"] = at or datetime.now()
        _last_run["results"] = cleaned


def get_last_run() -> dict:
    with _last_run_lock:
        return {"at": _last_run["at"], "results": list(_last_run["results"])}


def _get_random_interval() -> int:
    """Get a random interval within the configured range, clamped to 1..60."""
    settings = get_settings()
    try:
        low = int(settings.get("interval_min", 10))
        high = int(settings.get("interval_max", 14))
    except (TypeError, ValueError):
        low, high = 10, 14

    low, high = min(low, high), max(low, high)
    low = max(1, min(60, low))
    high = max(1, min(60, high))
    return random.randint(low, high)


def _run_visits():
    """Execute website visits. Called by the scheduler and by manual triggers."""
    global _last_run_time, _is_running

    with _scheduler_lock:
        if _is_running:
            logger.info("A visit run is already in progress; skipping this trigger")
            return
        _is_running = True

    started = datetime.now()
    _last_run_time = started
    try:
        websites = get_websites()
        enabled_sites = [w for w in websites if w.get("enabled", True)]

        if enabled_sites:
            logger.info("Running scheduled visits for %s websites", len(enabled_sites))
            results = visit_all_websites_sync(enabled_sites)
            record_run(results, started)

            success_count = sum(1 for r in results if r["success"])
            blocked = sum(1 for r in results if r.get("blocked"))
            logger.info(
                "Completed: %s/%s reached the origin%s",
                success_count, len(results),
                ", %s blocked" % blocked if blocked else "",
            )
        else:
            logger.info("No enabled websites to visit")

    except Exception as e:
        logger.error("Error during scheduled visits: %s", e)
    finally:
        with _scheduler_lock:
            _is_running = False
        _reschedule_with_random_interval()


def _reschedule_with_random_interval():
    """Schedule the next run with a fresh random interval."""
    scheduler = _scheduler
    if scheduler is None or not scheduler.running:
        return

    try:
        interval_minutes = _get_random_interval()
    except Exception as e:
        logger.error("Could not pick an interval: %s", e)
        return

    try:
        # replace_existing avoids a remove/add gap where the job does not exist.
        scheduler.add_job(
            _run_visits,
            trigger=IntervalTrigger(minutes=interval_minutes),
            id=JOB_ID,
            name="Keep Alive Website Visits",
            replace_existing=True,
            next_run_time=datetime.now() + timedelta(minutes=interval_minutes),
        )
        logger.info("Next visit scheduled in %s minutes", interval_minutes)

    except Exception as e:
        logger.error("Error rescheduling: %s", e)


def start_scheduler() -> bool:
    """Start the background scheduler. Returns True if started successfully."""
    global _scheduler

    with _scheduler_lock:
        if _scheduler is not None and _scheduler.running:
            logger.info("Scheduler already running")
            return True

        try:
            interval_minutes = _get_random_interval()
        except Exception as e:
            logger.error("Could not start scheduler: %s", e)
            interval_minutes = 10

        scheduler = BackgroundScheduler(daemon=True)
        try:
            # Add the job before starting so a failure here cannot leave us
            # running with nothing scheduled.
            scheduler.add_job(
                _run_visits,
                trigger=IntervalTrigger(minutes=interval_minutes),
                id=JOB_ID,
                name="Keep Alive Website Visits",
                replace_existing=True,
                next_run_time=datetime.now() + timedelta(minutes=interval_minutes),
            )
            scheduler.start()
        except Exception as e:
            logger.error("Failed to start scheduler: %s", e)
            try:
                scheduler.shutdown(wait=False)
            except Exception:
                pass
            return False

        _scheduler = scheduler
        logger.info("Scheduler started. First run in %s minutes", interval_minutes)
        return True


def stop_scheduler() -> bool:
    """Stop the background scheduler."""
    global _scheduler

    with _scheduler_lock:
        scheduler = _scheduler
        if scheduler is None:
            return True

        try:
            # wait=False: a visit in flight finishes on its own; stopping must not
            # block container shutdown.
            scheduler.shutdown(wait=False)
            logger.info("Scheduler stopped")
            return True
        except Exception as e:
            logger.error("Failed to stop scheduler: %s", e)
            return False
        finally:
            _scheduler = None


def is_scheduler_running() -> bool:
    """Check if scheduler is running."""
    scheduler = _scheduler
    return scheduler is not None and scheduler.running


def get_scheduler_status() -> dict:
    """Get current scheduler status, reading the real next-run time from the job."""
    scheduler = _scheduler
    running = scheduler is not None and scheduler.running

    next_run = None
    if running:
        try:
            job = scheduler.get_job(JOB_ID)
            if job is not None and job.next_run_time is not None:
                next_run = job.next_run_time
                # APScheduler returns a timezone-aware datetime; the UI works in
                # naive local time.
                if next_run.tzinfo is not None:
                    next_run = next_run.astimezone().replace(tzinfo=None)
        except Exception as e:
            logger.debug("Could not read next run time: %s", e)

    return {
        "running": running,
        "last_run": _last_run_time.isoformat() if _last_run_time else None,
        "next_run": next_run.isoformat() if next_run else None,
        "is_visiting": _is_running,
    }


def trigger_immediate_run():
    """Trigger an immediate visit run (manual trigger)."""
    with _scheduler_lock:
        if _is_running:
            logger.warning("A visit run is already in progress")
            return False

    # Run in a separate thread to not block
    thread = threading.Thread(target=_run_visits, daemon=True)
    thread.start()
    return True
