"""REST endpoints. Each module adds its routes to the shared Router."""
from __future__ import annotations

from typing import TYPE_CHECKING

from ..server import Router
from . import (captions, chat, clean, color, deps, engine, files, jobs, p5, projects, revisions, stt, styles, system,
               twins, voice)

if TYPE_CHECKING:
    from ..app import App


def register(router: Router, app: App) -> None:
    for module in (system, deps, projects, files, jobs, engine, voice, stt, styles, captions, chat, clean, revisions, color,
                   twins, p5):
        module.register(router, app)
