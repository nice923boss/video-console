"""The user's own style library (IM-07): style text plus an optional sample picture, shared by every project.

The picture is only a visual reminder of the look; generation still receives the text alone.
"""
from __future__ import annotations

import shutil
import threading
from pathlib import Path

from .util import AppError, new_id, now, read_json, write_json_atomic

MAX_STYLES = 100
MAX_NAME = 40
MAX_TEXT = 2000
PICTURE_EXT = (".jpg", ".jpeg", ".png", ".webp")


class StyleLibrary:
    def __init__(self, data_dir: Path):
        self.file = data_dir / "style-library.json"
        self.dir = data_dir / "style-library"
        self._lock = threading.Lock()

    def list(self) -> list[dict]:
        styles = read_json(self.file, [])
        return styles if isinstance(styles, list) else []

    def add(self, name: str, prompt: str, negative: str, picture: Path | None = None) -> dict:
        name, prompt, negative = (name or "").strip(), (prompt or "").strip(), (negative or "").strip()
        if not name:
            raise AppError("請輸入風格名稱", 400)
        if not prompt and not negative:
            raise AppError("專案還沒有風格描述或負向提示詞，沒有東西可存", 400)
        if picture is not None and picture.suffix.lower() not in PICTURE_EXT:
            raise AppError("樣張只能是圖片", 400)
        with self._lock:
            styles = self.list()
            if len(styles) >= MAX_STYLES:
                raise AppError(f"風格庫最多 {MAX_STYLES} 筆，請先刪掉不用的", 400)
            entry = {"id": new_id("st"), "name": name[:MAX_NAME], "prompt": prompt[:MAX_TEXT],
                     "negative": negative[:MAX_TEXT], "picture": None, "created": now()}
            if picture is not None:
                self.dir.mkdir(parents=True, exist_ok=True)
                dst = self.dir / f"{entry['id']}{picture.suffix.lower()}"
                shutil.copyfile(picture, dst)
                entry["picture"] = dst.name
            write_json_atomic(self.file, [entry, *styles])
        return entry

    def remove(self, sid: str) -> None:
        with self._lock:
            styles = self.list()
            entry = next((s for s in styles if s.get("id") == sid), None)
            if entry is None:
                raise AppError("找不到這個風格", 404)
            write_json_atomic(self.file, [s for s in styles if s is not entry])
        if entry.get("picture"):
            (self.dir / entry["picture"]).unlink(missing_ok=True)

    def picture(self, sid: str) -> Path:
        entry = next((s for s in self.list() if s.get("id") == sid), None)
        path = self.dir / entry["picture"] if entry and entry.get("picture") else None
        if path is None or not path.is_file():
            raise AppError("這個風格沒有樣張", 404)
        return path
