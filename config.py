import os
from dotenv import load_dotenv

load_dotenv()

# -- Front --
FRONT_API_KEY = os.getenv("FRONT_API_KEY", "")
FRONT_API_URL = os.getenv("FRONT_API_URL", "https://api2.frontapp.com")
LINKED_TAG_ID = os.getenv("LINKED_TAG_ID", "")

# -- Anthropic --
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5")

# -- Incremental sync --
# The sweep used to walk every tagged conversation on every run. It now asks
# Front's /events feed which conversations actually changed and looks only at
# those. These bound the fallback to the old full sweep.
EVENTS_ENABLED = os.getenv("EVENTS_ENABLED", "true").lower() == "true"
# If our cursor is older than this, fall back to a full sweep rather than
# paging a huge event backlog. Measured 2026-08-10 on this account: ~570
# activity events/hour over ~38 pages, so 36h is roughly 1,400 pages.
EVENTS_MAX_CURSOR_AGE_HOURS = int(os.getenv("EVENTS_MAX_CURSOR_AGE_HOURS", "36"))
# Only conversations touched inside this window are worth backfilling. Dormant
# ones stay skipped until an event wakes them, however old they are. Measured
# 2026-08-10: 6,684 of 12,157 tagged conversations (55%) fall inside 30 days.
PARENT_ACTIVE_WINDOW_DAYS = int(os.getenv("PARENT_ACTIVE_WINDOW_DAYS", "30"))

# Parents are independent of each other, so they can be worked concurrently.
# The ceiling is Front's 200 req/min, not CPU: a single worker was measured
# using only ~15% of that budget.
SWEEP_CONCURRENCY = int(os.getenv("SWEEP_CONCURRENCY", "6"))

# On a cold start (empty cache — e.g. a redeploy without a volume) don't
# re-derive a year of history for every link; clamp the lookback to this.
COLD_START_LOOKBACK_HOURS = int(os.getenv("COLD_START_LOOKBACK_HOURS", "24"))

# -- Scheduler --
CHECK_INTERVAL_HOURS = int(os.getenv("CHECK_INTERVAL_HOURS", "6"))
RUN_ON_STARTUP = os.getenv("RUN_ON_STARTUP", "false").lower() == "true"

# Quiet window — scheduler skips runs when local hour is in [START, END).
QUIET_HOURS_START = int(os.getenv("QUIET_HOURS_START", "23"))
QUIET_HOURS_END = int(os.getenv("QUIET_HOURS_END", "6"))
TIMEZONE = os.getenv("TIMEZONE", "America/Denver")

# -- Application --
# Railway mounts volumes at /data by convention
DATABASE_PATH = os.getenv(
    "DATABASE_PATH",
    "/data/links.db" if os.path.isdir("/data") else "links.db",
)
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8000"))
