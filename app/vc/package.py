"""Zip a project for hand-off: storyboard, shot list, captions, active pictures and videos, audio, latest export.

A full backup also carries every referenced media file under its own media/ path, the AI assistant's
documents, review snapshots, voice-clean stems and the folders of the linked p5 pages (p5src/N/, listed in
manifest.json as p5Sources), so it can be imported on another computer and edited on; manifest.json says
which kind a package is."""
from __future__ import annotations

import json
import logging
import os
import zipfile
from pathlib import Path
from typing import Callable
from urllib.parse import quote

from . import VERSION
from .export import captions_srt, export_filename, shots_csv
from .exportdir import file_ref, project_exports, unique_path
from .p5src import linked_pages
from .projdocs import FOLDER as DOCS_DIR
from .review_pack import REVIEWS
from .sequence import video_take
from .storyboard import render_storyboard, shot_picture
from .util import AppError, replace_with_retry, safe_filename
from .voiceclean import CLEAN_DIR

log = logging.getLogger("vc.package")

_CHUNK = 1 << 20
FORMAT = "video-console-package"
_FILE_KEYS = ("file", "thumb", "strip", "wave")
P5_DIR = "p5src"
# Folders and files a full backup restores besides the media/ files package_layout lists.
EXTRA_PREFIXES = (f"{DOCS_DIR}/", f"{REVIEWS}/", f"{CLEAN_DIR}/", f"{P5_DIR}/")
_P5_SKIP = {"node_modules", "__pycache__", "venv"}
P5_MAX = 2 * 1024 ** 3  # a p5 page folder is scripts, fonts and pictures; past this it is not one


def _entry(prefix: str, index: int, name: str, ext: str) -> str:
    """Archive name such as images/03_開場.png (number first so files sort in shot order)."""
    cleaned = safe_filename(str(name or "").replace("/", "_").replace("\\", "_"), "", 40).replace(" ", "_")
    stem = f"{index + 1:02d}_{cleaned}" if cleaned else f"{index + 1:02d}"
    return f"{prefix}/{stem}{ext.lower()}"


def _latest_export(project_dir: Path) -> Path | None:
    videos = [p for p in project_exports(project_dir) if p.suffix.lower() == ".mp4"]
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


def _tree(root: Path, prefix: str, limit: int | None = None) -> list[tuple[str, Path]] | None:
    """(prefix/relative name, path) of every file under root, hidden and half-written ones left out;
    None once the files add up to more than limit bytes."""
    out: list[tuple[str, Path]] = []
    total = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in _P5_SKIP and not d.startswith("."))
        for name in sorted(filenames):
            if not name.startswith(".") and not name.endswith(".part"):
                path = Path(dirpath) / name
                total += path.stat().st_size if limit is not None else 0
                if limit is not None and total > limit:
                    return None
                out.append((f"{prefix}/{path.relative_to(root).as_posix()}", path))
    return out


def live_audio_clips(project: dict) -> set[str]:
    """Ids of the audio clips on the timeline or parked on a shot: the ones whose voice-clean stems are used."""
    parked = [a for s in project.get("shots") or [] for a in (s.get("parked") or {}).get("audio") or []]
    return {a["id"] for a in [*(project.get("audio") or []), *parked] if isinstance(a, dict) and a.get("id")}


def backup_extras(project: dict, project_dir: Path, p5: bool = True) -> tuple[list[tuple[str, Path]], list[dict], list[str]]:
    """What a full backup holds besides media files: (archive name, path) pairs, the p5 source folders
    [{dir: "p5src/1", from: original folder}] (when p5), and the p5 folders it could not take (missing or
    too big)."""
    files = [*_tree(project_dir / DOCS_DIR, DOCS_DIR), *_tree(project_dir / REVIEWS, REVIEWS)]
    live = live_audio_clips(project)
    clean = project_dir / CLEAN_DIR
    for path in sorted(clean.glob("*.wav")) if clean.is_dir() else []:
        if not path.name.startswith(".") and path.name.split("-")[0] in live:
            files.append((f"{CLEAN_DIR}/{path.name}", path))
    sources: list[dict] = []
    left: list[str] = []
    roots = list(dict.fromkeys(Path(page).parent for page in linked_pages(project))) if p5 else []
    for root in roots:
        tree = _tree(root, f"{P5_DIR}/{len(sources) + 1}", P5_MAX) if root.is_dir() else None
        if not tree:
            left.append(str(root))
            continue
        sources.append({"dir": f"{P5_DIR}/{len(sources) + 1}", "from": str(root)})
        files.extend(tree)
    return files, sources, left


def plan_package(project: dict, project_dir: Path, opts: dict) -> tuple[list[tuple[str, Path]], dict[str, str], int]:
    """(archive name, source path) pairs, the storyboard picture per shot id, and the count of missing files.
    A full backup's extras (backup_extras) are not included."""
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
    out_dir: Path | None = None,
) -> dict:
    """Write <out_dir>/<name>_<time>_package.zip (default the project's exports/) and return
    {file, name, size, count, missing, p5Sources, p5Left}."""
    opts = {"includeVideos": True, "includeExport": True, "fullBackup": False, "includeP5": True, **(opts or {})}
    files, pictures, missing = plan_package(project, project_dir, opts)
    sources: list[dict] = []
    left: list[str] = []
    if opts["fullBackup"]:
        extras, sources, left = backup_extras(project, project_dir, bool(opts["includeP5"]))
        taken = {arc for arc, _ in files}
        files.extend((arc, path) for arc, path in extras if arc not in taken)
    texts = {
        "storyboard.html": render_storyboard(project, lambda shot: quote(pictures[shot["id"]])
                                             if shot["id"] in pictures else None),
        "shots.csv": shots_csv(project),
        "project.json": json.dumps(project, ensure_ascii=False, indent=2),
        "manifest.json": json.dumps({"format": FORMAT, "version": 1, "app": VERSION,
                                     "fullBackup": bool(opts["fullBackup"]),
                                     "includeVideos": bool(opts["includeVideos"]),
                                     "p5Sources": sources}, ensure_ascii=False, indent=2),
    }
    srt = captions_srt(project)
    if srt:
        texts["captions.srt"] = srt

    out_dir = out_dir or project_dir / "exports"
    out_dir.mkdir(parents=True, exist_ok=True)
    final = unique_path(out_dir, Path(export_filename(project)).stem + "_package.zip")
    name = final.name
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
    return {"file": file_ref(final, project_dir), "name": name, "size": final.stat().st_size,
            "count": len(files) + len(texts), "missing": missing, "p5Sources": len(sources), "p5Left": left}
