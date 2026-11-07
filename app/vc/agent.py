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
import urllib.parse
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from . import agentlook as look
from . import autocolor, edits, projdocs
from .bilingual import MAX_TEXT
from .export import active_adjustments
from .sequence import build_sequence, total_duration, video_take
from .server import FileResponse, Request, Response
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
MAX_DOCS = 8  # documents one message can mention

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
- Brightness, exposure and white balance: auto_color (measures each shot and writes its grade.wb). \
To look before changing, analyze_picture. Shaky footage: auto_stabilize measures each video shot and stabilizes \
the shaky ones; one shot by hand: edit stabilize, settings tune it (smoothing higher is steadier, tripod true \
locks the frame).
- Playback speed: shot.update patch speed, no split. An upper-track video clip (timeline type overlay): \
overlay.update {"id", "patch": {"speed", "length"}}, speed 0.25 to 4 (pictures have none); its length is in \
timeline seconds, so keep the same footage with length = length * old speed / new speed. \
Reorder or swap shots: shot.move (a swap is one shot.move).
- Cutting, trimming, freeze frames, detaching audio, volume and keyframes: use the edit tool, not raw ops. \
Split or trim only when the user asked to cut, shorten or freeze. \
A shot's freeze still is 5 seconds unless the freeze edit gets length (seconds).
- import_bilingual puts a Chinese/English parallel text onto two caption rows (Chinese above, English below). \
When the user attached a file, call it without text. english "double" keeps two English lines, "single" joins \
them into one. align true then times the captions to the speech in the clips.
- transcribe, align_captions and some edits start background jobs that take minutes. Start them, tell the user \
the job was started and that the result appears when it finishes; do not poll job_status in a loop.
- Seeing the picture: view_frame shows the shot on the timeline at a time through its colour grade and the \
adjustment layers, as pictures in the next message. Captions, titles, upper-track clips, masks and transitions \
are not drawn on it: read those with list_captions or timeline. Look before you judge how a shot looks, and say \
what you saw. Pictures the user attached show the preview as it is.
- Project documents: the user keeps the script, the client's notes and reference pictures in the project's \
document folder and mentions one as @name. A mentioned document's beginning comes with the message; read_document \
reads the rest page by page (next is the offset of the next page) or shows a picture document. list_documents \
lists them. Follow written notes point by point, and say which ones you could not do and why.
- How the app works: app_docs reads the API reference (doc api) or the user manual (doc manual): headings first, \
then a section, or find to search. call_api calls one of the app's own HTTP routes (path with {pid} for the open \
project) for what no other tool covers, such as GET /api/projects/{pid}/script or POST /api/jobs (an export). \
Prefer the dedicated tools; call_api cannot delete, undo, open files or windows, or touch other projects.
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
          "length: seconds of a shot's still, default 5); detach {id|t}; restore {id}; reverse {id|t}; stabilize {type?, id|t, on?, settings?} (shot or overlay; on omitted toggles, "
          "true makes sure it is stabilized, false switches back; settings: shakiness 1-10, smoothing 0-300 frames, "
          "zoom -100..100 %, sharpen 0-2, tripod, optzoom 0|1|2, crop keep|black, accuracy, stepsize, mincontrast, "
          "maxshift, maxangle, zoomspeed, optalgo opt|gauss|avg, interpol no|linear|bilinear|bicubic, relative; "
          "engine gyro uses Gyroflow on the camera original in the project's camera folder instead, and then only "
          "gyroSmooth 0.05-1 counts; "
          "a new combination renders a copy in a background job); stabilizeShots {ids, on?, settings?}; "
          "volume {type, id, db, in?, out?}; "
          "keyframe {type, id, t, values?}; roll/slip/slide {id, by} (picture only: other tracks stay, the shots' own sound is detached in place); "
          "declip {id, type?: shot|audio} (lower clipped peaks); lift {id, layer?, start?} (shot to an upper layer); "
          "drop {id, index} (upper clip back into the shots); rippleDelete {type, id} (close the gap on that track); "
          "title {template: opening|chapter|lower-box|lower-plain|corner, values {field: text}, start, len?}; "
          "glossaryFix {ids?}; place {media, at?, layer?, track?}; moveShots {ids, index}. type: shot (default), overlay, audio, "
          "caption, adjust. Without id the clip under t is used.",
          {"cmd": {"type": "string", "enum": ["split", "splitAll", "trim", "freeze", "detach", "restore", "reverse",
                                              "stabilize", "stabilizeShots", "volume", "keyframe", "roll",
                                              "slip", "slide", "declip", "lift", "drop", "rippleDelete", "title",
                                              "glossaryFix", "place", "moveShots"]},
           "t": NUM, "type": STR, "id": STR, "side": STR, "by": NUM, "db": NUM, "in": NUM, "out": NUM,
           "length": NUM, "values": {"type": "object"}, "ids": {"type": "array", "items": STR},
           "on": {"type": "boolean"}, "settings": {"type": "object"}, "layer": STR, "start": NUM,
           "index": {"type": "integer"}, "template": STR, "len": NUM, "media": STR, "at": NUM, "track": STR},
          ("cmd",)),
    _tool("analyze_picture", "Measure one picture without changing anything: brightness percentiles, clipped "
          "highlights, average colour, and the white balance / exposure auto_color would set. shot + t (clip "
          "seconds), or at (timeline seconds). x/y/size (0..1 of the picture) also measure a spot that should be "
          "white or grey.",
          {"shot": STR, "t": NUM, "at": NUM, "x": NUM, "y": NUM, "size": NUM}),
    _tool("auto_color", "Automatic white balance and exposure for shots, as one undo step: each shot is measured "
          "on a few frames and its grade.wb is set. ids: shot ids (omit for every shot). wb: auto (default) "
          "balances a mild colour cast and leaves near-grey pictures and coloured stage light as they are; on "
          "always balances (the user says the colour is off); off keeps the colour. exposure false skips "
          "brightness. Medium shots and close-ups are metered on the people's faces (brought to about 0.47), wide "
          "shots on the whole picture (each shot's own median kept inside 0.42-0.60); subject scene meters every "
          "shot on the whole picture. target 0.2-0.7 overrides the brightness aimed at (higher is brighter). "
          "strength 0-1 of the colour correction (default 0.6).",
          {"ids": {"type": "array", "items": STR}, "wb": {"type": "string", "enum": ["auto", "on", "off"]},
           "exposure": {"type": "boolean"}, "target": NUM, "strength": NUM,
           "subject": {"type": "string", "enum": ["auto", "scene"]}}),
    _tool("auto_stabilize", "Measure how much each video shot shakes (jitter, % of the picture width; a steady "
          "pan reads 0) and stabilize those at or past threshold (default 0.3), as one undo step; the copies render "
          "as background jobs. ids: shot ids (omit for every video shot in the sequence). dryRun true only "
          "reports. settings: stabilize settings on top of the automatic smoothing. engine: vidstab (from the "
          "picture, default), gyro (Gyroflow from the camera originals' gyro data, found by file name in the "
          "project's camera folder) or auto (gyro where the original is found, else vidstab); gyroFolder sets "
          "that folder.",
          {"ids": {"type": "array", "items": STR}, "threshold": NUM, "dryRun": {"type": "boolean"},
           "settings": {"type": "object"}, "engine": {"type": "string", "enum": ["vidstab", "gyro", "auto"]},
           "gyroFolder": STR}),
    _tool("import_bilingual", "Import a Chinese/English parallel text as captions on two rows (中文 above, English "
          "below), one after another from start. text: omit to use the user's attachment or the one document they "
          "mentioned; doc: a project document's name. replace: clear those two rows first. align: then queue "
          "speech alignment.",
          {"text": STR, "doc": STR, "english": {"type": "string", "enum": ["double", "single"]}, "start": NUM,
           "replace": {"type": "boolean"}, "align": {"type": "boolean"}}),
    _tool("align_captions", "Queue a background job that moves one caption row's captions onto the matching "
          "speech in the clips (the row below that shares their times follows).", {"track": STR}, ("track",)),
    _tool("transcribe", "Queue a background job that turns one video shot's speech into captions.",
          {"shotId": STR}, ("shotId",)),
    _tool("job_status", "State and progress of one background job.", {"id": STR}, ("id",)),
    _tool("undo", "Undo the latest undo step, only when you made it."),
    _tool("view_frame", "Look at the picture: the shot on the timeline at `at` (timeline seconds), or shot + t "
          "(seconds into that clip), or times (up to 6 timeline seconds, e.g. to compare shots). The frames come as "
          "pictures in the next message, through the shot's grade and the adjustment layers; captions, titles and "
          "upper-track clips are not drawn.",
          {"at": NUM, "shot": STR, "t": NUM, "times": {"type": "array", "items": NUM}}),
    _tool("list_documents", "The project's documents (script, client notes, reference pictures) by name, kind "
          "(text or image) and size."),
    _tool("read_document", "Read a project document: text from offset (characters), limit up to 9000; the result's "
          "next is where the following page starts. A picture document comes as a picture in the next message.",
          {"name": STR, "offset": {"type": "integer"}, "limit": {"type": "integer"}}, ("name",)),
    _tool("app_docs", "Read the app's own documentation. doc: api (HTTP API reference, the default) or manual "
          "(the user manual). Without section or find: the headings. section: a heading number such as \"3.7\" or "
          "words of a heading; offset pages through a long one. find: lines containing these words.",
          {"doc": {"type": "string", "enum": ["api", "manual"]}, "section": STR, "find": STR,
           "offset": {"type": "integer"}}),
    _tool("call_api", "Call one of the app's HTTP routes in-process, for what no other tool covers (see app_docs "
          "api). method GET, POST or PATCH; path such as /api/projects/{pid}/script (a query string is allowed); "
          "body: the JSON body. Reads anywhere in the open project and the styles, title templates and jobs; writes "
          "only to the open project's editing routes and POST /api/jobs.",
          {"method": {"type": "string", "enum": ["GET", "POST", "PATCH"]}, "path": STR,
           "body": {"type": "object", "additionalProperties": True}}, ("method", "path")),
]


