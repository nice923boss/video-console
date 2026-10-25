"""The cross-project style library (IM-07)."""
from __future__ import annotations

from typing import TYPE_CHECKING

from ..server import FileResponse, Request, Router
from ..style_library import StyleLibrary
from ..takes import find_item
from ..util import AppError

if TYPE_CHECKING:
    from ..app import App


def register(router: Router, app: App) -> None:
    library = StyleLibrary(app.paths.data)

    @router.route("GET", "/api/styles")
    def list_styles(req: Request) -> dict:
        return {"styles": library.list()}

    @router.route("POST", "/api/styles")
    def add_style(req: Request) -> dict:
        body = req.json_object()
        picture = None
        if body.get("item"):
            pid = str(body.get("pid") or "")
            item = find_item(app.store.get(pid), str(body["item"]))
            if item is None:
                raise AppError("找不到選的樣張", 404)
            picture = app.store.resolve_file(pid, item.get("thumb") or item["file"])
            if not picture.is_file():
                raise AppError("樣張檔案不見了", 410)
        entry = library.add(str(body.get("name") or ""), str(body.get("prompt") or ""),
                            str(body.get("negative") or ""), picture)
        return {"style": entry, "styles": library.list()}

    @router.route("DELETE", "/api/styles/{sid}")
    def remove_style(req: Request) -> dict:
        library.remove(req.params["sid"])
        return {"styles": library.list()}

    @router.route("GET", "/api/styles/{sid}/picture")
    def style_picture(req: Request) -> FileResponse:
        return FileResponse(library.picture(req.params["sid"]))
