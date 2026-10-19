"""Zip a project for hand-off: storyboard, shot list, captions, active pictures and videos, audio, latest export.

A full backup also carries every referenced media file under its own media/ path, so it can be imported
back without loss; manifest.json says which kind a package is."""
from __future__ import annotations

import json
import logging
import zipfile
from pathlib import Path
from typing import Callable
from urllib.parse import quote

from . import VERSION
from .export import captions_srt, export_filename, shots_csv
from .sequence import video_take
from .storyboard import render_storyboard, shot_picture
from .util import AppError, replace_with_retry, safe_filename

log = logging.getLogger("vc.package")

_CHUNK = 1 << 20
FORMAT = "video-console-package"
_FILE_KEYS = ("file", "thumb", "strip", "wave")


def _entry(prefix: str, index: int, name: str, ext: str) -> str:
    """Archive name such as images/03_開場.png (number first so files sort in shot order)."""
    cleaned = safe_filename(str(name or "").replace("/", "_").replace("\\", "_"), "", 40).replace(" ", "_")
    stem = f"{index + 1:02d}_{cleaned}" if cleaned else f"{index + 1:02d}"
    return f"{prefix}/{stem}{ext.lower()}"


def _latest_export(project_dir: Path) -> Path | None:
    videos = [p for p in (project_dir / "exports").glob("*.mp4") if p.is_file()]
    return max(videos, key=lambda p: p.stat().st_mtime) if videos else None


def package_layout(project: dict, opts: dict) -> list[tuple[str, str, str | None]]:
    """(archive name, project-relative path, shot id of a storyboard picture) for every media file a
    package holds. Import replays this on the packaged project.json to map entries back to media/ paths."""
    out: list[tuple[str, str, str | None]] = []
    if opts.get("fullBackup"):
        seen: set[str] = set()
        for item in [*(project.get("takes") or {}).values(), *(project.get("media") or {}).values()]:
            for key in _FILE_KEYS:
                rel = item.get(key)
                if isinstance(rel, str) and rel not in seen:
                    seen.add(rel)
                    out.append((rel, rel, None))
        for shot in project.get("shots") or []:
            rel = shot_picture(project, shot)
            if rel:
                out.append((rel, rel, shot["id"]))
        return out
    for i, shot in enumerate(project.get("shots") or []):
        name = shot.get("name") or ""
        rel = shot_picture(project, shot)
        if rel:
            out.append((_entry("images", i, name, Path(rel).suffix), rel, shot["id"]))
        vid = video_take(project, shot)
        if opts.get("includeVideos", True) and vid:
            out.append((_entry("videos", i, name, Path(vid["file"]).suffix), vid["file"], None))
    seen_media: set[str] = set()
    for clip in project.get("audio") or []:
        item = (project.get("media") or {}).get(clip.get("mediaId"))
        if item and item["id"] not in seen_media and item.get("file"):
            seen_media.add(item["id"])
            out.append((_entry("audio", len(seen_media) - 1, Path(item.get("name") or "").stem,
                               Path(item["file"]).suffix), item["file"], None))
    seen_overlay: set[str] = set()
    for clip in project.get("overlays") or []:
        item = (project.get("media") or {}).get(clip.get("mediaId"))
        if item and item["id"] not in seen_overlay and item.get("file"):
            seen_overlay.add(item["id"])
            out.append((_entry("overlays", len(seen_overlay) - 1, Path(item.get("name") or "").stem,
                               Path(item["file"]).suffix), item["file"], None))
    return out


def plan_package(project: dict, project_dir: Path, opts: dict) -> tuple[list[tuple[str, Path]], dict[str, str], int]:
    """(archive name, source path) pairs, the storyboard picture per shot id, and the count of missing files."""
    files: list[tuple[str, Path]] = []
    pictures: dict[str, str] = {}
    missing: set[str] = set()
    added: set[str] = set()
    for arc, rel, shot_id in package_layout(project, opts):
        path = project_dir / rel
        if not path.is_file():
            missing.add(arc)
            continue
        if shot_id:
            pictures[shot_id] = arc
        if arc not in added:
            added.add(arc)
            files.append((arc, path))
    if opts.get("includeExport", True):
        latest = _latest_export(project_dir)
        if latest:
            files.append((latest.name, latest))
    return files, pictures, len(missing)


def build_package(
    project: dict,
    project_dir: Path,
    opts: dict | None = None,
    on_progress: Callable[[float], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> dict:
    """Write exports/<name>_<time>_package.zip and return {file, name, size, count, missing}."""
    opts = {"includeVideos": True, "includeExport": True, "fullBackup": False, **(opts or {})}
    files, pictures, missing = plan_package(project, project_dir, opts)
    texts = {
        "storyboard.html": render_storyboard(project, lambda shot: quote(pictures[shot["id"]])
                                             if shot["id"] in pictures else None),
        "shots.csv": shots_csv(project),
        "project.json": json.dumps(project, ensure_ascii=False, indent=2),
        "manifest.json": json.dumps({"format": FORMAT, "version": 1, "app": VERSION,
                                     "fullBackup": bool(opts["fullBackup"]),
                                     "includeVideos": bool(opts["includeVideos"])}, ensure_ascii=False, indent=2),
    }
    srt = captions_srt(project)
    if srt:
        texts["captions.srt"] = srt

    out_dir = project_dir / "exports"
    out_dir.mkdir(parents=True, exist_ok=True)
    name = Path(export_filename(project)).stem + "_package.zip"
    final = out_dir / name
    part = out_dir / f".{name}.part"
    total = max(1, sum(path.stat().st_size for _, path in files))
    done = 0
    try:
        with zipfile.ZipFile(part, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for arc, text in texts.items():
                zf.writestr(arc, text.encode("utf-8"))
            for arc, path in files:
                # Pictures and videos are already compressed; storing them keeps packing fast.
                info = zipfile.ZipInfo.from_file(path, arc)
                info.compress_type = zipfile.ZIP_STORED
                with path.open("rb") as src, zf.open(info, "w") as dst:
                    while chunk := src.read(_CHUNK):
                        if cancelled and cancelled():
                            raise AppError("已取消", 499)
                        dst.write(chunk)
                        done += len(chunk)
                        if on_progress:
                            on_progress(done / total)
        replace_with_retry(part, final)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    log.info("Packaged %s (%d files, %d missing)", name, len(files) + len(texts), missing)
    return {"file": f"exports/{name}", "name": name, "size": final.stat().st_size,
            "count": len(files) + len(texts), "missing": missing}
