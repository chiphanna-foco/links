"""Front Link Sync — FastAPI application.

Endpoints
---------
GET  /api/status                        Health + last sync info
POST /api/check                         Trigger a full sweep (background task)
POST /api/check/{conversation_id}       Force-check a single parent conversation
"""
import asyncio
import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import FastAPI

from config import (
    CHECK_INTERVAL_HOURS,
    QUIET_HOURS_END,
    QUIET_HOURS_START,
    RUN_ON_STARTUP,
    TIMEZONE,
)
from database import get_last_sync, get_state_float, init_db
from link_sync import (
    EVENTS_CURSOR_KEY,
    check_linked_conversations,
    process_single_conversation,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)

# A full sweep takes many hours, but /api/check is polled far more often than
# that (a cron-job.org job hits it every 15 minutes). Queueing every trigger on
# a lock built an unbounded backlog of sweeps that could never drain, kept the
# process permanently busy, and made Front rate-limit us. Triggers that arrive
# while a sweep is in flight are now dropped, not queued.
#
# Check-and-set is only atomic because every mutation happens on the event-loop
# thread; the sweep itself runs in a worker thread via asyncio.to_thread.
_sync_running = False


def _try_begin_sync() -> bool:
    """Claim the sweep slot. False if a sweep is already in flight."""
    global _sync_running
    if _sync_running:
        return False
    _sync_running = True
    return True


# ---------------------------------------------------------------------------
# Quiet window
# ---------------------------------------------------------------------------

def _in_quiet_window() -> bool:
    """True if current local hour is in [QUIET_HOURS_START, QUIET_HOURS_END).

    Handles wrap across midnight (e.g. 23..6 covers 23, 0, 1, 2, 3, 4, 5).
    """
    try:
        now_hour = datetime.now(ZoneInfo(TIMEZONE)).hour
    except Exception:
        logger.exception("Invalid TIMEZONE %s — running anyway", TIMEZONE)
        return False

    start, end = QUIET_HOURS_START, QUIET_HOURS_END
    if start == end:
        return False
    if start > end:
        return now_hour >= start or now_hour < end
    return start <= now_hour < end


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------

async def _run_sync(force: bool = False):
    """Run one sweep. Caller must already hold the slot via _try_begin_sync()."""
    global _sync_running
    try:
        await asyncio.to_thread(check_linked_conversations, force)
    finally:
        _sync_running = False


async def _link_check_scheduler():
    interval = CHECK_INTERVAL_HOURS * 3600
    logger.info(
        "Scheduler started — interval=%dh, quiet window=%d..%d %s",
        CHECK_INTERVAL_HOURS, QUIET_HOURS_START, QUIET_HOURS_END, TIMEZONE,
    )
    while True:
        await asyncio.sleep(interval)
        if _in_quiet_window():
            logger.info("In quiet window — skipping scheduled run")
            continue
        if not _try_begin_sync():
            logger.warning(
                "DEGRADED: scheduled run skipped — previous sweep still in progress"
            )
            continue
        try:
            await _run_sync()
        except Exception:
            logger.exception("Scheduled link-sync run failed")


# ---------------------------------------------------------------------------
# App lifecycle
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    if RUN_ON_STARTUP and not _in_quiet_window() and _try_begin_sync():
        logger.info("RUN_ON_STARTUP=true — kicking off initial sync")
        asyncio.create_task(_run_sync())
    task = asyncio.create_task(_link_check_scheduler())
    yield
    task.cancel()


app = FastAPI(title="Front Link Sync", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

def _cursor_age_hours():
    """How stale the incremental cursor is; None means the next run is a full sweep."""
    cursor = get_state_float(EVENTS_CURSOR_KEY)
    if cursor is None:
        return None
    return round((time.time() - cursor) / 3600, 2)


@app.get("/api/status")
async def api_status():
    return {
        "status": "ok",
        "sweep_in_progress": _sync_running,
        "events_cursor_age_hours": _cursor_age_hours(),
        "last_sync": get_last_sync(),
        "check_interval_hours": CHECK_INTERVAL_HOURS,
        "quiet_hours": {
            "start": QUIET_HOURS_START,
            "end": QUIET_HOURS_END,
            "timezone": TIMEZONE,
            "in_quiet_window": _in_quiet_window(),
        },
    }


@app.post("/api/check")
async def api_check():
    """Trigger a full sweep (ignores quiet window — manual runs always execute).

    Returns 200 with `skipped: true` when a sweep is already running, so an
    external cron sees a healthy service instead of piling work on it.
    """
    if not _try_begin_sync():
        logger.info("Trigger ignored — sweep already in progress")
        return {"status": "already running", "skipped": True}
    asyncio.create_task(_run_sync())
    return {"status": "check started", "skipped": False}


@app.post("/api/check/{conversation_id}")
async def api_check_one(
    conversation_id: str, backfill: bool = False, debug: bool = False
):
    """Force-check a single parent conversation.

    backfill=true ignores cache, looks back to when each link was added, and
    REPLACES the section's bullets with a freshly-generated history.
    debug=true returns per-link diagnostics (AI reasoning, raw bullets,
    dropped bullets) without changing behavior.
    """
    result = await asyncio.to_thread(
        process_single_conversation, conversation_id, True, backfill, debug
    )
    return result
