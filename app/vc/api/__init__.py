"""REST endpoints. Each module adds its routes to the shared Router."""
from __future__ import annotations

from typing import TYPE_CHECKING

from ..server import Router
from . import deps, engine, files, jobs, projects, stt, system, voice

if TYPE_CHECKING:
    from ..app import App


def register(router: Router, app: App) -> None:
    for module in (system, deps, projects, files, jobs, engine, voice, stt):
        module.register(router, app)
