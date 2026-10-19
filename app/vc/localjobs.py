"""Jobs that run on this machine with ffmpeg only: camera moves on stills, frame grabs, imports, export, package."""
from __future__ import annotations

import logging
import math
import os
import re
import shutil
from pathlib import Path
from typing import Any, Callable

from .export import export_options, render
from .jobs import JobControl
from .media import KENBURNS, KENBURNS_LABELS, Media
from .ops import new_overlay, valid_id
from .package import build_package
from .sequence import build_sequence
from .settings import SettingsStore
from .store import ProjectStore
from .takes import (IMPORT_DIR, TAKE_DIR, commit_takes, find_item, find_shot, item_path, new_media, new_take,
                    shot_label, stage_file)
from .util import AppError, clamp, kind_of, new_id

log = logging.getLogger("vc.localjobs")

LOCAL_KINDS = ("kenburns", "lastframe", "ingest", "export", "package")
UPLOAD_RE = re.compile(r"^media/cache/upload-[a-z0-9_]+\.[a-z0-9]{1,5}$")
ENCODERS = ("auto", "libx264", "h264_nvenc", "h264_amf", "h264_qsv")


def number(value: Any, default: float) -> float:
    """A numeric request field; missing means default, garbage is refused."""
    if value is None or value == "":
        return float(default)
    if isinstance(value, bool):
        raise AppError("數值格式不正確")
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise AppError("數值格式不正確") from None
    if not math.isfinite(result):
        raise AppError("數值格式不正確")
    return result


def even_size(value: Any, default: int) -> int:
    return int(clamp(int(number(value, default)), 64, 4096)) // 2 * 2


def _link_or_copy(src: Path, dst: Path) -> None:
    """Hard link when possible (instant on NTFS), so the upload survives a failed run and can be retried."""
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


