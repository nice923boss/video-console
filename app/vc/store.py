"""Project storage: one folder per project, op batches with undo/redo, snapshots and trash."""
from __future__ import annotations

import copy
import logging
import re
import shutil
import threading
import time
from pathlib import Path
from typing import Any

from . import ops as opsmod
from .events import EventBus
from .paths import Paths
from .sequence import summary as seq_summary
from .settings import SettingsStore
from .util import AppError, is_within, new_id, now, random_seed, read_json, write_json_atomic

log = logging.getLogger("vc.store")

SCHEMA = 1
_PID_RE = re.compile(r"^p_[a-z0-9]{6,24}$")
_SUBDIRS = ("media/takes", "media/import", "media/thumbs", "media/cache", "exports", "history")
UNDO_LIMIT = 200
COALESCE_SEC = 2.5

STARTER_SHOTS = [
    ("清晨海港", "清晨的海邊小鎮，薄霧籠罩，金色陽光灑在濕潤的石板路上，電影感構圖，寬景", "鏡頭緩慢推近，海鷗從畫面上方飛過"),
    ("黃色雨衣", "穿黃色雨衣的小女孩站在白色燈塔前，回頭微笑，柔和逆光，淺景深", "她轉身朝燈塔跑去，雨衣隨風擺動"),
    ("燈塔夕照", "夕陽下的燈塔剪影，海浪拍打礁石，溫暖橘紅色調，電影感", "鏡頭緩慢拉遠，浪花飛濺"),
]


class _Entry:
    """Runtime state for one loaded project."""

    def __init__(self, project: dict):
        self.project = project
        self.lock = threading.RLock()
        self.undo: list[dict] = []
        self.redo: list[dict] = []
        self.last_snapshot = 0.0


