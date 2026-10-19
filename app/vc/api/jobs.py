"""The job queue: submit (one or many), cancel, retry, reorder, clear, pause, live previews."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..jobs import MAX_ACTIVE
from ..server import Request, Response, Router
from ..util import AppError

if TYPE_CHECKING:
    from ..app import App


def _submit(app: App, data: Any) -> dict:
    if not isinstance(data, dict):
        raise AppError("工作格式不正確")
    payload = data.get("payload")
    if payload is not None and not isinstance(payload, dict):
        raise AppError("工作參數格式不正確")
    return app.jobs.submit(str(data.get("kind") or ""), str(data.get("pid") or ""),
                           str(data.get("shotId") or "") or None, payload)


def register(router: Router, app: App) -> None:
    jobs = app.jobs

    @router.route("GET", "/api/jobs")
    def job_list(req: Request) -> dict:
        return {"jobs": jobs.list(req.query.get("pid") or None), "state": jobs.state()}

    @router.route("POST", "/api/jobs")
    def job_submit(req: Request) -> dict:
        return {"job": _submit(app, req.json_object())}

    # fixed paths before the /api/jobs/{jid}/... ones
    @router.route("POST", "/api/jobs/batch")
    def job_batch(req: Request) -> dict:
        items = req.json_object().get("jobs")
        if not isinstance(items, list) or not items:
            raise AppError("沒有要送出的工作")
        if len(items) > MAX_ACTIVE:
            raise AppError(f"一次最多送出 {MAX_ACTIVE} 個工作")
        done, errors = [], []
        for i, item in enumerate(items):
            try:
                done.append(_submit(app, item))
            except AppError as exc:
                errors.append({"index": i, "error": exc.message})
        return {"jobs": done, "errors": errors}

    @router.route("POST", "/api/jobs/clear")
    def job_clear(req: Request) -> dict:
        data = req.json_object()
        return {"removed": jobs.clear(data.get("pid") or None, data.get("status") or None)}

    @router.route("POST", "/api/jobs/cancel-all")
    def job_cancel_all(req: Request) -> dict:
        return {"canceled": jobs.cancel_all(req.json_object().get("pid") or None)}

    @router.route("POST", "/api/jobs/pause")
    def job_pause(req: Request) -> dict:
        return jobs.pause(req.json_object().get("paused") is True)

    @router.route("GET", "/api/jobs/{jid}")
    def job_get(req: Request) -> dict:
        return jobs.get(req.params["jid"])

    @router.route("DELETE", "/api/jobs/{jid}")
    def job_remove(req: Request) -> None:
        jobs.remove(req.params["jid"])

    @router.route("POST", "/api/jobs/{jid}/cancel")
    def job_cancel(req: Request) -> dict:
        return jobs.cancel(req.params["jid"])

    @router.route("POST", "/api/jobs/{jid}/retry")
    def job_retry(req: Request) -> dict:
        return jobs.retry(req.params["jid"])

    @router.route("POST", "/api/jobs/{jid}/top")
    def job_top(req: Request) -> dict:
        return jobs.move_top(req.params["jid"])

    @router.route("GET", "/api/jobs/{jid}/preview")
    def job_preview(req: Request) -> Response:
        mime, data = jobs.preview(req.params["jid"])
        return Response(data, content_type=mime)
