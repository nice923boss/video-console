"""Bring a package made by package.py back in: extract its media and a project.json that only references them.

A full backup restores every take and media file. A hand-off package only holds the active pictures,
videos and audio, so takes and media whose files it lacks are dropped from the imported project."""
from __future__ import annotations

import json
import logging
import shutil
import zipfile
from pathlib import Path

from .ops import _MEDIA_PATH
from .package import FORMAT, package_layout
from .util import AppError, is_within

log = logging.getLogger("vc.unpack")

_CHUNK = 1 << 20


def _read_json(zf: zipfile.ZipFile, name: str) -> object:
    try:
        return json.loads(zf.read(name).decode("utf-8"))
    except (KeyError, ValueError, UnicodeDecodeError):
        return None


def _valid_project(project: object) -> bool:
    return (isinstance(project, dict) and isinstance(project.get("settings"), dict)
            and isinstance(project.get("shots"), list)
            and all(isinstance(s, dict) and isinstance(s.get("id"), str) for s in project["shots"]))


def _prune(project: dict, restored: set[str]) -> int:
    """Drop takes / media whose file did not come out of the package and every reference to them;
    returns how many were dropped. Missing thumbnails fall back to the picture itself, or to none."""
    dropped = 0
    for key in ("takes", "media"):
        items = project.get(key) if isinstance(project.get(key), dict) else {}
        kept = {}
        for item_id, item in items.items():
            if not isinstance(item, dict) or item.get("file") not in restored:
                dropped += 1
                continue
            for extra in ("thumb", "strip", "wave"):
                if item.get(extra) and item[extra] not in restored:
                    item[extra] = item["file"] if extra == "thumb" and item.get("kind") == "image" else None
            kept[item_id] = item
        project[key] = kept
    for shot in project["shots"]:
        for key in ("activeImage", "activeVideo"):
            if shot.get(key) not in project["takes"]:
                shot[key] = None
        if shot.get("ref") not in project["takes"] and shot.get("ref") not in project["media"]:
            shot["ref"] = None
        shot["extraRefs"] = [r for r in shot.get("extraRefs") or [] if r in project["takes"] or r in project["media"]]
    for member in project.get("cast") or []:
        if isinstance(member, dict) and member.get("lookRef") not in project["takes"] and member.get("lookRef") not in project["media"]:
            member["lookRef"] = None
    audio = project.get("audio") if isinstance(project.get("audio"), list) else []
    project["audio"] = [c for c in audio if isinstance(c, dict) and c.get("mediaId") in project["media"]]
    layers = project.get("layers") if isinstance(project.get("layers"), list) else []
    project["layers"] = [x for x in layers if isinstance(x, dict) and x.get("id")]
    ids = {x["id"] for x in project["layers"]}
    overlays = project.get("overlays") if isinstance(project.get("overlays"), list) else []
    project["overlays"] = [o for o in overlays if isinstance(o, dict) and o.get("mediaId") in project["media"]
                           and o.get("layer") in ids]
    tracks = project.get("captionTracks") if isinstance(project.get("captionTracks"), list) else []
    project["captionTracks"] = [x for x in tracks if isinstance(x, dict) and x.get("id")]
    track_ids = {x["id"] for x in project["captionTracks"]}
    for cap in project.get("captions") or []:
        if isinstance(cap, dict) and cap.get("track") and cap["track"] not in track_ids:
            cap["track"] = None
    return dropped


def unpack_package(zip_path: Path, dst: Path) -> dict:
    """Extract the package at zip_path into the empty folder dst.

    Returns {"project", "fullBackup", "files", "dropped"}; the project still carries its old id."""
    try:
        zf = zipfile.ZipFile(zip_path)
    except (zipfile.BadZipFile, OSError):
        raise AppError("這不是有效的 ZIP 檔", 400) from None
    with zf:
        names = set(zf.namelist())
        project = _read_json(zf, "project.json")
        if not _valid_project(project):
            raise AppError("ZIP 裡沒有可用的 project.json，不是 Video Console 打包的專案", 400)
        manifest = _read_json(zf, "manifest.json") if "manifest.json" in names else None
        manifest = manifest if isinstance(manifest, dict) and manifest.get("format") == FORMAT else {}
        full = bool(manifest.get("fullBackup"))
        restored: set[str] = set()
        for arc, rel, _ in package_layout(project, {"fullBackup": full, "includeVideos": True}):
            if rel in restored or arc not in names:
                continue
            target = dst / rel
            if not _MEDIA_PATH.match(rel) or ".." in rel.split("/") or not is_within(target, dst):
                log.warning("Skipped unsafe package path %r", rel)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(arc) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out, _CHUNK)
            restored.add(rel)
    dropped = _prune(project, restored)
    log.info("Unpacked %s (%s, %d files, %d dropped)", zip_path.name, "full" if full else "hand-off",
             len(restored), dropped)
    return {"project": project, "fullBackup": full, "files": len(restored), "dropped": dropped}
