"""Client review page (SB-13): a single HTML file of the cut, shot by shot, where a client writes a comment under
each shot and downloads them as a .json file; review_ops() turns that file into comments on the shots."""
from __future__ import annotations

import json
import time
from typing import Any, Callable

from .opfields import _str
from .sequence import build_sequence, total_duration
from .storyboard import _clock, _esc
from .util import AppError, fmt_seconds

_CSS = """
:root { --ink:#16181b; --muted:#5d636b; --line:#d9dce1; --paper:#ffffff; --frame:#101214; --accent:#2f6fd0; }
* { box-sizing: border-box; }
body { margin: 0; background: #eceef1; color: var(--ink);
  font: 14px/1.6 "Microsoft JhengHei", "Noto Sans TC", "PingFang TC", sans-serif; }
.sheet { max-width: 1100px; margin: 20px auto 90px; padding: 0 16px; }
header { background: var(--paper); border-radius: 10px; padding: 20px 22px; margin-bottom: 16px;
  box-shadow: 0 2px 14px rgba(0,0,0,.06); }
h1 { font-size: 22px; margin: 0 0 4px; }
.meta { color: var(--muted); font-size: 13px; }
.how { margin: 12px 0 14px; padding: 10px 12px; background: #eef4fd; border-radius: 6px; font-size: 13px; }
label { display: block; font-weight: 600; font-size: 13px; margin: 10px 0 4px; }
input, textarea { width: 100%; font: inherit; color: inherit; border: 1px solid var(--line); border-radius: 6px;
  padding: 7px 9px; background: #fff; }
input:focus, textarea:focus { outline: 2px solid var(--accent); outline-offset: -1px; border-color: transparent; }
input { max-width: 320px; }
textarea { resize: vertical; min-height: 64px; }
.grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; }
.card { background: var(--paper); border: 1px solid var(--line); border-radius: 8px; overflow: hidden;
  display: flex; flex-direction: column; }
.card.has { border-color: var(--accent); box-shadow: 0 0 0 1px var(--accent); }
.frame { position: relative; background: var(--frame); display: flex; align-items: center; justify-content: center; }
.frame img { width: 100%; height: 100%; object-fit: contain; display: block; }
.frame .empty { color: #8b929b; font-size: 12px; }
.badge { position: absolute; left: 8px; top: 8px; background: rgba(0,0,0,.72); color: #fff; font-weight: 700;
  font-size: 12px; padding: 1px 8px; border-radius: 99px; }
.body { padding: 10px 12px 12px; display: flex; flex-direction: column; gap: 6px; }
.title { display: flex; justify-content: space-between; gap: 8px; font-weight: 700; }
.time { color: var(--muted); font-weight: 400; font-size: 12px; white-space: nowrap; }
.row { display: grid; grid-template-columns: 3em 1fr; gap: 6px; font-size: 13px; }
.row span { color: var(--muted); }
.row p { margin: 0; white-space: pre-wrap; overflow-wrap: anywhere; }
.card label { margin-top: 4px; }
.bar { position: fixed; left: 0; right: 0; bottom: 0; background: rgba(255,255,255,.96); border-top: 1px solid var(--line);
  padding: 10px 16px; display: flex; justify-content: center; align-items: center; gap: 16px; }
.bar span { color: var(--muted); font-size: 13px; }
button { font: inherit; font-weight: 700; color: #fff; background: var(--accent); border: 0; border-radius: 6px;
  padding: 9px 18px; cursor: pointer; }
button:hover { filter: brightness(1.08); }
@media (max-width: 720px) { .grid { grid-template-columns: 1fr; } .bar { justify-content: space-between; } }
"""

