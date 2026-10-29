"""Plain audio copies for the preview's sound (PB-01). The preview plays every sound through Web Audio from
decoded sample blocks, which it reads from these files by byte range: 16-bit stereo 48 kHz WAV, the rate and
channel layout the export mixes at (a mono source is spread to both sides at -3 dB, as the export's aformat
does). Sample 0 is media time 0, as the browser's own player counts it, so a file whose sound starts late or
early lines up. The copies are made on a background thread, saved in media/thumbs and recorded on the take or
media item (previewAudio), like the smaller video copies of lowres.py; the export always reads the original.
A sound used at another speed gets a copy at that speed too (P2-2: previewAudioSpeeds, by speed_key), stretched by
the export's own atempo chain so its pitch stays, whose sample k is media time k * speed."""
from __future__ import annotations

import logging
import math
import queue
import threading
from pathlib import Path
from typing import Callable

from .export import _atempo
from .media import Media
from .store import ProjectStore
from .takes import THUMB_DIR
from .util import AppError

log = logging.getLogger("vc.audioproxy")

RATE = 48000
IDLE_EXIT = 5.0  # seconds the worker waits for more before it stops


def speed_key(speed: float) -> str:
    """The name a speed's copy goes by: rounded to 4 decimals, halves up, no trailing zeros. The preview's speedKey
    does the same arithmetic, so both name a speed alike (string formatting rounds halves differently in each)."""
    return f"{math.floor(speed * 10000 + 0.5) / 10000:.4f}".rstrip("0").rstrip(".")


def _uses(project: dict) -> list[tuple[str, str]]:
    """(item id, speed key) of each sound the preview plays: active shot videos, picture-in-picture clips, audio
    clips and detached shot sounds."""
    uses = [(s.get("activeVideo"), float(s.get("speed") or 1.0)) for s in project.get("shots", []) if not s.get("disabled")]
    uses += [(o.get("mediaId"), 1.0) for o in project.get("overlays", []) if not o.get("disabled")]
    for a in project.get("audio", []):
        uses.append((a.get("mediaId") if a.get("mediaId") in project["media"] else a.get("takeId"),
                     float(a.get("speed") or 1.0)))
    return [(item_id, speed_key(speed)) for item_id, speed in uses if item_id]


def _made(item: dict, key: str) -> str | None:
    return item.get("previewAudio") if key == "1" else (item.get("previewAudioSpeeds") or {}).get(key)


def wanted(project: dict) -> list[tuple[str, str]]:
    """(item id, speed key) of the played sounds whose file has no audio copy at that speed yet; "1" is the
    plain copy."""
    out: list[tuple[str, str]] = []
    for item_id, key in _uses(project):
        item = project["takes"].get(item_id) or project["media"].get(item_id)
        if item and item.get("file") and item.get("hasAudio") and not _made(item, key) and (item_id, key) not in out:
            out.append((item_id, key))
    return out


class AudioProxy:
    def __init__(self, store: ProjectStore, media: Callable[[], Media]):
        self._store = store
        self._media = media
        self._queued: set[tuple[str, str, str]] = set()
        self._todo: queue.Queue[tuple[str, str, str]] = queue.Queue()
        self._lock = threading.Lock()
        self._worker: threading.Thread | None = None

    def request(self, pid: str) -> int:
        """Queue the copies the project still lacks; returns how many are still to be made. Each file is tried
        once per run, so one ffmpeg cannot read is not read again on every request."""
        self._media().require()
        with self._lock:
            ids = wanted(self._store.get(pid))
            for item_id, speed in ids:
                key = (pid, item_id, speed)
                if key not in self._queued:
                    self._queued.add(key)
                    self._todo.put(key)
            if ids and self._worker is None:
                self._worker = threading.Thread(target=self._run, name="audioproxy", daemon=True)
                self._worker.start()
            return len(ids)

    def _run(self) -> None:
        while True:
            try:
                pid, item_id, speed = self._todo.get(timeout=IDLE_EXIT)
            except queue.Empty:
                with self._lock:
                    if self._todo.empty():
                        self._worker = None
                        return
                continue
            try:
                self._make(pid, item_id, speed)
            except AppError as exc:  # the project was deleted meanwhile, or the file is unreadable
                log.warning("Audio copy of %s in %s skipped: %s", item_id, pid, exc.message)
            except Exception:
                log.exception("Audio copy of %s in %s failed", item_id, pid)

    def _make(self, pid: str, item_id: str, speed: str) -> None:
        project = self._store.get(pid)
        item = project["takes"].get(item_id) or project["media"].get(item_id)
        pdir = self._store.project_dir(pid)
        src = pdir / item["file"] if item else None
        if src is None or not src.is_file() or _made(item, speed):
            return
        dst = pdir / THUMB_DIR / (f"{item_id}_a.wav" if speed == "1" else f"{item_id}_a_{speed}.wav")
        part = dst.with_name(f"{dst.stem}.part.wav")  # the browser never reads a half-written file
        dst.parent.mkdir(parents=True, exist_ok=True)
        self._media().run(transcode_args(src, part, float(speed)), timeout=3 * 3600)
        part.replace(dst)
        rel = f"{THUMB_DIR}/{dst.name}"
        if speed == "1":
            patch = {"previewAudio": rel}
        else:  # copies at speeds nothing plays any more are dropped, their files left to the cleanup
            project = self._store.get(pid)
            item = project["takes"].get(item_id) or project["media"].get(item_id) or {}
            used = {k for i, k in _uses(project) if i == item_id}
            kept = {k: v for k, v in (item.get("previewAudioSpeeds") or {}).items() if k in used}
            patch = {"previewAudioSpeeds": {**kept, speed: rel}}
        self._store.apply(pid, [{"op": "item.derive", "id": item_id, "patch": patch}], origin="job", undoable=False)
        log.info("Made the audio copy of %s at speed %s in %s", item_id, speed, pid)


def transcode_args(src: Path, dst: Path, speed: float = 1.0) -> list[str]:
    """The first sound stream as 16-bit stereo 48 kHz PCM, played at speed. first_pts=0 pads or trims the start so
    the file's sample 0 is media time 0 (a stream that starts after the picture, or AAC's encoder delay)."""
    return ["-i", str(src), "-map", "0:a:0", "-vn", "-sn", "-dn", "-map_metadata", "-1",
            "-af", f"aresample={RATE}:first_pts=0,{_atempo(speed)}"
                   f"aformat=sample_fmts=s16:sample_rates={RATE}:channel_layouts=stereo",
            "-c:a", "pcm_s16le", str(dst)]
