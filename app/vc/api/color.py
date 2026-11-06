"""Colour analysis endpoints (vc/autocolor.py): measure a picture, and white balance / expose shots."""
from __future__ import annotations

from typing import TYPE_CHECKING

from .. import autocolor
from ..server import Request, Router

if TYPE_CHECKING:
    from ..app import App


def register(router: Router, app: App) -> None:
    store = app.store

    @router.route("POST", "/api/projects/{pid}/analyze")
    def analyze(req: Request) -> dict:
        """Stats of one picture (shot + t, overlay + t, media + t, or the timeline time at), the x/y/size patch,
        and a suggested white balance block. Changes nothing."""
        pid = req.params["pid"]
        return autocolor.analyze(app.media(), store.project_dir(pid), store.get(pid), req.json_object())

    @router.route("POST", "/api/projects/{pid}/auto-color")
    def auto_color(req: Request) -> dict:
        """Automatic white balance and/or exposure on shots (body.ids, default every shot in the sequence),
        written to each shot's grade.wb as one undo step; dryRun true only reports."""
        pid = req.params["pid"]
        body = req.json_object()
        ops, report = autocolor.auto_color(app.media(), store.project_dir(pid), store.get(pid), body)
        if body.get("dryRun"):
            return {"changed": False, "shots": report}
        label = str(body.get("label") or "自動調色")[:80]
        res = store.apply(pid, ops, origin="user", label=label)
        return {**res, "shots": report}