# Drafts are kept in the browser while the page is open (per project and export), and the button downloads
# them as the file review_ops() reads.
_JS = """
(function () {
  var meta = JSON.parse(document.getElementById("vc-review-meta").textContent);
  var key = "vc-review:" + meta.project + ":" + meta.exported;
  var by = document.getElementById("by");
  var note = document.getElementById("note");
  var count = document.getElementById("count");
  var boxes = Array.prototype.slice.call(document.querySelectorAll("textarea[data-shot]"));
  function filled() { return boxes.filter(function (b) { return b.value.trim(); }); }
  function show() {
    var n = filled().length;
    count.textContent = n ? "已寫 " + n + " 個鏡頭的意見" : "還沒有寫鏡頭意見";
    boxes.forEach(function (b) { b.closest(".card").classList.toggle("has", !!b.value.trim()); });
  }
  function save() {
    var d = { by: by.value, note: note.value, shots: {} };
    filled().forEach(function (b) { d.shots[b.dataset.shot] = b.value; });
    try { localStorage.setItem(key, JSON.stringify(d)); } catch (e) { /* private window: drafts are not kept */ }
    show();
  }
  try {
    var d = JSON.parse(localStorage.getItem(key) || "{}");
    by.value = d.by || "";
    note.value = d.note || "";
    boxes.forEach(function (b) { b.value = (d.shots || {})[b.dataset.shot] || ""; });
  } catch (e) { /* nothing saved, or storage blocked */ }
  document.addEventListener("input", save);
  show();
  document.getElementById("download").addEventListener("click", function () {
    var name = by.value.trim();
    if (!name) { alert("請先填寫您的稱呼"); by.focus(); return; }
    var shots = filled().map(function (b) {
      var s = meta.shots[b.dataset.shot];
      return { id: b.dataset.shot, no: s.no, name: s.name, text: b.value.trim() };
    });
    if (!shots.length && !note.value.trim()) { alert("還沒有寫任何意見"); return; }
    var out = { vc: "review-comments", version: 1, project: meta.project, name: meta.name, exported: meta.exported,
      by: name, at: new Date().toISOString(), note: note.value.trim(), shots: shots };
    var a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([JSON.stringify(out, null, 2)], { type: "application/json" }));
    a.download = meta.file.replace("{by}", name.replace(/[\\\\/:*?"<>|]/g, "_"));
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(function () { URL.revokeObjectURL(a.href); }, 2000);
  });
})();
"""


def _row(label: str, text: str) -> str:
    text = (text or "").strip()
    return f'<div class="row"><span>{label}</span><p>{_esc(text)}</p></div>' if text else ""


def _spoken(project: dict, start: float, end: float) -> str:
    """The subtitles shown while a clip plays, in order."""
    caps = [c for c in project.get("captions") or [] if float(c.get("start", 0)) < end and float(c.get("end", 0)) > start]
    return "\n".join(str(c.get("text") or "").strip() for c in caps if str(c.get("text") or "").strip())


def render_review(project: dict, picture_url: Callable[[dict], str | None], stem: str) -> str:
    """The review page of the sequence as it is now. picture_url(shot) gives the image to show (or None); the
    downloaded comments are named after stem."""
    clips = build_sequence(project)
    settings = project.get("settings") or {}
    aspect = f"{int(settings.get('width') or 16)} / {int(settings.get('height') or 9)}"
    exported = time.strftime("%Y-%m-%dT%H:%M:%S")
    shots: dict[str, dict] = {}
    cards = []
    for i, clip in enumerate(clips):
        shot = clip["shot"]
        name = (shot.get("name") or "").strip() or f"鏡頭 {i + 1}"
        shots[shot["id"]] = {"no": i + 1, "name": name}
        picture = picture_url(shot)
        frame = f'<img src="{_esc(picture)}" alt="" loading="lazy">' if picture else '<span class="empty">尚無畫面</span>'
        end = clip["start"] + clip["length"]
        cards.append(
            f'<article class="card"><div class="frame" style="aspect-ratio:{aspect}">{frame}'
            f'<span class="badge">#{i + 1:02d}</span></div><div class="body">'
            f'<div class="title"><span>{_esc(name)}</span>'
            f'<span class="time">{_clock(clip["start"])} 起，{clip["length"]:.1f} 秒</span></div>'
            f'{_row("畫面", shot.get("prompt", ""))}{_row("字幕", _spoken(project, clip["start"], end))}'
            f'<label for="r-{_esc(shot["id"])}">意見</label>'
            f'<textarea id="r-{_esc(shot["id"])}" data-shot="{_esc(shot["id"])}" rows="3" maxlength="2000" '
            f'placeholder="這個鏡頭要改的地方，沒有就留空"></textarea></div></article>'
        )
    title = _esc(project.get("name") or "未命名專案")
    meta = {"vc": "review", "version": 1, "project": project["id"], "name": project.get("name") or "",
            "exported": exported, "file": f"{stem}_審稿意見_{{by}}.json", "shots": shots}
    meta_json = json.dumps(meta, ensure_ascii=False).replace("<", "\\u003c")
    return (
        '<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{title}｜審稿</title><style>{_CSS}</style></head><body><main class=\"sheet\">"
        f"<header><h1>{title}</h1>"
        f'<div class="meta">{len(clips)} 個鏡頭，總長 {_esc(fmt_seconds(total_duration(clips)))}，'
        f"{exported.replace('T', ' ')[:16]} 匯出</div>"
        '<div class="how">在想修改的鏡頭下方寫意見，寫完按最下方的「下載審稿意見」，把下載的 .json 檔傳回給製作方。'
        "寫到一半關掉也沒關係，同一台裝置、同一個瀏覽器再打開這個檔案會接著寫。</div>"
        '<label for="by">您的稱呼（必填）</label><input id="by" maxlength="60" autocomplete="name">'
        '<label for="note">整體意見</label>'
        '<textarea id="note" rows="3" maxlength="2000" placeholder="不屬於某個鏡頭的意見，例如節奏、配樂、片長"></textarea>'
        f"</header><div class=\"grid\">{''.join(cards) or '<p>序列裡還沒有鏡頭。</p>'}</div></main>"
        '<div class="bar"><span id="count"></span><button type="button" id="download">下載審稿意見</button></div>'
        f'<script type="application/json" id="vc-review-meta">{meta_json}</script><script>{_JS}</script>'
        "</body></html>"
    )


