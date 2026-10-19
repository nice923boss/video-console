"""Printable HTML storyboard (one card per shot), used by the in-app print view and the project package."""
from __future__ import annotations

import html
import re
import time
from typing import Callable

from .sequence import build_sequence, image_take, total_duration, video_take
from .util import fmt_seconds

TRANSITION_LABELS = {
    "cut": "直接切換",
    "crossfade": "交叉淡化",
    "fade_black": "黑場淡化",
    "fade_white": "白場淡化",
    "wipe_left": "向左擦除",
    "slide_left": "向左滑入",
}
_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{3,8}$")

_CSS = """
:root { --ink:#16181b; --muted:#5d636b; --line:#d9dce1; --paper:#ffffff; --frame:#101214; --accent:#5f7d12; }
* { box-sizing: border-box; }
body { margin: 0; background: #eceef1; color: var(--ink);
  font: 13px/1.55 "Microsoft JhengHei", "Noto Sans TC", "PingFang TC", sans-serif; }
.sheet { max-width: 1240px; margin: 24px auto; background: var(--paper); padding: 28px 32px 36px;
  border-radius: 10px; box-shadow: 0 2px 18px rgba(0,0,0,.08); }
header { display: flex; justify-content: space-between; align-items: flex-end; gap: 24px;
  border-bottom: 2px solid var(--ink); padding-bottom: 12px; margin-bottom: 14px; }
h1 { font-size: 24px; margin: 0; letter-spacing: .02em; }
.meta { color: var(--muted); font-size: 12px; text-align: right; white-space: nowrap; }
.meta b { color: var(--ink); font-weight: 600; }
.block { margin: 0 0 14px; padding: 10px 12px; background: #f5f6f8; border-radius: 6px; }
.block h2 { font-size: 12px; margin: 0 0 4px; color: var(--muted); font-weight: 600; }
.block p { margin: 0; white-space: pre-wrap; }
.grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 16px; }
.card { border: 1px solid var(--line); border-radius: 8px; overflow: hidden; break-inside: avoid;
  page-break-inside: avoid; display: flex; flex-direction: column; }
.card.off { opacity: .55; }
.bar { height: 4px; background: var(--line); }
.frame { position: relative; background: var(--frame); display: flex; align-items: center; justify-content: center; }
.frame img { width: 100%; height: 100%; object-fit: contain; display: block; }
.frame .empty { color: #8b929b; font-size: 12px; }
.badge { position: absolute; left: 8px; top: 8px; background: rgba(0,0,0,.72); color: #fff; font-weight: 700;
  font-size: 12px; padding: 1px 8px; border-radius: 99px; }
.kind { position: absolute; right: 8px; top: 8px; background: rgba(0,0,0,.6); color: #e6e8eb; font-size: 11px;
  padding: 1px 7px; border-radius: 99px; }
.body { padding: 9px 11px 11px; display: flex; flex-direction: column; gap: 5px; }
.title { display: flex; justify-content: space-between; gap: 8px; font-weight: 700; font-size: 14px; }
.time { color: var(--muted); font-weight: 400; font-size: 12px; white-space: nowrap; }
.row { display: grid; grid-template-columns: 4.2em 1fr; gap: 6px; }
.row span { color: var(--muted); }
.row p { margin: 0; white-space: pre-wrap; overflow-wrap: anywhere; }
.flag { color: #9a5b00; font-size: 12px; }
@page { size: A4 landscape; margin: 10mm; }
@media print {
  body { background: #fff; }
  .sheet { margin: 0; max-width: none; padding: 0; box-shadow: none; border-radius: 0; }
  .grid { gap: 10px; }
}
@media (max-width: 900px) { .grid { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
@media (max-width: 560px) { .grid { grid-template-columns: 1fr; } header { flex-direction: column; align-items: flex-start; }
  .meta { text-align: left; } }
"""


def _esc(text: object) -> str:
    return html.escape(str(text or ""), quote=True)


def _clock(t: float) -> str:
    t = max(0.0, float(t))
    minutes, sec = divmod(t, 60)
    return f"{int(minutes):d}:{sec:04.1f}"


