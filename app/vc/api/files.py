"""Project files: media serving, uploads, exported files, storyboard / caption / shot-list downloads."""
from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from ..export import captions_srt, export_filename, shots_csv
from ..server import FileResponse, Request, Response, Router
from ..storyboard import render_storyboard, shot_picture
from ..util import AppError, is_within, kind_of, new_id
from .system import open_folder

if TYPE_CHECKING:
    from ..app import App

log = logging.getLogger("vc.api")

MAX_UPLOAD = 4 * 1024 ** 3
EXT_RE = re.compile(r"^\.[a-z0-9]{1,5}$")
BOM = "\ufeff"  # Excel and many subtitle tools need it to read UTF-8 as UTF-8
TRUE = ("1", "true", "yes")


def export_entries(folder: Path) -> list[dict[str, Any]]:
    """Finished files in exports/, newest first (in-progress *.part files are skipped)."""
    items = []
    for f in folder.iterdir() if folder.is_dir() else ():
        if f.name.endswith(".part"):
            continue
        try:
            st = f.stat()
        except OSError:
            continue  # removed while listing
        if not f.is_file():
            continue
        kind = "package" if f.suffix.lower() == ".zip" else (kind_of(f.name) or "file")
        items.append({"name": f.name, "path": f"exports/{f.name}", "size": st.st_size,
                      "modified": st.st_mtime, "kind": kind})
    items.sort(key=lambda e: e["modified"], reverse=True)
    return items


def download_stem(project: dict) -> str:
    return export_filename(project).rsplit(".", 1)[0]


def register(router: Router, app: App) -> None:
    store = app.store

    @router.route("GET", "/api/projects/{pid}/file")
    def project_file(req: Request) -> FileResponse:
        path = store.resolve_file(req.params["pid"], req.query.get("path", ""))
        name = None
        if req.query.get("download") in TRUE:
            name = (req.query.get("name") or "").strip()[:200] or path.name
        return FileResponse(path, download_name=name)

    @router.route("POST", "/api/projects/{pid}/upload")
    def upload(req: Request) -> dict:
        pid = req.params["pid"]
        q = req.query
        name = (q.get("name") or "").strip()[:200]
        ext = os.path.splitext(name)[1].lower()
        if kind_of(name) is None or not EXT_RE.match(ext):
            raise AppError("不支援的檔案類型", 415)
        rel = f"media/cache/upload-{new_id('u')}{ext}"
        dst = store.project_dir(pid) / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        req.save_body(dst, MAX_UPLOAD)
        payload = {"upload": rel, "name": name, "target": q.get("target") or "library",
                   "track": q.get("track") in TRUE, "start": q.get("start"),
                   "layer": q.get("layer") or None}
        try:
            job = app.jobs.submit("ingest", pid, q.get("shot") or None, payload)
        except BaseException:
            dst.unlink(missing_ok=True)
            raise
        return {"job": job}

    # ------------------------------------------------------------------ exported files
    @router.route("GET", "/api/projects/{pid}/exports")
    def export_list(req: Request) -> list[dict]:
        return export_entries(store.project_dir(req.params["pid"]) / "exports")

    @router.route("DELETE", "/api/projects/{pid}/exports/{name}")
    def export_delete(req: Request) -> None:
        folder = store.project_dir(req.params["pid"]) / "exports"
        name = req.params["name"]
        path = folder / name
        if name in (".", "..") or Path(name).name != name or "\\" in name or not is_within(path, folder):
            raise AppError("檔案名稱不合法", 403)
        if not path.is_file():
            raise AppError("找不到檔案（可能已被刪除）", 404)
        try:
            path.unlink()
        except OSError:
            raise AppError("檔案正在使用中，請先停止播放", 409) from None
        log.info("Deleted export %s", name)

    # ------------------------------------------------------------------ storyboard, captions, shot list
    @router.route("GET", "/api/projects/{pid}/storyboard")
    def storyboard(req: Request) -> Response:
        pid = req.params["pid"]
        project = store.get(pid)

        def picture_url(shot: dict) -> str | None:
            rel = shot_picture(project, shot)
            return f"/api/projects/{pid}/file?path={quote(rel)}" if rel else None

        return Response(render_storyboard(project, picture_url), content_type="text/html; charset=utf-8")

    @router.route("GET", "/api/projects/{pid}/captions.srt")
    def captions(req: Request) -> Response:
        project = store.get(req.params["pid"])
        text = captions_srt(project)
        if not text:
            raise AppError("這個專案還沒有字幕可以匯出", 409)
        return Response(BOM + text, content_type="application/x-subrip; charset=utf-8",
                        download_name=download_stem(project) + ".srt")

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
        path = store.resolve_file(pid, rel)
        if not path.exists():
            raise AppError("找不到檔案（可能已被刪除）", 404)
        subprocess.Popen(f'explorer /select,"{path}"')  # noqa: S602 - resolved path inside the project folder
