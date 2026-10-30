"""The in-app assistant: a text model edits the open project through a small set of tools.

One chat turn is a loop: send the conversation and the tool list to the model, run the tool calls it asks for
in-process (the writes go through the same routes as the HTTP API), feed the results back, and stop when it
answers in plain text, after MAX_STEPS rounds, or when the user presses stop (checked between rounds; a model
call already in flight runs to its end). Each step is published as a "chat" event so the panel can show it live.
"""
from __future__ import annotations

import json
import logging
import math
import re
import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from . import edits
from .bilingual import MAX_TEXT
from .sequence import video_take
from .server import Request
from .util import AppError, new_id

if TYPE_CHECKING:
    from .app import App

log = logging.getLogger(__name__)

MAX_STEPS = 12  # model rounds per turn
MAX_HISTORY = 20  # messages of earlier turns sent back to the model
MAX_MESSAGE = 4000  # characters per message
MAX_RESULT = 12_000  # characters of one tool result the model sees
MAX_ROWS = 200  # clips / items / captions one read returns
PREVIEW = 1500  # characters of an attachment shown to the model up front
LABEL = "AI 助理："  # undo label prefix of the steps apply_ops makes
CHAT_ID = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
ID_PREFIX = {"caption.add": ("item", "c"), "marker.add": ("item", "mk"), "captionTrack.add": ("track", "ct")}
CAPTION_KEYS = ("id", "start", "end", "text", "position", "style", "speaker", "pair", "disabled", "title")

SYSTEM = """You are the editing assistant inside Video Console, a video editor. You work on the one open project \
through tools; the user sees every change as an undo step and can undo it.

Rules:
- Read before you change: call project_info first, then timeline or list_captions for the part you need. \
Use only ids that tool results gave you; never invent ids.
- Make the smallest change that does what the user asked. Group one logical change into one apply_ops call \
with a short Traditional Chinese label; the label becomes the undo step name.
- Times are timeline seconds. The main caption row is track "main"; other rows are captionTracks ids.
- apply_ops cheat sheet (ids of new items are filled in when omitted). Every op is one object whose "op" field \
names it, the other fields beside it, e.g. {"op": "caption.update", "id": "c_ab12cd34ef", "patch": {"text": "新字"}}:
  caption.add {"item": {"start", "end", "text", "track"?, "position"?: "bottom"|"middle"|"top", "style"?}} \
(omit track for the main row)
  caption.update {"id", "patch": {fields to change}}   caption.delete {"id"}
  captionTrack.add {"track": {"name"}, "index"?}   captionTrack.update {"id", "patch": {"name"|"hidden"}}
  captionTrack.delete {"id"} (also removes the captions on that row)
  marker.add {"item": {"start", "color": "blue"|"red"|"orange"|"yellow"|"green"|"purple", "name"}}
  marker.update {"id", "patch"}   marker.delete {"id"}
  shot.update {"id", "patch": {"name"|"duration"|"disabled"|"speed"|...}} (duration: still or blank shots only, \
a video shot's length is its trim; speed 0.25 to 4, video shots only) \
  shot.delete {"id"}   shot.move {"id", "index"} (index: the shot's new 0-based place in the shot list). \
Captions and audio follow the picture when shots change length, go or move.
- Text the client wants shown on screen (a phone number, a slogan, a price) is a caption; position picks bottom, \
middle or top of the frame. New text: caption.add, never overwrite another caption. New wording or position of a \
caption: one caption.update of that whole caption, no split or trim.
- Playback speed: shot.update patch speed, no split. Reorder or swap shots: shot.move (a swap is one shot.move).
- Cutting, trimming, freeze frames, detaching audio, volume and keyframes: use the edit tool, not raw ops. \
Split or trim only when the user asked to cut, shorten or freeze. \
A shot's freeze still is 5 seconds unless the freeze edit gets length (seconds).
- import_bilingual puts a Chinese/English parallel text onto two caption rows (Chinese above, English below). \
When the user attached a file, call it without text. english "double" keeps two English lines, "single" joins \
them into one. align true then times the captions to the speech in the clips.
- transcribe, align_captions and some edits start background jobs that take minutes. Start them, tell the user \
the job was started and that the result appears when it finishes; do not poll job_status in a loop.
- When a tool returns an error, fix the call or explain the problem; never repeat the same call unchanged.
- undo only reverts a step you made.
- Reply in Traditional Chinese (繁體中文), short and plain: what you did, or what you need from the user. \
Do not use the dash character U+2014."""


