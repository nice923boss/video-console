"""Project storage: one folder per project, op batches with undo/redo, snapshots and trash."""
from __future__ import annotations

import copy
import json
import logging
import os
import re
import shutil
import threading
import time
from pathlib import Path
from typing import Any

from . import opfields
from . import ops as opsmod
from .events import EventBus
from .paths import Paths
from .sequence import summary as seq_summary
from .settings import SettingsStore
from .util import AppError, is_within, new_id, now, random_seed, read_json, replace_with_retry, write_json_atomic
from .voiceclean import CLEAN_DIR

log = logging.getLogger("vc.store")

SCHEMA = 1
_PID_RE = re.compile(r"^p_[a-z0-9]{6,24}$")
_SUBDIRS = ("media/takes", "media/import", "media/thumbs", "media/cache", "exports", "history")
UNDO_LIMIT = 200
# Cache entries (uploads, job frames, export work files) older than a day belong to nothing still running.
CACHE_MAX_AGE = 86400
CACHE_PREFIXES = ("upload-", "job-", "export-")
COALESCE_SEC = 2.5
# Each edit appends its ops to project.journal (small, fsynced); project.json itself is rewritten once edits
# pause for FLUSH_IDLE seconds, or every FLUSH_MAX seconds while they keep coming. Loading replays what the
# journal holds beyond project.json, so a crash between the two loses nothing.
JOURNAL = "project.journal"
FLUSH_IDLE = 1.0
FLUSH_MAX = 10.0
FLUSH_RETRY = 30.0

BLANK_SHOT_NAME = "鏡頭 1"
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
        self.save_lock = threading.Lock()  # one project.json write at a time, outside the edit lock
        self.journal: list[tuple[int, str]] = []  # journal lines not yet folded into project.json
        self.pending_snapshots: list[dict] = []
        self.dirty_since = 0.0
        self.last_edit = 0.0
        self.next_try = 0.0
        self.gone = False  # moved to the trash: nothing may write into its old folder again


