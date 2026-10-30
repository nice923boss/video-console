"""Video Console backend package."""
from pathlib import Path

APP_ID = "video-console"
APP_NAME = "Video Console"
VERSION = "1.8.8"
DEFAULT_PORT = 8790
# The code this process runs: VERSION plus the newest backend source time. A packaged build has no .py files,
# so packaging/build_exe.py stores the value the source had in BUILD_STAMP. A launch compares it with the
# running copy's, so one left in the tray after an update gets restarted.
BUILD_STAMP = Path(__file__).with_name("BUILD")
BUILD = (BUILD_STAMP.read_text(encoding="utf-8").strip() if BUILD_STAMP.is_file() else
         f"{VERSION}+{int(max((p.stat().st_mtime for p in Path(__file__).parent.rglob('*.py')), default=0))}")
