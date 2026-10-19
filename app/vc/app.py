"""The application object: builds every service, serves the API and UI, and exits when left idle."""
from __future__ import annotations

import logging
import re
import shutil
import threading
import time
import uuid
from pathlib import Path

from . import DEFAULT_PORT, VERSION, api, video2x
from .api.deps import deps_status
from .api.system import system_info
from .comfy.manager import ComfyManager
from .comfy.workflows import WorkflowStore
from .deps import DepInstaller
from .events import EventBus
from .jobs import JobManager
from .llm import LLMClient
from .media import Media, find_ffmpeg
from .notify import Notifier
from .paths import Paths
from .providers.catalog import ModelCatalog
from .runners import Runners
from .secrets import SecretStore
from .server import HttpServer, Router, serve_in_thread
from .settings import SettingsStore
from .store import ProjectStore
from .util import new_id

log = logging.getLogger("vc.app")

IDLE_EXIT_SEC = 120  # no window open and nothing to run for this long: the server exits
WATCH_SEC = 5
CACHE_MAX_AGE = 86400
CACHE_PREFIXES = ("upload-", "job-", "export-")
CLIENT_ID_RE = re.compile(r"^[a-z0-9-]{8,64}$")


class App:
    def __init__(self, data_dir: str | Path | None = None):
        self.paths = Paths(data_dir)
        self.paths.ensure()
        self.instance = new_id("i")
        self.started = time.time()
        self.bus = EventBus()
        self.settings = SettingsStore(self.paths.settings_file, self.bus)
        self.secrets = SecretStore(self.paths.secrets_file)
        self.store = ProjectStore(self.paths, self.bus, self.settings)
        self.comfy = ComfyManager(self.settings, self.secrets, self.bus, self._client_id())
        self.workflows = WorkflowStore(self.paths.workflows, bus=self.bus)
        self._media: tuple[str, Media] | None = None
        self._media_lock = threading.Lock()
        video2x.PORTABLE_EXE = self.paths.tools / "video2x" / "video2x.exe"
        self.deps = DepInstaller(self.paths.tools, self._deps_changed)
        self.model_catalog = ModelCatalog(self.paths.model_catalog_file)
        self.runners = Runners(store=self.store, settings=self.settings, secrets=self.secrets, comfy=self.comfy,
                               workflows=self.workflows, media=self.media, catalog=self.model_catalog)
        self.jobs = JobManager(self.paths, self.bus, self.runners)
        self.notifier = Notifier(self.settings, self.secrets, self.bus)
        self.jobs.add_listener(self.notifier.on_job)
        self.llm = LLMClient(self.settings, self.secrets)
        self.router = Router()
        api.register(self.router, self)
        self.server: HttpServer | None = None
        self._stop_lock = threading.Lock()
        self._stopping = False
        self._stopped = threading.Event()

    # ---------------------------------------------------------------- shared helpers
    def media(self) -> Media:
        """ffmpeg for the configured path; looked up once and reused until the setting changes."""
        configured = str(self.settings.section("ffmpeg").get("path") or "")
        with self._media_lock:
            if self._media is None or self._media[0] != configured:
                self._media = (configured, Media(find_ffmpeg(configured, self.paths.tools)))
            return self._media[1]

    def reset_media(self) -> None:
        with self._media_lock:
            self._media = None

    def _deps_changed(self, pkg_id: str, installed: bool) -> None:
        if installed and pkg_id == "ffmpeg":
            self.reset_media()
            self.bus.publish("system", system_info(self))
        self.bus.publish("deps", deps_status(self))

    def hello(self) -> dict:
        return {"instance": self.instance, "version": VERSION}

    def _client_id(self) -> str:
        """A ComfyUI client id kept across restarts, so resumed jobs still receive their progress events."""
        path = self.paths.run / "client_id"
        try:
            cid = path.read_text(encoding="utf-8").strip()
        except OSError:
            cid = ""
        if not CLIENT_ID_RE.match(cid):
            cid = "vc-" + uuid.uuid4().hex
            path.write_text(cid, encoding="utf-8")
        return cid

    def _sweep_cache(self) -> None:
        """Remove what interrupted uploads, jobs and exports left in the project folders."""
        cutoff = time.time() - CACHE_MAX_AGE
        removed = 0
        for pdir in self.paths.projects.iterdir():
            for folder in (pdir / "media" / "cache", pdir / "exports"):
                if not folder.is_dir():
                    continue
                for f in folder.iterdir():
                    try:
                        stale = f.name.startswith(CACHE_PREFIXES) and f.stat().st_mtime < cutoff
                        if f.name.endswith(".part") or (folder.name == "cache" and stale):
                            shutil.rmtree(f) if f.is_dir() else f.unlink()
                            removed += 1
                    except OSError as exc:
                        log.warning("Could not remove %s: %s", f, exc)
        if removed:
            log.info("Removed %d leftover cache files", removed)

    # ---------------------------------------------------------------- lifecycle
    def start(self, port: int = DEFAULT_PORT, *, watchdog: bool = True) -> int:
        """Start serving on 127.0.0.1:port (0 picks a free port) and return the port."""
        self._sweep_cache()
        self.server = HttpServer("127.0.0.1", port, self.router, self.bus, self.hello)
        serve_in_thread(self.server)
        self.comfy.start()
        self.jobs.start()
        if watchdog:
            threading.Thread(target=self._watch, name="idle-watch", daemon=True).start()
        log.info("Video Console %s on http://127.0.0.1:%d (data: %s)", VERSION, self.server.port, self.paths.data)
        return self.server.port

    def _busy(self) -> bool:
        if self.bus.subscriber_count:
            return True  # a window is open
        state = self.jobs.state()
        counts = state["counts"]
        return bool(counts.get("running") or (counts.get("queued") and not state["paused"]))

    def _watch(self) -> None:
        """Exit once every window has been closed for a while and no job is left to run."""
        idle_since: float | None = None
        while not self._stopped.wait(WATCH_SEC):
            if self._busy() or not self.settings.section("ui").get("autoExit", True):
                idle_since = None
                continue
            if idle_since is None:
                idle_since = time.monotonic()
            elif time.monotonic() - idle_since >= IDLE_EXIT_SEC:
                log.info("No window and no work for %d s; shutting down", IDLE_EXIT_SEC)
                self.stop()
                return

    def wait(self) -> None:
        """Block until stop() has finished (wakes every second so Ctrl+C still works on Windows)."""
        while not self._stopped.wait(1.0):
            pass

    def stop(self) -> None:
        with self._stop_lock:
            if self._stopping:
                return
            self._stopping = True
        log.info("Shutting down")
        try:
            if self.server is not None:
                self.server.shutdown()
                self.server.server_close()
            self.comfy.stop()
            self.jobs.stop()
        finally:
            self._stopped.set()
