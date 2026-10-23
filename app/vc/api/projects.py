"""Projects: create / open / edit (op batches) / undo / history / trash, and script import."""
from __future__ import annotations

import copy
import shutil
from typing import TYPE_CHECKING, Any

from .. import edits
from ..ops import apply_ops
from ..pacing import break_down, fill_coverage, pace_notes
from ..script import MAX_SHOTS, docx_to_text, normalize_shot, parse_script
from ..sequence import build_sequence
from ..store import BLANK_SHOT_NAME
from ..unpack import unpack_package
from ..server import Request, Router
from ..util import AppError, new_id, random_seed

if TYPE_CHECKING:
    from ..app import App

MAX_IMPORT = 32 * 1024 ** 3  # a full backup carries every take, so it may be far larger than one upload
MAX_DOCX = 50 * 1024 ** 2


def _short(value: Any, limit: int) -> str | None:
    return str(value)[:limit] if value else None


def untouched_blank_shot(project: dict) -> str | None:
    """Id of the empty 鏡頭 1 a new project starts with, while it is still the only shot and nobody has used it."""
    if len(project["shots"]) != 1 or project.get("captions"):
        return None
    shot = project["shots"][0]
    if shot.get("name") != BLANK_SHOT_NAME:
        return None
    if any(shot.get(k) for k in ("prompt", "imagePrompt", "motion", "camera", "notes", "activeImage", "activeVideo",
                                  "ref", "extraRefs")):
        return None
    if any(t.get("shot") == shot["id"] for t in project["takes"].values()):
        return None
    return shot["id"]


def script_ops(project: dict, defaults: dict, shots: list[dict], index: int, captions: bool) -> list[dict]:
    """shot.add ops for parsed shots, plus caption.add ops timed to where those shots land in the sequence."""
    shot_ops: list[dict] = []
    texts: dict[str, str] = {}
    for i, s in enumerate(shots):
        sid = new_id("s")
        notes = s["notes"]
        if s["caption"] and not captions:
            notes = "\n".join(filter(None, (notes, f"字幕：{s['caption']}")))
        shot = {"id": sid, "name": s["name"], "prompt": s["prompt"], "motion": s["motion"], "camera": s["camera"],
                "notes": notes, "seed": random_seed()}
        if s["duration"]:
            shot["duration"] = s["duration"]
        shot_ops.append({"op": "shot.add", "shot": shot, "index": index + i})
        if captions and s["caption"]:
            texts[sid] = s["caption"]
    if not texts:
        return shot_ops
    work = copy.deepcopy(project)
    apply_ops(work, shot_ops, defaults)
    caption_ops = [
        {"op": "caption.add", "item": {"id": new_id("c"), "start": clip["start"],
                                       "end": round(clip["start"] + clip["length"], 4),
                                       "text": texts[clip["shot"]["id"]], "position": "bottom"}}
        for clip in build_sequence(work) if clip["shot"]["id"] in texts
    ]
    return shot_ops + caption_ops