@dataclass
class _Turn:
    pid: str
    chat_id: str
    attachment: str
    made: list[str] = field(default_factory=list)  # undo labels of the steps this turn made, oldest first
    images: list[tuple[str, str]] = field(default_factory=list)  # (label, data URL) waiting for the next message
    sent: int = 0  # pictures sent to the model so far


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
        self._blind: set[tuple[str, str]] = set()  # (baseUrl, model) of models that refused a picture

    def cancel(self, chat_id: str) -> bool:
        with self._lock:
            stop = self._stops.get(chat_id)
        if stop:
            stop.set()
        return bool(stop)

    def run(self, pid: str, data: dict) -> dict:
        """{id?, messages: [{role, content}], attachment?: {name, text}, docs?: [project document names],
        frames?: [{name, url: JPEG data URL}]} -> {id, reply, steps, stopped, made}
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
        notes, texts, images = self._mentioned(pid, data.get("docs"))
        messages[-1]["content"] += notes
        if not attachment and len(texts) == 1:
            text = texts[0]
        images += look.frame_list(data.get("frames"))
        turn = _Turn(pid, chat_id, text)
        if images:
            if self._blind_model():
                messages[-1]["content"] += "\n\n" + look.BLIND_NOTE
            else:
                messages[-1] = {"role": "user", "content": look.pictures(messages[-1]["content"], images)}
                turn.sent = len(images)
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
            answer = self._complete(messages)
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
            if turn.images:  # tool results are text only: the pictures go in one user message after them
                labels = ", ".join(label for label, _ in turn.images)
                messages.append({"role": "user", "content": look.pictures(f"[Pictures: {labels}]", turn.images)})
                turn.images = []
        return {"reply": f"已達單次對話的步驟上限（{MAX_STEPS} 輪），先停在這裡；要繼續請再交代一次。",
                "steps": steps, "stopped": "limit"}

    # ------------------------------------------------------------------ pictures and documents
    def _model_key(self) -> tuple[str, str]:
        cfg = self._app.settings.section("llm")
        return str(cfg.get("baseUrl") or ""), str(cfg.get("model") or "")

    def _blind_model(self) -> bool:
        return self._model_key() in self._blind

    def _complete(self, messages: list[dict]) -> dict:
        """One model round. A model that fails on a message with pictures is asked again without them; when that
        works it is remembered as one that cannot take pictures (until the app restarts)."""
        if self._blind_model() and look.has_images(messages):
            messages[:] = look.without_images(messages)
        try:
            return self._app.llm.complete(messages, TOOLS)
        except AppError as exc:
            if exc.status != 502 or not look.has_images(messages):
                raise
            log.info("model call with pictures failed (%s); asking again without them", exc.message)
            plain = look.without_images(messages)
            answer = self._app.llm.complete(plain, TOOLS)
            self._blind.add(self._model_key())
            messages[:] = plain
            return answer

    def _can_see(self, turn: _Turn, count: int) -> None:
        if self._blind_model():
            raise AppError("目前的文字模型不能看圖片：請使用者到「引擎 › 文字模型」換成支援圖片的模型")
        if turn.sent + count > look.MAX_IMAGES:
            raise AppError(f"這一輪已看了 {turn.sent} 張圖，上限 {look.MAX_IMAGES} 張；請先依目前看到的回覆使用者")

    def _show(self, turn: _Turn, label: str, url: str) -> None:
        turn.images.append((label, url))
        turn.sent += 1

    def _mentioned(self, pid: str, raw: Any) -> tuple[str, list[str], list[tuple[str, str]]]:
        """(notes for the message, full texts of the text documents, pictures) of the @mentioned documents."""
        if raw is None:
            return "", [], []
        if not isinstance(raw, list) or len(raw) > MAX_DOCS or not all(isinstance(n, str) for n in raw):
            raise AppError(f"提到的文件格式不正確（最多 {MAX_DOCS} 個）")
        project_dir = self._app.store.project_dir(pid)
        notes, texts, images = "", [], []
        for name in dict.fromkeys(raw):
            path = projdocs.find(project_dir, name)
            if projdocs.kind_of(path.name) == "image":
                url = projdocs.jpeg_url(self._app.media(), path, project_dir / "media" / "cache")
                images.append((f"document \"{path.name}\"", url))
                notes += f"\n\n[Document \"{path.name}\": a picture, shown below.]"
                continue
            text = projdocs.text_of(path)
            texts.append(text)
            more = (f" {len(text)} characters; read_document reads the rest from offset {PREVIEW}"
                    if len(text) > PREVIEW else f" {len(text)} characters, all of it")
            notes += f"\n\n[Document \"{path.name}\",{more}. Beginning:]\n{text[:PREVIEW]}"
        return notes, texts, images

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

    def _route(self, method: str, path: str, body: dict, query: dict | None = None) -> Any:
        """Call one of the app's own routes in-process, exactly as the HTTP API would."""
        fn, params, other = self._app.router.match(method, path)
        if fn is None:
            raise AppError(f"沒有這個 API：{method} {path}" + ("（這個路徑要用別的 method）" if other else ""), 404)
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        return fn(Request(_Body(raw), method, path, query or {}, params))

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

    def _t_analyze_picture(self, turn: _Turn, args: dict) -> dict:
        out = self._route("POST", f"/api/projects/{turn.pid}/analyze", args)
        reading = {"picture": autocolor.brief(out["stats"]), "metered": autocolor.metered(out["decision"]),
                   "suggest": out["suggest"]}
        if out.get("region"):
            reading["spot"] = autocolor.brief(out["region"])
        return {**{k: out[k] for k in ("shot", "t", "item") if k in out}, **reading}

    def _t_auto_color(self, turn: _Turn, args: dict) -> dict:
        wb = args.get("wb", "auto")
        body = {**args, "wb": {"on": True, "off": False}.get(wb, wb), "target": args.get("target") or "auto",
                "label": LABEL + "自動調色"}
        out = self._route("POST", f"/api/projects/{turn.pid}/auto-color", body)
        if out.get("changed"):
            turn.made.append(LABEL + "自動調色")
        shots = [{"id": r["id"], "wb": r["decision"]["wb"], "metered": autocolor.metered(r["decision"]),
                  "before": autocolor.brief(r["before"]), "after": autocolor.brief(r["after"])}
                 for r in out["shots"][:MAX_ROWS]]
        return {"changed": out.get("changed"), "shots": shots}

    def _t_auto_stabilize(self, turn: _Turn, args: dict) -> dict:
        out = self._route("POST", f"/api/projects/{turn.pid}/auto-stabilize", {**args, "label": LABEL + "自動防抖"})
        if out.get("changed"):
            turn.made.append(LABEL + "自動防抖")
        shots = [{k: r[k] for k in ("id", "decision", "jitter", "reason") if k in r} for r in out["shots"][:MAX_ROWS]]
        return {"changed": out.get("changed"), "shots": shots, "jobs": [j["id"] for j in out.get("jobs") or []]}

    def _t_import_bilingual(self, turn: _Turn, args: dict) -> dict:
        body = {k: v for k, v in args.items() if k != "doc"}
        if args.get("doc"):
            body["text"] = projdocs.text_of(projdocs.find(self._app.store.project_dir(turn.pid), str(args["doc"])))
        else:
            body["text"] = args.get("text") or turn.attachment
        if not body["text"]:
            raise AppError("沒有要匯入的文字：使用者沒有附加檔案或提到文件，也沒有給 text")
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

    # ------------------------------------------------------------------ pictures, documents, manuals, the API
    def _t_view_frame(self, turn: _Turn, args: dict) -> dict:
        p = self._app.store.get(turn.pid)
        clips = build_sequence(p)
        times = args.get("times")
        if isinstance(times, list) and times:
            wanted = [look.timeline_point(clips, _num(x, 0.0)) for x in times[:look.MAX_LOOK]]
        elif args.get("shot"):
            shot_id = str(args["shot"])
            t = max(0.0, _num(args.get("t"), 0.0))
            clip = next((c for c in clips if c["shot"]["id"] == shot_id), None)
            wanted = [(shot_id, t, clip["start"] + min(t, clip["length"]) if clip else None)]
        else:
            wanted = [look.timeline_point(clips, _num(args.get("at"), 0.0))]
        self._can_see(turn, len(wanted))
        project_dir = self._app.store.project_dir(turn.pid)
        spans = active_adjustments(p, total_duration(clips)) if clips else []
        frames = []
        for shot_id, t, at in wanted:
            info, url = look.render_frame(self._app.media(), p, project_dir, spans, shot_id, t, at)
            frames.append(info)
            where = f"{info['at']}s" if "at" in info else f"{info['t']}s into the shot"
            self._show(turn, f"frame at {where} (shot {shot_id})", url)
        return {"frames": frames, "note": "The pictures follow in the next message, in this order. They show the "
                                          "shot with its grade and the adjustment layers; captions, titles, "
                                          "upper-track clips, masks and transitions are not drawn."}

    def _t_list_documents(self, turn: _Turn, args: dict) -> dict:
        docs = projdocs.listing(self._app.store.project_dir(turn.pid))
        out: dict = {"documents": docs}
        if not docs:
            out["note"] = "沒有專案文件：使用者可以在 AI 助理面板的「專案文件」上傳"
        return out

    def _t_read_document(self, turn: _Turn, args: dict) -> dict:
        project_dir = self._app.store.project_dir(turn.pid)
        path = projdocs.find(project_dir, str(args.get("name") or ""))
        if projdocs.kind_of(path.name) == "image":
            self._can_see(turn, 1)
            self._show(turn, f"document \"{path.name}\"",
                       projdocs.jpeg_url(self._app.media(), path, project_dir / "media" / "cache"))
            return {"name": path.name, "kind": "image", "note": "The picture follows in the next message."}
        text = projdocs.text_of(path)
        offset = _int(args.get("offset"), 0, 0, len(text))
        limit = _int(args.get("limit"), look.PAGE, 200, look.MAX_PAGE)
        out: dict = {"name": path.name, "kind": "text", "length": len(text), "offset": offset,
                     "text": text[offset:offset + limit]}
        if offset + limit < len(text):
            out["next"] = offset + limit
        return out

    def _t_app_docs(self, turn: _Turn, args: dict) -> dict:
        return look.lookup(str(args.get("doc") or "api"), str(args.get("section") or "").strip(),
                           str(args.get("find") or "").strip(), _int(args.get("offset"), 0, 0, 10 ** 9))

    def _t_call_api(self, turn: _Turn, args: dict) -> Any:
        method = str(args.get("method") or "GET").upper()
        split = urllib.parse.urlsplit(str(args.get("path") or "").strip())
        path = split.path.replace("{pid}", turn.pid).rstrip("/")
        query = dict(urllib.parse.parse_qsl(split.query, keep_blank_values=True))
        body = args.get("body") or {}
        if not isinstance(body, dict):
            raise AppError("body 要是 JSON 物件")
        if not look.api_allowed(method, path, turn.pid):
            raise AppError(f"call_api 不能用 {method} {path or '/'}：只能讀這個專案與樣式、標題範本、背景工作；寫入只限"
                           "這個專案的剪輯 API 與 POST /api/jobs。刪除、復原、開檔案或視窗請使用者自己在介面上操作", 403)
        if path == "/api/jobs":
            body = {**body, "pid": turn.pid}
        before = self._app.store.undo_state(turn.pid)
        out = self._route(method, path, body, query)
        after = self._app.store.undo_state(turn.pid)
        if after["undoLabel"] and (after["undo"], after["undoLabel"]) != (before["undo"], before["undoLabel"]):
            turn.made.append(after["undoLabel"])
        if isinstance(out, FileResponse):
            return {"note": f"這個 API 回傳檔案 {out.path.name}，助理讀不到檔案內容"}
        if isinstance(out, Response):
            if not out.content_type.startswith(("text/", "application/json")):
                return {"note": f"這個 API 回傳 {out.content_type}，不是文字"}
            data = out.body.decode("utf-8", "replace") if isinstance(out.body, bytes) else out.body
            return {"status": out.status, "contentType": out.content_type, "text": data}
        return out if out is not None else {"ok": True}