def _tool(name: str, description: str, properties: dict | None = None, required: tuple = ()) -> dict:
    return {"type": "function", "function": {"name": name, "description": description, "parameters": {
        "type": "object", "properties": properties or {}, "required": list(required)}}}


NUM = {"type": "number"}
STR = {"type": "string"}
TOOLS = [
    _tool("project_info", "Project summary: name, size, length, shot count, caption rows with how many captions "
          "each holds, caption styles, the latest undo step. Call this first."),
    _tool("timeline", "Picture clips (shots) in order with start/length in timeline seconds, the items on the "
          "other tracks (overlay, audio, caption, adjust) and markers. from/to limit it to a window.",
          {"from": NUM, "to": NUM}),
    _tool("list_captions", "Captions in timeline order. track: \"main\" or a caption row id; omit for all rows. "
          "from/to: a window in seconds. limit up to 200, offset for the next page.",
          {"track": STR, "from": NUM, "to": NUM, "offset": {"type": "integer"}, "limit": {"type": "integer"}}),
    _tool("apply_ops", "Apply project ops as one undo step (see the cheat sheet in the instructions).",
          # llama.cpp turns this into a grammar: without additionalProperties only "op" could be written
          {"ops": {"type": "array", "items": {"type": "object", "properties": {"op": STR}, "required": ["op"],
                                              "additionalProperties": True}},
           "label": STR}, ("ops", "label")),
    _tool("edit", "One timeline edit, for cutting and timing only: a caption's new wording or position is one "
          "apply_ops caption.update, never a split. split {t, type?, id?};splitAll {t}; trim {t, side: start|end, type?, id?}; "
          "freeze {t, type?, id?, length?} (shot, or an overlay video: the still goes in when its grab job is done; "
          "length: seconds of a shot's still, default 5); detach {id|t}; restore {id}; reverse {id|t}; stabilize {type?, id|t} (toggles; shot or overlay); "
          "volume {type, id, db, in?, out?}; "
          "keyframe {type, id, t, values?}; roll/slip/slide {id, by} (picture only: other tracks stay, the shots' own sound is detached in place). type: shot (default), overlay, audio, "
          "caption, adjust. Without id the clip under t is used.",
          {"cmd": {"type": "string", "enum": ["split", "splitAll", "trim", "freeze", "detach", "restore", "reverse",
                                              "stabilize", "volume", "keyframe", "roll", "slip", "slide"]},
           "t": NUM, "type": STR, "id": STR, "side": STR, "by": NUM, "db": NUM, "in": NUM, "out": NUM,
           "length": NUM, "values": {"type": "object"}}, ("cmd",)),
    _tool("import_bilingual", "Import a Chinese/English parallel text as captions on two rows (中文 above, English "
          "below), one after another from start. text: omit to use the user's attachment. replace: clear those "
          "two rows first. align: then queue speech alignment.",
          {"text": STR, "english": {"type": "string", "enum": ["double", "single"]}, "start": NUM,
           "replace": {"type": "boolean"}, "align": {"type": "boolean"}}),
    _tool("align_captions", "Queue a background job that moves one caption row's captions onto the matching "
          "speech in the clips (the row below that shares their times follows).", {"track": STR}, ("track",)),
    _tool("transcribe", "Queue a background job that turns one video shot's speech into captions.",
          {"shotId": STR}, ("shotId",)),
    _tool("job_status", "State and progress of one background job.", {"id": STR}, ("id",)),
    _tool("undo", "Undo the latest undo step, only when you made it."),
]


@dataclass
class _Turn:
    pid: str
    chat_id: str
    attachment: str
    made: list[str] = field(default_factory=list)  # undo labels of the steps this turn made, oldest first


class _Body:
    """Stands in for the HTTP handler so a route can read a JSON body in-process."""

    def __init__(self, body: bytes) -> None:
        self.headers = {"Content-Length": str(len(body))}
        self._body = body

    def read_body(self, size: int) -> bytes:
        return self._body[:size]


def _num(value: Any, default: float) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def _int(value: Any, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(value)))
    except (TypeError, ValueError):
        return default


