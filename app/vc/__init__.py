"""Video Console backend package."""
from pathlib import Path

APP_ID = "video-console"
APP_NAME = "Video Console"
VERSION = "1.6.0"
DEFAULT_PORT = 8790
# The code this process runs: VERSION plus the newest backend source time (a packaged build has no .py files).
# A launch compares it with the running copy's, so one left in the tray after an update gets restarted.
BUILD = f"{VERSION}+{int(max((p.stat().st_mtime for p in Path(__file__).parent.rglob('*.py')), default=0))}"
