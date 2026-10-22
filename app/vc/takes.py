"""Bring generated or imported files into a project: final location, browser-safe video, thumbnails, metadata."""
from __future__ import annotations

import json
import logging
import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from .media import Media, image_size
from .util import AppError, kind_of, new_id, now

if TYPE_CHECKING:
    from .store import ProjectStore

log = logging.getLogger("vc.takes")

TAKE_DIR = "media/takes"
IMPORT_DIR = "media/import"
THUMB_DIR = "media/thumbs"
MAX_REF_BYTES = 4 * 1024 * 1024
MAX_REF_SIDE = 2048
_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
         ".gif": "image/gif", ".bmp": "image/bmp"}


def stage_file(
    media: Media,
    project_dir: Path,
    src: Path,
    folder: str,
    item_id: str,
    *,
    cancelled: Callable[[], bool] | None = None,
    on_progress: Callable[[float, str], None] | None = None,
) -> dict:
    """Move src to project_dir/folder/<item_id><ext> and describe it.

    Videos the browser cannot play are re-encoded to H.264 MP4 (the original is removed). Returns the
    file part of a take / media item: kind, file, thumb, strip, wave, width, height, duration, fps, hasAudio.
    """
    kind = kind_of(src.name)
    if kind is None:
        raise AppError(f"不支援的檔案類型：{src.suffix or '（沒有副檔名）'}", 415)
    dst_dir = project_dir / folder
    dst_dir.mkdir(parents=True, exist_ok=True)
    thumbs = project_dir / THUMB_DIR
    thumbs.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / f"{item_id}{src.suffix.lower()}"
    shutil.move(str(src), dst)
    info: dict = {"kind": kind, "file": f"{folder}/{dst.name}", "thumb": None, "strip": None, "width": 0,
                  "height": 0, "duration": 0.0, "fps": 0.0, "hasAudio": False}
    try:
        if kind == "image":
            _stage_image(media, dst, thumbs / f"{item_id}.jpg", info)
        elif kind == "video":
            dst = _stage_video(media, dst, thumbs, item_id, info, cancelled, on_progress)
            info["file"] = f"{folder}/{dst.name}"
        else:
            _stage_audio(media, dst, thumbs / f"{item_id}_wave.json", info)
    except BaseException:
        dst.unlink(missing_ok=True)
        for leftover in thumbs.glob(f"{item_id}*"):
            leftover.unlink(missing_ok=True)
        raise
    return info


def _stage_image(media: Media, path: Path, thumb: Path, info: dict) -> None:
    size = image_size(path)
    if size is None and media.available:
        probe = media.probe(path)
        size = (probe["width"], probe["height"])
    if not size or not size[0]:
        raise AppError("無法讀取這張圖片（檔案可能損毀）", 415)
    info["width"], info["height"] = size
    if media.available:
        try:
            media.thumbnail(path, thumb)
            info["thumb"] = f"{THUMB_DIR}/{thumb.name}"
            return
        except AppError as exc:
            log.warning("Thumbnail failed for %s: %s", path.name, exc.message)
    # No ffmpeg (or it could not read the format): the browser shows the full image instead.
    info["thumb"] = info["file"]


def _stage_video(media: Media, path: Path, thumbs: Path, item_id: str, info: dict,
                 cancelled: Callable[[], bool] | None, on_progress: Callable[[float, str], None] | None) -> Path:
    media.require()
    probe = media.probe(path)
    if not probe["hasVideo"]:
        raise AppError("這個影片檔沒有畫面軌", 415)
    if not media.web_playable(probe, path):
        if on_progress:
            on_progress(0.97, "轉換成瀏覽器可播放的格式")
        web = path.with_name(f"{item_id}.web.mp4")
        try:
            media.to_web_video(path, web, cancelled)
        except BaseException:
            web.unlink(missing_ok=True)
            raise
        path.unlink(missing_ok=True)
        final = path.with_name(f"{item_id}.mp4")
        web.replace(final)
        path = final
        probe = media.probe(path)
    duration = float(probe["duration"] or 0.0)
    info.update(width=probe["width"], height=probe["height"], duration=round(duration, 3),
                fps=round(float(probe["fps"] or 0.0), 3), hasAudio=bool(probe["hasAudio"]))
    thumb = thumbs / f"{item_id}.jpg"
    try:
        media.thumbnail(path, thumb, at=round(duration * 0.4, 2) if duration > 1 else None)
        info["thumb"] = f"{THUMB_DIR}/{thumb.name}"
        if duration > 0:
            strip = thumbs / f"{item_id}_strip.jpg"
            media.strip(path, strip, duration)
            info["strip"] = f"{THUMB_DIR}/{strip.name}"
    except AppError as exc:
        log.warning("Video thumbnails failed for %s: %s", path.name, exc.message)
    if info["hasAudio"]:
        # drawn under the picture clip on the timeline; a failure only loses the drawing, not the import
        wave = thumbs / f"{item_id}_wave.json"
        try:
            peaks, wave_duration = media.waveform(path)
            wave.write_text(json.dumps({"peaks": peaks, "duration": wave_duration}), encoding="utf-8")
            info["wave"] = f"{THUMB_DIR}/{wave.name}"
        except AppError as exc:
            log.warning("Video waveform failed for %s: %s", path.name, exc.message)
    return path


