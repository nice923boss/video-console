"""What the assistant can look at besides the project data (vc/agent.py): frames of the edit, project documents
(vc/projdocs.py), the app's own manuals, and which routes its call_api tool may use.

Tool results are text only, so pictures travel as OpenAI-style image parts in a user message after them; a model
that cannot take pictures gets BLIND_NOTE in their place."""
from __future__ import annotations

import base64
import re
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import autocolor, projdocs
from .grade import build_cube, clean_grade, is_neutral
from .media import AS_709
from .paths import ROOT_DIR
from .util import AppError, new_id

if TYPE_CHECKING:
    from .media import Media

MAX_FRAMES = 2  # pictures of the preview one message can carry
MAX_FRAME_URL = 3 * 1024 ** 2  # characters of one such picture's data URL
FRAME_URL = re.compile(r"^data:image/jpeg;base64,[A-Za-z0-9+/=]+$")
MAX_IMAGES = 12  # pictures the model is sent in one turn
MAX_LOOK = 6  # frames one view_frame call returns
PAGE = 6000  # characters read_document and app_docs return by default
MAX_PAGE = 9000
MAX_FINDS = 30  # lines one app_docs search returns
APP_DOCS = {"api": "API.md", "manual": "使用說明.md"}
HEADING = re.compile(r"^(#{1,4}) +(.+?)\s*$")
BLIND_NOTE = ("[A picture was left out here: the current model cannot take pictures. Tell the user to pick a "
              "vision model (one that accepts images) in 引擎 › 文字模型 if they want you to look at pictures.]")
# call_api: writes the assistant may make in the open project (paths after /api/projects/{pid}); undo, history,
# file pickers, uploads, Explorer and the chat itself stay with the user. The SRT import reads a raw body and the
# revision apply route runs the assistant itself, so neither is here.
API_WRITES = re.compile(r"^/(ops|edit|analyze|auto-color|auto-stabilize|captions/align|captions/bilingual|captions/lyrics"
                        r"|script|script/apply|voice-channels|voice-clean|media/[^/]+/p5/link)$")
API_PATCHES = re.compile(r"^/revisions/[^/]+/items/[^/]+$")
API_GLOBAL_GETS = re.compile(r"^/api/(styles|title-templates|jobs|jobs/[^/]+|system/fonts)$")
_docs: dict[str, tuple[int, str]] = {}  # manual name -> (mtime_ns, text)
_docs_lock = threading.Lock()


# ------------------------------------------------------------------ pictures in messages
def image_part(url: str) -> dict:
    return {"type": "image_url", "image_url": {"url": url}}


def pictures(text: str, images: list[tuple[str, str]]) -> list[dict]:
    """Message content: text, then each picture after its label."""
    parts: list[dict] = [{"type": "text", "text": text}]
    for label, url in images:
        parts += [{"type": "text", "text": f"[{label}]"}, image_part(url)]
    return parts


def has_images(messages: list[dict]) -> bool:
    return any(isinstance(m.get("content"), list) and any(p.get("type") == "image_url" for p in m["content"])
               for m in messages)


def without_images(messages: list[dict]) -> list[dict]:
    """The conversation with each picture swapped for a note that it was left out."""
    out = []
    for m in messages:
        if isinstance(m.get("content"), list):
            text = "\n".join(BLIND_NOTE if p.get("type") == "image_url" else str(p.get("text") or "")
                             for p in m["content"])
            m = {**m, "content": text}
        out.append(m)
    return out


def frame_list(raw: Any) -> list[tuple[str, str]]:
    """The preview pictures a message carries: [{name?, url: JPEG data URL}] -> [(label, url)]."""
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > MAX_FRAMES:
        raise AppError(f"附上的畫面格式不正確（最多 {MAX_FRAMES} 張）")
    out = []
    for f in raw:
        url = f.get("url") if isinstance(f, dict) else None
        if not isinstance(url, str) or len(url) > MAX_FRAME_URL or not FRAME_URL.match(url):
            raise AppError("附上的畫面格式不正確（需要 JPEG）")
        out.append((f"picture the user attached: {str(f.get('name') or 'preview')[:80]}", url))
    return out


# ------------------------------------------------------------------ frames of the edit
def timeline_point(clips: list[dict], at: float) -> tuple[str, float, float]:
    """(shot id, seconds into its clip, timeline seconds) of the picture on the timeline at `at`."""
    if not clips:
        raise AppError("序列裡沒有鏡頭")
    end = clips[-1]["start"] + clips[-1]["length"]
    at = min(max(0.0, at), max(0.0, end - 0.05))
    clip = next((c for c in clips if c["start"] <= at < c["start"] + c["length"]), clips[-1])
    if clip["source"] == "empty":
        raise AppError(f"{at:g} 秒是空白鏡頭「{clip['shot'].get('name') or clip['shot']['id']}」，沒有畫面")
    return clip["shot"]["id"], at - clip["start"], at


