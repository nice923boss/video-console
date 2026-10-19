"""Voice engines for the cast editor: Audio8Voice's connection status and test, its voices and role
presets, and preparing a voice as soon as it is picked; VoAI's key check and its speakers by model."""
from __future__ import annotations

import re
from typing import TYPE_CHECKING

from ..jobs import ACTIVE
from ..server import Request, Router
from ..util import AppError
from ..voice import Audio8Voice, VoAI

if TYPE_CHECKING:
    from ..app import App

URL_RE = re.compile(r"^https?://[^\s/]+")


def register(router: Router, app: App) -> None:
    client = Audio8Voice(app.settings)
    cloud = VoAI(app.secrets)

    @router.route("GET", "/api/voice/status")
    def voice_status(req: Request) -> dict:
        return client.status()

    @router.route("POST", "/api/voice/test")
    def voice_test(req: Request) -> dict:
        url = str(req.json_object().get("url") or "").strip().rstrip("/")
        if not URL_RE.match(url):
            raise AppError("Audio8Voice 位址格式不正確，例如 http://127.0.0.1:18301")
        return client.health(url)

    @router.route("GET", "/api/voice/voices")
    def voice_list(req: Request) -> dict:
        return {"voices": client.voices(), "presets": client.presets()}

    @router.route("POST", "/api/voice/prepare")
    def voice_prepare(req: Request) -> dict:
        """Queue a "voice" job preparing a voice, unless Audio8Voice has it ready or one is already queued."""
        body = req.json_object()
        voice_id = str(body.get("voice") or "")
        voice = next((v for v in client.voices() if v.get("id") == voice_id), None)
        if voice is None:
            raise AppError(f"Audio8Voice 找不到這個聲音：{voice_id}", 404)
        if voice.get("prepared"):
            return {"prepared": True, "job": None}
        job = next((j for j in app.jobs.list() if j["kind"] == "voice" and j["status"] in ACTIVE
                    and j["payload"].get("prepare") == voice_id), None)
        if job is None:
            job = app.jobs.submit("voice", str(body.get("pid") or ""), None,
                                  {"prepare": voice_id, "name": voice.get("name")})
        return {"prepared": False, "job": job}

    @router.route("GET", "/api/voice/voai")
    def voai_status(req: Request) -> dict:
        """Whether a VoAI key is set and works, with its speakers by model; refresh=1 skips the cache."""
        return cloud.status(refresh=req.query.get("refresh") == "1")
