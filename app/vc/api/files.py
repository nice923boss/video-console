"""Project files: media serving, uploads, exported files, storyboard / caption / shot-list downloads, the client
review page and its comments."""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from .. import explorer, folderpick
from ..export import captions_srt, export_filename, shots_csv
from ..exportdir import file_ref, find_export, forget_export, project_exports
from ..review import render_review, review_ops
from ..server import FileResponse, Request, Response, Router
from ..storyboard import picture_data, render_storyboard
from ..stt import srt_ops, srt_text
from ..util import AUDIO_EXTS, IMAGE_EXTS, VIDEO_EXTS, AppError, kind_of, new_id
from .system import open_folder

if TYPE_CHECKING:
    from ..app import App

log = logging.getLogger("vc.api")

MAX_UPLOAD = 4 * 1024 ** 3
MAX_SRT = 10 * 1024 ** 2
MAX_REVIEW_FILE = 2 * 1024 ** 2
EXT_RE = re.compile(r"^\.[a-z0-9]{1,5}$")
BOM = "\ufeff"  # Excel and many subtitle tools need it to read UTF-8 as UTF-8
TRUE = ("1", "true", "yes")
PICKS_KEPT = 8  # pick-files results import-picked still accepts


def _spec(exts: set[str]) -> str:
    return ";".join(f"*{e}" for e in sorted(exts))


FILE_TYPES = [("影片、圖片與聲音", _spec(IMAGE_EXTS | VIDEO_EXTS | AUDIO_EXTS)), ("聲音", _spec(AUDIO_EXTS)),
              ("所有檔案", "*.*")]
AUDIO_TYPES = [("聲音", _spec(AUDIO_EXTS)), ("所有檔案", "*.*")]


def export_entries(project_dir: Path) -> list[dict[str, Any]]:
    """The project's finished videos and packages, newest first (in-progress *.part files are skipped)."""
    items = []
    for f in project_exports(project_dir):
        try:
            st = f.stat()
        except OSError:
            continue  # removed while listing
        kind = "package" if f.suffix.lower() == ".zip" else (kind_of(f.name) or "file")
        items.append({"name": f.name, "path": file_ref(f, project_dir), "size": st.st_size,
                      "modified": st.st_mtime, "kind": kind})
    items.sort(key=lambda e: e["modified"], reverse=True)
    return items


def download_stem(project: dict) -> str:
    return export_filename(project).rsplit(".", 1)[0]


