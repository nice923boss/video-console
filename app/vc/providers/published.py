"""Model menus of the vendors that offer no model-list API: Kling, Runway, MiniMax and Stability (CL-04).

The menus live in model_lists.json beside this file. The maintainer edits that file in the GitHub repository
and pushes it to main; "偵測模型" fetches the copy on main, so a new model reaches the menu without a new
release. Per provider and field: {"options": [id, newest first], "retired": [id], "replaces": {saved id: id}}.
Durations stay built into each provider: the list only names models.
"""
from __future__ import annotations

import json
from pathlib import Path

from .. import net
from ..util import AppError

LISTS_FILE = Path(__file__).with_name("model_lists.json")
LISTS_URL = "https://raw.githubusercontent.com/nice923boss/video-console/main/app/vc/providers/model_lists.json"
BUNDLED: dict = json.loads(LISTS_FILE.read_text(encoding="utf-8"))


def options(pid: str, key: str) -> list[str]:
    """The bundled menu of one field, for the field definition."""
    return list(BUNDLED["providers"][pid][key]["options"])


def _fetch() -> tuple[dict, str]:
    """The newer of the published and the bundled lists, plus a remark when the published one is unusable."""
    try:
        data = net.request("GET", LISTS_URL, timeout=15).json()
    except AppError as exc:
        return BUNDLED, f"取不到 GitHub 上的最新清單（{exc}），先列出程式內建的清單"
    if not isinstance(data, dict) or not isinstance(data.get("providers"), dict):
        return BUNDLED, "GitHub 上的清單格式不對，先列出程式內建的清單"
    return (data, "") if str(data.get("version") or "") >= str(BUNDLED["version"]) else (BUNDLED, "")


def detect(pid: str, config: dict, vendor: str) -> dict[str, dict]:
    """Catalog fields (shape in catalog.py) from the list. Never complete: a model missing from the list may
    still be callable, so only ids marked retired get a warning."""
    lists, problem = _fetch()
    remark = problem or (f"選單由 Video Console 在 GitHub 上維護（{lists.get('version')} 版），"
                         f"不是{vendor}的即時清單；選單沒有的模型仍可直接輸入代號")
    published = (lists.get("providers") or {}).get(pid) or {}
    out = {}
    for key, bundled in BUNDLED["providers"][pid].items():
        spec = published.get(key) if isinstance(published.get(key), dict) else bundled
        current = (config.get(key) or "").strip()
        retired = [m for m in spec.get("retired") or [] if isinstance(m, str)]
        values = [m for m in spec.get("options") or [] if isinstance(m, str) and m not in retired]
        suggested = (spec.get("replaces") or {}).get(current)
        out[key] = {"options": [{"value": m, "label": m, "date": ""} for m in values],
                    "suggested": suggested if suggested in values else None, "for": current,
                    "known": {m: "deprecated" for m in retired}, "complete": False, "message": remark}
    return out
