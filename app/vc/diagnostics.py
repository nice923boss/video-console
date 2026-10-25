"""Debug bundle for bug reports (QA-09): the log files, app facts, settings, jobs and optionally one project's
state in a zip, and clearing the log files. API keys stay out: secrets are listed only as set or not."""
from __future__ import annotations

import io
import json
import logging
import logging.handlers
import re
import sys
import time
import zipfile
from pathlib import Path
from typing import IO, Any

log = logging.getLogger("vc.diagnostics")

_LOG_NAME_RE = re.compile(r"^(app\.log(\.\d+)?|stdout\.log)$")


def log_files(log_dir: Path) -> list[Path]:
    if not log_dir.is_dir():
        return []
    return sorted(p for p in log_dir.iterdir() if p.is_file() and _LOG_NAME_RE.match(p.name))


def log_bytes(log_dir: Path) -> int:
    return sum(p.stat().st_size for p in log_files(log_dir))


def _dump(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, default=str)


def debug_bundle(log_dir: Path, facts: dict, settings: dict, secrets: dict, jobs: list,
                 project: dict | None = None) -> bytes:
    """The zip's bytes. The ntfy topic works like a password on a public server, so it is masked too."""
    ntfy = settings.get("ntfy") or {}
    settings = {**settings, "ntfy": {**ntfy, "topic": "***" if ntfy.get("topic") else ""}}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for path in log_files(log_dir):
            z.write(path, f"logs/{path.name}")
        z.writestr("system.json", _dump({**facts, "bundled": time.time()}))
        z.writestr("settings.json", _dump(settings))
        z.writestr("secrets.json", _dump({name: bool(s.get("set")) for name, s in secrets.items()}))
        z.writestr("jobs.json", _dump(jobs))
        if project is not None:
            z.writestr("project.json", _dump(project))
    return buf.getvalue()


def _open_streams() -> dict[Path, tuple[IO[str], Any]]:
    """Log files this process holds open, with the lock writers take: the rotating handler's and, under
    pythonw, stdout/stderr's."""
    out: dict[Path, tuple[IO[str], Any]] = {}
    for h in logging.getLogger().handlers:
        if isinstance(h, logging.handlers.RotatingFileHandler) and h.stream is not None:
            out[Path(h.baseFilename).resolve()] = (h.stream, h.lock)
    for stream in (sys.stdout, sys.stderr):
        name = getattr(stream, "name", None)
        if isinstance(name, str) and name.endswith("stdout.log"):
            out[Path(name).resolve()] = (stream, None)
    return out


def _truncate(stream: IO[str], lock: Any) -> None:
    if lock is not None:
        lock.acquire()
    try:
        stream.flush()
        stream.seek(0)
        stream.truncate()  # through the open handle, so the next line starts at offset 0
    finally:
        if lock is not None:
            lock.release()


def clear_logs(log_dir: Path) -> int:
    """Empty the files still in use and delete the rest; returns the bytes freed."""
    freed = 0
    held = _open_streams()
    for path in log_files(log_dir):
        size = path.stat().st_size
        try:
            if path.resolve() in held:
                _truncate(*held[path.resolve()])
            else:
                path.unlink()
        except OSError as exc:
            log.warning("Could not clear %s: %s", path.name, exc)
            continue
        freed += size
    log.info("Log files cleared (%d bytes)", freed)
    return freed
