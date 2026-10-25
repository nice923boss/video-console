"""Voice cleanup on audio clips: the stems pass behind 降噪 / 去回音, and the overview of a clip's channels."""
from __future__ import annotations

from typing import TYPE_CHECKING

from ..server import Request, Router
from ..util import AppError
from ..voiceclean import channel_levels, claim, ensure_stems

if TYPE_CHECKING:
    from ..app import App


def register(router: Router, app: App) -> None:
    store = app.store

    def audio_item(pid: str, req: Request) -> tuple[dict, dict]:
        project = store.get(pid)
        aid = req.json_object().get("id")
        item = next((a for a in project.get("audio") or [] if a.get("id") == aid), None)
        if item is None:
            raise AppError("找不到這段音訊", 404)
        return project, item

    @router.route("POST", "/api/projects/{pid}/voice-clean")
    def voice_clean(req: Request) -> dict:
        """{id}: the stems of the audio clip's noise reduction / echo remover, running the pass when none cover
        the clip as it is now: {"stems": {file, from, to} or None}. A newer request for the clip stops this one
        (499)."""
        pid = req.params["pid"]
        project, item = audio_item(pid, req)
        stems = ensure_stems(app.media(), project, store.project_dir(pid), item, claim(f"{pid}/{item['id']}"))
        return {"stems": stems}

    @router.route("POST", "/api/projects/{pid}/voice-channels")
    def voice_channels(req: Request) -> dict:
        """{id}: levels of every channel of the audio clip's file over the clip (voiceclean.channel_levels).
        A newer request in the project stops this one (499)."""
        pid = req.params["pid"]
        project, item = audio_item(pid, req)
        return {"streams": channel_levels(app.media(), project, store.project_dir(pid), item, claim(f"{pid}/channels"))}