def _overlaps(start: float, length: float, lo: float, hi: float) -> bool:
    return start + length >= lo and start <= hi


def _capped(rows: list, key: str, out: dict) -> dict:
    out[key] = rows[:MAX_ROWS]
    if len(rows) > MAX_ROWS:
        out[f"{key}More"] = len(rows) - MAX_ROWS
    return out


def _main_row(op: dict) -> dict:
    """list_captions calls the main row "main"; the project stores it as no track, so a model copying it back works."""
    key = {"caption.add": "item", "caption.update": "patch"}.get(op["op"])
    part = op.get(key) if key else None
    if isinstance(part, dict) and part.get("track") == "main":
        return {**op, key: {**part, "track": None}}
    return op


def history(messages: Any) -> list[dict]:
    """The client's earlier turns as model messages: user/assistant text only, the last one the user's."""
    if not isinstance(messages, list) or not messages:
        raise AppError("對話內容格式不正確")
    out = []
    for m in messages[-MAX_HISTORY:]:
        if not isinstance(m, dict) or m.get("role") not in ("user", "assistant"):
            raise AppError("對話內容格式不正確")
        out.append({"role": m["role"], "content": str(m.get("content") or "")[:MAX_MESSAGE]})
    if out[-1]["role"] != "user" or not out[-1]["content"].strip():
        raise AppError("請先輸入要交代的事")
    return out


