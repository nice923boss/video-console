"""Caption imports that go beyond one .srt: a 中英對照 text onto two caption tracks, and speech alignment."""
from __future__ import annotations

from typing import TYPE_CHECKING

from ..bilingual import bilingual_plan
from ..server import Request, Router
from ..util import AppError

if TYPE_CHECKING:
    from ..app import App

PREVIEW_SEGMENTS = 300  # segments a dry run sends back


def register(router: Router, app: App) -> None:
    store = app.store

    @router.route("POST", "/api/projects/{pid}/captions/bilingual")
    def captions_bilingual(req: Request) -> dict:
        """{text, english: "double"|"single", start, replace, dryRun, align}: the text's Chinese lines onto the
        中文 caption track and their English onto the English track under it, as one undo step. dryRun only
        returns what would be added; align then queues speech alignment of the Chinese track."""
        pid = req.params["pid"]
        data = req.json_object()
        try:
            start = float(data.get("start") or 0)
        except (TypeError, ValueError):
            raise AppError("開始秒數格式不正確") from None
        plan = bilingual_plan(store.get(pid), data.get("text"), str(data.get("english") or "double"), start,
                              bool(data.get("replace")))
        out = {"pairs": plan["pairs"], "notes": plan["notes"], "tracks": plan["tracks"],
               "count": len(plan["segments"]), "segments": plan["segments"][:PREVIEW_SEGMENTS]}
        if data.get("dryRun"):
            return out
        result = store.apply(pid, plan["ops"], label="匯入雙語字幕")
        out["rev"] = result["rev"]
        if data.get("align"):
            try:
                out["job"] = app.jobs.submit("align", pid, None, {"track": plan["tracks"]["zh"]})
            except AppError as err:  # the captions are in; say why alignment could not start
                out["alignError"] = str(err)
        return out

    @router.route("POST", "/api/projects/{pid}/captions/align")
    def captions_align(req: Request) -> dict:
        """{track: "main" | a caption track id}: queue speech alignment of that row's captions (and the
        captions paired with them) to what is said on the timeline."""
        data = req.json_object()
        return {"job": app.jobs.submit("align", req.params["pid"], None, {"track": str(data.get("track") or "main")})}
