"""Front Link Sync — FastAPI application.

Endpoints
---------
GET  /api/status                        Health + last sync info
POST /api/check                         Trigger a full sweep (background task)
POST /api/check/{conversation_id}       Force-check a single parent conversation
"""
import asyncio
import logging
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
from database import get_last_sync, init_db
from link_sync import check_linked_conversations, process_single_conversation

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)

_sync_lock = asyncio.Lock()


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
    async with _sync_lock:
        await asyncio.to_thread(check_linked_conversations, force)


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
    if RUN_ON_STARTUP and not _in_quiet_window():
        logger.info("RUN_ON_STARTUP=true — kicking off initial sync")
        asyncio.create_task(_run_sync())
    task = asyncio.create_task(_link_check_scheduler())
    yield
    task.cancel()


app = FastAPI(title="Front Link Sync", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/api/status")
async def api_status():
    return {
        "status": "ok",
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
    """Trigger a full sweep (ignores quiet window — manual runs always execute)."""
    asyncio.create_task(_run_sync())
    return {"status": "check started"}


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