def register(router: Router, app: App) -> None:
    store = app.store

    # ------------------------------------------------------------------ project list
    @router.route("GET", "/api/projects")
    def project_list(req: Request) -> list[dict]:
        return store.list_projects()

    @router.route("POST", "/api/projects")
    def project_create(req: Request) -> dict:
        data = req.json_object()
        patch = data.get("settings") if isinstance(data.get("settings"), dict) else None
        project = store.create(str(data.get("name") or ""), patch, starter=bool(data.get("starter")))
        app.settings.set_last_project(project["id"])
        return {"project": project}

    @router.route("POST", "/api/projects/import")
    def project_import(req: Request) -> dict:
        """Raw body: a .zip made by 打包專案. It always becomes a new project, so nothing is overwritten."""
        if not str(req.query.get("name") or "").lower().endswith(".zip"):
            raise AppError("只能匯入 Video Console 打包的 .zip 檔", 415)
        app.paths.tmp.mkdir(parents=True, exist_ok=True)
        work = app.paths.tmp / new_id("import")
        archive = work.with_suffix(".zip")
        try:
            req.save_body(archive, MAX_IMPORT)
            work.mkdir()
            result = unpack_package(archive, work)
            project = store.adopt(work, result["project"])
        finally:
            archive.unlink(missing_ok=True)
            shutil.rmtree(work, ignore_errors=True)
        app.settings.set_last_project(project["id"])
        return {"project": project, "fullBackup": result["fullBackup"], "files": result["files"],
                "dropped": result["dropped"]}

    @router.route("GET", "/api/projects/{pid}")
    def project_get(req: Request) -> dict:
        pid = req.params["pid"]
        return {"project": store.get(pid), "undo": store.undo_state(pid)}

    @router.route("POST", "/api/projects/{pid}/open")
    def project_open(req: Request) -> None:
        pid = req.params["pid"]
        store.get(pid)
        app.settings.set_last_project(pid)

    @router.route("DELETE", "/api/projects/{pid}")
    def project_delete(req: Request) -> None:
        pid = req.params["pid"]
        if app.jobs.active_count(pid):
            raise AppError("這個專案還有工作在執行，請先取消或等待完成", 409)
        store.trash(pid)
        if app.settings.section("ui").get("lastProjectId") == pid:
            app.settings.set_last_project(None)
        app.jobs.clear(pid)

    @router.route("POST", "/api/projects/{pid}/duplicate")
    def project_duplicate(req: Request) -> dict:
        return {"project": store.duplicate(req.params["pid"])}

    # ------------------------------------------------------------------ editing
    @router.route("POST", "/api/projects/{pid}/ops")
    def project_ops(req: Request) -> dict:
        data = req.json_object()
        ops = data.get("ops")
        if not isinstance(ops, list):
            raise AppError("操作格式不正確")
        return store.apply(req.params["pid"], ops, origin="user", undoable=bool(data.get("undoable", True)),
                           batch=_short(data.get("batch"), 64), coalesce=_short(data.get("coalesce"), 64),
                           label=str(data.get("label") or "")[:80])

    @router.route("GET", "/api/projects/{pid}/timeline")
    def project_timeline(req: Request) -> dict:
        return edits.timeline(store.get(req.params["pid"]))

    @router.route("POST", "/api/projects/{pid}/edit")
    def project_edit(req: Request) -> dict:
        """One named timeline edit (vc/edits.py), applied as one undo step; some also queue a job."""
        pid = req.params["pid"]
        plan = edits.plan(store.get(pid), req.json_object())
        out: dict = {"label": plan["label"], "changed": False}
        ops = plan["ops"]
        if ops:
            # Like the edit page with linked editing on: items on later shots follow them.
            if any(o["op"].startswith("shot.") for o in ops):
                ops = [{"op": "linked", "ops": ops}]
            out.update(store.apply(pid, ops, origin="user", label=plan["label"]))
        if plan.get("job"):
            job = plan["job"]
            out["job"] = app.jobs.submit(job["kind"], pid, job["shotId"], job["payload"])
        if plan.get("id"):
            out["id"] = plan["id"]
        return out

    @router.route("POST", "/api/projects/{pid}/undo")
    def project_undo(req: Request) -> dict:
        return store.undo(req.params["pid"])

    @router.route("POST", "/api/projects/{pid}/redo")
    def project_redo(req: Request) -> dict:
        return store.redo(req.params["pid"])

    @router.route("GET", "/api/projects/{pid}/history")
    def project_history(req: Request) -> list[dict]:
        return store.history(req.params["pid"])

    @router.route("POST", "/api/projects/{pid}/history/{rev}/restore")
    def project_restore(req: Request) -> dict:
        rev = req.params["rev"]
        if not rev.isdigit():
            raise AppError("找不到這個版本", 404)
        return store.restore_history(req.params["pid"], int(rev))

    # ------------------------------------------------------------------ disk usage
    @router.route("GET", "/api/projects/{pid}/usage")
    def project_usage(req: Request) -> dict:
        info = store.usage(req.params["pid"])
        return {"used": info["used"], "unused": info["unused"], "orphans": len(info["orphans"])}

    @router.route("POST", "/api/projects/{pid}/purge")
    def project_purge(req: Request) -> dict:
        return {"freed": store.purge_orphans(req.params["pid"])}

    # ------------------------------------------------------------------ trash
    @router.route("GET", "/api/trash")
    def trash_list(req: Request) -> list[dict]:
        return store.list_trash()

    @router.route("POST", "/api/trash/{folder}/restore")
    def trash_restore(req: Request) -> dict:
        return store.restore_trash(req.params["folder"])

    @router.route("DELETE", "/api/trash")
    def trash_empty(req: Request) -> dict:
        return {"removed": store.empty_trash()}

    # ------------------------------------------------------------------ script import
    @router.route("POST", "/api/script/parse")
    def script_parse(req: Request) -> dict:
        data = req.json_object()
        text = data.get("text")
        mode = str(data.get("mode") or "auto")
        detail = data.get("detail") is True
        if mode == "llm":
            shots = app.llm.script_to_shots(str(text or ""), detail=detail)
            if not detail:
                return {"shots": shots, "mode": "llm", "notes": []}
            shots, notes = fill_coverage(shots)
            return {"shots": shots, "mode": "llm", "notes": pace_notes(shots) + notes}
        result = parse_script(text, mode)
        if detail:
            return break_down(result)
        return {**result, "shots": [{k: v for k, v in s.items() if k != "lines"} for s in result["shots"]]}

    @router.route("POST", "/api/script/docx")
    def script_docx(req: Request) -> dict:
        return docx_to_text(req.body(MAX_DOCX))

    @router.route("POST", "/api/projects/{pid}/script/apply")
    def script_apply(req: Request) -> dict:
        pid = req.params["pid"]
        data = req.json_object()
        raw = data.get("shots")
        if not isinstance(raw, list) or not raw:
            raise AppError("沒有要匯入的鏡頭")
        if len(raw) > MAX_SHOTS:
            raise AppError(f"一次最多匯入 {MAX_SHOTS} 個鏡頭")
        shots = [s for s in (normalize_shot(item) for item in raw) if s]
        if not shots:
            raise AppError("沒有可以匯入的鏡頭")
        project = store.get(pid)
        # the script replaces an untouched starting shot instead of leaving it blank in front; one undo brings it back
        lead: list[dict] = []
        blank = untouched_blank_shot(project)
        if blank:
            lead = [{"op": "shot.delete", "id": blank}]
            project = {**project, "shots": []}
        count = len(project["shots"])
        index = data.get("index")
        index = count if not isinstance(index, int) or isinstance(index, bool) else max(0, min(count, index))
        ops = lead + script_ops(project, app.settings.section("defaults"), shots, index,
                                captions=data.get("captions", True) is not False)
        result = store.apply(pid, ops, label="匯入劇本")
        shot_ids = [op["shot"]["id"] for op in ops if op["op"] == "shot.add"]
        return {"rev": result["rev"], "added": len(shot_ids), "captions": sum(op["op"] == "caption.add" for op in ops),
                "shotIds": shot_ids}
