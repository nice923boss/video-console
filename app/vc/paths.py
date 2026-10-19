"""Filesystem layout of the app and its runtime data folder."""
from __future__ import annotations

import os
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
ROOT_DIR = APP_DIR.parent
WEB_DIR = APP_DIR / "web"
BUILTIN_WORKFLOWS_DIR = APP_DIR / "workflows"


class Paths:
    """All runtime locations derive from one data folder, so tests can point it elsewhere."""

    def __init__(self, data_dir: str | Path | None = None):
        base = data_dir or os.environ.get("VC_DATA_DIR") or (ROOT_DIR / "data")
        self.data = Path(base).resolve()
        self.projects = self.data / "projects"
        self.trash = self.data / "trash"
        self.logs = self.data / "logs"
        self.run = self.data / "run"
        self.workflows = self.data / "workflows"
        self.tmp = self.data / "tmp"
        self.tools = self.data / "tools"  # ffmpeg and Video2X installed from the settings page
        self.settings_file = self.data / "settings.json"
        self.secrets_file = self.data / "secrets.json"
        self.jobs_file = self.data / "jobs.json"
        self.model_catalog_file = self.data / "model-catalog.json"
        self.pid_file = self.run / "server.json"

    def ensure(self) -> None:
        for p in (self.data, self.projects, self.trash, self.logs, self.run, self.workflows, self.tmp):
            p.mkdir(parents=True, exist_ok=True)
