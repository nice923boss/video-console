"""Logging to a rotating file; stdout/stderr are redirected there when running under pythonw."""
from __future__ import annotations

import logging
import logging.handlers
import re
import sys
import threading
from pathlib import Path

_TOKEN_RE = re.compile(r"(Bearer|Key)\s+[A-Za-z0-9._\-:]{8,}")


class _RedactFilter(logging.Filter):
    """Last line of defence: mask obvious bearer tokens if one ever reaches a log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        if "Bearer " in msg or "Key " in msg:
            record.msg, record.args = _TOKEN_RE.sub(r"\1 ***", msg), ()
        return True


def _log_thread_crash(args: threading.ExceptHookArgs) -> None:
    """A background thread that dies leaves its traceback in app.log, with a time, for the debug bundle (QA-09)."""
    if args.exc_type is SystemExit:
        return
    name = args.thread.name if args.thread else "?"
    logging.getLogger("vc").critical("Thread %s crashed", name,
                                     exc_info=(args.exc_type, args.exc_value, args.exc_traceback))


def setup_logging(log_dir: Path, verbose: bool = False) -> logging.Logger:
    log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    for h in list(root.handlers):
        root.removeHandler(h)
    fmt = logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
    fh = logging.handlers.RotatingFileHandler(
        log_dir / "app.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    fh.addFilter(_RedactFilter())
    root.addHandler(fh)
    if sys.stdout is None or sys.stderr is None:
        # pythonw: no console, so stray prints and tracebacks go to a file instead of vanishing
        stream = open(log_dir / "stdout.log", "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stdout or stream
        sys.stderr = sys.stderr or stream
    else:
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(fmt)
        sh.addFilter(_RedactFilter())
        root.addHandler(sh)
    threading.excepthook = _log_thread_crash
    return logging.getLogger("vc")
