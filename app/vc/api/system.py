"""Bootstrap snapshot, settings, secrets, system info and shutdown."""
from __future__ import annotations

import logging
import os
import platform
import re
import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .. import BUILD, VERSION, explorer
from ..captions import list_fonts
from ..exportdir import ensure_root, export_root
from ..presets import all_presets
from ..server import Request, Router
from ..util import AppError
from .engine import provider_list, provider_secret_names

if TYPE_CHECKING:
    from ..app import App

log = logging.getLogger("vc.api")

FIXED_SECRETS = frozenset({"comfyorg.api_key", "llm.api_key", "ntfy.token", "voai.api_key"})
COMFY_TOKEN_RE = re.compile(r"^comfy\.[a-z0-9_-]{1,32}\.token$")
MAX_SECRET_LEN = 4096
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


def system_info(app: App) -> dict[str, Any]:
    media = app.media()
    return {
        "version": VERSION,
        "python": platform.python_version(),
        "platform": platform.platform(terse=True),
        "dataDir": str(app.paths.data),
        "ffmpeg": {"available": media.available, "path": media.exe or "", "version": media.version()},
        "instance": app.instance,
        "started": app.started,
        "pid": os.getpid(),
    }


def check_secret_name(name: str) -> None:
    if name in FIXED_SECRETS or COMFY_TOKEN_RE.match(name) or name in provider_secret_names():
        return
    raise AppError("不支援的金鑰名稱")


def open_folder(path: Path) -> None:
    if sys.platform != "win32":
        raise AppError("只有 Windows 支援開啟資料夾", 501)
    path.mkdir(parents=True, exist_ok=True)
    explorer.show(path)


def register(router: Router, app: App) -> None:
    @router.route("GET", "/api/ping")
    def ping(req: Request) -> dict:
        running = app.jobs.state()["counts"].get("running") or 0
        tray = bool(app.tray and app.tray.running)  # a client that restarts this copy shows the icon again
        return {"ok": True, "instance": app.instance, "version": VERSION, "build": BUILD, "running": running,
                "tray": tray}

    @router.route("GET", "/api/bootstrap")
    def bootstrap(req: Request) -> dict:
        return {
            "instance": app.instance,
            "version": VERSION,
            "settings": app.settings.get(),
            "secrets": app.secrets.status(),
            "projects": app.store.list_projects(),
            "jobs": app.jobs.list(),
            "queue": app.jobs.state(),
            "comfy": app.comfy.status(),
            "workflows": app.workflows.list(),
            "providers": provider_list(app),
            "presets": all_presets(),
            "system": system_info(app),
        }

    @router.route("GET", "/api/system")
    def system(req: Request) -> dict:
        return system_info(app)

    @router.route("GET", "/api/system/fonts")
    def fonts(req: Request) -> dict:
        return {"fonts": list_fonts()}

    @router.route("POST", "/api/system/ffmpeg")
    def redetect_ffmpeg(req: Request) -> dict:
        app.reset_media()
        return system_info(app)

    @router.route("POST", "/api/system/reveal")
    def reveal_folder(req: Request) -> None:
        what = str(req.json_object().get("what") or "data")
        if what == "exports":
            folder = str(app.settings.section("export").get("folder") or "")
            open_folder(ensure_root(export_root(folder, app.paths.data)))
            return
        folders = {"data": app.paths.data, "logs": app.paths.logs, "workflows": app.paths.workflows,
                   "projects": app.paths.projects}
        if what not in folders:
            raise AppError("不支援的資料夾")
        open_folder(folders[what])

    @router.route("POST", "/api/shutdown")
    def shutdown(req: Request) -> dict:
        def later() -> None:
            time.sleep(0.3)  # let this response reach the client first
            app.stop()

        threading.Thread(target=later, name="shutdown", daemon=True).start()
        return {"ok": True}

    @router.route("POST", "/api/tray")
    def show_tray(req: Request) -> dict:
        """A copy started without a window shows its tray icon once the user opens a window onto it."""
        if app.tray is not None:
            app.tray.start()
        return {"tray": bool(app.tray and app.tray.running)}

    @router.route("GET", "/api/settings")
    def get_settings(req: Request) -> dict:
        return app.settings.get()

    @router.route("PUT", "/api/settings")
    def put_settings(req: Request) -> dict:
        patch = req.json_object()
        before = {s["id"] for s in app.settings.section("comfy")["servers"]}
        saved = app.settings.update(patch)
        if "comfy" in patch:
            for sid in before - {s["id"] for s in saved["comfy"]["servers"]}:
                app.secrets.set(f"comfy.{sid}.token", None)  # a removed server's token is no longer needed
            app.comfy.poke()
        if "ffmpeg" in patch:
            app.reset_media()
        return saved

    @router.route("GET", "/api/secrets")
    def get_secrets(req: Request) -> dict:
        return app.secrets.status()

    @router.route("PUT", "/api/secrets")
    def put_secret(req: Request) -> dict:
        data = req.json_object()
        name = str(data.get("name") or "")
        value = data.get("value")
        if value is not None and not isinstance(value, str):
            raise AppError("金鑰內容格式不正確")
        check_secret_name(name)
        value = (value or "").strip()
        if len(value) > MAX_SECRET_LEN or _CONTROL_RE.search(value):
            raise AppError("金鑰內容太長，或含有不允許的字元（例如換行）")
        app.secrets.set(name, value or None)
        log.info("Secret %s %s", name, "saved" if value else "cleared")
        if name.startswith("comfy"):
            app.comfy.poke()
        return app.secrets.status()
