"""Non-secret user settings with defaults. Secrets (API keys) live in SecretStore."""
from __future__ import annotations

import copy
import logging
import re
import threading
from pathlib import Path
from typing import Any

from . import video2x
from .opfields import MAX_CAPTION_STYLES, clean_caption_style
from .events import EventBus
from .util import AppError, deep_merge, read_json, write_json_atomic

log = logging.getLogger("vc.settings")

LOCAL_COMFY_URL = "http://127.0.0.1:8000"
DEFAULT_NEGATIVE = "blurry, low quality, deformed, distorted, watermark, text, logo, signature, jpeg artifacts"
MAX_LABEL_COLORS = 12
MAX_GLOSSARY = 5000  # characters in the caption word list
TEXT_SCALES = (1, 1.15, 1.3)  # interface text size: standard, large, larger
NTFY_WHEN = ("all", "failed")
# The export dialog's platform presets (web/js/lib/export-presets.js PRESETS); "" keeps the project size.
EXPORT_PRESETS = ("", "youtube", "shorts", "reels", "line")
EXPORT_RESOLUTIONS = ("", "1080", "2160")  # output short side when no preset is picked; "" keeps the project size

DEFAULTS: dict[str, Any] = {
    "version": 1,
    "ui": {
        "theme": "dark",
        "safeArea": False,
        "thirds": False,
        "lastProjectId": None,
        "autoExit": True,
        "confirmDelete": True,
        "textScale": 1,
        "defaultsSeen": "",  # the default-model revision the user already answered (see default_updates.py)
        # label colours offered for shots and clips: {value: "#rrggbb", label}
        "labelColors": [
            {"value": "#ff6b6b", "label": "紅色"},
            {"value": "#f5b14c", "label": "橘色"},
            {"value": "#c6f24e", "label": "萊姆綠"},
            {"value": "#4fd18b", "label": "綠色"},
            {"value": "#6ea8ff", "label": "藍色"},
            {"value": "#b28cff", "label": "紫色"},
        ],
    },
    "defaults": {
        "width": 1920,
        "height": 1080,
        "fps": 24,
        "aspect": "16:9",
        "shotDuration": 5.0,
        "negative": DEFAULT_NEGATIVE,
        "route": "A",
    },
    "comfy": {
        "servers": [
            {"id": "local", "name": "本機 ComfyUI", "url": LOCAL_COMFY_URL, "enabled": True},
        ],
        "timeoutMin": 30,
        "upscaleWorkflow": "upscale-ultrasharp",
        "useComfyOrgKey": False,
        "livePreview": True,
    },
    "routes": [
        {
            "id": "A",
            "name": "ComfyUI",
            "color": "#c6f24e",
            "image": {"backend": "comfy", "server": "auto", "workflow": "flux3-klein-t2i",
                      "workflowI2I": "flux3-klein-edit"},
            "video": {"backend": "comfy", "server": "auto", "workflow": "ltxv-2b-i2v"},
        },
        {
            "id": "B",
            "name": "雲端 API",
            "color": "#6ea8ff",
            "image": {"backend": "provider", "provider": "openai-images"},
            "video": {"backend": "provider", "provider": "veo"},
        },
    ],
    "providers": {},
    "ffmpeg": {"path": "", "encoder": "auto"},
    # folder: where every project's videos and ZIP files go; empty means <data>/exports
    "export": {"quality": "high", "burnCaptions": False, "captionSize": 52, "fadeIn": 0.0, "fadeOut": 0.0,
               "folder": "", "preset": "", "resolution": ""},
    "notify": {
        "desktop": True,
        "sound": True,
        # when: "all" pushes every batch summary, "failed" only batches with a failure
        "ntfy": {"enabled": False, "server": "https://ntfy.sh", "topic": "", "when": "all"},
    },
    "llm": {
        "enabled": False,
        "baseUrl": "http://127.0.0.1:11434/v1",
        "model": "gemma3:4b",
        "temperature": 0.7,
    },
    "history": {"keep": 50, "intervalSec": 120},
    "queue": {"autoRetry": 0},  # automatic retries of a job that failed on a network error, 0 to 5
    "voice": {"url": "http://127.0.0.1:18301"},
    "stt": {"folder": "", "python": ""},  # Speech-to-subtitle; empty: the sibling folder and its own Python
    "video2x": {"enabled": False, "method": "anime4k"},  # upscale ComfyUI videos to 1080P before they become takes
    "captionStyles": [],  # caption style library shared by every project; projects keep their own copies
    "captionStylesAuto": False,  # new projects start with a copy of every library style (the first is the default)
    # Caption spelling word list (CP-12): a correct term per line, optionally "=wrong,wrong"; checked in the browser
    "glossary": "",
}

