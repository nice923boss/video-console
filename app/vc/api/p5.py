"""Endpoints for editing the text of a p5 animation from the inspector (vc/p5src.py, vc/p5render.py).

The re-render itself is a "p5render" job (POST /api/jobs); these read the texts and link a media item to its
p5 page. Linking is an undoable media.update, and the user's p5 files are only read.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING

from .. import folderpick, p5src
from ..server import Request, Router
from ..util import AppError

if TYPE_CHECKING:
    from ..app import App

PAGE_TYPES = [("p5 動畫頁面", "*.html;*.htm"), ("所有檔案", "*.*")]


def _media(project: dict, mid: str) -> dict:
    item = project["media"].get(mid)
    if item is None:
        raise AppError("找不到素材", 404)
    return item


def _suggestion(project: dict, name: str) -> dict:
    """{page, found}: the page that draws this clip's segment next to a page the project already links
    (index.html or three.html), found True; else the first linked page: animations of one video share a folder."""
    link = p5src.project_link(project, name)
    if link:
        return {"page": link["page"], "found": True}
    pages = p5src.linked_pages(project)
    return {"page": pages[0] if pages else "", "found": False}


def _unlinked_clips(project: dict, skip: str) -> dict[str, list[str]]:
    """{segment: [media ids]} of the videos named after a segment (M04.webm) that have no p5 link yet."""
    out: dict[str, list[str]] = {}
    for item in project["media"].values():
        link = item.get("p5")
        if item["id"] == skip or item.get("kind") != "video" or (isinstance(link, dict) and link.get("page")):
            continue
        segment = p5src.segment_guess(item.get("name") or "")
        if segment:
            out.setdefault(segment, []).append(item["id"])
    return out


def sibling_links(project: dict, page: str, skip: str = "") -> list[dict]:
    """media.update ops linking every unlinked clip whose segment `page` or a page next to it draws."""
    clips = _unlinked_clips(project, skip)
    if not clips:
        return []
    hits = p5src.segment_pages(p5src.check_page(page), list(clips))
    return [{"op": "media.update", "id": mid, "patch": {"p5": {"page": str(hits[seg]), "segment": seg, "edits": []}}}
            for seg, ids in clips.items() if seg in hits for mid in ids]


def register(router: Router, app: App) -> None:
    store = app.store

    @router.route("GET", "/api/projects/{pid}/media/{mid}/p5")
    def p5_texts(req: Request) -> dict:
        project = store.get(req.params["pid"])
        item = _media(project, req.params["mid"])
        link = item.get("p5")
        if not isinstance(link, dict) or not link.get("page"):
            name = item.get("name") or ""
            return {"linked": False, "segment": p5src.segment_guess(name), **_suggestion(project, name)}
        try:
            return {"linked": True, **p5src.read_texts(link["page"], link["segment"], link.get("edits") or [])}
        except AppError as err:  # moved or renamed source: the panel still offers to link again
            return {"linked": True, "page": link["page"], "segment": link["segment"], "texts": [], "stale": [],
                    "error": err.message}

    @router.route("POST", "/api/projects/{pid}/media/{mid}/p5/link")
    def p5_link(req: Request) -> dict:
        """{page, segment}: link (checked against the source); page "" unlinks. The other unlinked clips named
        after a segment that this page or a page next to it draws are linked in the same undo step
        (returned as "alsoLinked")."""
        pid, body = req.params["pid"], req.json_object()
        project = store.get(pid)
        item = _media(project, req.params["mid"])
        page = str(body.get("page") or "").strip().strip('"')
        if not page:
            if "p5" not in item:
                return {"linked": False}
            store.apply(pid, [{"op": "media.update", "id": item["id"], "patch": {}, "unset": ["p5"]}],
                        label="取消連結 p5 原始碼")
            return {"linked": False}
        segment = str(body.get("segment") or "").strip()
        link = p5src.clean_link({"page": page, "segment": segment, "edits": []})
        p5src.find_segment(p5src.check_page(link["page"]), link["segment"])
        old = item.get("p5") if isinstance(item.get("p5"), dict) else {}
        if (old.get("page"), old.get("segment")) == (link["page"], link["segment"]):
            link["edits"] = old.get("edits") or []  # linking again to the same place keeps the edits
        more = sibling_links(project, link["page"], skip=item["id"])
        store.apply(pid, [{"op": "media.update", "id": item["id"], "patch": {"p5": link}}, *more],
                    label="連結 p5 原始碼")
        return {"linked": True, "alsoLinked": len(more), **p5src.read_texts(link["page"], link["segment"], link["edits"])}

    @router.route("POST", "/api/projects/{pid}/pick-p5-page")
    def p5_pick(req: Request) -> dict:
        """The open-file dialog for a p5 page; {page: ""} when canceled."""
        if sys.platform != "win32":
            raise AppError("只有 Windows 支援系統選檔視窗，請直接貼上頁面路徑", 501)
        store.project_dir(req.params["pid"])
        start = str(req.json_object().get("start") or "")
        chosen = folderpick.pick_files(Path(start) if start else None, "選擇 p5 動畫頁面（index.html）", PAGE_TYPES)
        return {"page": chosen[0] if chosen else ""}