class ProjectStore:
    def __init__(self, paths: Paths, bus: EventBus, settings: SettingsStore):
        self.paths = paths
        self.bus = bus
        self.settings = settings
        self._lock = threading.Lock()
        self._entries: dict[str, _Entry] = {}
        self._summaries: dict[str, dict] = {}
        self._scan()

    # ------------------------------------------------------------ loading
    def _scan(self) -> None:
        self.paths.projects.mkdir(parents=True, exist_ok=True)
        for d in sorted(self.paths.projects.iterdir()):
            if d.is_dir() and _PID_RE.match(d.name):
                project = self._load_file(d)
                if project:
                    self._entries[d.name] = _Entry(project)
                    self._summaries[d.name] = self._summary(project)

    def _load_file(self, d: Path) -> dict | None:
        try:
            project = read_json(d / "project.json")
            if isinstance(project, dict) and project.get("id") == d.name:
                return self._upgrade(project)
            raise ValueError("missing or mismatched project.json")
        except (ValueError, OSError) as exc:
            snaps = sorted((d / "history").glob("*.json")) if (d / "history").is_dir() else []
            for snap in reversed(snaps):
                data = read_json(snap)
                if isinstance(data, dict) and data.get("id") == d.name:
                    log.warning("Project %s unreadable (%s); recovered from %s", d.name, exc, snap.name)
                    write_json_atomic(d / "project.json", data)
                    return self._upgrade(data)
            log.error("Project %s unreadable and no snapshot to recover from: %s", d.name, exc)
            return None

    def _upgrade(self, project: dict) -> dict:
        project.setdefault("schema", SCHEMA)
        for key, default in (("takes", {}), ("audio", []), ("captions", []), ("media", {}), ("notes", ""),
                             ("cast", []), ("tracks", {}), ("layers", []), ("overlays", []), ("captionStyles", [])):
            project.setdefault(key, default)
        project.setdefault("style", {"prompt": "", "negative": "", "preset": ""})
        project["settings"].setdefault("fadeIn", 0.0)
        project["settings"].setdefault("fadeOut", 0.0)
        defaults = self._defaults()
        project["shots"] = [{**opsmod.blank_shot(s["id"], defaults), **s} for s in project.get("shots", [])]
        return project

    def _defaults(self) -> dict:
        return self.settings.section("defaults")

    def _summary(self, project: dict) -> dict:
        thumb = None
        for shot in project["shots"]:
            for key in ("activeVideo", "activeImage"):
                take = project["takes"].get(shot.get(key) or "")
                if take and take.get("thumb"):
                    thumb = take["thumb"]
                    break
            if thumb:
                break
        return {
            "id": project["id"],
            "name": project["name"],
            "updated": project.get("updated"),
            "created": project.get("created"),
            "rev": project.get("rev", 0),
            "shots": len(project["shots"]),
            "takes": len(project["takes"]),
            "thumb": thumb,
            "settings": project["settings"],
            **seq_summary(project),
        }

    def _entry(self, pid: str) -> _Entry:
        if not _PID_RE.match(pid or ""):
            raise AppError("專案代號格式不正確", 404)
        with self._lock:
            entry = self._entries.get(pid)
        if entry is None:
            raise AppError("找不到這個專案（可能已移到垃圾桶）", 404)
        return entry

    # ------------------------------------------------------------ queries
    def list_projects(self) -> list[dict]:
        with self._lock:
            items = list(self._summaries.values())
        return sorted(items, key=lambda s: s.get("updated") or 0, reverse=True)

    def exists(self, pid: str) -> bool:
        with self._lock:
            return pid in self._entries

    def get(self, pid: str) -> dict:
        entry = self._entry(pid)
        with entry.lock:
            return copy.deepcopy(entry.project)

    def undo_state(self, pid: str) -> dict:
        entry = self._entry(pid)
        with entry.lock:
            return {
                "undo": len(entry.undo),
                "redo": len(entry.redo),
                "undoLabel": entry.undo[-1]["label"] if entry.undo else "",
                "redoLabel": entry.redo[-1]["label"] if entry.redo else "",
            }

    def project_dir(self, pid: str) -> Path:
        self._entry(pid)
        return self.paths.projects / pid

    def resolve_file(self, pid: str, rel: str) -> Path:
        """Map a project-relative path (media/... or exports/...) to disk, refusing traversal."""
        root = self.project_dir(pid)
        rel = (rel or "").replace("\\", "/").lstrip("/")
        if not (rel.startswith("media/") or rel.startswith("exports/")) or ".." in rel.split("/"):
            raise AppError("檔案路徑不合法", 403)
        path = (root / rel).resolve()
        if not is_within(path, root):
            raise AppError("檔案路徑不合法", 403)
        return path

    # ------------------------------------------------------------ mutation
    def create(self, name: str, settings_patch: dict | None = None, starter: bool = False) -> dict:
        pid = new_id("p")
        d = self.paths.projects / pid
        for sub in _SUBDIRS:
            (d / sub).mkdir(parents=True, exist_ok=True)
        defaults = self._defaults()
        settings = {
            "width": defaults["width"],
            "height": defaults["height"],
            "fps": defaults["fps"],
            "aspect": defaults["aspect"],
            "fadeIn": 0.0,
            "fadeOut": 0.0,
        }
        for key, conv in opsmod.SETTINGS_FIELDS.items():
            if settings_patch and key in settings_patch:
                settings[key] = conv(settings_patch[key])
        settings["width"], settings["height"] = opsmod.snap8(settings["width"]), opsmod.snap8(settings["height"])
        shots = []
        seeds = (STARTER_SHOTS if starter else [("鏡頭 1", "", "")])
        for i, (shot_name, prompt, motion) in enumerate(seeds):
            shot = opsmod.blank_shot(new_id("s"), defaults)
            shot.update({"name": shot_name, "prompt": prompt, "motion": motion, "seed": random_seed()})
            if starter:
                shot["transition"] = {"type": "crossfade" if i < len(seeds) - 1 else "cut", "duration": 0.5}
            shots.append(shot)
        project = {
            "schema": SCHEMA,
            "id": pid,
            "name": (name or "").strip()[:120] or "未命名專案",
            "rev": 0,
            "created": now(),
            "updated": now(),
            "settings": settings,
            "style": {"prompt": "", "negative": defaults["negative"], "preset": ""},
            "shots": shots,
            "takes": {},
            "audio": [],
            "captions": [],
            "cast": [],
            "media": {},
            "notes": "",
            "tracks": {},
            "layers": [],
            "overlays": [],
            "captionStyles": [],
        }
        write_json_atomic(d / "project.json", project)
        with self._lock:
            self._entries[pid] = _Entry(project)
            self._summaries[pid] = self._summary(project)
        self._publish_list()
        return copy.deepcopy(project)

    def apply(
        self,
        pid: str,
        ops: list[dict],
        *,
        origin: str = "user",
        undoable: bool = True,
        batch: str | None = None,
        coalesce: str | None = None,
        label: str = "",
    ) -> dict:
        entry = self._entry(pid)
        with entry.lock:
            work = copy.deepcopy(entry.project)
            inverse = opsmod.apply_ops(work, ops, self._defaults())
            if not inverse:
                return {"rev": entry.project["rev"], "changed": False}
            self._commit(entry, work)
            if undoable:
                self._push_undo(entry, inverse, coalesce, label)
            rev = work["rev"]
        self._publish(entry, batch, origin)
        return {"rev": rev, "changed": True}

    def _push_undo(self, entry: _Entry, inverse: list[dict], coalesce: str | None, label: str) -> None:
        top = entry.undo[-1] if entry.undo else None
        t = time.time()
        if coalesce and top and top.get("coalesce") == coalesce and t - top["time"] < COALESCE_SEC:
            top["time"] = t
        else:
            entry.undo.append({"ops": inverse, "label": label, "coalesce": coalesce, "time": t})
            if len(entry.undo) > UNDO_LIMIT:
                del entry.undo[0]
        entry.redo.clear()

    def _step(self, pid: str, source: str) -> dict:
        entry = self._entry(pid)
        with entry.lock:
            stack = entry.undo if source == "undo" else entry.redo
            other = entry.redo if source == "undo" else entry.undo
            if not stack:
                raise AppError("沒有可以" + ("復原" if source == "undo" else "重做") + "的步驟", 409)
            item = stack.pop()
            work = copy.deepcopy(entry.project)
            try:
                inverse = opsmod.apply_ops(work, item["ops"], self._defaults())
            except AppError:
                stack.clear()
                raise AppError("無法" + ("復原" if source == "undo" else "重做") + "：內容已被其他變更影響", 409)
            self._commit(entry, work)
            other.append({"ops": inverse, "label": item["label"], "coalesce": None, "time": time.time()})
            rev = work["rev"]
        self._publish(entry, None, source)
        return {"rev": rev, "label": item["label"]}

    def undo(self, pid: str) -> dict:
        return self._step(pid, "undo")

    def redo(self, pid: str) -> dict:
        return self._step(pid, "redo")

    def _commit(self, entry: _Entry, work: dict, force_snapshot: bool = False) -> None:
        old = entry.project
        work["rev"] = old["rev"] + 1
        work["updated"] = now()
        interval = float(self.settings.section("history").get("intervalSec", 120))
        if force_snapshot or time.time() - entry.last_snapshot >= interval:
            self._snapshot(old)
            entry.last_snapshot = time.time()
        write_json_atomic(self.paths.projects / work["id"] / "project.json", work)
        entry.project = work
        with self._lock:
            self._summaries[work["id"]] = self._summary(work)

    def _snapshot(self, project: dict) -> None:
        hist = self.paths.projects / project["id"] / "history"
        hist.mkdir(parents=True, exist_ok=True)
        write_json_atomic(hist / f"{project['rev']:08d}.json", project)
        keep = int(self.settings.section("history").get("keep", 50))
        snaps = sorted(hist.glob("*.json"))
        for old in snaps[: max(0, len(snaps) - keep)]:
            try:
                old.unlink()
            except OSError:
                pass

    def _publish(self, entry: _Entry, batch: str | None, origin: str) -> None:
        with entry.lock:
            project = entry.project
            payload = {
                "project": project,
                "batch": batch,
                "origin": origin,
                "undo": len(entry.undo),
                "redo": len(entry.redo),
                "undoLabel": entry.undo[-1]["label"] if entry.undo else "",
                "redoLabel": entry.redo[-1]["label"] if entry.redo else "",
            }
            self.bus.publish("project", payload)
        self._publish_list()

    def _publish_list(self) -> None:
        self.bus.publish("projects", self.list_projects())

    # ------------------------------------------------------------ history
    def history(self, pid: str) -> list[dict]:
        hist = self.project_dir(pid) / "history"
        out = []
        for snap in sorted(hist.glob("*.json"), reverse=True):
            data = read_json(snap) or {}
            out.append(
                {
                    "rev": data.get("rev", int(snap.stem)),
                    "time": data.get("updated") or snap.stat().st_mtime,
                    "name": data.get("name", ""),
                    "shots": len(data.get("shots", [])),
                    "takes": len(data.get("takes", {})),
                }
            )
        return out

    def restore_history(self, pid: str, rev: int) -> dict:
        snap = self.project_dir(pid) / "history" / f"{int(rev):08d}.json"
        data = read_json(snap)
        if not isinstance(data, dict):
            raise AppError("找不到這個版本", 404)
        entry = self._entry(pid)
        with entry.lock:
            work = copy.deepcopy(entry.project)
            inverse = opsmod.apply_ops(work, [{"op": "project.replace", "project": data}], self._defaults())
            self._commit(entry, work, force_snapshot=True)
            self._push_undo(entry, inverse, None, f"還原到 r{rev}")
            new_rev = work["rev"]
        self._publish(entry, None, "restore")
        return {"rev": new_rev}

    # ------------------------------------------------------------ project files
    def duplicate(self, pid: str) -> dict:
        src = self.get(pid)
        new_pid = new_id("p")
        src_dir = self.project_dir(pid)
        dst_dir = self.paths.projects / new_pid
        shutil.copytree(src_dir, dst_dir, ignore=shutil.ignore_patterns("history", "exports", "cache"))
        for sub in _SUBDIRS:
            (dst_dir / sub).mkdir(parents=True, exist_ok=True)
        src.update({"id": new_pid, "name": f"{src['name']}（副本）", "rev": 0, "created": now(), "updated": now()})
        write_json_atomic(dst_dir / "project.json", src)
        with self._lock:
            self._entries[new_pid] = _Entry(src)
            self._summaries[new_pid] = self._summary(src)
        self._publish_list()
        return copy.deepcopy(src)

    def adopt(self, folder: Path, project: dict) -> dict:
        """Move an unpacked project folder (see unpack.py) in as a new project with a fresh id."""
        defaults = self._defaults()
        settings = {"width": defaults["width"], "height": defaults["height"], "fps": defaults["fps"],
                    "aspect": defaults["aspect"], "fadeIn": 0.0, "fadeOut": 0.0}
        for key, conv in opsmod.SETTINGS_FIELDS.items():
            if key in project["settings"]:
                try:
                    settings[key] = conv(project["settings"][key])
                except AppError:
                    pass  # keep the default for a value this version cannot read
        settings["width"], settings["height"] = opsmod.snap8(settings["width"]), opsmod.snap8(settings["height"])
        pid = new_id("p")
        project.update({"schema": SCHEMA, "id": pid, "rev": 0, "created": now(), "updated": now(),
                        "settings": settings, "name": str(project.get("name") or "").strip()[:120] or "匯入的專案"})
        for sub in _SUBDIRS:
            (folder / sub).mkdir(parents=True, exist_ok=True)
        write_json_atomic(folder / "project.json", project)
        dst = self.paths.projects / pid
        shutil.move(str(folder), str(dst))
        loaded = self._load_file(dst)
        if not loaded:
            shutil.rmtree(dst, ignore_errors=True)
            raise AppError("匯入的專案檔無法讀取", 400)
        with self._lock:
            self._entries[pid] = _Entry(loaded)
            self._summaries[pid] = self._summary(loaded)
        self._publish_list()
        return copy.deepcopy(loaded)

    def trash(self, pid: str) -> None:
        entry = self._entry(pid)
        with entry.lock:
            src = self.paths.projects / pid
            self.paths.trash.mkdir(parents=True, exist_ok=True)
            dst = self.paths.trash / f"{pid}__{int(time.time())}"
            try:
                shutil.move(str(src), str(dst))
            except OSError as exc:
                log.warning("Moving project %s to trash failed: %s", pid, exc)
                raise AppError("專案檔案正在使用中（可能正在播放或匯出），請稍後再試", 409) from None
            with self._lock:
                self._entries.pop(pid, None)
                self._summaries.pop(pid, None)
        self._publish_list()

    def list_trash(self) -> list[dict]:
        out = []
        if not self.paths.trash.is_dir():
            return out
        for d in sorted(self.paths.trash.iterdir(), reverse=True):
            data = read_json(d / "project.json") or {}
            out.append(
                {
                    "folder": d.name,
                    "id": data.get("id"),
                    "name": data.get("name", d.name),
                    "deleted": d.stat().st_mtime,
                    "shots": len(data.get("shots", [])),
                }
            )
        return out

    def restore_trash(self, folder: str) -> dict:
        src = self.paths.trash / folder
        if not re.match(r"^p_[a-z0-9]{6,24}__\d+$", folder or "") or not src.is_dir():
            raise AppError("垃圾桶中找不到這個專案", 404)
        pid = folder.split("__")[0]
        if self.exists(pid) or (self.paths.projects / pid).exists():
            raise AppError("已有相同代號的專案，無法還原", 409)
        shutil.move(str(src), str(self.paths.projects / pid))
        project = self._load_file(self.paths.projects / pid)
        if not project:
            raise AppError("專案檔損毀，無法還原", 500)
        with self._lock:
            self._entries[pid] = _Entry(project)
            self._summaries[pid] = self._summary(project)
        self._publish_list()
        return {"id": pid}

    def empty_trash(self) -> int:
        count = 0
        for d in list(self.paths.trash.iterdir()) if self.paths.trash.is_dir() else []:
            shutil.rmtree(d, ignore_errors=True)
            count += 0 if d.exists() else 1
        return count

    def usage(self, pid: str) -> dict[str, Any]:
        """Disk usage of a project, split into referenced and unreferenced media files."""
        project = self.get(pid)
        root = self.project_dir(pid)
        referenced: set[str] = set()
        for item in list(project["takes"].values()) + list(project["media"].values()):
            for key in ("file", "thumb", "strip", "wave"):
                if item.get(key):
                    referenced.add(item[key])
        used = unused = 0
        orphans: list[str] = []
        fresh = time.time() - 300  # files a running job just wrote may not be referenced yet
        for sub in ("media/takes", "media/import", "media/thumbs"):
            for f in (root / sub).glob("*"):
                if not f.is_file():
                    continue
                rel = f.relative_to(root).as_posix()
                st = f.stat()
                size = st.st_size
                if rel in referenced or st.st_mtime > fresh:
                    used += size
                else:
                    unused += size
                    orphans.append(rel)
        return {"used": used, "unused": unused, "orphans": orphans}

    def purge_orphans(self, pid: str) -> int:
        """Delete media files no take/media entry references (deleted takes stay undoable until this)."""
        entry = self._entry(pid)
        with entry.lock:
            info = self.usage(pid)
            root = self.project_dir(pid)
            for rel in info["orphans"]:
                try:
                    (root / rel).unlink()
                except OSError:
                    log.warning("Could not delete orphan %s", rel)
            entry.undo.clear()
            entry.redo.clear()
        self._publish(entry, None, "purge")
        return info["unused"]
