"""Projects: create / open / edit (op batches) / undo / history / trash, and script import."""
from __future__ import annotations

import copy
import shutil
from typing import TYPE_CHECKING, Any

from .. import edits, scriptsync
from ..cover import newly_covered
from ..exportcheck import export_check
from ..exportdir import remove_shared_exports
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


def _moves_overlays(ops: list) -> bool:
    return any(str(o.get("op") or "").startswith(("overlay.", "layer.")) or _moves_overlays(o.get("ops") or [])
               for o in ops if isinstance(o, dict))


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
    """shot.add ops for parsed shots, plus caption.add ops timed to where those shots land in the sequence and
    bound to the shot they came with (CP-07)."""
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
                                       "text": texts[clip["shot"]["id"]], "position": "bottom",
                                       "shot": clip["shot"]["id"]}}
        for clip in build_sequence(work) if clip["shot"]["id"] in texts
    ]
    return shot_ops + caption_ops


def register(router: Router, app: App) -> None:
    store = app.store

    def apply_user(pid: str, ops: list, **kw: Any) -> dict:
        """store.apply for an edit made in the editor. When it lays upper-track clips over shots so they hide
        them from end to end, "covered" lists those shots: the editor offers to hide their picture."""
        before = store.get(pid) if _moves_overlays(ops) else None
        res = store.apply(pid, ops, origin="user", **kw)
        covered = newly_covered(before, store.get(pid)) if before and res.get("changed") else []
        return {**res, "covered": covered} if covered else res

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
        app.backfill.check(pid)

    @router.route("POST", "/api/projects/{pid}/preview-files")
    def preview_files(req: Request) -> dict:
        """Make the smaller preview copies of the played video files at body.scale (2 or 4) in the background
        (PV-06); returns how many are still to be made. Each copy arrives as a project change."""
        return {"pending": app.lowres.request(req.params["pid"], req.json_object().get("scale"))}

    @router.route("POST", "/api/projects/{pid}/audio-files")
    def audio_files(req: Request) -> dict:
        """Make the plain audio copies the preview plays its sound from (PB-01, audioproxy.py) in the background;
        returns how many are still to be made. Each copy arrives as a project change."""
        return {"pending": app.audioproxy.request(req.params["pid"])}

    @router.route("DELETE", "/api/projects/{pid}")
    def project_delete(req: Request) -> dict:
        """Move the project to the trash; ?exports=1 also deletes what it wrote to the shared export folder."""
        pid = req.params["pid"]
        if app.jobs.active_count(pid):
            raise AppError("這個專案還有工作在執行，請先取消或等待完成", 409)
        removed, in_use = remove_shared_exports(store.project_dir(pid)) if req.query.get("exports") == "1" else (0, 0)
        store.trash(pid)
        if app.settings.section("ui").get("lastProjectId") == pid:
            app.settings.set_last_project(None)
        app.jobs.clear(pid)
        return {"removedExports": removed, "exportsInUse": in_use}

    @router.route("POST", "/api/projects/{pid}/duplicate")
    def project_duplicate(req: Request) -> dict:
        """body.name: the copy's name (optional), e.g. a delivery version such as 第一版交稿."""
        return {"project": store.duplicate(req.params["pid"], str(req.json_object().get("name") or ""))}

    # ------------------------------------------------------------------ editing
    @router.route("POST", "/api/projects/{pid}/ops")
    def project_ops(req: Request) -> dict:
        data = req.json_object()
        ops = data.get("ops")
        if not isinstance(ops, list):
            raise AppError("操作格式不正確")
        return apply_user(req.params["pid"], ops, undoable=bool(data.get("undoable", True)),
                          batch=_short(data.get("batch"), 64), coalesce=_short(data.get("coalesce"), 64),
                          label=str(data.get("label") or "")[:80])

    @router.route("GET", "/api/projects/{pid}/timeline")
    def project_timeline(req: Request) -> dict:
        return edits.timeline(store.get(req.params["pid"]))

    @router.route("POST", "/api/projects/{pid}/edit")
    def project_edit(req: Request) -> dict:
        """One named timeline edit (vc/edits.py), applied as one undo step; some also queue a job."""
        pid = req.params["pid"]
        data = req.json_object()
        project = store.get(pid)
        plan = edits.plan(project, data)
        # link false (the edit page's link toggle off, or a slip) leaves the other tracks put; shots taken out of
        # the sequence still get their items back when they return (as ops() in web/js/lib/actions.js).
        link = data.get("link", True) is not False and plan.get("link", True)
        out: dict = {"label": plan["label"], "changed": False}
        ops = plan["ops"]
        if ops:
            if any(o["op"].startswith("shot.") for o in ops):
                if link:
                    ops = [{"op": "linked", "ops": ops}]
                elif any(s.get("parked") for s in project["shots"]):
                    ops = [{"op": "linked", "ops": ops, "shift": False}]
            out.update(apply_user(pid, ops, label=plan["label"], coalesce=_short(data.get("coalesce"), 64)))
        def submit(job: dict) -> dict:
            payload = job["payload"] if data.get("link", True) is not False else {**job["payload"], "link": False}
            return app.jobs.submit(job["kind"], pid, job["shotId"], payload)

        if plan.get("job"):
            out["job"] = submit(plan["job"])
        if "jobs" in plan:
            out["jobs"] = [submit(job) for job in plan["jobs"]]
        for key in ("id", "by", "level", "partial"):
            if key in plan:
                out[key] = plan[key]
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
        return {"used": info["used"], "unused": info["unused"], "orphans": len(info["orphans"]),
                "trashed": info["trashed"]}

    @router.route("GET", "/api/projects/{pid}/missing")
    def project_missing(req: Request) -> dict:
        pid = req.params["pid"]
        root = store.project_dir(pid)
        media = store.get(pid)["media"].values()
        return {"media": [m["id"] for m in media if m.get("file") and not (root / m["file"]).is_file()]}

    @router.route("GET", "/api/projects/{pid}/export-check")
    def project_export_check(req: Request) -> dict:
        pid = req.params["pid"]
        return export_check(store.get(pid), store.project_dir(pid))

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
        ops = script_ops(project, app.settings.section("defaults"), shots, index,
                         captions=data.get("captions", True) is not False)
        # With the timeline's link toggle on, items after the insertion point move back to make room.
        sent = [{"op": "linked", "ops": ops}] if data.get("link", True) is not False else ops
        result = store.apply(pid, lead + sent, label="匯入劇本")
        shot_ids = [op["shot"]["id"] for op in ops if op["op"] == "shot.add"]
        return {"rev": result["rev"], "added": len(shot_ids), "captions": sum(op["op"] == "caption.add" for op in ops),
                "shotIds": shot_ids}

    # SB-14: the storyboard as one editable script; `ids` and `text` go back with the edit as its base
    @router.route("GET", "/api/projects/{pid}/script")
    def script_get(req: Request) -> dict:
        project = store.get(req.params["pid"])
        return {"text": scriptsync.render(project), "ids": [s["id"] for s in project["shots"]],
                "rev": project.get("rev")}

    @router.route("POST", "/api/projects/{pid}/script")
    def script_sync(req: Request) -> dict:
        pid = req.params["pid"]
        data = req.json_object()
        text, base, ids = data.get("text"), data.get("base"), data.get("ids")
        if not isinstance(text, str) or not isinstance(base, str) or len(text) > scriptsync.MAX_TEXT:
            raise AppError("劇本內容不正確或太長")
        if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
            raise AppError("缺少劇本對應的鏡頭清單，請重新整理劇本")
        project = store.get(pid)
        ops, info = scriptsync.sync_ops(project, app.settings.section("defaults"), text, base, ids,
                                        data.get("link", True) is not False)
        if not ops:
            return {**info, "rev": project.get("rev"), "changed": False}
        result = store.apply(pid, ops, label="套用劇本")
        return {**info, "rev": result["rev"], "changed": result["changed"]}