def _stage_audio(media: Media, path: Path, wave: Path, info: dict) -> None:
    media.require()
    probe = media.probe(path)
    if not probe["hasAudio"]:
        raise AppError("這個檔案裡沒有聲音軌", 415)
    peaks, duration = media.waveform(path)
    wave.write_text(json.dumps({"peaks": peaks, "duration": duration}), encoding="utf-8")
    info.update(kind="audio", duration=round(float(probe["duration"] or duration), 3), hasAudio=True,
                wave=f"{THUMB_DIR}/{wave.name}")


def new_take(shot_id: str | None, staged: dict, *, source: dict, prompt: str = "", negative: str = "",
             seed: int | None = None, params: dict | None = None, parent: str | None = None,
             job_id: str | None = None, label: str = "", take_id: str | None = None) -> dict:
    take = {
        "id": take_id or new_id("t"),
        "shot": shot_id,
        **{k: staged.get(k) for k in ("kind", "file", "thumb", "strip", "width", "height", "duration", "fps",
                                       "hasAudio")},
        "created": now(),
        "source": source,
        "prompt": prompt,
        "negative": negative,
        "seed": seed,
        "params": params or {},
        "parent": parent,
        "favorite": False,
        "rating": 0,
        "note": "",
        "label": label,
        "jobId": job_id,
    }
    if staged.get("wave"):
        take["wave"] = staged["wave"]
    return take


def new_media(staged: dict, name: str, media_id: str) -> dict:
    item = {"id": media_id, "name": name[:200], "created": now(), "favorite": False, "note": ""}
    item.update({k: staged.get(k) for k in ("kind", "file", "thumb", "strip", "width", "height", "duration", "fps",
                                             "hasAudio")})
    if staged.get("wave"):
        item["wave"] = staged["wave"]
    return item


# ---------------------------------------------------------------- references
def find_item(project: dict, item_id: str | None) -> dict | None:
    """A take or an imported media item by id."""
    if not item_id:
        return None
    return project["takes"].get(item_id) or project["media"].get(item_id)


def item_path(project_dir: Path, item: dict) -> Path:
    path = project_dir / item["file"]
    if not path.is_file():
        raise AppError(f"找不到檔案 {item['file']}（可能已被刪除或搬移）", 410)
    return path


def reference_image(media: Media, path: Path) -> tuple[bytes, str]:
    """Bytes + MIME type of a reference image, shrunk when it is too large to upload comfortably."""
    data = path.read_bytes()
    ext = path.suffix.lower()
    size = image_size(path) or (0, 0)
    too_big = len(data) > MAX_REF_BYTES or max(size) > MAX_REF_SIDE
    if (not too_big and ext in (".png", ".jpg", ".jpeg", ".webp")) or not media.available:
        return data, _MIME.get(ext, "image/png")
    with tempfile.TemporaryDirectory(prefix="vc-ref-") as tmp:
        out = Path(tmp) / "ref.png"
        scale = f"scale='min({MAX_REF_SIDE},iw)':'min({MAX_REF_SIDE},ih)':force_original_aspect_ratio=decrease"
        media.run(["-i", str(path), "-frames:v", "1", "-vf", scale, str(out)], timeout=120)
        result = out.read_bytes()
        if len(result) > MAX_REF_BYTES:
            out = Path(tmp) / "ref.jpg"
            media.run(["-i", str(path), "-frames:v", "1", "-vf", scale, "-q:v", "3", str(out)], timeout=120)
            return out.read_bytes(), "image/jpeg"
        return result, "image/png"


# ---------------------------------------------------------------- shots
def find_shot(project: dict, shot_id: str | None) -> tuple[int, dict]:
    for i, shot in enumerate(project["shots"]):
        if shot["id"] == shot_id:
            return i, shot
    raise AppError("找不到這個鏡頭（可能已被刪除）", 404)


def shot_label(index: int, shot: dict) -> str:
    name = (shot.get("name") or "").strip()
    return f"#{index + 1} {name}" if name else f"鏡頭 {index + 1}"


def take_ops(project: dict, takes: list[dict], slot: str | None = None, extra: dict | None = None) -> list[dict]:
    """Ops adding finished takes; the first one becomes its shot's `slot` (activeImage / activeVideo) plus `extra`.

    A take whose shot was deleted while the job ran is kept as a loose take (shot None).
    """
    shots = {s["id"] for s in project["shots"]}
    ops: list[dict] = []
    for i, take in enumerate(takes):
        if take.get("shot") not in shots:
            take = {**take, "shot": None}
        ops.append({"op": "take.add", "take": take})
        patch = {**({slot: take["id"]} if slot else {}), **(extra or {})} if i == 0 else {}
        if patch and take["shot"]:
            ops.append({"op": "shot.update", "id": take["shot"], "patch": patch})
    return ops


def commit_takes(store: ProjectStore, pid: str, takes: list[dict], slot: str | None = None,
                 extra: dict | None = None) -> None:
    """Record a job's takes in the project (not undoable: undo is for the user's own edits)."""
    store.apply(pid, take_ops(store.get(pid), takes, slot, extra), origin="job", undoable=False)
