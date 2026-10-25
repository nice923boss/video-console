"""Compare this build with the latest release on GitHub (IN-04).

GitHub allows 60 API calls an hour per address without a token, so one answer, good or bad, is kept for six
hours; the settings page can ask again at once.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any

from . import VERSION, net
from .deps import version_key
from .util import AppError

log = logging.getLogger("vc.release")

RELEASE_URL = "https://api.github.com/repos/nice923boss/video-console/releases/latest"
RELEASES_PAGE = "https://github.com/nice923boss/video-console/releases"
KEEP_SEC = 6 * 3600

_lock = threading.Lock()
_last: tuple[float, dict[str, Any]] | None = None


def latest(force: bool = False) -> dict[str, Any]:
    """{current, latest, newer, url, published, remark}; `remark` says why `latest` is empty."""
    global _last
    with _lock:
        if not force and _last and time.monotonic() - _last[0] < KEEP_SEC:
            return _last[1]
    info = _fetch()
    with _lock:
        _last = (time.monotonic(), info)
    return info


def _fetch() -> dict[str, Any]:
    out = {"current": VERSION, "latest": "", "newer": False, "url": RELEASES_PAGE, "published": "", "remark": ""}
    try:
        data = net.request("GET", RELEASE_URL, headers={"Accept": "application/vnd.github+json"}, timeout=15).json()
    except AppError as exc:
        log.warning("Release check failed: %s", exc.message)
        return {**out, "remark": f"取不到 GitHub 上的版本資訊（{exc.message}）"}
    tag = str(data.get("tag_name") or "") if isinstance(data, dict) else ""
    version = tag.strip().removeprefix("v")
    if not version:
        return {**out, "remark": "GitHub 上的版本資訊格式不對"}
    url = str(data.get("html_url") or "")
    return {**out, "latest": version, "newer": version_key(version) > version_key(VERSION),
            "url": url if url.startswith(RELEASES_PAGE + "/") else RELEASES_PAGE,
            "published": str(data.get("published_at") or "")[:10]}
