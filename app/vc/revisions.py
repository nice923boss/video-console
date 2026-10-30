"""Client revision notes (客戶修改意見, part 2): the ZIP the review page packs (review_pack.py), or the folder it
was unpacked to, comes back as a round kept in the project (revisions/<id>/round.json, the attachments beside it
in 附件/). Each note is listed for us to accept (同意), accept with our own instruction (微調) or leave (不採用).
An accepted note becomes an edit: an inserted picture or clip goes in at its frame on a blank shot of its own,
with no model involved; a written note becomes a prompt for the in-app assistant (agent.py), which makes the
change with its tools or answers that a person has to do it."""
from __future__ import annotations

import json
import math
import re
import shutil
import time
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

from . import edits
from .review_pack import load_pack
from .util import (
    IMAGE_EXTS,
    VIDEO_EXTS,
    AppError,
    new_id,
    now,
    read_json,
    write_json_atomic,
)

ROUNDS = "revisions"
ROUND = "round.json"
DATA = "修改意見.json"
FILES = "附件"
ROUND_ID = re.compile(r"^rv_[a-z0-9]{10}$")
ITEM_ID = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
MAX_ITEMS, MAX_MARKS, MAX_TEXT = 300, 40, 4000  # the page allows 300 notes and 40 marks each
MAX_DATA, MAX_FILE = 16 * 1024 ** 2, 1024 ** 3
DECISIONS = (None, "agree", "tweak", "skip")
MANUAL = "需人工處理"
MOVED = "審稿後這個鏡頭已被刪除或移出剪輯序列，找不到插入位置"
RULES = ("做法：先用工具查清楚相關的鏡頭、字幕或畫面軌，只改這條意見提到的地方，其他內容不要動。"
         "時間點只是用來找到意見指的鏡頭或字幕，不代表要在那裡切開。"
         "客戶已經說清楚要改成什麼的（例如字幕移到畫面上方、改成某個字），直接照做，不用先確認畫面。\n"
         "你看不到畫面，也看不到客戶附的圖片或影片。需要看畫面才能判斷、要重新生成畫面、或現有工具做不到的事，"
         f"不要硬改，回覆的第一行寫「{MANUAL}：原因」。\n完成後用一兩句話說明改了什麼。")


