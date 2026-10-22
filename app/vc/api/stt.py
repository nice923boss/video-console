"""Speech-to-subtitle for the engines page: run its self-check with the saved folder and Python."""
from __future__ import annotations

from typing import TYPE_CHECKING

from ..server import Request, Router
from ..stt import SttClient

if TYPE_CHECKING:
    from ..app import App


def register(router: Router, app: App) -> None:
    client = SttClient(app.settings)

    @router.route("POST", "/api/stt/check")
    def stt_check(req: Request) -> dict:
        return client.check()