def register(router: Router, app: App) -> None:
    store = app.store

    def resolve(pid: str, rel: str) -> Path:
        """A project file, or one of the project's exports in the shared export folder (a full path)."""
        if not Path(rel).is_absolute():
            return store.resolve_file(pid, rel)
        path = find_export(store.project_dir(pid), rel)
        if path is None:
            raise AppError("找不到檔案（可能已被刪除）", 404)
        return path

    @router.route("GET", "/api/projects/{pid}/file")
    def project_file(req: Request) -> FileResponse:
        path = resolve(req.params["pid"], req.query.get("path", ""))
        name = None
        if req.query.get("download") in TRUE:
            name = (req.query.get("name") or "").strip()[:200] or path.name
        return FileResponse(path, download_name=name)

    def ingest(pid: str, name: str, q: dict[str, str], fill: Callable[[Path], object]) -> dict:
        """Stage a file under media/cache (fill writes it) and queue its ingest job; cleans up if either fails."""
        ext = os.path.splitext(name)[1].lower()
        if kind_of(name) is None or not EXT_RE.match(ext):
            raise AppError("不支援的檔案類型", 415)
        rel = f"media/cache/upload-{new_id('u')}{ext}"
        dst = store.project_dir(pid) / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        payload = {"upload": rel, "name": name, "target": q.get("target") or "library",
                   "track": q.get("track") in TRUE, "start": q.get("start"),
                   "audioTrack": q.get("audioTrack") or None, "layer": q.get("layer") or None, "gain": q.get("gain"),
                   "link": q.get("link") != "0", "media": q.get("media") or None}
        try:
            fill(dst)
            job = app.jobs.submit("ingest", pid, q.get("shot") or None, payload)
        except BaseException:
            dst.unlink(missing_ok=True)
            raise
        return {"job": job}

    @router.route("POST", "/api/projects/{pid}/upload")
    def upload(req: Request) -> dict:
        name = (req.query.get("name") or "").strip()[:200]
        return ingest(req.params["pid"], name, req.query, lambda dst: req.save_body(dst, MAX_UPLOAD))

    # AU-09: files picked in the system's open-file dialog. Unlike an upload, the folder they came from is known,
    # so the page can remember an import volume per folder. Only paths the user picked can be imported.
    picks: dict[str, list[Path]] = {}

    @router.route("POST", "/api/projects/{pid}/pick-files")
    def pick_files(req: Request) -> dict:
        """Open the dialog and wait for the user; no token and no files when canceled."""
        if sys.platform != "win32":
            raise AppError("只有 Windows 支援系統選檔視窗", 501)
        store.project_dir(req.params["pid"])
        audio = bool(req.json_object().get("audio"))
        chosen = folderpick.pick_files(None, "加入音訊" if audio else "匯入檔案", AUDIO_TYPES if audio else FILE_TYPES)
        kinds = ("audio",) if audio else ("image", "video", "audio")
        files = [f for f in map(Path, chosen) if kind_of(f.name) in kinds]
        if not files:
            return {"files": [], "skipped": len(chosen)}
        token = new_id("k")
        picks[token] = files
        while len(picks) > PICKS_KEPT:
            picks.pop(next(iter(picks)))
        return {"token": token, "folder": str(files[0].parent), "files": [{"name": f.name} for f in files],
                "skipped": len(chosen) - len(files)}

    @router.route("POST", "/api/projects/{pid}/import-picked")
    def import_picked(req: Request) -> dict:
        """File number index of a pick-files result becomes an ingest job, with the same options as an upload."""
        files = picks.get(req.query.get("token") or "") or []
        index = req.query.get("index") or ""
        if not index.isdigit() or int(index) >= len(files):
            raise AppError("選檔結果已過期，請重新選擇檔案", 410)
        src = files[int(index)]
        if not src.is_file():
            raise AppError(f"找不到檔案：{src.name}（可能已被移動或刪除）", 404)
        # A copy, never a link: the project keeps its own file even if the original is edited or deleted.
        return ingest(req.params["pid"], src.name, req.query, lambda dst: shutil.copyfile(src, dst))

    # ------------------------------------------------------------------ exported files
    @router.route("GET", "/api/projects/{pid}/exports")
    def export_list(req: Request) -> list[dict]:
        return export_entries(store.project_dir(req.params["pid"]))

    @router.route("DELETE", "/api/projects/{pid}/exports/{name}")
    def export_delete(req: Request) -> None:
        pdir = store.project_dir(req.params["pid"])
        name = req.params["name"]
        if name in (".", "..") or Path(name).name != name or "\\" in name:
            raise AppError("檔案名稱不合法", 403)
        path = find_export(pdir, name)
        if path is None:
            raise AppError("找不到檔案（可能已被刪除）", 404)
        try:
            path.unlink()
        except OSError:
            raise AppError("檔案正在使用中，請先停止播放", 409) from None
        forget_export(pdir, path)
        log.info("Deleted export %s", path)

    # ------------------------------------------------------------------ storyboard, captions, shot list
    @router.route("GET", "/api/projects/{pid}/storyboard")
    def storyboard(req: Request) -> Response:
        pid = req.params["pid"]
        project = store.get(pid)

        def picture_url(shot: dict) -> str | None:
            return picture_data(project, shot, lambda rel: store.resolve_file(pid, rel))

        return Response(render_storyboard(project, picture_url), content_type="text/html; charset=utf-8")

    @router.route("GET", "/api/projects/{pid}/captions.srt")
    def captions(req: Request) -> Response:
        project = store.get(req.params["pid"])
        text = captions_srt(project)
        if not text:
            raise AppError("這個專案還沒有字幕可以匯出", 409)
        return Response(BOM + text, content_type="application/x-subrip; charset=utf-8",
                        download_name=download_stem(project) + ".srt")

    @router.route("POST", "/api/projects/{pid}/captions.srt")
    def captions_import(req: Request) -> dict:
        """An .srt file's subtitles, as one undo step: at their own times plus offset= seconds; replace=1 removes
        the old ones first, and voices=delete|keep|regen says what happens to their voice-overs (srt_ops)."""
        pid = req.params["pid"]
        try:
            offset = max(0.0, float(req.query.get("offset") or 0))
        except ValueError:
            raise AppError("偏移秒數格式不正確") from None
        voices = req.query.get("voices") or "delete"
        if voices not in ("delete", "keep", "regen"):
            raise AppError("配音處理方式不正確")
        ops = srt_ops(store.get(pid), srt_text(req.body(MAX_SRT)), req.query.get("replace") in TRUE, offset, voices)
        added = [op["item"] for op in ops if op["op"] == "caption.add"]
        if not added:
            raise AppError("檔案裡沒有可匯入的字幕，請確認是 SRT 格式")
        result = store.apply(pid, ops, label="匯入 SRT")
        return {"rev": result["rev"], "added": len(added), "voiced": [c["id"] for c in added if c.get("speaker")]}

    @router.route("GET", "/api/projects/{pid}/review.html")
    def review_page(req: Request) -> Response:
        pid = req.params["pid"]
        project = store.get(pid)
        stem = download_stem(project)

        def picture_url(shot: dict) -> str | None:
            return picture_data(project, shot, lambda rel: store.resolve_file(pid, rel))

        return Response(render_review(project, picture_url, stem), content_type="text/html; charset=utf-8",
                        download_name=f"{stem}_審稿.html")

    @router.route("POST", "/api/projects/{pid}/review")
    def review_import(req: Request) -> dict:
        """A comments file from the review page, as one undo step (review_ops)."""
        pid = req.params["pid"]
        project = store.get(pid)
        try:
            data = json.loads(req.body(MAX_REVIEW_FILE).decode("utf-8-sig"))
        except (UnicodeDecodeError, ValueError):
            raise AppError("檔案不是有效的 JSON，請選擇客戶傳回的審稿意見檔") from None
        ops, counts = review_ops(project, data)
        if not ops:
            if counts["duplicate"]:
                raise AppError("這份審稿意見已經匯入過了")
            raise AppError("檔案裡沒有任何意見")
        by = str(data.get("by") or "").strip()[:60] or "客戶"
        result = store.apply(pid, ops, label=f"匯入審稿意見：{by}")
        return {"rev": result["rev"], "by": by, **counts}

    @router.route("GET", "/api/projects/{pid}/shots.csv")
    def shot_list(req: Request) -> Response:
        project = store.get(req.params["pid"])
        return Response(shots_csv(project), content_type="text/csv; charset=utf-8",
                        download_name=download_stem(project) + ".csv")

    # ------------------------------------------------------------------ explorer
    @router.route("POST", "/api/projects/{pid}/reveal")
    def reveal(req: Request) -> None:
        pid = req.params["pid"]
        rel = str(req.json_object().get("path") or "")
        if not rel:
            open_folder(store.project_dir(pid))
            return
        if sys.platform != "win32":
            raise AppError("只有 Windows 支援開啟資料夾", 501)
        path = resolve(pid, rel)
        if not path.exists():
            raise AppError("找不到檔案（可能已被刪除）", 404)
        explorer.show(path, select=True)