# ---------------------------------------------------------------- reading what the client sent
def _zip_name(info: zipfile.ZipInfo) -> str:
    """The entry's name. The page writes UTF-8 names; a ZIP made again by hand may hold the system code page's."""
    if info.flag_bits & 0x800:
        return info.filename
    raw = info.filename.encode("cp437")
    for enc in ("utf-8", "cp950"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            pass
    return info.filename


class _Zip:
    """The packed ZIP; 修改意見.json may sit at the top or in one folder."""

    def __init__(self, z: zipfile.ZipFile) -> None:
        entries = {_zip_name(i).replace("\\", "/"): i for i in z.infolist() if not i.is_dir()}
        found = sorted((n for n in entries if PurePosixPath(n).name == DATA), key=lambda n: n.count("/"))
        if not found:
            raise AppError(f"壓縮檔裡沒有「{DATA}」，這不是審稿頁打包的修改意見")
        base = found[0][:-len(DATA)]
        self._z = z
        self._files = {n[len(base):]: i for n, i in entries.items() if n.startswith(base)}

    def size(self, rel: str) -> int | None:
        info = self._files.get(rel)
        return info.file_size if info else None

    def read(self, rel: str) -> bytes:
        return self._z.read(self._files[rel])

    def copy(self, rel: str, dst: Path) -> None:
        with self._z.open(self._files[rel]) as src, open(dst, "wb") as out:
            shutil.copyfileobj(src, out, 1024 * 1024)


class _Dir:
    """The ZIP unpacked: 修改意見.json in the folder picked, or in one folder inside it."""

    def __init__(self, folder: Path) -> None:
        if not folder.is_dir():
            raise AppError("找不到這個資料夾", 404)
        subs = sorted(d for d in folder.iterdir() if d.is_dir())
        base = folder if (folder / DATA).is_file() else next((d for d in subs if (d / DATA).is_file()), None)
        if base is None:
            raise AppError(f"這個資料夾裡沒有「{DATA}」，請選客戶回傳的壓縮檔解開後的資料夾")
        self._base = base

    def size(self, rel: str) -> int | None:
        path = self._base / rel
        return path.stat().st_size if path.is_file() else None

    def read(self, rel: str) -> bytes:
        return (self._base / rel).read_bytes()

    def copy(self, rel: str, dst: Path) -> None:
        shutil.copyfile(self._base / rel, dst)


# ---------------------------------------------------------------- cleaning the notes
def _num(value: Any, low: float, high: float, default: float = 0.0) -> float:
    try:
        x = float(value)
    except (TypeError, ValueError):
        return default
    return min(high, max(low, x)) if math.isfinite(x) else default


def _text(value: Any, limit: int) -> str:
    return str(value).strip()[:limit] if isinstance(value, (str, int, float)) else ""


def _mark(raw: Any) -> dict | None:
    if not isinstance(raw, dict):
        return None
    kind = raw.get("type")
    if kind == "rect":
        return {"type": "rect", **{k: round(_num(raw.get(k), 0, 1), 4) for k in ("x", "y", "w", "h")}}
    if kind == "arrow":
        return {"type": "arrow", **{k: round(_num(raw.get(k), 0, 1), 4) for k in ("x1", "y1", "x2", "y2")}}
    text = _text(raw.get("text"), 200) if kind == "text" else ""
    return {"type": "text", "x": round(_num(raw.get("x"), 0, 1), 4), "y": round(_num(raw.get("y"), 0, 1), 4),
            "text": text} if text else None


def attachment_path(rel: Any, kind: str | None = None) -> str | None:
    """附件/<name> with a picture or video extension (of kind, when given); None for anything else."""
    rel = str(rel or "")
    name = rel[len(FILES) + 1:] if rel.startswith(FILES + "/") else ""
    if not name or "/" in name or "\\" in name or name in (".", "..") or ":" in name:
        return None
    ext = PurePosixPath(name).suffix.lower()
    exts = {"image": IMAGE_EXTS, "video": VIDEO_EXTS}.get(kind or "", IMAGE_EXTS | VIDEO_EXTS)
    return rel if ext in exts else None


def _item(raw: dict, index: int, seen: set[str], duration: float, src: _Zip | _Dir) -> dict | None:
    kind = raw.get("kind")
    if kind not in ("note", "insert"):
        return None
    cid = str(raw.get("id") or "")
    if not ITEM_ID.match(cid) or cid in seen:
        cid = f"c{index + 1}"
    seen.add(cid)
    item = {"id": cid, "kind": kind, "text": _text(raw.get("text"), MAX_TEXT),
            "t": round(_num(raw.get("t"), 0, duration), 3), "frame": int(_num(raw.get("frame"), 0, 1e8)),
            "shot": _text(raw.get("shot"), 40), "no": int(_num(raw.get("no"), 0, 1e5)),
            "shotName": _text(raw.get("shotName"), 120), "offset": round(_num(raw.get("offset"), 0, 36000), 3),
            "marks": [], "decision": None, "extra": "", "status": "pending", "result": ""}
    marks = raw.get("marks") if isinstance(raw.get("marks"), list) else []
    item["marks"] = [m for m in map(_mark, marks[:MAX_MARKS]) if m]
    if kind == "insert":
        ins = raw.get("insert") if isinstance(raw.get("insert"), dict) else {}
        typ = ins.get("type") if ins.get("type") in ("image", "video") else None
        rel = attachment_path(ins.get("file"), typ) if typ else None
        size = src.size(rel) if rel else None
        item["insert"] = {"type": typ, "name": _text(ins.get("name"), 200) or (PurePosixPath(rel).name if rel else ""),
                          "duration": round(_num(ins.get("duration"), 0, 36000), 3), "file": None, "size": size or 0}
        if not rel or size is None:
            item.update(status="manual", result="附件遺失或格式不正確，請客戶重新傳送這個檔案")
        elif size > MAX_FILE:
            item.update(status="manual", result="附件超過 1 GB，沒有載入，請另外向客戶索取")
        else:
            item["insert"]["file"] = rel
    return item


def _read_data(src: _Zip | _Dir) -> dict:
    size = src.size(DATA)
    if size is None or size > MAX_DATA:
        raise AppError(f"「{DATA}」太大或讀不到")
    try:
        data = json.loads(src.read(DATA).decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError):
        raise AppError(f"「{DATA}」內容損壞，無法讀取") from None
    if not isinstance(data, dict) or data.get("vc") != "review-comments":
        raise AppError(f"「{DATA}」不是審稿頁產生的修改意見")
    return data


def import_round(pdir: Path, project: dict, src: _Zip | _Dir, source: str) -> dict:
    """Check what the client sent belongs to this project and was not loaded before, and keep it as a round."""
    data = _read_data(src)
    rid = str(data.get("review") or "")
    pack = load_pack(pdir, rid)
    if pack is None and data.get("project") != project["id"]:
        name = _text(data.get("name"), 120) or "另一個專案"
        raise AppError(f"這份修改意見是「{name}」的審稿包，不屬於這個專案，請到那個專案載入", 409)
    by, at = _text(data.get("by"), 60), _text(data.get("at"), 40)
    for old in list_rounds(pdir):
        if (old["review"], old["by"], old["at"]) == (rid, by, at):
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(old["loaded"]))
            raise AppError(f"這份修改意見已經載入過（{when}），"
                           "要重新載入請先移除那一輪", 409)
    duration = float(pack["duration"]) if pack else 36000.0
    raws = data.get("items") if isinstance(data.get("items"), list) else []
    seen: set[str] = {"all"}
    items = [i for i in (_item(r, n, seen, duration, src) for n, r in enumerate(raws[:MAX_ITEMS])
                         if isinstance(r, dict)) if i]
    items.sort(key=lambda i: i["t"])
    note = _text(data.get("note"), MAX_TEXT)
    if note:  # the client's overall note, one more item to decide on
        items.insert(0, {"id": "all", "kind": "general", "text": note, "t": None, "frame": 0, "shot": "", "no": 0,
                         "shotName": "", "offset": 0.0, "marks": [], "decision": None, "extra": "",
                         "status": "pending", "result": ""})
    if not items:
        raise AppError("這份修改意見沒有任何意見")
    round_ = {"id": new_id("rv"), "review": rid, "pack": bool(pack), "project": _text(data.get("project"), 40),
              "name": _text(data.get("name"), 120), "video": _text(data.get("video"), 200), "by": by, "at": at,
              "note": note, "loaded": now(), "source": source[:200],
              "fps": (pack or project.get("settings") or {}).get("fps") or 30, "items": items}
    folder = pdir / ROUNDS / round_["id"]
    (folder / FILES).mkdir(parents=True)
    try:
        for item in items:
            if (item.get("insert") or {}).get("file"):
                src.copy(item["insert"]["file"], folder / item["insert"]["file"])
        write_json_atomic(folder / ROUND, round_)
    except BaseException:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    return round_