_TOP_LEVEL = set(DEFAULTS) - {"version"}
_ROUTE_ID = re.compile(r"^[A-Za-z0-9_-]{1,12}$")
_SERVER_ID = re.compile(r"^[a-z0-9_-]{1,32}$")
_HEX = re.compile(r"^#[0-9a-f]{6}$")


def _label_colors(raw: Any) -> list[dict]:
    """Keep the well-formed entries (a bad one is dropped, not fatal: it must not reset every setting)."""
    picked: list[dict] = []
    for c in raw if isinstance(raw, list) else []:
        value = str(c.get("value") or "").lower() if isinstance(c, dict) else ""
        if _HEX.match(value) and all(x["value"] != value for x in picked):
            picked.append({"value": value, "label": str(c.get("label") or "").strip()[:12] or value})
    return picked[:MAX_LABEL_COLORS]


def _validate(cfg: dict) -> None:
    cfg["ui"]["labelColors"] = _label_colors(cfg["ui"].get("labelColors"))
    scale = cfg["ui"].get("textScale")
    if isinstance(scale, bool) or scale not in TEXT_SCALES:
        cfg["ui"]["textScale"] = 1
    servers = cfg["comfy"]["servers"]
    seen: set[str] = set()
    for s in servers:
        if not _SERVER_ID.match(str(s.get("id", ""))):
            raise AppError("ComfyUI 伺服器代號只能用英數字、底線、連字號")
        if s["id"] in seen:
            raise AppError(f"ComfyUI 伺服器代號重複：{s['id']}")
        seen.add(s["id"])
        url = str(s.get("url", "")).strip().rstrip("/")
        if not re.match(r"^https?://[^\s/]+", url):
            raise AppError(f"ComfyUI 位址格式不正確：{url or '（空白）'}")
        s["url"] = url
        s["name"] = str(s.get("name") or s["id"])[:60]
        s["enabled"] = bool(s.get("enabled", True))
    route_ids: set[str] = set()
    if not cfg["routes"]:
        raise AppError("至少需要保留一條運算路由")
    for r in cfg["routes"]:
        if not _ROUTE_ID.match(str(r.get("id", ""))):
            raise AppError("路由代號只能用 1 到 12 個英數字")
        if r["id"] in route_ids:
            raise AppError(f"路由代號重複：{r['id']}")
        route_ids.add(r["id"])
        r["name"] = str(r.get("name") or "").strip()[:40] or r["id"]
        if not re.match(r"^#[0-9a-fA-F]{6}$", str(r.get("color", ""))):
            r["color"] = "#8a8f98"
        for kind in ("image", "video"):
            sec = r.get(kind) or {}
            if sec.get("backend") not in ("comfy", "provider", "none"):
                raise AppError(f"路由 {r['id']} 的{'生圖' if kind == 'image' else '影片'}後端設定不正確")
            r[kind] = sec
    providers = cfg["providers"]
    if not isinstance(providers, dict) or not all(isinstance(v, dict) for v in providers.values()):
        raise AppError("雲端 API 設定格式不正確")
    ntfy = cfg["notify"]["ntfy"]
    topic = str(ntfy.get("topic") or "").strip()
    if topic and not re.match(r"^[A-Za-z0-9_-]{1,64}$", topic):
        raise AppError("ntfy 主題只能用英數字、底線、連字號")
    if ntfy.get("enabled") and not topic:
        raise AppError("開啟手機推播前，請先填寫 ntfy 主題")
    server = str(ntfy.get("server") or "").strip().rstrip("/")
    if not re.match(r"^https?://[^\s/]+", server):
        raise AppError("ntfy 伺服器位址格式不正確，例如 https://ntfy.sh")
    ntfy.update(topic=topic, server=server, enabled=bool(ntfy.get("enabled")),
                when=ntfy.get("when") if ntfy.get("when") in NTFY_WHEN else "all")
    llm = cfg["llm"]
    base = str(llm.get("baseUrl") or "").strip().rstrip("/")
    if not re.match(r"^https?://[^\s/]+", base):
        raise AppError("LLM 服務位址格式不正確，例如 http://127.0.0.1:11434/v1")
    try:
        temperature = min(max(float(llm.get("temperature", 0.7)), 0.0), 2.0)
    except (TypeError, ValueError):
        raise AppError("LLM 溫度必須是 0 到 2 之間的數字") from None
    llm.update(baseUrl=base, model=str(llm.get("model") or "").strip()[:120], temperature=temperature,
               enabled=bool(llm.get("enabled")))
    voice = cfg["voice"]
    voice_url = str(voice.get("url") or "").strip().rstrip("/")
    if not re.match(r"^https?://[^\s/]+", voice_url):
        raise AppError("Audio8Voice 位址格式不正確，例如 http://127.0.0.1:18301")
    voice["url"] = voice_url
    stt = cfg["stt"]
    stt.update(folder=str(stt.get("folder") or "").strip()[:500], python=str(stt.get("python") or "").strip()[:500])
    export = cfg["export"]
    out_folder = str(export.get("folder") or "").strip().strip('"').strip()
    # Only the form is checked: a drive that is unplugged now must not reset every setting on the next start.
    if out_folder and (len(out_folder) > 500 or re.search(r"[\x00-\x1f]", out_folder)
                       or not Path(out_folder).is_absolute()):
        raise AppError("匯出資料夾要填完整路徑，例如 D:\\影片輸出")
    export["folder"] = out_folder
    export["preset"] = export.get("preset") if export.get("preset") in EXPORT_PRESETS else ""
    export["resolution"] = export.get("resolution") if export.get("resolution") in EXPORT_RESOLUTIONS else ""
    upscale = cfg["video2x"]
    upscale.update(enabled=bool(upscale.get("enabled")),
                   method=upscale.get("method") if upscale.get("method") in video2x.METHODS else "anime4k")
    styles = cfg["captionStyles"]
    if not isinstance(styles, list) or len(styles) > MAX_CAPTION_STYLES:
        raise AppError(f"字幕樣式庫最多 {MAX_CAPTION_STYLES} 個樣式")
    cfg["captionStyles"] = [clean_caption_style(s) for s in styles]
    if len({s["id"] for s in cfg["captionStyles"]}) != len(styles):
        raise AppError("字幕樣式庫有重複的樣式代號")
    cfg["captionStylesAuto"] = bool(cfg["captionStylesAuto"])
    if not isinstance(cfg["glossary"], str) or len(cfg["glossary"]) > MAX_GLOSSARY:
        raise AppError(f"字幕用語詞庫最多 {MAX_GLOSSARY} 字")
    hist = cfg["history"]
    defaults = cfg["defaults"]
    try:
        hist.update(keep=min(max(int(hist.get("keep", 50)), 5), 500),
                    intervalSec=min(max(float(hist.get("intervalSec", 120)), 10.0), 3600.0))
        defaults["shotDuration"] = min(max(float(defaults.get("shotDuration", 5.0)), 0.2), 600.0)
    except (TypeError, ValueError):
        raise AppError("版本紀錄或鏡頭長度必須是數字") from None
    try:
        cfg["queue"]["autoRetry"] = min(max(int(cfg["queue"].get("autoRetry", 0)), 0), 5)
    except (TypeError, ValueError):
        raise AppError("自動重試次數必須是 0 到 5 的整數") from None


