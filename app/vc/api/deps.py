"""Status of the external tools the app needs, and their one-click installs."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .. import video2x
from ..deps import version_key
from ..server import Request, Router

if TYPE_CHECKING:
    from ..app import App


def deps_status(app: App) -> dict[str, Any]:
    media = app.media()
    tasks = app.deps.tasks()
    upscaler = video2x.find()
    comfy_online = [s["name"] for s in app.comfy.status() if s.get("online")]

    def package(pkg_id: str, required: bool, ready: bool, path: str, version: str) -> dict[str, Any]:
        pkg = app.deps.package(pkg_id)
        # Only the copy in data/tools can be updated here; one on PATH or installed elsewhere is the user's.
        installed = app.deps.installed_version(pkg_id) if app.deps.is_managed(pkg_id, path) else None
        update = installed is not None and version_key(pkg.version) > version_key(installed)
        return {"id": pkg_id, "required": required, "ready": ready, "path": path, "version": version,
                "installable": True, "package": pkg.name, "sizeMB": round(pkg.size / 1_000_000),
                "installedVersion": installed, "latest": pkg.version, "update": update, "task": tasks.get(pkg_id)}

    return {"items": [
        package("ffmpeg", True, media.available, media.exe or "", media.version()),
        package("video2x", False, upscaler is not None, str(upscaler or ""), ""),
        {"id": "comfyui", "required": False, "ready": bool(comfy_online), "path": "、".join(comfy_online),
         "version": "", "installable": False, "task": None},
    ]}


def register(router: Router, app: App) -> None:
    @router.route("GET", "/api/system/deps")
    def get_deps(req: Request) -> dict:
        return deps_status(app)

    @router.route("POST", "/api/system/deps/check")
    def check_deps(req: Request) -> dict:
        remark = app.deps.check()
        return {**deps_status(app), "remark": remark}

    @router.route("POST", "/api/system/deps/install")
    def install_dep(req: Request) -> dict:
        app.deps.start(str(req.json_object().get("id") or ""))
        return deps_status(app)