class Agent:
    def __init__(self, app: App) -> None:
        self._app = app
        self._stops: dict[str, threading.Event] = {}
        self._lock = threading.Lock()

    def cancel(self, chat_id: str) -> bool:
        with self._lock:
            stop = self._stops.get(chat_id)
        if stop:
            stop.set()
        return bool(stop)

    def run(self, pid: str, data: dict) -> dict:
        """{id?, messages: [{role, content}], attachment?: {name, text}} -> {id, reply, steps, stopped, made}
        (made: the undo labels of the steps the turn left, oldest first)."""
        self._app.store.get(pid)  # 404 before the model is called
        chat_id = str(data.get("id") or new_id("ch"))
        if not CHAT_ID.match(chat_id):
            raise AppError("對話代號格式不正確")
        messages = history(data.get("messages"))
        attachment = data.get("attachment")
        text = ""
        if attachment is not None:
            if not isinstance(attachment, dict) or not isinstance(attachment.get("text"), str):
                raise AppError("附件格式不正確")
            text = attachment["text"]
            if len(text) > MAX_TEXT:
                raise AppError(f"附件太長（上限 {MAX_TEXT} 字）", 413)
            name = str(attachment.get("name") or "附件")[:120]
            messages[-1]["content"] += (f"\n\n[Attached file \"{name}\", {len(text)} characters. import_bilingual "
                                        f"uses it when called without text. Beginning:]\n{text[:PREVIEW]}")
        turn = _Turn(pid, chat_id, text)
        stop = threading.Event()
        with self._lock:
            self._stops[chat_id] = stop
        try:
            out = self._loop(turn, [{"role": "system", "content": SYSTEM}, *messages], stop)
            return {"id": chat_id, **out, "made": turn.made}
        finally:
            with self._lock:
                self._stops.pop(chat_id, None)
            self._app.bus.publish("chat", {"pid": pid, "id": chat_id, "done": True})

    def _loop(self, turn: _Turn, messages: list[dict], stop: threading.Event) -> dict:
        steps: list[dict] = []
        for _ in range(MAX_STEPS):
            if stop.is_set():
                return {"reply": "已停止。", "steps": steps, "stopped": "cancel"}
            answer = self._app.llm.complete(messages, TOOLS)
            if not answer["tool_calls"]:
                return {"reply": answer["content"] or "（模型沒有回覆內容）", "steps": steps, "stopped": None}
            messages.append({"role": "assistant", "content": answer["content"], "tool_calls": [
                {"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": c["arguments"]}}
                for c in answer["tool_calls"]]})
            for call in answer["tool_calls"]:
                step, result = self._run_tool(turn, call)
                steps.append(step)
                self._app.bus.publish("chat", {"pid": turn.pid, "id": turn.chat_id, "step": step})
                content = json.dumps(result, ensure_ascii=False)
                if len(content) > MAX_RESULT:
                    content = content[:MAX_RESULT] + " ...(truncated; read a smaller window)"
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": content})
        return {"reply": f"已達單次對話的步驟上限（{MAX_STEPS} 輪），先停在這裡；要繼續請再交代一次。",
                "steps": steps, "stopped": "limit"}

    def _run_tool(self, turn: _Turn, call: dict) -> tuple[dict, Any]:
        name = call["name"]
        step: dict = {"tool": name, "args": {}, "ok": False}
        try:
            args = json.loads(call["arguments"] or "{}")
        except ValueError:
            step["error"] = "參數不是有效的 JSON"
            return step, {"error": step["error"]}
        if not isinstance(args, dict):
            args = {}
        step["args"] = args
        fn = getattr(self, f"_t_{name}", None) if name in {t["function"]["name"] for t in TOOLS} else None
        if fn is None:
            step["error"] = f"沒有這個工具：{name}"
            return step, {"error": step["error"]}
        try:
            result = fn(turn, args)
        except AppError as exc:
            step["error"] = exc.message
            return step, {"error": exc.message}
        except (TypeError, ValueError, KeyError, AttributeError) as exc:  # malformed arguments the routes did not catch
            log.warning("chat tool %s failed on %r", name, args, exc_info=True)
            step["error"] = f"參數格式不正確（{type(exc).__name__}）"
            return step, {"error": step["error"]}
        step["ok"] = True
        if isinstance(result, dict) and isinstance(result.get("job"), dict):
            step["job"] = result["job"].get("id")
        return step, result

    def _route(self, method: str, path: str, body: dict) -> Any:
        """Call one of the app's own routes in-process, exactly as the HTTP API would."""
        fn, params, _ = self._app.router.match(method, path)
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        return fn(Request(_Body(raw), method, path, {}, params))

    # ------------------------------------------------------------------ tools
    def _t_project_info(self, turn: _Turn, args: dict) -> dict:
        p = self._app.store.get(turn.pid)
        tl = edits.timeline(p)
        rows = p.get("captionTracks") or []
        ids = {t["id"] for t in rows}
        counts: dict[str, int] = {}
        for c in p["captions"]:
            row = c.get("track") if c.get("track") in ids else "main"
            counts[row] = counts.get(row, 0) + 1
        tracks = [{"id": "main", "name": "字幕", "captions": counts.get("main", 0)}]
        tracks += [{"id": t["id"], "name": t.get("name") or "", "hidden": bool(t.get("hidden")),
                    "captions": counts.get(t["id"], 0)} for t in rows]
        s = p["settings"]
        return {"name": p["name"], "width": s.get("width"), "height": s.get("height"), "fps": s.get("fps"),
                "duration": tl["duration"], "shots": len(tl["clips"]), "audio": len(p.get("audio") or []),
                "overlays": len(p.get("overlays") or []), "markers": len(tl["markers"]), "captionTracks": tracks,
                "captionStyles": [{"id": c["id"], "name": c.get("name") or ""} for c in p.get("captionStyles") or []],
                "undo": self._app.store.undo_state(turn.pid)["undoLabel"]}

    def _t_timeline(self, turn: _Turn, args: dict) -> dict:
        tl = edits.timeline(self._app.store.get(turn.pid))
        lo, hi = _num(args.get("from"), 0), _num(args.get("to"), math.inf)
        out: dict = {"duration": tl["duration"]}
        _capped([c for c in tl["clips"] if _overlaps(c["start"], c["length"], lo, hi)], "clips", out)
        _capped([i for i in tl["items"] if _overlaps(i["start"], i["length"], lo, hi)], "items", out)
        return _capped([m for m in tl["markers"] if lo <= m["start"] <= hi], "markers", out)

    def _t_list_captions(self, turn: _Turn, args: dict) -> dict:
        p = self._app.store.get(turn.pid)
        ids = {t["id"] for t in p.get("captionTracks") or []}
        track = args.get("track") or None
        if track and track != "main" and track not in ids:
            raise AppError("字幕軌道不存在")
        lo, hi = _num(args.get("from"), 0), _num(args.get("to"), math.inf)
        caps = []
        for c in sorted(p["captions"], key=lambda c: float(c.get("start", 0))):
            row = c.get("track") if c.get("track") in ids else "main"
            start = float(c.get("start", 0))
            if (track is None or row == track) and _overlaps(start, float(c.get("end", start)) - start, lo, hi):
                caps.append({**{k: c[k] for k in CAPTION_KEYS if k in c}, "track": row})
        offset = _int(args.get("offset"), 0, 0, len(caps))
        limit = _int(args.get("limit"), 100, 1, MAX_ROWS)
        return {"total": len(caps), "offset": offset, "captions": caps[offset:offset + limit]}

    def _t_apply_ops(self, turn: _Turn, args: dict) -> dict:
        ops = args.get("ops")
        if not isinstance(ops, list) or not ops:
            raise AppError("ops 要是非空的陣列")
        filled = []
        for op in ops:
            if not isinstance(op, dict):
                raise AppError("每個 op 都要是物件")
            if not isinstance(op.get("op"), str):
                raise AppError('每個 op 物件都要有 "op" 欄位寫操作名稱，其他欄位放在旁邊，例如 '
                               '{"op": "caption.update", "id": "c_ab12cd34ef", "patch": {"text": "新字"}}')
            op = _main_row(op)
            key, prefix = ID_PREFIX.get(op.get("op"), (None, ""))
            if key and isinstance(op.get(key), dict) and not op[key].get("id"):
                op = {**op, key: {**op[key], "id": new_id(prefix)}}
            filled.append(op)
        p = self._app.store.get(turn.pid)
        shots = {s["id"]: s for s in p["shots"]}
        for o in filled:  # a video shot plays its trim whatever its duration field says
            shot = shots.get(o.get("id")) if o["op"] == "shot.update" else None
            if shot and "duration" in (o.get("patch") or {}) and video_take(p, shot):
                raise AppError(f"鏡頭 {shot['id']} 是影片鏡頭，長度由修剪範圍決定，改 duration 不會變；"
                               "要縮短請用 edit trim（side end，t 是時間軸上新的結尾秒數）")
        if any(o["op"].startswith("shot.") for o in filled):  # as the edit route: the other tracks follow
            filled = [{"op": "linked", "ops": filled}]
        label = (LABEL + (str(args.get("label") or "").strip() or "修改"))[:80]
        result = self._app.store.apply(turn.pid, filled, origin="user", label=label)
        if not result["changed"]:
            return {**result, "note": "沒有任何變更（要改的 id 可能不存在，或值本來就是這樣）"}
        turn.made.append(label)
        return {**result, "label": label}

    def _t_edit(self, turn: _Turn, args: dict) -> dict:
        out = self._route("POST", f"/api/projects/{turn.pid}/edit", args)
        if out.get("changed"):
            turn.made.append(out["label"])
        return out

    def _t_import_bilingual(self, turn: _Turn, args: dict) -> dict:
        body = {**args, "text": args.get("text") or turn.attachment}
        if not body["text"]:
            raise AppError("沒有要匯入的文字：使用者沒有附加檔案，也沒有給 text")
        out = self._route("POST", f"/api/projects/{turn.pid}/captions/bilingual", body)
        turn.made.append("匯入雙語字幕")
        return {**out, "segments": out["segments"][:5]}  # the model needs the count, not every line back

    def _t_align_captions(self, turn: _Turn, args: dict) -> dict:
        return self._route("POST", f"/api/projects/{turn.pid}/captions/align", {"track": args.get("track")})

    def _t_transcribe(self, turn: _Turn, args: dict) -> dict:
        return {"job": self._app.jobs.submit("transcribe", turn.pid, str(args.get("shotId") or "") or None, {})}

    def _t_job_status(self, turn: _Turn, args: dict) -> dict:
        job = self._app.jobs.get(str(args.get("id") or ""))
        return {k: job.get(k) for k in ("id", "kind", "title", "status", "progress", "message", "error", "shotId")}

    def _t_undo(self, turn: _Turn, args: dict) -> dict:
        label = self._app.store.undo_state(turn.pid)["undoLabel"]
        if not label:
            raise AppError("沒有可以復原的步驟")
        if not label.startswith(LABEL) and label not in turn.made:
            raise AppError(f"最新一步「{label}」不是 AI 助理做的，請使用者自己按復原")
        out = self._app.store.undo(turn.pid)
        if turn.made and turn.made[-1] == label:
            turn.made.pop()
        return out