def render_frame(media: Media, project: dict, project_dir: Path, spans: list[tuple[float, float, dict]],
                 shot_id: str, t: float, at: float | None) -> tuple[dict, str]:
    """(info, JPEG data URL) of one frame of a shot `t` seconds into its clip, through its own grade and the
    adjustment layers (spans, from export.active_adjustments) active at timeline second `at`, as the export bakes
    them into .cube files. At most projdocs.IMAGE_WIDTH pixels wide. `at` None: a shot outside the sequence."""
    shot, take, t_in, speed, length = autocolor.shot_source(project, shot_id)
    local = min(t, max(0.0, length - 0.05))
    still = take.get("kind") == "image"
    file_t = 0.0 if still else t_in + local * speed
    src = autocolor.source_file(project_dir, take)
    own = clean_grade(shot["grade"]) if shot.get("grade") else None
    own = own if own and not is_neutral(own) else None
    layers = [g for a, b, g in spans if at is not None and a <= at < b]
    cache = project_dir / "media" / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    stamp = new_id("look")
    out = cache / f"{stamp}.jpg"
    cubes = [(cache / f"{stamp}-{k}.cube", g) for k, g in enumerate(([own] if own else []) + layers)]
    luts = [f"lut3d=file=media/cache/{c.name}:interp=tetrahedral" for c, _ in cubes]  # relative: no drive colon
    split = 1 if own else 0  # the shot's grade before the fit (as the export), the layers on the scaled frame
    vf = ([] if still else [AS_709]) + luts[:split]
    vf += [f"scale='min({projdocs.IMAGE_WIDTH},iw)':-2:flags=lanczos"] + luts[split:]
    try:
        for cube, grade in cubes:
            cube.write_text(build_cube(grade), encoding="utf-8")
        seek = ["-ss", f"{file_t:.3f}"] if file_t > 0 else []
        media.run(seek + ["-i", str(src), "-frames:v", "1", "-an", "-sn", "-vf", ",".join(vf), "-q:v", "4",
                          str(out)], timeout=120, cwd=project_dir)
        url = "data:image/jpeg;base64," + base64.b64encode(out.read_bytes()).decode("ascii")
    finally:
        for f in (out, *(c for c, _ in cubes)):
            f.unlink(missing_ok=True)
    info: dict = {"shot": shot["id"], "name": shot.get("name") or "", "t": round(local, 3),
                  "fileTime": round(file_t, 3), "grades": len(cubes)}
    if at is not None:
        info["at"] = round(at, 3)
    else:
        info["note"] = "this shot is not in the sequence: no adjustment layers applied"
    if shot.get("disabled") or shot.get("pictureHidden"):
        info["off"] = "the shot's picture is switched off, so the video shows black here"
    return info, url


# ------------------------------------------------------------------ the app's own manuals
def app_doc(which: str) -> str:
    """The text of docs/API.md ("api") or docs/使用說明.md ("manual"), cached while the file is unchanged."""
    name = APP_DOCS.get(which)
    if not name:
        raise AppError("doc 只能是 api 或 manual")
    path = ROOT_DIR / "docs" / name
    if not path.is_file():
        raise AppError(f"找不到說明文件 {name}", 404)
    stamp = path.stat().st_mtime_ns
    with _docs_lock:
        cached = _docs.get(name)
        if cached and cached[0] == stamp:
            return cached[1]
    text = path.read_text(encoding="utf-8")
    with _docs_lock:
        _docs[name] = (stamp, text)
    return text


def sections(text: str) -> list[dict]:
    """Markdown headings outside code blocks: {title, level, start, end} (end: where the next heading of the same
    or a higher level starts)."""
    heads, pos, fenced = [], 0, False
    for line in text.splitlines(keepends=True):
        if line.startswith("```"):
            fenced = not fenced
        found = None if fenced else HEADING.match(line)
        if found:
            heads.append({"title": found.group(2), "level": len(found.group(1)), "start": pos})
        pos += len(line)
    for i, h in enumerate(heads):
        h["end"] = next((n["start"] for n in heads[i + 1:] if n["level"] <= h["level"]), len(text))
    return heads


def section_match(heads: list[dict], key: str) -> dict | None:
    key = key.strip().lstrip("#").strip()
    number = key.rstrip(".")
    for h in heads:  # a heading number: "3.7" finds "3.7 剪輯指令", "3" finds "3. 修改專案"
        first = h["title"].split(" ", 1)[0].rstrip(".")
        if number and first == number:
            return h
    low = key.lower()
    return next((h for h in heads if low and low in h["title"].lower()), None)


def lookup(which: str, section: str, find: str, offset: int) -> dict:
    """app_docs: the headings (no section, no find), one section's text from offset, or the lines holding every
    word of find with the section each is under."""
    text = app_doc(which)
    heads = sections(text)
    if find:
        words = find.lower().split()
        hits, pos = [], 0
        for line in text.splitlines(keepends=True):
            if all(w in line.lower() for w in words):
                under = [h["title"] for h in heads if h["start"] <= pos]
                hits.append({"section": under[-1] if under else "", "line": line.strip()[:300]})
                if len(hits) >= MAX_FINDS:
                    break
            pos += len(line)
        out: dict = {"doc": which, "find": find, "hits": hits}
        if not hits:
            out["note"] = "沒有找到；換個關鍵字，或不帶參數先看章節標題"
        return out
    if not section:
        return {"doc": which, "headings": ["#" * h["level"] + " " + h["title"] for h in heads if h["level"] <= 3]}
    head = section_match(heads, section)
    if head is None:
        raise AppError(f"找不到章節「{section}」；不帶 section 呼叫可以先看章節標題")
    body = text[head["start"]:head["end"]]
    offset = max(0, min(offset, len(body)))
    out = {"doc": which, "section": head["title"], "length": len(body), "offset": offset,
           "text": body[offset:offset + PAGE]}
    if offset + PAGE < len(body):
        out["next"] = offset + PAGE
    return out


def api_allowed(method: str, path: str, pid: str) -> bool:
    """Whether call_api may send method to path (query left off) for the project pid."""
    mine = f"/api/projects/{pid}"
    rest = path[len(mine):] if path == mine or path.startswith(mine + "/") else None
    if method == "GET":
        return rest is not None or bool(API_GLOBAL_GETS.match(path))
    if method == "POST":
        return (rest is not None and bool(API_WRITES.match(rest))) or path == "/api/jobs"
    if method == "PATCH":
        return rest is not None and bool(API_PATCHES.match(rest))
    return False