def shot_picture(project: dict, shot: dict) -> str | None:
    """Project-relative file that best represents a shot: its image, else its video's thumbnail."""
    img = image_take(project, shot)
    if img and img.get("file"):
        return img["file"]
    vid = video_take(project, shot)
    if vid and vid.get("thumb"):
        return vid["thumb"]
    return None


def _row(label: str, text: str) -> str:
    text = (text or "").strip()
    return f'<div class="row"><span>{label}</span><p>{_esc(text)}</p></div>' if text else ""


def _card(project: dict, index: int, shot: dict, clip: dict | None, picture: str | None, aspect: str) -> str:
    in_seq = clip is not None
    length = clip["length"] if clip else float(shot.get("duration") or 0)
    timing = f"{_clock(clip['start'])} 起，{length:.1f} 秒" if clip else f"{length:.1f} 秒"
    kind = "影片" if video_take(project, shot) else ("圖片" if image_take(project, shot) else "")
    frame = (f'<img src="{_esc(picture)}" alt="">' if picture else '<span class="empty">尚無畫面</span>')
    trans = (clip or {}).get("transition") or shot.get("transition") or {}
    trans_text = ""
    if in_seq and trans.get("type", "cut") != "cut":
        trans_text = f"{TRANSITION_LABELS.get(trans['type'], trans['type'])}（{float(trans['duration']):.1f} 秒）"
    color = str(shot.get("color") or "")
    bar = f'<div class="bar" style="background:{color}"></div>' if _COLOR_RE.match(color) else '<div class="bar"></div>'
    name = (shot.get("name") or "").strip() or f"鏡頭 {index + 1}"
    parts = [
        f'<article class="card{"" if in_seq else " off"}">',
        bar,
        f'<div class="frame" style="aspect-ratio:{aspect}">{frame}<span class="badge">#{index + 1:02d}</span>',
        f'<span class="kind">{kind}</span>' if kind else "",
        "</div>",
        '<div class="body">',
        f'<div class="title"><span>{_esc(name)}</span><span class="time">{timing}</span></div>',
        "" if in_seq else '<div class="flag">未放入序列</div>',
        _row("畫面", shot.get("prompt", "")),
        _row("動態", shot.get("motion", "")),
        _row("運鏡", shot.get("camera", "")),
        _row("轉場", trans_text),
        _row("備註", shot.get("notes", "")),
        "</div></article>",
    ]
    return "".join(parts)


def render_storyboard(project: dict, picture_url: Callable[[dict], str | None]) -> str:
    """Full HTML document. picture_url(shot) returns the image URL to show for a shot (or None)."""
    clips = {c["shot"]["id"]: c for c in build_sequence(project)}
    settings = project.get("settings") or {}
    width, height = int(settings.get("width") or 16), int(settings.get("height") or 9)
    aspect = f"{width} / {height}"
    total = total_duration(list(clips.values()))
    shots = project.get("shots") or []
    cards = "".join(
        _card(project, i, shot, clips.get(shot["id"]), picture_url(shot), aspect) for i, shot in enumerate(shots)
    )
    style = (project.get("style") or {}).get("prompt", "")
    blocks = ""
    if style.strip():
        blocks += f'<section class="block"><h2>整體風格</h2><p>{_esc(style.strip())}</p></section>'
    if (project.get("notes") or "").strip():
        blocks += f'<section class="block"><h2>專案備註</h2><p>{_esc(project["notes"].strip())}</p></section>'
    title = _esc(project.get("name") or "未命名專案")
    meta = (
        f"<div><b>{len(shots)}</b> 個鏡頭，序列 <b>{len(clips)}</b> 個，總長 <b>{_esc(fmt_seconds(total))}</b></div>"
        f"<div>{width} × {height}，{_esc(settings.get('fps', ''))} fps，"
        f"{time.strftime('%Y-%m-%d %H:%M')} 產生</div>"
    )
    return (
        '<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{title}｜分鏡表</title><style>{_CSS}</style></head><body><main class=\"sheet\">"
        f"<header><h1>{title}</h1><div class=\"meta\">{meta}</div></header>"
        f"{blocks}<div class=\"grid\">{cards or '<p>這個專案還沒有鏡頭。</p>'}</div>"
        "</main></body></html>"
    )
