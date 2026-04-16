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
