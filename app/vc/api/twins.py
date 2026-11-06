"""Endpoints that go with vc/twins.py, and the server's sources for it: wave files and the settings glossary."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from .. import twins
from ..server import Request, Router
from ..title_templates import template_list
from ..util import AppError, read_json

if TYPE_CHECKING:
    from ..app import App

log = logging.getLogger("vc.twins")


def register(router: Router, app: App) -> None:
    def wave(pid: str, rel: str) -> Any:
        try:
            return read_json(app.store.resolve_file(pid, rel))
        except (AppError, ValueError, OSError) as err:
            log.warning("cannot read wave %s of project %s: %s", rel, pid, err)
            return None

    twins.bind(twins.Sources(wave=wave, glossary=lambda: app.settings.section("glossary") or ""))

    @router.route("GET", "/api/title-templates")
    def title_templates(req: Request) -> dict:
        return {"templates": template_list()}
