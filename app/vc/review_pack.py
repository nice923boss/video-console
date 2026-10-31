"""Review package (客戶修改意見, part 1): a fresh export of the cut in a folder of its own beside 審稿.html. The
page plays that video next to its shot list; the client leaves notes on exact frames (boxes, arrows and labels
drawn on the picture), inserts pictures or clips of their own at a frame, and packs it all into one ZIP
(修改意見.json + 附件/) to send back, which revisions.py reads. The page's markup, script and style live in
review_kit/ and are inlined, so the page is a single file next to the video. The shots of the export are kept in
the project (reviews/<id>.json) so returned notes land back on the shots they were made on."""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from .duck import speech_rows
from .export import export_filename
from .sequence import VOICE_TRACK, audio_track, build_sequence
from .storyboard import _esc
from .util import fmt_seconds, read_json, write_json_atomic

KIT = Path(__file__).with_name("review_kit")
PAGE = "審稿.html"
REVIEWS = "reviews"
REVIEW_ID = re.compile(r"^r_[a-z0-9]{10}$")
_SLOT = re.compile(r"__[A-Z_]+__")


def folder_name(project: dict) -> str:
    """<name>_審稿_<YYYYmmdd-HHMMSS>, matching the export's own file name."""
    stem, stamp = export_filename(project, "x")[:-2].rsplit("_", 1)
    return f"{stem}_審稿_{stamp}"


def shot_table(project: dict) -> list[dict]:
    """The shots of the cut as exported: where each starts in the video and how long it plays."""
    rows = []
    for i, clip in enumerate(build_sequence(project)):
        shot = clip["shot"]
        rows.append({"id": shot["id"], "no": i + 1, "name": (shot.get("name") or "").strip() or f"鏡頭 {i + 1}",
                     "start": round(clip["start"], 3), "length": round(clip["length"], 3)})
    return rows


def music_clips(project: dict) -> list[dict]:
    """The background music, by start: the clips on a row marked 背景音樂, or with none marked, any music row's
    clips that are not voice. Voice-over lines and a shot's own sound split off to a row are not music. A review
    package plays the cut without these under a soundtrack the client tries; revisions.music_plan swaps them."""
    tracks = project.get("tracks") or {}
    bgm, speech = set(tracks.get("bgmRows") or []), set(speech_rows(tracks))

    def music(clip: dict) -> bool:
        key = audio_track(clip)
        if key == VOICE_TRACK or clip.get("shotId") or clip.get("takeId"):
            return False
        return key in bgm if bgm else key not in speech and not clip.get("speech")

    return sorted((a for a in project.get("audio") or [] if music(a)), key=lambda a: float(a.get("start") or 0.0))


def _kit(name: str) -> str:
    return (KIT / name).read_text(encoding="utf-8")


def render_page(snap: dict, video_name: str, stem: str) -> str:
    settings = {k: snap[k] for k in ("width", "height", "fps")}
    meta = {"vc": "review", "version": 2, "project": snap["project"], "name": snap["name"], "review": snap["id"],
            "exported": snap["exported"], "video": video_name, "duration": snap["duration"], **settings,
            "file": f"{stem}_修改意見_{{by}}.zip", "shots": snap["shots"], "stem": snap.get("stem"),
            "musicVolume": snap.get("musicVolume")}
    title = _esc(snap["name"] or "未命名專案")
    line = (f"{len(snap['shots'])} 個鏡頭，總長 {fmt_seconds(snap['duration'])}，"
            f"{snap['exported'].replace('T', ' ')[:16]} 匯出")
    fills = {
        "__TITLE__": title,
        "__META_LINE__": _esc(line),
        "__VIDEO__": _esc(video_name),
        "__VIEWBOX__": f"0 0 {settings['width']} {settings['height']}",
        "__STYLE__": _kit("page.css"),
        "__META_JSON__": json.dumps(meta, ensure_ascii=False).replace("<", "\\u003c"),
        "__SCRIPT__": _kit("zip.js") + _kit("page.js"),
    }
    # One pass, so a project named like a placeholder is not filled in again.
    return _SLOT.sub(lambda m: fills.get(m.group(0), m.group(0)), _kit("page.html"))


def write_pack(project: dict, project_dir: Path, folder: Path, video: Path, duration: float, rid: str,
               stem: str | None = None) -> dict:
    """Write 審稿.html next to the exported video and keep the export's shot table in the project. project is
    the same snapshot the video was rendered from, so the table matches the picture. stem names the sound of the
    cut without its background music beside the video, which the page plays under a soundtrack the client tries
    (musicVolume: the music's own volume, where the page's volume slider starts)."""
    settings = project.get("settings") or {}
    music = music_clips(project)
    snap = {"id": rid, "project": project["id"], "name": project.get("name") or "",
            "exported": time.strftime("%Y-%m-%dT%H:%M:%S"), "folder": str(folder), "page": str(folder / PAGE),
            "video": str(video), "duration": round(float(duration), 3),
            "width": int(settings.get("width") or 1920), "height": int(settings.get("height") or 1080),
            "fps": settings.get("fps") or 30, "shots": shot_table(project), "stem": stem,
            "musicVolume": round(min(1.0, max(0.0, float(music[0].get("volume", 1.0)))), 2) if music else None}
    (folder / PAGE).write_text(render_page(snap, video.name, video.stem), encoding="utf-8")
    write_json_atomic(project_dir / REVIEWS / f"{rid}.json", snap)
    return snap


def load_pack(project_dir: Path, rid: str) -> dict | None:
    if not REVIEW_ID.match(str(rid or "")):
        return None
    data = read_json(project_dir / REVIEWS / f"{rid}.json", None)
    return data if isinstance(data, dict) and data.get("id") == rid else None


def list_packs(project_dir: Path) -> list[dict]:
    """The review packages exported from this project (a copy keeps its original's), newest first."""
    folder = project_dir / REVIEWS
    packs = [load_pack(project_dir, f.stem) for f in (folder.glob("r_*.json") if folder.is_dir() else ())]
    return sorted((p for p in packs if p), key=lambda p: p["exported"], reverse=True)
