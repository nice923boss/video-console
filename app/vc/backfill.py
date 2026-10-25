"""Fill in what older imports lack, on a background thread when a project is opened: the waveform drawn under
video clips (only made at import before), the alpha marker that keeps see-through WebM clips see-through in
the export (only recorded at import before), the thumbnail and filmstrip of files imported while ffmpeg was
missing or could not read them (PF-03; the browser then decodes the full file on every open), and the
per-character times karaoke captions follow in voice-overs made before CP-13. What is made is saved in
media/thumbs or the project, so it is made once. Each item is tried once per run, so a file ffmpeg cannot read is
not read again on every open."""
from __future__ import annotations

import json
import logging
import queue
import threading
from pathlib import Path
from typing import Callable

from .chartimes import char_times
from .media import Media
from .store import ProjectStore
from .takes import THUMB_DIR
from .util import AppError

log = logging.getLogger("vc.backfill")

IDLE_EXIT = 5.0  # seconds the worker waits for more before it stops


def _no_thumb(item: dict) -> bool:
    """An image shown from its full file, or a video without a thumbnail or filmstrip."""
    if item.get("kind") == "image":
        return item.get("thumb") in (None, "", item["file"])
    return not item.get("thumb") or (float(item.get("duration") or 0) > 0 and not item.get("strip"))


def _no_char_times(item: dict) -> bool:
    """A voice-over (voice.py) without its per-character times."""
    source = item.get("source") or {}
    return (item.get("kind") == "audio" and source.get("backend") in ("audio8voice", "voai")
            and bool(source.get("text")) and "charTimes" not in item)


def missing(project: dict) -> list[str]:
    """Video takes and media items without a waveform although they have sound, WebM files without an
    alpha marker (which may simply mean they are not see-through), images and videos without their
    thumbnail or filmstrip, and voice-overs without per-character times."""
    ids = []
    for group in ("takes", "media"):
        for item in project.get(group, {}).values():
            if _no_char_times(item) and item.get("file"):
                ids.append(item["id"])
                continue
            if item.get("kind") not in ("video", "image") or not item.get("file"):
                continue
            if item["kind"] == "image":
                if _no_thumb(item):
                    ids.append(item["id"])
                continue
            no_wave = item.get("hasAudio") and not item.get("wave")
            unchecked = str(item["file"]).lower().endswith(".webm") and not item.get("alphaCodec")
            if no_wave or unchecked or _no_thumb(item):
                ids.append(item["id"])
    return ids


class Backfill:
    def __init__(self, store: ProjectStore, media: Callable[[], Media]):
        self._store = store
        self._media = media
        self._tried: set[tuple[str, str]] = set()
        self._todo: queue.Queue[tuple[str, list[str]]] = queue.Queue()
        self._lock = threading.Lock()
        self._worker: threading.Thread | None = None

    def check(self, pid: str) -> None:
        """Queue what the project still lacks and return at once. Without ffmpeg nothing is marked as tried,
        so the items are picked up once it is installed."""
        if not self._media().available:
            return
        with self._lock:
            ids = [i for i in missing(self._store.get(pid)) if (pid, i) not in self._tried]
            if not ids:
                return
            self._tried.update((pid, i) for i in ids)
            self._todo.put((pid, ids))
            if self._worker is None:
                self._worker = threading.Thread(target=self._run, name="backfill", daemon=True)
                self._worker.start()

    def _run(self) -> None:
        while True:
            try:
                pid, ids = self._todo.get(timeout=IDLE_EXIT)
            except queue.Empty:
                with self._lock:
                    if self._todo.empty():
                        self._worker = None
                        return
                continue
            for item_id in ids:
                try:
                    self._fill(pid, item_id)
                except AppError as exc:  # the project was deleted meanwhile, or the file is unreadable
                    log.warning("Backfill of %s in %s skipped: %s", item_id, pid, exc.message)
                except Exception:
                    log.exception("Backfill of %s in %s failed", item_id, pid)

    def _fill(self, pid: str, item_id: str) -> None:
        project = self._store.get(pid)
        item = project["takes"].get(item_id) or project["media"].get(item_id)
        pdir = self._store.project_dir(pid)
        path = pdir / item["file"] if item else None
        if path is None or not path.is_file():
            return
        media = self._media()
        patch = {}
        if _no_char_times(item):
            patch["charTimes"] = char_times(path, item["source"]["text"], media.pcm16)
        if path.suffix.lower() == ".webm" and not item.get("alphaCodec"):
            probe = media.probe(path)
            if probe.get("alpha"):
                patch["alphaCodec"] = probe["vcodec"]
        if item.get("hasAudio") and not item.get("wave"):
            wave = pdir / THUMB_DIR / f"{item_id}_wave.json"
            peaks, duration, level = media.waveform(path)
            wave.parent.mkdir(parents=True, exist_ok=True)
            wave.write_text(json.dumps({"peaks": peaks, "duration": duration, "level": level}), encoding="utf-8")
            patch["wave"] = f"{THUMB_DIR}/{wave.name}"
        if _no_thumb(item):
            patch.update(self._thumbs(media, path, pdir / THUMB_DIR, item))
        if patch:
            self._store.apply(pid, [{"op": "item.derive", "id": item_id, "patch": patch}], origin="job",
                              undoable=False)
            log.info("Backfilled %s for %s in %s", ", ".join(patch), item_id, pid)

    @staticmethod
    def _thumbs(media: Media, path: Path, thumbs: Path, item: dict) -> dict:
        """The thumbnail (and a video's filmstrip) as takes.py makes them at import; each part on its own,
        so one that fails does not keep the other back."""
        thumbs.mkdir(parents=True, exist_ok=True)
        made = {}
        duration = float(item.get("duration") or 0)
        if item["kind"] == "image" or not item.get("thumb"):
            thumb = thumbs / f"{item['id']}.jpg"
            try:
                at = round(duration * 0.4, 2) if item["kind"] == "video" and duration > 1 else None
                media.thumbnail(path, thumb, at=at)
                made["thumb"] = f"{THUMB_DIR}/{thumb.name}"
            except AppError as exc:
                log.warning("Thumbnail backfill failed for %s: %s", path.name, exc.message)
        if item["kind"] == "video" and duration > 0 and not item.get("strip"):
            strip = thumbs / f"{item['id']}_strip.jpg"
            try:
                media.strip(path, strip, duration)
                made["strip"] = f"{THUMB_DIR}/{strip.name}"
            except AppError as exc:
                log.warning("Filmstrip backfill failed for %s: %s", path.name, exc.message)
        return made
