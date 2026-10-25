"""The storyboard as one script text (SB-14), and that text written back to the shots.

The project keeps no script of its own: the text is rendered from the shots whenever it is read, so an edit made
on a shot is in it the next time. Applying an edited text compares it, field by field, with the text it was
rendered from and writes only the fields that differ, so a shot edited meanwhile keeps that edit unless the same
field was changed in the text too.

    #3 屋頂賞月
    描述：夜晚的屋頂，小黑貓背對鏡頭
      續行縮排兩格
    運鏡：緩慢推近
    字幕：第一句
    字幕：第二句
    片長：4

"#N" is the N-th shot when the text was rendered. "#" with no number, or a number past the last shot, starts a
new shot, added after the shot of the block above it. A block left out leaves its shot alone: the text never
deletes or reorders shots. Caption lines follow the shot editor (CP-06): the i-th line is the shot's i-th bound
caption, an emptied line removes that caption, and further lines are added after the last one.
"""
from __future__ import annotations

import copy
import re

from .ops import apply_ops
from .sequence import build_sequence
from .util import AppError, new_id, random_seed

MAX_TEXT = 1_000_000
MAX_NEW = 200  # new shots in one apply, as in script import
INDENT = "  "
MIN_LINE = 0.5  # shortest caption a new line gets, as in the shot editor's "再加一句"
TEXT_FIELDS = ("name", "prompt", "motion", "camera", "notes")
_LABEL = {"描述": "prompt", "畫面": "prompt", "動作": "motion", "運鏡": "camera", "字幕": "captions",
          "旁白": "captions", "片長": "duration", "秒數": "duration", "備註": "notes"}
_HEAD_RE = re.compile(r"#(\d*)[ \t]*(.*)")
_FIELD_RE = re.compile(r"(" + "|".join(_LABEL) + r")[ \t]*[:：][ \t]*(.*)")
_SECONDS_RE = re.compile(r"(\d+(?:\.\d+)?)[ \t]*(?:秒|s|sec)?", re.IGNORECASE)


def shot_captions(project: dict, sid: str) -> list[dict]:
    """The captions bound to a shot, in time order."""
    return sorted((c for c in project.get("captions") or [] if c.get("shot") == sid),
                  key=lambda c: float(c.get("start") or 0.0))


def _field(label: str, text: str) -> list[str]:
    first, *rest = (text or "").split("\n")
    return [f"{label}：{first}", *(f"{INDENT}{line}" if line else "" for line in rest)]


def render(project: dict) -> str:
    blocks = []
    for i, shot in enumerate(project["shots"]):
        name = (shot.get("name") or "").replace("\n", " ")
        lines = [f"#{i + 1} {name}".rstrip(), *_field("描述", shot.get("prompt", ""))]
        for label, key in (("動作", "motion"), ("運鏡", "camera")):
            if shot.get(key):
                lines += _field(label, shot[key])
        for cap in shot_captions(project, shot["id"]) or [{}]:
            lines += _field("字幕", cap.get("text", ""))
        lines.append(f"片長：{round(float(shot.get('duration') or 0.0), 3):g}")
        if shot.get("notes"):
            lines += _field("備註", shot["notes"])
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + "\n"


def parse(text: str) -> list[dict]:
    """The blocks in text order: {number, name, head, line, prompt, motion, camera, notes, duration, captions}.

    Lines before the first "#" line are ignored. A line that is not a label continues the field above it (text
    right under the header is the description); blank lines stay in a field only before an indented line."""
    blocks: list[dict] = []
    block: dict | None = None
    key = ""
    blank = 0
    for n, raw in enumerate(text.split("\n"), 1):
        line = raw.rstrip()
        if not line.strip():
            blank += 1
            continue
        indented = line[0] in " \t　"
        if not indented and line.startswith("#"):
            m = _HEAD_RE.fullmatch(line)
            block = {"number": int(m.group(1)) if m.group(1) else None, "name": m.group(2).strip(), "head": line,
                     "line": n, "prompt": "", "motion": "", "camera": "", "notes": "", "duration": "", "captions": []}
            blocks.append(block)
            key, blank = "", 0
            continue
        if block is None:
            continue
        m = None if indented else _FIELD_RE.fullmatch(line)
        if m:
            key, value = _LABEL[m.group(1)], m.group(2).strip()
            if key == "captions":
                block["captions"].append(value)
            else:
                block[key] = f"{block[key]}\n{value}" if block[key] else value
        else:
            key = key or "prompt"
            body = line[len(INDENT):] if line.startswith(INDENT) else line.lstrip()
            sep = "\n" * (blank + 1 if indented else 1)
            if key == "captions":
                block["captions"][-1] = f"{block['captions'][-1]}{sep}{body}" if block["captions"][-1] else body
            else:
                block[key] = f"{block[key]}{sep}{body}" if block[key] else body
        blank = 0
    return blocks


def _seconds(block: dict) -> float | None:
    value = block["duration"]
    if not value:
        return None
    m = _SECONDS_RE.fullmatch(value)
    seconds = float(m.group(1)) if m else -1.0
    if not 0.2 <= seconds <= 600:
        raise AppError(f"第 {block['line']} 行「{block['head']}」的片長「{value}」要是 0.2 到 600 秒")
    return seconds


def _wrap(ops: list[dict], project: dict, link: bool) -> list[dict]:
    """Shot edits as the page sends them (actions.js ops): with the link toggle on, the other tracks follow."""
    if not ops:
        return []
    if link:
        return [{"op": "linked", "ops": ops}]
    if any(s.get("parked") for s in project["shots"]):
        return [{"op": "linked", "ops": ops, "shift": False}]
    return ops