class ProjectStore:
    def __init__(self, paths: Paths, bus: EventBus, settings: SettingsStore):
        self.paths = paths
        self.bus = bus
        self.settings = settings
        self._lock = threading.Lock()
        self._entries: dict[str, _Entry] = {}
        self._summaries: dict[str, dict] = {}
        self._closing = threading.Event()
        self._flusher: threading.Thread | None = None
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
                return self._replay(d, self._upgrade(project))
            raise ValueError("missing or mismatched project.json")
        except (ValueError, OSError) as exc:
            snaps = sorted((d / "history").glob("*.json")) if (d / "history").is_dir() else []
            for snap in reversed(snaps):
                data = read_json(snap)
                if isinstance(data, dict) and data.get("id") == d.name:
                    log.warning("Project %s unreadable (%s); recovered from %s", d.name, exc, snap.name)
                    write_json_atomic(d / "project.json", data)
                    return self._replay(d, self._upgrade(data))
            log.error("Project %s unreadable and no snapshot to recover from: %s", d.name, exc)
            return None

    def _replay(self, d: Path, project: dict) -> dict:
        """Re-apply the edits project.journal holds beyond project.json (the app stopped before saving them)."""
        path = d / JOURNAL
        if not path.exists():
            return project
        replayed = 0
        try:
            lines = path.read_text(encoding="utf-8").split("\n")
        except OSError as exc:
            log.warning("Project %s: journal unreadable (%s)", d.name, exc)
            lines = []
        for line in filter(None, lines):
            try:
                item = json.loads(line)
                if item["rev"] <= project["rev"]:
                    continue
                if item["rev"] != project["rev"] + 1:
                    break
                work = copy.deepcopy(project)
                opsmod.apply_ops(work, item["ops"], item["defaults"])
            except (ValueError, KeyError, TypeError, AppError) as exc:
                log.warning("Project %s: journal stops at r%s (%s)", d.name, project["rev"] + 1, exc)
                break
            work["rev"], work["updated"] = item["rev"], item["updated"]
            project = work
            replayed += 1
        if replayed:
            log.info("Project %s: replayed %d unsaved edits from the journal", d.name, replayed)
            write_json_atomic(d / "project.json", project)
        try:
            path.unlink()
        except OSError as exc:
            log.warning("Project %s: could not remove the replayed journal (%s)", d.name, exc)
        return project

    def _upgrade(self, project: dict) -> dict:
        project.setdefault("schema", SCHEMA)
        for key, default in (("takes", {}), ("audio", []), ("captions", []), ("media", {}), ("notes", ""),
                             ("cast", []), ("tracks", {}), ("layers", []), ("overlays", []), ("captionStyles", []),
                             ("adjustments", []), ("markers", []), ("captionTracks", [])):
            project.setdefault(key, default)
        project.setdefault("style", {"prompt": "", "negative": "", "preset": ""})
        project["settings"].setdefault("fadeIn", 0.0)
        project["settings"].setdefault("fadeOut", 0.0)
        defaults = self._defaults()
        project["shots"] = [{**opfields.blank_shot(s["id"], defaults), **s} for s in project.get("shots", [])]
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

    def locked(self, pid: str) -> threading.RLock:
        """The project's lock, held around a read that decides an apply (no other write slips in between)."""
        return self._entry(pid).lock

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
        for key, conv in opfields.SETTINGS_FIELDS.items():
            if settings_patch and key in settings_patch:
                settings[key] = conv(settings_patch[key])
        settings["width"], settings["height"] = opfields.snap8(settings["width"]), opfields.snap8(settings["height"])
        shots = []
        seeds = (STARTER_SHOTS if starter else [(BLANK_SHOT_NAME, "", "")])
        for i, (shot_name, prompt, motion) in enumerate(seeds):
            shot = opfields.blank_shot(new_id("s"), defaults)
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
            "adjustments": [],
            "markers": [],
            "captionTracks": [],
        }
        library = self.settings.section("captionStyles") if self.settings.section("captionStylesAuto") else []
        if library:  # as if each library style were added by hand: the first becomes the default
            project["captionStyles"] = [{**s, "id": new_id("cs")} for s in library]
            settings["captionStyle"] = project["captionStyles"][0]["id"]
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
            ops_json = json.dumps(ops, ensure_ascii=False)  # as sent: apply_ops may fill in the op dicts
            inverse = opsmod.apply_ops(work, ops, self._defaults())
            if not inverse:
                return {"rev": entry.project["rev"], "changed": False}
            self._commit(entry, work, ops_json)
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
            ops_json = json.dumps(item["ops"], ensure_ascii=False)
            try:
                inverse = opsmod.apply_ops(work, item["ops"], self._defaults())
            except AppError:
                stack.clear()
                raise AppError("無法" + ("復原" if source == "undo" else "重做") + "：內容已被其他變更影響", 409)
            self._commit(entry, work, ops_json)
            other.append({"ops": inverse, "label": item["label"], "coalesce": None, "time": time.time()})
            rev = work["rev"]
        self._publish(entry, None, source)
        return {"rev": rev, "label": item["label"]}

    def undo(self, pid: str) -> dict:
        return self._step(pid, "undo")

    def redo(self, pid: str) -> dict:
        return self._step(pid, "redo")

    def _commit(self, entry: _Entry, work: dict, ops_json: str, force_snapshot: bool = False) -> None:
        old = entry.project
        work["rev"] = old["rev"] + 1
        work["updated"] = now()
        line = (f'{{"rev":{work["rev"]},"updated":{json.dumps(work["updated"])},'
                f'"defaults":{json.dumps(self._defaults(), ensure_ascii=False)},"ops":{ops_json}}}')
        with open(self.paths.projects / work["id"] / JOURNAL, "a", encoding="utf-8", newline="\n") as f:
            f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())
        entry.journal.append((work["rev"], line))
        interval = float(self.settings.section("history").get("intervalSec", 120))
        if force_snapshot or time.time() - entry.last_snapshot >= interval:
            entry.pending_snapshots.append(old)  # written with the next save; projects are never edited in place
            entry.last_snapshot = time.time()
        entry.last_edit = time.time()
        entry.dirty_since = entry.dirty_since or entry.last_edit
        entry.project = work
        with self._lock:
            self._summaries[work["id"]] = self._summary(work)
        self._start_flusher()

    # ------------------------------------------------------------ saving
    def _start_flusher(self) -> None:
        with self._lock:
            if self._flusher is None and not self._closing.is_set():
                self._flusher = threading.Thread(target=self._flush_loop, name="vc-store-flush", daemon=True)
                self._flusher.start()

    def _flush_loop(self) -> None:
        while not self._closing.wait(0.25):
            t = time.time()
            with self._lock:
                entries = list(self._entries.values())
            for entry in entries:
                if entry.dirty_since and t >= entry.next_try and (
                        t - entry.last_edit >= FLUSH_IDLE or t - entry.dirty_since >= FLUSH_MAX):
                    self._flush_entry(entry)

    def _flush_entry(self, entry: _Entry) -> None:
        """Fold the journal into project.json (with any history snapshot that came due)."""
        with entry.save_lock:
            with entry.lock:
                if not entry.dirty_since or entry.gone:
                    return
                project, snaps = entry.project, entry.pending_snapshots
                entry.pending_snapshots, entry.dirty_since = [], 0.0
            try:
                for snap in snaps:
                    self._snapshot(snap)
                write_json_atomic(self.paths.projects / project["id"] / "project.json", project)
            except Exception:
                log.exception("Saving project %s failed; its journal keeps the edits", project["id"])
                with entry.lock:
                    entry.pending_snapshots = snaps + entry.pending_snapshots
                    entry.dirty_since = entry.dirty_since or time.time()
                    entry.next_try = time.time() + FLUSH_RETRY
                return
            with entry.lock:
                entry.journal = [(rev, line) for rev, line in entry.journal if rev > project["rev"]]
                self._write_journal(project["id"], [line for _, line in entry.journal])

    def _write_journal(self, pid: str, lines: list[str]) -> None:
        path = self.paths.projects / pid / JOURNAL
        try:
            if not lines:
                path.unlink(missing_ok=True)
                return
            tmp = path.with_name(".tmp-" + JOURNAL)
            tmp.write_text("".join(line + "\n" for line in lines), encoding="utf-8", newline="\n")
            replace_with_retry(tmp, path)
        except OSError as exc:  # the stale lines left behind are skipped on load (their rev is already saved)
            log.warning("Trimming the journal of %s failed: %s", pid, exc)

    def flush(self, pid: str | None = None) -> None:
        """Save unsaved edits to project.json now: before reading the folder from disk, and at shutdown."""
        if pid is not None:
            entries = [self._entry(pid)]
        else:
            with self._lock:
                entries = list(self._entries.values())
        for entry in entries:
            self._flush_entry(entry)

    def close(self) -> None:
        self._closing.set()
        if self._flusher is not None:
            self._flusher.join(timeout=5)
        self.flush()

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
        self.flush(pid)  # a snapshot that came due is written with the next save
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
            ops = [{"op": "project.replace", "project": data}]
            ops_json = json.dumps(ops, ensure_ascii=False)
            inverse = opsmod.apply_ops(work, ops, self._defaults())
            self._commit(entry, work, ops_json, force_snapshot=True)
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
        shutil.copytree(src_dir, dst_dir, ignore=shutil.ignore_patterns("history", "exports", "cache", JOURNAL))
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
        for key, conv in opfields.SETTINGS_FIELDS.items():
            if key in project["settings"]:
                try:
                    settings[key] = conv(project["settings"][key])
                except AppError:
                    pass  # keep the default for a value this version cannot read
        settings["width"], settings["height"] = opfields.snap8(settings["width"]), opfields.snap8(settings["height"])
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
        self._flush_entry(entry)
        with entry.save_lock, entry.lock:
            src = self.paths.projects / pid
            self.paths.trash.mkdir(parents=True, exist_ok=True)
            dst = self.paths.trash / f"{pid}__{int(time.time())}"
            try:
                shutil.move(str(src), str(dst))
            except OSError as exc:
                log.warning("Moving project %s to trash failed: %s", pid, exc)
                raise AppError("專案檔案正在使用中（可能正在播放或匯出），請稍後再試", 409) from None
            entry.gone = True
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
        """Disk usage of a project, split into referenced and unreferenced media files (stale cache,
        interrupted-save leftovers and media in the project trash count as unreferenced)."""
        project = self.get(pid)
        root = self.project_dir(pid)
        referenced: set[str] = set()
        kept = [m for m in project["media"].values() if not m.get("trashed")]
        for item in list(project["takes"].values()) + kept:
            for key in ("file", "thumb", "strip", "wave", "preview2", "preview4", "previewAudio"):
                if item.get(key):
                    referenced.add(item[key])
            referenced.update((item.get("previewAudioSpeeds") or {}).values())
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
        parked = [a for s in project["shots"] for a in (s.get("parked") or {}).get("audio") or []]
        live = {a["id"] for a in [*(project.get("audio") or []), *parked]}
        for f in (root / CLEAN_DIR).glob("*"):  # stems of audio clips no longer in the project, half-written passes
            st = f.stat()
            gone = f.suffix == ".part" or f.name.lstrip(".").split("-")[0] not in live
            if f.is_file() and gone and st.st_mtime <= fresh:
                unused += st.st_size
                orphans.append(f.relative_to(root).as_posix())
        stale = time.time() - CACHE_MAX_AGE
        leftovers = [f for f in (root / "media" / "cache").glob("*") if f.name.startswith(CACHE_PREFIXES)]
        for f in leftovers + list(root.glob(".tmp-*")):
            if f.stat().st_mtime < stale:
                files = [x for x in f.rglob("*") if x.is_file()] if f.is_dir() else [f]
                unused += sum(x.stat().st_size for x in files)
                orphans.append(f.relative_to(root).as_posix())
        return {"used": used, "unused": unused, "orphans": orphans,
                "trashed": len(project["media"]) - len(kept)}

    def purge_orphans(self, pid: str) -> int:
        """Delete media files no take/media entry references (deleted takes stay undoable until this), and
        empty the project trash."""
        entry = self._entry(pid)
        with entry.lock:
            trashed = [m["id"] for m in entry.project["media"].values() if m.get("trashed")]
            if trashed:
                self.apply(pid, [{"op": "media.delete", "id": mid} for mid in trashed], origin="purge", undoable=False)
            info = self.usage(pid)
            root = self.project_dir(pid)
            for rel in info["orphans"]:
                try:
                    path = root / rel
                    shutil.rmtree(path) if path.is_dir() else path.unlink()
                except OSError:
                    log.warning("Could not delete orphan %s", rel)
            entry.undo.clear()
            entry.redo.clear()
        self._publish(entry, None, "purge")
        return info["unused"]
