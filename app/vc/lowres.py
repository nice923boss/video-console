"""Smaller copies of video files for a lighter preview (PV-06). When the preview resolution is set to 1/2 or
1/4, the video files the sequence and the picture-in-picture clips play are copied at that size on a background
thread, so long or 4K footage decodes faster in the browser. The copies are saved in media/thumbs next to the
other derived files and recorded on the take or media item (preview2 / preview4), so each is made once; the
export always reads the original."""
from __future__ import annotations

import logging
import queue
import threading
from pathlib import Path
from typing import Callable

from .media import Media
from .store import ProjectStore
from .takes import THUMB_DIR
from .util import AppError

log = logging.getLogger("vc.lowres")

SCALES = (2, 4)
IDLE_EXIT = 5.0  # seconds the worker waits for more before it stops


def wanted(project: dict, scale: int) -> list[str]:
    """Video takes and media items the preview plays (active shot videos, picture-in-picture clips) that have
    no copy at this scale yet. See-through WebM files keep playing the original (H.264 has no alpha)."""
    ids = [s.get("activeVideo") for s in project.get("shots", []) if not s.get("disabled")]
    ids += [o.get("mediaId") for o in project.get("overlays", []) if not o.get("disabled")]
    out: list[str] = []
    for item_id in ids:
        item = project["takes"].get(item_id) or project["media"].get(item_id) if item_id else None
        if (item and item.get("kind") == "video" and item.get("file") and not item.get(f"preview{scale}")
                and not item.get("alphaCodec") and item_id not in out):
            out.append(item_id)
    return out


class LowRes:
    def __init__(self, store: ProjectStore, media: Callable[[], Media]):
        self._store = store
        self._media = media
        self._queued: set[tuple[str, str, int]] = set()
        self._todo: queue.Queue[tuple[str, str, int]] = queue.Queue()
        self._lock = threading.Lock()
        self._worker: threading.Thread | None = None

    def request(self, pid: str, scale: int) -> int:
        """Queue the copies the project still lacks at this scale; returns how many are still to be made.
        Each file is tried once per run, so one ffmpeg cannot read is not read again on every request."""
        if scale not in SCALES:
            raise AppError("預覽解析度只能是 1/2 或 1/4")
        self._media().require()
        with self._lock:
            ids = wanted(self._store.get(pid), scale)
            for item_id in ids:
                key = (pid, item_id, scale)
                if key not in self._queued:
                    self._queued.add(key)
                    self._todo.put(key)
            if ids and self._worker is None:
                self._worker = threading.Thread(target=self._run, name="lowres", daemon=True)
                self._worker.start()
            return len(ids)

    def _run(self) -> None:
        while True:
            try:
                pid, item_id, scale = self._todo.get(timeout=IDLE_EXIT)
            except queue.Empty:
                with self._lock:
                    if self._todo.empty():
                        self._worker = None
                        return
                continue
            try:
                self._make(pid, item_id, scale)
            except AppError as exc:  # the project was deleted meanwhile, or the file is unreadable
                log.warning("Preview copy of %s in %s skipped: %s", item_id, pid, exc.message)
            except Exception:
                log.exception("Preview copy of %s in %s failed", item_id, pid)

    def _make(self, pid: str, item_id: str, scale: int) -> None:
        project = self._store.get(pid)
        item = project["takes"].get(item_id) or project["media"].get(item_id)
        pdir = self._store.project_dir(pid)
        src = pdir / item["file"] if item else None
        if src is None or not src.is_file() or item.get(f"preview{scale}"):
            return
        dst = pdir / THUMB_DIR / f"{item_id}_p{scale}.mp4"
        part = dst.with_name(f"{dst.stem}.part.mp4")  # the browser never sees a half-written file
        dst.parent.mkdir(parents=True, exist_ok=True)
        self._media().run(transcode_args(src, part, scale), timeout=3 * 3600)
        part.replace(dst)
        self._store.apply(pid, [{"op": "item.derive", "id": item_id,
                                 "patch": {f"preview{scale}": f"{THUMB_DIR}/{dst.name}"}}],
                          origin="job", undoable=False)
        log.info("Made the 1/%d preview copy of %s in %s", scale, item_id, pid)


def transcode_args(src: Path, dst: Path, scale: int) -> list[str]:
    """H.264 at 1/scale of the picture size (even sides), a keyframe every 15 frames so seeking stays quick,
    and the first sound stream, which the preview plays from the same element."""
    return ["-i", str(src), "-map", "0:v:0", "-map", "0:a:0?", "-sn",
            "-vf", f"scale=trunc(iw/{scale}/2)*2:trunc(ih/{scale}/2)*2:flags=bilinear",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "26", "-g", "15", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(dst)]
