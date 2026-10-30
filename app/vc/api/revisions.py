"""Client revision notes (vc/revisions.py): load what the client sent back (a ZIP upload, or a folder picked in
the system dialog), list a round's notes with their review video and attachments, record our decision on each,
and apply an accepted note to the project."""
from __future__ import annotations

import logging
import shutil
import sys
import threading
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from .. import folderpick, revisions
from ..agent import Agent
from ..review_pack import load_pack
from ..server import FileResponse, Request, Router
from ..util import AppError, new_id, now

if TYPE_CHECKING:
    from ..app import App

log = logging.getLogger("vc.api")

MAX_ZIP = 4 * 1024 ** 3


def register(router: Router, app: App) -> None:
    store = app.store
    agent = Agent(app)
    lock = threading.Lock()  # one read-modify-write of a round.json at a time

    def get_round(pid: str, rid: str) -> dict:
        round_ = revisions.load_round(store.project_dir(pid), rid)
        if round_ is None:
            raise AppError("找不到這一輪修改意見", 404)
        return round_

    def get_item(round_: dict, cid: str) -> dict:
        item = next((i for i in round_["items"] if i["id"] == cid), None)
        if item is None:
            raise AppError("找不到這條意見", 404)
        return item

    def update_item(pid: str, rid: str, item: dict) -> dict:
        with lock:
            round_ = get_round(pid, rid)
            round_["items"] = [item if i["id"] == item["id"] else i for i in round_["items"]]
            revisions.save_round(store.project_dir(pid), round_)
        return item

    @router.route("GET", "/api/projects/{pid}/revisions")
    def round_list(req: Request) -> dict:
        return {"rounds": revisions.list_rounds(store.project_dir(req.params["pid"]))}

    @router.route("POST", "/api/projects/{pid}/revisions")
    def round_upload(req: Request) -> dict:
        """The ZIP the review page packed, as the request body (?name= its file name)."""
        pid = req.params["pid"]
        project, pdir = store.get(pid), store.project_dir(pid)
        name = (req.query.get("name") or "").strip()[:200] or "修改意見.zip"
        if not name.lower().endswith(".zip"):
            raise AppError("請選客戶回傳的 ZIP 壓縮檔", 415)
        tmp = pdir / revisions.ROUNDS / f".incoming-{new_id('z')}.zip"
        tmp.parent.mkdir(parents=True, exist_ok=True)
        try:
            req.save_body(tmp, MAX_ZIP)
            with lock:
                return {"round": revisions.import_zip(pdir, project, tmp, name)}
        finally:
            tmp.unlink(missing_ok=True)

    @router.route("POST", "/api/projects/{pid}/revisions/pick")
    def round_pick(req: Request) -> dict:
        """The unpacked folder, picked in the system dialog; {round: null} when canceled."""
        if sys.platform != "win32":
            raise AppError("只有 Windows 支援系統選資料夾視窗，請改用 ZIP 檔載入", 501)
        pid = req.params["pid"]
        project, pdir = store.get(pid), store.project_dir(pid)
        folder = folderpick.pick(None, "選擇客戶回傳的修改意見資料夾（ZIP 解開後的資料夾）")
        if not folder:
            return {"round": None}
        with lock:
            return {"round": revisions.import_folder(pdir, project, Path(folder))}

    @router.route("GET", "/api/projects/{pid}/revisions/{rid}")
    def round_get(req: Request) -> dict:
        return {"round": get_round(req.params["pid"], req.params["rid"])}

    @router.route("DELETE", "/api/projects/{pid}/revisions/{rid}")
    def round_delete(req: Request) -> dict:
        with lock:
            revisions.delete_round(store.project_dir(req.params["pid"]), req.params["rid"])
        return {"ok": True}

    @router.route("GET", "/api/projects/{pid}/revisions/{rid}/video")
    def round_video(req: Request) -> FileResponse:
        """The video of the review package the notes were made on, to show each note on its frame."""
        pid = req.params["pid"]
        round_ = get_round(pid, req.params["rid"])
        pack = load_pack(store.project_dir(pid), round_["review"])
        video = Path(pack["video"]) if pack else None
        if video is None or not video.is_file():
            raise AppError("找不到審稿影片（可能已被移動或刪除）", 404)
        return FileResponse(video)

    @router.route("GET", "/api/projects/{pid}/revisions/{rid}/file")
    def round_file(req: Request) -> FileResponse:
        """One attachment, ?path=附件/<name>."""
        pid, rid = req.params["pid"], req.params["rid"]
        get_round(pid, rid)
        rel = revisions.attachment_path(req.query.get("path"))
        path = store.project_dir(pid) / revisions.ROUNDS / rid / rel if rel else None
        if path is None or not path.is_file():
            raise AppError("找不到這個附件", 404)
        return FileResponse(path)

    @router.route("PATCH", "/api/projects/{pid}/revisions/{rid}/items/{cid}")
    def item_decide(req: Request) -> dict:
        """{decision?: agree | tweak | skip | null, extra?: our instruction for a tweak}."""
        pid, rid = req.params["pid"], req.params["rid"]
        data = req.json_object()
        with lock:
            round_ = get_round(pid, rid)
            item = revisions.set_decision(get_item(round_, req.params["cid"]), data)
            round_["items"] = [item if i["id"] == item["id"] else i for i in round_["items"]]
            revisions.save_round(store.project_dir(pid), round_)
        return {"item": item}

    @router.route("POST", "/api/projects/{pid}/revisions/{rid}/items/{cid}/apply")
    def item_apply(req: Request) -> dict:
        """Make the edit for an accepted note. A picture or clip goes in on a blank shot of its own (its import
        runs as a job); a written note goes to the assistant, which blocks until the model is done."""
        pid, rid = req.params["pid"], req.params["rid"]
        item = get_item(get_round(pid, rid), req.params["cid"])
        if item["decision"] not in ("agree", "tweak"):
            raise AppError("這條意見還沒勾選「同意」或「微調」")
        if item["status"] == "done":
            raise AppError("這條意見已經套用過了", 409)
        try:
            out = apply_insert(pid, rid, item) if item["kind"] == "insert" else apply_note(pid, item)
        except AppError as exc:
            out = {"status": "manual" if exc.message == revisions.MOVED else "failed", "result": exc.message}
        except Exception as exc:  # the round keeps a readable reason; the log keeps the trace
            log.exception("applying revision note %s/%s failed", rid, item["id"])
            out = {"status": "failed", "result": f"套用失敗：{exc}"}
        return {"item": update_item(pid, rid, {**item, **out, "applied": now()})}

    def apply_insert(pid: str, rid: str, item: dict) -> dict:
        ins = item["insert"]
        src = store.project_dir(pid) / revisions.ROUNDS / rid / ins["file"]
        if not src.is_file():
            raise AppError("找不到這條意見的附件（可能已被刪除）")
        rel = f"media/cache/upload-{new_id('u')}{PurePosixPath(ins['file']).suffix.lower()}"
        dst = store.project_dir(pid) / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        try:
            with store.locked(pid):
                plan = revisions.insert_plan(store.get(pid), item)
                store.apply(pid, [{"op": "linked", "ops": plan["ops"]}], origin="user", label=plan["label"])
        except BaseException:
            dst.unlink(missing_ok=True)
            raise
        what = "圖片" if ins["type"] == "image" else "影片"
        try:
            job = app.jobs.submit("ingest", pid, plan["id"], {"upload": rel, "name": ins["name"], "target": "shot",
                                                              "link": True})
        except AppError as exc:
            dst.unlink(missing_ok=True)
            return {"status": "failed", "shotId": plan["id"],
                    "result": f"已在 {plan['t']:.2f} 秒插入空白鏡頭，但{what}匯入失敗：{exc.message}"}
        return {"status": "done", "shotId": plan["id"], "job": job["id"],
                "result": f"已在 {plan['t']:.2f} 秒插入空白鏡頭，{what}「{ins['name']}」在背景匯入中"}

    def apply_note(pid: str, item: dict) -> dict:
        before = store.get(pid)
        reply = agent.run(pid, {"messages": [{"role": "user", "content": revisions.note_prompt(before, item)}]})
        changed = store.get(pid)["rev"] != before["rev"] or any(s.get("job") for s in reply["steps"])
        status, result = revisions.agent_outcome(reply, changed)
        if status != "done" and reply["made"]:
            result += roll_back(pid, reply["made"])
        return {"status": status, "result": result, "steps": len(reply["steps"])}

    def roll_back(pid: str, labels: list[str]) -> str:
        """Undo what the assistant left of a note it did not finish, newest first, while its steps are on top."""
        for label in reversed(labels):
            if store.undo_state(pid)["undoLabel"] != label:
                return "（AI 做到一半的修改沒能全部復原，請檢查專案）"
            try:
                store.undo(pid)
            except AppError as exc:
                log.warning("rolling back revision note steps of %s failed: %s", pid, exc.message)
                return "（AI 做到一半的修改沒能全部復原，請檢查專案）"
        return "（AI 做到一半的修改已復原）"