def import_zip(pdir: Path, project: dict, path: Path, source: str) -> dict:
    try:
        with zipfile.ZipFile(path) as z:
            return import_round(pdir, project, _Zip(z), source)
    except zipfile.BadZipFile:
        raise AppError("這不是有效的 ZIP 壓縮檔（可能沒下載完整）") from None


def import_folder(pdir: Path, project: dict, folder: Path) -> dict:
    return import_round(pdir, project, _Dir(folder), folder.name)


# ---------------------------------------------------------------- rounds kept in the project
def load_round(pdir: Path, rid: str) -> dict | None:
    if not ROUND_ID.match(str(rid or "")):
        return None
    data = read_json(pdir / ROUNDS / rid / ROUND, None)
    return data if isinstance(data, dict) and data.get("id") == rid else None


def save_round(pdir: Path, round_: dict) -> None:
    write_json_atomic(pdir / ROUNDS / round_["id"] / ROUND, round_)


def list_rounds(pdir: Path) -> list[dict]:
    """The rounds loaded in this project (a copy keeps its original's), newest first, without their items."""
    folder = pdir / ROUNDS
    rounds = [load_round(pdir, d.name) for d in (folder.iterdir() if folder.is_dir() else ())]
    out = []
    for r in sorted((r for r in rounds if r), key=lambda r: r["loaded"], reverse=True):
        items = r["items"]
        out.append({**{k: v for k, v in r.items() if k != "items"}, "count": len(items),
                    "decided": sum(1 for i in items if i["decision"]),
                    "done": sum(1 for i in items if i["status"] == "done")})
    return out


def delete_round(pdir: Path, rid: str) -> None:
    if load_round(pdir, rid) is None:
        raise AppError("找不到這一輪修改意見", 404)
    shutil.rmtree(pdir / ROUNDS / rid)


def set_decision(item: dict, data: dict) -> dict:
    """{decision?, extra?} on a note not applied yet."""
    if item["status"] == "done":
        raise AppError("這條意見已經套用，要重做請先復原專案再改", 409)
    out = dict(item)
    if "decision" in data:
        if data["decision"] not in DECISIONS:
            raise AppError("decision 只能是 agree、tweak、skip 或 null")
        if data["decision"] in ("agree", "tweak") and item["kind"] == "insert" and not item["insert"]["file"]:
            raise AppError("這條意見的附件遺失，無法自動套用")
        out["decision"] = data["decision"]
    if "extra" in data:
        out["extra"] = _text(data["extra"], MAX_TEXT)
    return out