_KEY = ("by", "at", "text")


def review_ops(project: dict, data: Any) -> tuple[list[dict], dict]:
    """The ops that add a downloaded comments file to the project, and what they add: {shots, general, missing,
    duplicate}. A comment on a shot no longer in the project goes to the project's own comments with the shot's
    number and name; one already there (same reviewer, time and words) is skipped, so a file can be read twice."""
    if not isinstance(data, dict) or data.get("vc") != "review-comments":
        raise AppError("這不是審稿網頁下載的意見檔，請選擇客戶傳回的 .json 檔")
    if data.get("project") != project["id"]:
        other = _str(120)(data.get("name")).strip() or "另一個專案"
        raise AppError(f"這份審稿意見屬於「{other}」，請在那個專案匯入")
    entries = data.get("shots") or []
    if not isinstance(entries, list):
        raise AppError("審稿意見檔格式不正確")
    by = _str(60)(data.get("by")).strip() or "客戶"
    at = _str(40)(data.get("at")).strip() or time.strftime("%Y-%m-%dT%H:%M:%S")
    shots = {s["id"]: s for s in project["shots"]}
    added: dict[str, list[dict]] = {}
    general: list[dict] = []
    counts = {"shots": 0, "general": 0, "missing": 0, "duplicate": 0}

    def add(target: list[dict], have: list[dict], text: str, kind: str) -> None:
        item = {"by": by, "at": at, "text": text}
        if any(tuple(c.get(k) for k in _KEY) == tuple(item[k] for k in _KEY) for c in [*have, *target]):
            counts["duplicate"] += 1
            return
        target.append(item)
        counts[kind] += 1

    for e in entries:
        if not isinstance(e, dict):
            raise AppError("審稿意見檔格式不正確")
        text = _str(2000)(e.get("text")).strip()
        if not text:
            continue
        shot = shots.get(e.get("id"))
        if shot:
            add(added.setdefault(shot["id"], []), shot.get("review") or [], text, "shots")
        else:
            no = e.get("no")
            label = f"#{no:02d} " if isinstance(no, int) else ""
            name = _str(120)(e.get("name")).strip()
            add(general, project.get("review") or [], f"（已刪除的鏡頭 {label}{name}）{text}", "missing")
    note = _str(2000)(data.get("note")).strip()
    if note:
        add(general, project.get("review") or [], note, "general")
    ops = [{"op": "shot.update", "id": sid, "patch": {"review": [*(shots[sid].get("review") or []), *items]}}
           for sid, items in added.items() if items]
    if general:
        ops.append({"op": "project.update", "patch": {"review": [*(project.get("review") or []), *general]}})
    return ops, counts
