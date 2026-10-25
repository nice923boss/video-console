"""Reuse of a cloud result made from the very same request (CL-02).

Cloud calls are billed, so a request that matches an earlier take of the project (same provider settings,
prompt, size, seed, length and reference picture) gets a copy of that take's file instead of a new call.
Only a locked seed repeats a request: an unlocked shot draws a new seed every time.
"""
from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

REUSED_NOTE = "相同提示詞與參數已生成過，沿用那個版本，沒有再送出（要重新生成請換種子或解除種子鎖定）"
# Payload fields that change what the vendor makes. The reference is a take or media id, whose file never changes.
_FIELDS = ("mode", "prompt", "negative", "width", "height", "seed", "duration", "fps", "denoise", "image")


def request_key(provider: str, config: dict, payload: dict) -> str:
    settings = {k: v for k, v in config.items() if k != "price"}
    body = {"provider": provider, "config": settings, **{k: payload.get(k) for k in _FIELDS}}
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:32]


def find(project: dict, project_dir: Path, kind: str, key: str, work: Path) -> tuple[Path, dict] | None:
    """A copy in work/ of the newest take made from this request, with that take's source."""
    for take in sorted(project["takes"].values(), key=lambda t: t.get("created") or 0, reverse=True):
        src = project_dir / str(take.get("file") or "")
        if (take.get("source") or {}).get("requestKey") == key and take.get("kind") == kind and src.is_file():
            dst = work / f"reused{src.suffix}"
            shutil.copy2(src, dst)
            return dst, take["source"]
    return None