def _new_lines(sid: str, last: dict | None, clip: dict | None, texts: list[str], where: str) -> list[dict]:
    """Captions for lines past the shot's last one: after it up to the clip's end, or sharing its time with it."""
    if clip is None:
        raise AppError(f"{where}不在輸出序列，放入序列後才能加字幕")
    clip_end = round(clip["start"] + clip["length"], 3)
    n = len(texts)
    if last is None:
        start, end, parts = clip["start"], clip_end, n
    elif clip_end - last["end"] >= MIN_LINE * n:
        start, end, parts = last["end"], clip_end, n
    else:
        start, end, parts = last["start"], last["end"], n + 1
    step = (end - start) / parts
    if step < MIN_LINE:
        raise AppError(f"{where}的時間不夠再加 {n} 句字幕，請先到剪輯頁延長鏡頭或字幕")
    times = [round(start + step * i, 3) for i in range(parts)] + [round(end, 3)]
    ops: list[dict] = []
    if parts > n:
        ops.append({"op": "caption.update", "id": last["id"], "patch": {"end": times[1]}})
        times = times[1:]
    position = (last or {}).get("position") or "bottom"
    return ops + [{"op": "caption.add", "item": {"id": new_id("c"), "start": times[i], "end": times[i + 1],
                                                 "text": text, "position": position, "shot": sid}}
                  for i, text in enumerate(texts)]


def _caption_ops(work: dict, sid: str, texts: list[str], old: list[str], clip: dict | None,
                 where: str) -> list[dict]:
    """Line i of the text against line i as rendered: only a line that changed touches the shot's i-th caption, so
    a caption added on the shot meanwhile stays."""
    caps = shot_captions(work, sid)
    ops: list[dict] = []
    gone: set[str] = set()
    extra: list[str] = []
    for i in range(max(len(texts), len(old))):
        text = texts[i] if i < len(texts) else ""  # a line taken out of the text removes its caption
        if i < len(old) and text == old[i]:
            continue
        if i >= len(caps):
            if text:
                extra.append(text)
        elif not text:
            ops.append({"op": "caption.delete", "id": caps[i]["id"]})
            gone.add(caps[i]["id"])
        elif text != caps[i].get("text", ""):
            ops.append({"op": "caption.update", "id": caps[i]["id"], "patch": {"text": text}})
    if extra:
        kept = [cap for cap in caps if cap["id"] not in gone]
        ops += _new_lines(sid, kept[-1] if kept else None, clip, extra, where)
    return ops


def sync_ops(project: dict, defaults: dict, text: str, base: str, ids: list[str], link: bool) -> tuple[list, dict]:
    """The ops that write an edited script back to the shots, and what they change.

    `base` is the text as rendered and `ids` the shot ids it was rendered for, in order."""
    blocks = parse(text)
    before = {b["number"]: b for b in parse(base) if b["number"]}
    numbered = [b["number"] for b in blocks if b["number"] and b["number"] <= len(ids)]
    twice = next((n for n in numbered if numbered.count(n) > 1), None)
    if twice:
        raise AppError(f"「#{twice}」出現了兩次，一個鏡頭只能有一段")
    shots = {s["id"]: s for s in project["shots"]}
    order = [s["id"] for s in project["shots"]]
    shot_ops: list[dict] = []
    lines: dict[str, tuple[list[str], list[str], str]] = {}
    notes: list[str] = []
    added: list[str] = []
    updated = 0
    anchor: str | None = None  # the shot a new block goes after; None: the start
    for b in blocks:
        n = b["number"]
        where = f"第 {b['line']} 行「{b['head']}」"
        sid = ids[n - 1] if n and n <= len(ids) else None
        if sid is not None:
            if sid not in shots:
                notes.append(f"{where}的鏡頭已被刪除，這段沒有寫回")
                continue
            old = before.get(n) or {**{k: None for k in TEXT_FIELDS}, "duration": None, "captions": None}
            patch = {k: b[k] for k in TEXT_FIELDS if b[k] != old[k]}
            if b["duration"] != old["duration"]:
                seconds = _seconds(b)
                if seconds is None:
                    raise AppError(f"{where}的片長不能空白")
                patch["duration"] = seconds
            if patch:
                shot_ops.append({"op": "shot.update", "id": sid, "patch": patch})
                updated += 1
            if b["captions"] != old["captions"]:
                lines[sid] = (b["captions"], old["captions"] or [], where)
            anchor = sid
            continue
        sid = new_id("s")
        shot = {"id": sid, **{k: b[k] for k in TEXT_FIELDS}, "seed": random_seed()}
        seconds = _seconds(b)
        if seconds is not None:
            shot["duration"] = seconds
        index = order.index(anchor) + 1 if anchor else 0
        order.insert(index, sid)
        shot_ops.append({"op": "shot.add", "shot": shot, "index": index})
        added.append(sid)
        if any(b["captions"]):
            lines[sid] = (b["captions"], [], where)
        anchor = sid
    if len(added) > MAX_NEW:
        raise AppError(f"一次最多新增 {MAX_NEW} 個鏡頭")
    ops = _wrap(shot_ops, project, link)
    caption_ops: list[dict] = []
    if lines:
        # captions are timed on the sequence the shot edits leave, with the other tracks already moved along
        work = copy.deepcopy(project)
        if ops:
            apply_ops(work, ops, defaults)
        clips = {c["shot"]["id"]: c for c in build_sequence(work)}
        for sid, (texts, old, where) in lines.items():
            caption_ops += _caption_ops(work, sid, texts, old, clips.get(sid), where)
    changed = sum(op["op"] != "caption.update" or "text" in op["patch"] for op in caption_ops)
    return ops + caption_ops, {"updated": updated, "added": len(added), "captions": changed, "notes": notes,
                               "shotIds": added}