class SettingsStore:
    def __init__(self, path: Path, bus: EventBus | None = None):
        self._path = path
        self._bus = bus
        self._lock = threading.Lock()
        stored = read_json(path, {}) or {}
        # deep_merge copies only the levels it patches, so start from a private copy of DEFAULTS
        merged = deep_merge(copy.deepcopy(DEFAULTS), {k: v for k, v in stored.items() if k in _TOP_LEVEL})
        try:
            _validate(merged)
        except AppError as exc:
            log.warning("Stored settings invalid (%s); falling back to defaults", exc.message)
            merged = copy.deepcopy(DEFAULTS)
        self._data = merged

    def get(self) -> dict:
        with self._lock:
            return copy.deepcopy(self._data)

    def section(self, key: str) -> Any:
        with self._lock:
            return copy.deepcopy(self._data.get(key))

    def route(self, route_id: str | None) -> dict:
        with self._lock:
            routes = self._data["routes"]
            for r in routes:
                if r["id"] == route_id:
                    return copy.deepcopy(r)
            return copy.deepcopy(routes[0])

    def update(self, patch: dict) -> dict:
        unknown = set(patch) - _TOP_LEVEL
        if unknown:
            raise AppError(f"未知的設定項目：{', '.join(sorted(unknown))}")
        with self._lock:
            # _validate normalizes in place; a private copy keeps a rejected patch from touching live data
            merged = deep_merge(copy.deepcopy(self._data), patch)
            _validate(merged)
            self._data = merged
            write_json_atomic(self._path, merged)
            snapshot = copy.deepcopy(merged)
        if self._bus:
            self._bus.publish("settings", snapshot)
        return snapshot

    def set_last_project(self, pid: str | None) -> None:
        with self._lock:
            if self._data["ui"].get("lastProjectId") == pid:
                return
            self._data["ui"]["lastProjectId"] = pid
            write_json_atomic(self._path, self._data)