# ---------------------------------------------------------------- turning a note into an edit
def locate(project: dict, item: dict) -> dict | None:
    """Where the note sits in the cut now, found by the shot it was made on: {clip, t, local}."""
    clip = next((c for c in edits.timeline(project)["clips"] if c["shotId"] == item["shot"]), None) \
        if item.get("shot") else None
    if clip is None:
        return None
    local = min(item["offset"], clip["length"])
    return {"clip": clip, "t": round(clip["start"] + local, 3), "local": local}


def insert_plan(project: dict, item: dict) -> dict:
    """The ops that put a blank shot for the client's picture or clip at the note's frame: the shot there is cut
    in two (unless the frame is at its very start or end) and the blank shot goes between the halves."""
    spot = locate(project, item)
    if spot is None:
        raise AppError(MOVED)
    clip, local = spot["clip"], spot["local"]
    least = edits.MIN_VIDEO if clip["source"] == "video" else edits.MIN_STILL
    if local < least:
        ops, index = [], clip["index"]
    elif clip["length"] - local < least:
        ops, index = [], clip["index"] + 1
    else:
        ops, index = edits.split(project, spot["t"], "shot", clip["shotId"])["ops"], clip["index"] + 1
    ins = item["insert"]
    sid = new_id("s")
    duration = round(ins["duration"], 2) if ins["duration"] >= 0.5 else 3.0
    shot = {"id": sid, "name": f"客戶素材：{PurePosixPath(ins['name']).stem}"[:60],
            "duration": min(duration, 600.0), "notes": item["text"]}
    what = "圖片" if ins["type"] == "image" else "影片"
    return {"ops": [*ops, {"op": "shot.add", "shot": shot, "index": index}], "id": sid, "t": spot["t"],
            "label": f"修改意見：插入{what}「{ins['name']}」"[:80]}


def _pct(v: float) -> str:
    return f"{round(v * 100)}%"


def describe_marks(marks: list[dict]) -> list[str]:
    out = []
    for m in marks:
        if m["type"] == "rect":
            out.append(f"紅框：左上角 ({_pct(m['x'])}, {_pct(m['y'])})，寬 {_pct(m['w'])}、高 {_pct(m['h'])}")
        elif m["type"] == "arrow":
            out.append(f"箭頭：從 ({_pct(m['x1'])}, {_pct(m['y1'])}) 指向 ({_pct(m['x2'])}, {_pct(m['y2'])})")
        else:
            out.append(f"文字標註「{m['text']}」：位於 ({_pct(m['x'])}, {_pct(m['y'])})")
    return out


def note_prompt(project: dict, item: dict) -> str:
    """What the assistant is asked to do for one written note."""
    if item["kind"] == "general":
        lines = ["客戶對整支影片的修改意見（沒有指定時間點），請照意見修改目前的專案。"]
    else:
        lines = [(f"客戶在審稿影片 {item['t']:.2f} 秒處（鏡頭 #{item['no']}「{item['shotName']}」內第 "
                  f"{item['offset']:.2f} 秒）留下修改意見，請照意見修改目前的專案。")]
        spot = locate(project, item)
        if spot:
            lines.append(f"這個位置在目前時間軸上是 {spot['t']:.2f} 秒，鏡頭 id {item['shot']}"
                         f"（第 {spot['clip']['index'] + 1} 個鏡頭）。")
        else:
            lines.append("審稿後這個鏡頭已被刪除或移出剪輯序列，請用 timeline 工具找對應的位置；找不到就回覆需人工處理。")
        if item["marks"]:
            lines.append("客戶在畫面上的標註（座標是畫面寬高的百分比，原點在左上角）：")
            lines += [f"- {m}" for m in describe_marks(item["marks"])]
    lines.append(f"客戶原話：「{item['text'] or '（沒有文字，只有標註）'}」")
    if item["decision"] == "tweak" and item["extra"]:
        lines.append(f"我方補充指示（照這段調整做法）：「{item['extra']}」")
    lines.append(RULES)
    return "\n".join(lines)


def agent_outcome(reply: dict, changed: bool) -> tuple[str, str]:
    """(status, result) for an assistant turn: manual when it says so or changed nothing."""
    text = str(reply.get("reply") or "").strip()
    if text.startswith(MANUAL):
        return "manual", text
    if reply.get("stopped"):
        return "failed", text
    if not changed:
        return "manual", f"AI 沒有做任何修改：{text}"
    return "done", text
