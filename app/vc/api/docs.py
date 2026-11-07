"""Project documents (vc/projdocs.py): the files in a project's docs/ folder the AI assistant can read, such as
the script, the client's notes or reference pictures."""
from __future__ import annotations

from typing import TYPE_CHECKING

from .. import projdocs
from ..server import FileResponse, Request, Router
from ..util import AppError
from .system import open_folder

if TYPE_CHECKING:
    from ..app import App


def register(router: Router, app: App) -> None:
    store = app.store

    @router.route("GET", "/api/projects/{pid}/docs")
    def docs_list(req: Request) -> dict:
        """{docs: [{name, kind: text|image, size, modified}], folder}"""
        folder = projdocs.folder(store.project_dir(req.params["pid"]))
        return {"docs": projdocs.listing(folder.parent), "folder": str(folder)}

    @router.route("POST", "/api/projects/{pid}/docs")
    def docs_upload(req: Request) -> dict:
        """Raw file body, ?name=file name -> {doc}. A name already taken gets " (2)" added."""
        project_dir = store.project_dir(req.params["pid"])
        length = req.content_length()
        if length is not None and length > projdocs.MAX_FILE:
            raise AppError(f"檔案太大，專案文件單檔上限 {projdocs.MAX_FILE // 1024 ** 2} MB", 413)
        name = (req.query.get("name") or "").strip()
        doc = projdocs.save(project_dir, name, lambda dst: req.save_body(dst, projdocs.MAX_FILE))
        return {"doc": doc}

    @router.route("POST", "/api/projects/{pid}/docs/delete")
    def docs_delete(req: Request) -> dict:
        """{name}: removes the file from the docs folder."""
        projdocs.delete(store.project_dir(req.params["pid"]), str(req.json_object().get("name") or ""))
        return {"ok": True}

    @router.route("GET", "/api/projects/{pid}/docs/file")
    def docs_file(req: Request) -> FileResponse:
        """?name= -> the file itself (pictures for the panel's thumbnails)."""
        path = projdocs.find(store.project_dir(req.params["pid"]), req.query.get("name") or "")
        return FileResponse(path, cache="no-cache")

    @router.route("POST", "/api/projects/{pid}/docs/reveal")
    def docs_reveal(req: Request) -> None:
        """Opens the docs folder in Explorer (files copied there by hand count as project documents too)."""
        open_folder(projdocs.folder(store.project_dir(req.params["pid"])))