class LocalJobs:
    def __init__(self, store: ProjectStore, settings: SettingsStore, media: Callable[[], Media]):
        self._store = store
        self._settings = settings
        self._media = media

    # ---------------------------------------------------------------- dispatch
    def prepare(self, kind: str, project: dict, shot_id: str | None, payload: dict) -> dict:
        return getattr(self, f"_prepare_{kind}")(project, shot_id, payload)

    def execute(self, ctl: JobControl) -> dict:
        work = self._store.project_dir(ctl.pid) / "media" / "cache" / f"job-{ctl.id}"
        work.mkdir(parents=True, exist_ok=True)
        try:
            return getattr(self, f"_run_{ctl.kind}")(ctl, work)
        finally:
            shutil.rmtree(work, ignore_errors=True)

    # ---------------------------------------------------------------- kenburns
    def _prepare_kenburns(self, project: dict, shot_id: str | None, payload: dict) -> dict:
        index, shot = find_shot(project, shot_id)
        item = find_item(project, payload.get("source") or shot.get("activeImage"))
        if not item:
            raise AppError("這個鏡頭還沒有圖片，請先生成或匯入圖片")
        if item.get("kind") != "image":
            raise AppError("運鏡動畫需要圖片")
        preset = str(payload.get("preset") or "zoom_in")
        if preset not in KENBURNS:
            raise AppError(f"不支援的運鏡方式：{preset}")
        settings = project["settings"]
        return {
            "title": f"運鏡動畫：{shot_label(index, shot)}",
            "lane": "local:ffmpeg",
            "shotId": shot["id"],
            "payload": {
                "source": item["id"],
                "preset": preset,
                "duration": round(clamp(number(payload.get("duration"), shot.get("duration") or 5.0), 0.5, 60.0), 2),
                "width": even_size(payload.get("width"), settings["width"]),
                "height": even_size(payload.get("height"), settings["height"]),
                "fps": int(clamp(number(payload.get("fps"), settings["fps"]), 1, 60)),
            },
        }

    def _run_kenburns(self, ctl: JobControl, work: Path) -> dict:
        p = ctl.payload
        project = self._store.get(ctl.pid)
        pdir = self._store.project_dir(ctl.pid)
        item = find_item(project, p["source"])
        if not item:
            raise AppError("來源圖片已被刪除", 410)
        src = item_path(pdir, item)
        media = self._media()
        media.require()
        out = work / "kenburns.mp4"
        media.kenburns(src, out, p["width"], p["height"], p["fps"], p["duration"], p["preset"],
                       cancelled=ctl.cancelled,
                       on_progress=lambda f: ctl.progress(0.02 + f * 0.9, "產生運鏡動畫"))
        ctl.check()
        tid = new_id("t")
        staged = stage_file(media, pdir, out, TAKE_DIR, tid, cancelled=ctl.cancelled, on_progress=ctl.progress)
        label = KENBURNS_LABELS.get(p["preset"], p["preset"])
        take = new_take(
            ctl.shot_id, staged, take_id=tid, job_id=ctl.id, parent=item["id"], label=label,
            source={"backend": "local", "tool": "kenburns", "preset": p["preset"], "presetName": label},
            prompt=item.get("prompt") or "", negative=item.get("negative") or "", seed=item.get("seed"),
            params={"duration": p["duration"], "preset": p["preset"]},
        )
        commit_takes(self._store, ctl.pid, [take], "activeVideo")
        return {"takeIds": [tid], "message": "運鏡動畫完成"}

    # ---------------------------------------------------------------- lastframe
    def _prepare_lastframe(self, project: dict, shot_id: str | None, payload: dict) -> dict:
        index, shot = find_shot(project, shot_id)
        item = find_item(project, payload.get("source") or shot.get("activeVideo"))
        if not item:
            raise AppError("這個鏡頭還沒有影片")
        if item.get("kind") != "video":
            raise AppError("擷取畫面需要影片")
        target = payload.get("target") or "next"
        if target not in ("next", "self"):
            raise AppError("擷取畫面的目標不正確")
        t_index, t_shot = index, shot
        if target == "next":
            if index + 1 >= len(project["shots"]):
                raise AppError("這是最後一個鏡頭，沒有下一個鏡頭可以接")
            t_index, t_shot = index + 1, project["shots"][index + 1]
        at = payload.get("at")
        if at is not None:
            at = round(clamp(number(at, 0.0), 0.0, max(0.0, float(item.get("duration") or 0.0))), 3)
        title = ("擷取畫面" if at is not None else "擷取尾幀") + f"：{shot_label(index, shot)}"
        if target == "next":
            title += f" → {shot_label(t_index, t_shot)}"
        return {"title": title, "lane": "local:ffmpeg", "shotId": t_shot["id"],
                "payload": {"source": item["id"], "fromShot": shot["id"], "target": target, "at": at}}

    def _run_lastframe(self, ctl: JobControl, work: Path) -> dict:
        p = ctl.payload
        project = self._store.get(ctl.pid)
        pdir = self._store.project_dir(ctl.pid)
        item = find_item(project, p["source"])
        if not item:
            raise AppError("來源影片已被刪除", 410)
        src = item_path(pdir, item)
        media = self._media()
        media.require()
        out = work / "frame.png"
        ctl.progress(0.2, "擷取畫面")
        if p.get("at") is None:
            media.last_frame(src, out)
        else:
            media.frame_at(src, p["at"], out)
        if not out.is_file():
            raise AppError("無法從影片擷取畫面（影片可能損毀）", 500)
        ctl.check()
        tid = new_id("t")
        staged = stage_file(media, pdir, out, TAKE_DIR, tid)
        take = new_take(
            ctl.shot_id, staged, take_id=tid, job_id=ctl.id, parent=item["id"],
            label="尾幀" if p.get("at") is None else f"{p['at']:.2f} 秒畫面",
            source={"backend": "local", "tool": "lastframe", "from": item["id"]},
            prompt=item.get("prompt") or "", negative=item.get("negative") or "",
        )
        commit_takes(self._store, ctl.pid, [take], "activeImage")
        return {"takeIds": [tid], "message": "已擷取畫面"}

    # ---------------------------------------------------------------- ingest
    def _prepare_ingest(self, project: dict, shot_id: str | None, payload: dict) -> dict:
        rel = str(payload.get("upload") or "")
        if not UPLOAD_RE.match(rel):
            raise AppError("上傳檔案路徑不正確")
        if not (self._store.project_dir(project["id"]) / rel).is_file():
            raise AppError("找不到上傳的檔案", 404)
        name = str(payload.get("name") or "").strip()[:200] or Path(rel).name
        kind = kind_of(rel)
        if kind is None:
            raise AppError(f"不支援的檔案類型：{Path(rel).suffix}", 415)
        target = payload.get("target") or "library"
        if target not in ("library", "shot"):
            raise AppError("匯入目標不正確")
        out: dict[str, Any] = {"upload": rel, "name": name, "target": target}
        shot_key = None
        title = f"匯入：{name}"
        if target == "shot":
            if kind == "audio":
                raise AppError("聲音檔只能放到素材庫或音軌")
            index, shot = find_shot(project, shot_id)
            shot_key = shot["id"]
            title += f" → {shot_label(index, shot)}"
        elif kind == "audio" and payload.get("track"):
            out["track"] = True
            out["start"] = round(clamp(number(payload.get("start"), 0.0), 0.0, 36000.0), 3)
        elif kind in ("image", "video") and payload.get("layer"):
            if not valid_id(payload["layer"]):
                raise AppError("畫面軌道代號不正確")
            out["layer"] = payload["layer"]
            out["start"] = round(clamp(number(payload.get("start"), 0.0), 0.0, 36000.0), 3)
            title += " → 畫面軌"
        return {"title": title, "lane": "local:ffmpeg", "shotId": shot_key, "payload": out}

    def _run_ingest(self, ctl: JobControl, work: Path) -> dict:
        p = ctl.payload
        pdir = self._store.project_dir(ctl.pid)
        upload = pdir / p["upload"]
        if not upload.is_file():
            raise AppError("找不到上傳的檔案（可能已被清除），請重新匯入", 410)
        media = self._media()
        src = work / f"source{upload.suffix.lower()}"
        _link_or_copy(upload, src)
        ctl.progress(0.05, "處理檔案")
        if p["target"] == "shot":
            tid = new_id("t")
            staged = stage_file(media, pdir, src, IMPORT_DIR, tid, cancelled=ctl.cancelled, on_progress=ctl.progress)
            take = new_take(ctl.shot_id, staged, take_id=tid, job_id=ctl.id, label=Path(p["name"]).stem[:120],
                            source={"backend": "import", "name": p["name"]})
            commit_takes(self._store, ctl.pid, [take], "activeImage" if staged["kind"] == "image" else "activeVideo")
            result: dict[str, Any] = {"takeIds": [tid]}
        else:
            mid = new_id("m")
            staged = stage_file(media, pdir, src, IMPORT_DIR, mid, cancelled=ctl.cancelled, on_progress=ctl.progress)
            item = new_media(staged, p["name"], mid)
            ops = [{"op": "media.add", "media": item}]
            if staged["kind"] == "audio" and p.get("track"):
                ops.append({"op": "audio.add", "item": {"id": new_id("a"), "mediaId": mid, "name": p["name"][:120],
                                                        "start": p.get("start", 0.0), "volume": 1.0}})
            project = self._store.get(ctl.pid) if p.get("layer") else None
            if project and any(x.get("id") == p["layer"] for x in project.get("layers", [])):
                frame = project["settings"]
                ops.append({"op": "overlay.add", "item": new_overlay(new_id("o"), item, p["layer"], p.get("start", 0.0),
                                                                    frame["width"], frame["height"])})
            self._store.apply(ctl.pid, ops, origin="job", undoable=False)
            result = {"mediaIds": [mid]}
        upload.unlink(missing_ok=True)
        return {**result, "message": "匯入完成"}

    # ---------------------------------------------------------------- export / package
    def _prepare_export(self, project: dict, shot_id: str | None, payload: dict) -> dict:
        overrides = dict(payload.get("options") or {})
        for key in ("burnCaptions", "clipAudio"):
            if key in overrides:
                overrides[key] = bool(overrides[key])
        if (overrides.get("encoder") or "auto") not in ENCODERS:
            raise AppError("不支援的影片編碼器")
        opts = export_options(project, self._settings.section("export"), overrides)
        if not build_sequence(project):
            raise AppError("序列裡沒有任何鏡頭可以輸出（請確認鏡頭有勾選「放入序列」）")
        return {"title": f"輸出影片：{project['name']}", "lane": "local:export", "shotId": None,
                "payload": {"options": opts}}

    def _run_export(self, ctl: JobControl, work: Path) -> dict:
        media = self._media()
        media.require()
        opts = ctl.payload["options"]
        pref = opts.get("encoder") or "auto"
        if pref == "auto":
            pref = self._settings.section("ffmpeg").get("encoder") or "auto"
        ctl.progress(0.0, "準備輸出")
        result = render(media, self._store.get(ctl.pid), self._store.project_dir(ctl.pid), opts, pref,
                        on_progress=lambda f: ctl.progress(f, "輸出影片中"), cancelled=ctl.cancelled)
        if result["missing"]:
            ctl.note(f"有 {result['missing']} 個鏡頭的畫面檔案遺失，已用文字卡代替")
        if result["empty"]:
            ctl.note(f"有 {result['empty']} 個鏡頭還沒有畫面，已用文字卡代替")
        return {"file": result["file"], "export": result, "message": f"輸出完成：{result['name']}"}

    def _prepare_package(self, project: dict, shot_id: str | None, payload: dict) -> dict:
        return {"title": f"打包專案：{project['name']}", "lane": "local:export", "shotId": None,
                "payload": {"includeVideos": bool(payload.get("includeVideos", True)),
                            "includeExport": bool(payload.get("includeExport", True)),
                            "fullBackup": bool(payload.get("fullBackup", False))}}

    def _run_package(self, ctl: JobControl, work: Path) -> dict:
        ctl.progress(0.0, "打包中")
        result = build_package(self._store.get(ctl.pid), self._store.project_dir(ctl.pid), ctl.payload,
                               on_progress=lambda f: ctl.progress(f, "打包中"), cancelled=ctl.cancelled)
        if result["missing"]:
            ctl.note(f"有 {result['missing']} 個檔案找不到，沒有放進打包檔")
        return {"file": result["file"], "package": result, "message": f"打包完成：{result['name']}"}
