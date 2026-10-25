"""Background job queue: lanes with concurrency limits, persistence, restart recovery and live progress.

A job is a plain dict (persisted in jobs.json). The queue only schedules; what a job does lives in an
Executor (runners.Runners). Every lane ("comfy", "provider:kling", "local:ffmpeg" ...) runs at most
LANE_LIMITS jobs at once, in queue order, so a slow cloud render never blocks local ComfyUI work.
Within a lane, waiting voice-over lines start before everything else (see queue_key).
"""
from __future__ import annotations

import copy
import logging
import threading
import time
from collections import Counter
from typing import Any, Callable, Protocol

from .events import EventBus
from .net import HttpError, advise
from .paths import Paths
from .util import AppError, new_id, now, random_seed, read_json, write_json_atomic

log = logging.getLogger("vc.jobs")

KINDS = ("image", "video", "upscale", "upscalevideo", "kenburns", "lastframe", "reverse", "ingest", "export", "package",
         "voice", "transcribe", "align")
ACTIVE = ("queued", "running")
FINISHED = ("done", "failed", "canceled", "interrupted")
LANE_LIMITS = {"comfy": 1, "provider": 2, "local:ffmpeg": 2, "local:export": 1}
KEEP_FINISHED = 300
MAX_ACTIVE = 500
PUBLISH_INTERVAL = 0.25
SAVE_INTERVAL = 1.0
MAX_PREVIEW_BYTES = 8 * 1024 * 1024
RETRYABLE = (0, 429, 502, 503, 504)  # no response, rate limited, gateway trouble: worth another try
RETRY_DELAY = 15  # seconds before the first automatic retry, doubled for each one after it

Listener = Callable[[dict, int], None]


class Executor(Protocol):
    def prepare(self, kind: str, pid: str, shot_id: str | None, payload: dict) -> dict:
        """Validate a request; return {"title", "lane", "payload", "shotId"}. Raise AppError to refuse."""

    def admit(self, prepared: dict) -> None:
        """Last check before a prepared job is queued, against outside services. Raise AppError to refuse."""

    def execute(self, ctl: JobControl) -> dict:
        """Do the work; return the job result ({"takeIds": [...], "file": ..., "message": ...})."""

    def after_restart(self, job: dict) -> str:
        """For a job that was running when the app stopped: "resume", "restart" or "interrupt"."""


def lane_limit(lane: str) -> int:
    return LANE_LIMITS.get(lane) or LANE_LIMITS.get(lane.split(":", 1)[0], 1)


def epoch_or_none(value: Any) -> float | None:
    """A schedule time read from JSON: a positive number of epoch seconds, anything else None."""
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0 else None


def queue_key(job: dict) -> tuple[int, float]:
    """Start order of waiting jobs: voice-over first (a line takes seconds, a render minutes), then by order.
    It does not stop a render already running. The queue view sorts the same way (views/queue.js)."""
    return (0 if job["kind"] == "voice" else 1, job["order"])


class JobControl:
    """Hooks handed to a running job: progress, cancellation, live preview, remote handle, notes."""

    def __init__(self, manager: JobManager, job: dict):
        self._manager = manager
        self._cancel = threading.Event()
        self.id: str = job["id"]
        self.kind: str = job["kind"]
        self.pid: str = job["pid"]
        self.shot_id: str | None = job.get("shotId")
        self.payload: dict = copy.deepcopy(job.get("payload") or {})
        self.remote_data: dict | None = copy.deepcopy(job.get("remote"))

    @property
    def resuming(self) -> bool:
        """True when this run picks up a task submitted before the app restarted."""
        return bool(self.remote_data)

    def progress(self, frac: float, message: str | None = None) -> None:
        self._manager._progress(self.id, frac, message)

    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def check(self) -> None:
        if self._cancel.is_set():
            raise AppError("已取消", 499)

    def preview(self, mime: str, data: bytes) -> None:
        self._manager._preview(self.id, mime, data)

    def remote(self, data: dict) -> None:
        self.remote_data = copy.deepcopy(data)
        self._manager._set_remote(self.id, data)

    def note(self, text: str) -> None:
        self._manager._note(self.id, text)

    def update_payload(self, changes: dict) -> None:
        """Change request fields while running (e.g. the translated prompt), so the job record shows what was sent."""
        self.payload.update(copy.deepcopy(changes))
        self._manager._update_payload(self.id, changes)

    def spawn(self, kind: str, payload: dict, shot_id: str | None = None) -> dict:
        """Queue a follow-up job (e.g. image then video) in the same project."""
        return self._manager.submit(kind, self.pid, shot_id, payload, parent=self.id)


class JobManager:
    def __init__(self, paths: Paths, bus: EventBus, executor: Executor, retry_limit: Callable[[], int] = lambda: 0):
        self._paths = paths
        self._bus = bus
        self._executor = executor
        self._retry_limit = retry_limit  # automatic retries after a network error (settings queue.autoRetry)
        self._lock = threading.RLock()
        self._save_lock = threading.Lock()
        self._jobs: dict[str, dict] = {}
        self._controls: dict[str, JobControl] = {}
        self._previews: dict[str, tuple[str, bytes]] = {}
        self._dirty: set[str] = set()
        self._listeners: list[Listener] = []
        self._paused = False
        # Night run (QU-06): the queue starts by itself at resume_at and pauses again at pause_at (epoch seconds).
        self._resume_at: float | None = None
        self._pause_at: float | None = None
        self._order = 0.0
        self._save_due = False
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._load()

    # ---------------------------------------------------------------- lifecycle
    def start(self) -> None:
        self._recover()
        for target, name in ((self._dispatch_loop, "jobs-dispatch"), (self._tick_loop, "jobs-tick")):
            thread = threading.Thread(target=target, name=name, daemon=True)
            thread.start()
            self._threads.append(thread)
        self._wake.set()

    def stop(self) -> None:
        """Stop scheduling and persist. Running jobs are not cancelled: ComfyUI / cloud tasks they
        submitted keep going and are picked up again on the next start."""
        self._stop.set()
        self._wake.set()
        self._save()

    def add_listener(self, listener: Listener) -> None:
        """Called (job, active_count) whenever a job reaches a final state."""
        self._listeners.append(listener)

    def _load(self) -> None:
        data = read_json(self._paths.jobs_file, {}) or {}
        self._paused = bool(data.get("paused"))
        self._resume_at = epoch_or_none(data.get("resumeAt"))
        self._pause_at = epoch_or_none(data.get("pauseAt"))
        for job in data.get("jobs") or []:
            if isinstance(job, dict) and isinstance(job.get("id"), str) and job.get("kind") in KINDS:
                job.setdefault("notes", [])
                job.setdefault("order", job.get("created") or 0.0)
                self._jobs[job["id"]] = job
        self._order = max([float(j.get("order") or 0.0) for j in self._jobs.values()] + [time.time()])

    def _recover(self) -> None:
        """Decide what happens to jobs that were running when the app last stopped."""
        with self._lock:
            jobs = [j for j in self._jobs.values() if j["status"] == "running"]
        for job in jobs:
            try:
                policy = self._executor.after_restart(copy.deepcopy(job))
            except Exception:  # noqa: BLE001 - a broken record must not stop the app from starting
                log.exception("Recovery check failed for job %s", job["id"])
                policy = "interrupt"
            with self._lock:
                if policy == "resume" and job.get("remote"):
                    job.update(status="queued", message="程式重新啟動，等待接續原本的任務")
                elif policy == "restart":
                    job.update(status="queued", message="程式重新啟動，重新排隊", remote=None, progress=0.0)
                else:
                    job.update(status="interrupted", finished=now(), error="程式關閉時任務仍在進行",
                               message="程式關閉時中斷，可按「重試」重新送出")
        with self._lock:
            for job in self._jobs.values():
                if job["status"] == "queued" and not job.get("remote"):
                    job["message"] = "排隊中"
        self._save()

    # ---------------------------------------------------------------- queries
    @staticmethod
    def _public(job: dict) -> dict:
        return copy.deepcopy(job)

    def list(self, pid: str | None = None) -> list[dict]:
        with self._lock:
            jobs = [self._public(j) for j in self._jobs.values() if pid is None or j["pid"] == pid]
        return sorted(jobs, key=lambda j: j.get("created") or 0)

    def get(self, jid: str) -> dict:
        with self._lock:
            job = self._jobs.get(jid)
            if job is None:
                raise AppError("找不到這個工作（可能已從清單清除）", 404)
            return self._public(job)

    def state(self) -> dict:
        with self._lock:
            counts = Counter(j["status"] for j in self._jobs.values())
            return {"paused": self._paused, "resumeAt": self._resume_at, "pauseAt": self._pause_at, "counts": dict(counts),
                    "active": counts.get("queued", 0) + counts.get("running", 0)}

    def preview(self, jid: str) -> tuple[str, bytes]:
        with self._lock:
            item = self._previews.get(jid)
        if item is None:
            raise AppError("目前沒有即時預覽", 404)
        return item

    def active_count(self, pid: str | None = None) -> int:
        with self._lock:
            return sum(1 for j in self._jobs.values() if j["status"] in ACTIVE and (pid is None or j["pid"] == pid))

    # ---------------------------------------------------------------- commands
    def submit(self, kind: str, pid: str, shot_id: str | None, payload: dict | None, *,
               parent: str | None = None) -> dict:
        if kind not in KINDS:
            raise AppError(f"不支援的工作類型：{kind}")
        if self.active_count() >= MAX_ACTIVE:
            raise AppError("佇列裡的工作太多了，請等目前的工作完成再送出", 429)
        prepared = self._executor.prepare(kind, pid, shot_id, dict(payload or {}))
        self._executor.admit(prepared)
        # The timeline's link toggle at submit time, read when the take lands (takes.linked).
        if (payload or {}).get("link") is False:
            prepared["payload"] = {**prepared["payload"], "link": False}
        job = {
            "id": new_id("j"),
            "kind": kind,
            "pid": pid,
            "shotId": prepared.get("shotId", shot_id),
            "title": prepared["title"],
            "status": "queued",
            "progress": 0.0,
            "message": "排隊中",
            "created": now(),
            "started": None,
            "finished": None,
            "lane": prepared["lane"],
            "payload": prepared["payload"],
            "result": None,
            "error": None,
            "remote": None,
            "notes": list(prepared.get("notes") or []),
            "order": self._next_order(),
            "attempt": 1,
            "parent": parent,
            "previewRev": 0,
        }
        with self._lock:
            self._jobs[job["id"]] = job
        self._emit(job["id"], save=True)
        self._wake.set()
        return self._public(job)

    def cancel(self, jid: str) -> dict:
        with self._lock:
            job = self._require(jid)
            if job["status"] == "queued":
                job.update(status="canceled", finished=now(), message="已取消", remote=None)
                listeners_job: dict | None = self._public(job)
            elif job["status"] == "running":
                ctl = self._controls.get(jid)
                if ctl is not None:
                    ctl._cancel.set()
                job["message"] = "取消中…"
                listeners_job = None
            else:
                return self._public(job)
        self._emit(jid, save=True)
        if listeners_job:
            self._notify(listeners_job)
        self._wake.set()
        return self.get(jid)

    def cancel_all(self, pid: str | None = None) -> int:
        with self._lock:
            ids = [j["id"] for j in self._jobs.values() if j["status"] in ACTIVE and (pid is None or j["pid"] == pid)]
        for jid in ids:
            self.cancel(jid)
        return len(ids)

    def retry(self, jid: str, new_seed: bool = False) -> dict:
        """Queue a finished job again with the same request; new_seed swaps a seeded request's seed for a fresh one."""
        with self._lock:
            job = self._require(jid)
            if job["status"] in ACTIVE:
                raise AppError("這個工作還在進行中", 409)
            if job["status"] == "done" and job["kind"] in ("export", "package", "ingest"):
                raise AppError("這個工作已經完成", 409)
            if new_seed:
                if job["payload"].get("seed") is None:
                    raise AppError("這個工作沒有種子可以更換", 409)
                job["payload"]["seed"] = random_seed()
            job.update(status="queued", progress=0.0, message="排隊中", error=None, result=None, remote=None,
                       started=None, finished=None, order=self._next_order(), notes=[], previewRev=0,
                       attempt=int(job.get("attempt") or 1) + 1, autoRetries=0, notBefore=None)
        self._emit(jid, save=True)
        self._wake.set()
        return self.get(jid)

    def move_top(self, jid: str) -> dict:
        with self._lock:
            job = self._require(jid)
            if job["status"] != "queued":
                raise AppError("只有排隊中的工作可以調整順序", 409)
            orders = [j["order"] for j in self._jobs.values() if j["status"] == "queued"]
            job["order"] = min(orders) - 1.0
        self._emit(jid, save=True)
        return self.get(jid)

    def move(self, jid: str, before: str | None) -> dict:
        """Put a waiting job just ahead of another waiting job, or last when `before` is None (QU-02 drag)."""
        with self._lock:
            job = self._require(jid)
            if job["status"] != "queued":
                raise AppError("只有排隊中的工作可以調整順序", 409)
            rest = sorted((j for j in self._jobs.values() if j["status"] == "queued" and j["id"] != jid),
                          key=lambda j: j["order"])
            at = len(rest) if before is None else next((i for i, j in enumerate(rest) if j["id"] == before), -1)
            if at < 0:
                raise AppError("目標工作已經開始或不存在，請重新整理後再試", 409)
            if at == len(rest):
                job["order"] = rest[-1]["order"] + 1.0 if rest else job["order"]
            else:
                low = rest[at - 1]["order"] if at else rest[at]["order"] - 2.0
                job["order"] = (low + rest[at]["order"]) / 2
        self._emit(jid, save=True)
        return self.get(jid)

    def remove(self, jid: str) -> None:
        with self._lock:
            job = self._require(jid)
            if job["status"] in ACTIVE:
                raise AppError("進行中的工作不能移除，請先取消", 409)
            self._jobs.pop(jid, None)
            self._previews.pop(jid, None)
        self._publish_all()

    def clear(self, pid: str | None = None, status: str | None = None) -> int:
        """Remove finished jobs (optionally only one project's, or only one final status)."""
        wanted = (status,) if status in FINISHED else FINISHED
        with self._lock:
            ids = [j["id"] for j in self._jobs.values()
                   if j["status"] in wanted and (pid is None or j["pid"] == pid)]
            for jid in ids:
                self._jobs.pop(jid, None)
                self._previews.pop(jid, None)
        if ids:
            self._publish_all()
        return len(ids)

    def pause(self, paused: bool) -> dict:
        """Pause or resume by hand, which also drops a night-run schedule."""
        with self._lock:
            self._paused = bool(paused)
            self._resume_at = self._pause_at = None
        self._save_due = True
        self._bus.publish("queue", self.state())
        self._wake.set()
        return self.state()

    def schedule(self, start: float | None, stop: float | None) -> dict:
        """Night run (QU-06): stay paused until `start`, then pause again at `stop`; running jobs finish."""
        t = now()
        if start is not None and start <= t:
            raise AppError("開始時間要在現在之後")
        if stop is not None and stop <= (start or t):
            raise AppError("暫停時間要在開始時間之後")
        with self._lock:
            self._resume_at, self._pause_at = start, stop
            if start is not None:
                self._paused = True
        self._save_due = True
        self._bus.publish("queue", self.state())
        return self.state()

    def _run_schedule(self) -> None:
        t = now()
        with self._lock:
            due = False
            if self._resume_at is not None and t >= self._resume_at:
                self._paused, self._resume_at, due = False, None, True
            if self._pause_at is not None and t >= self._pause_at:
                self._paused, self._pause_at, due = True, None, True
        if due:
            log.info("Queue %s by schedule", "paused" if self._paused else "started")
            self._save_due = True
            self._bus.publish("queue", self.state())

    # ---------------------------------------------------------------- hooks used by JobControl
    def _progress(self, jid: str, frac: float, message: str | None) -> None:
        with self._lock:
            job = self._jobs.get(jid)
            if job is None or job["status"] != "running":
                return
            try:
                value = max(0.0, min(0.995, float(frac)))
            except (TypeError, ValueError):
                value = job["progress"]
            job["progress"] = round(max(job["progress"], value), 4)
            if message and not job["message"].startswith("取消中"):
                job["message"] = str(message)[:300]
            self._dirty.add(jid)

    def _preview(self, jid: str, mime: str, data: bytes) -> None:
        if not data or len(data) > MAX_PREVIEW_BYTES:
            return
        with self._lock:
            job = self._jobs.get(jid)
            if job is None or job["status"] != "running":
                return
            self._previews[jid] = (mime or "image/jpeg", data)
            job["previewRev"] = int(job.get("previewRev") or 0) + 1
            self._dirty.add(jid)

    def _set_remote(self, jid: str, data: dict) -> None:
        with self._lock:
            job = self._jobs.get(jid)
            if job is None:
                return
            job["remote"] = copy.deepcopy(data)
        # The handle is what lets a restart resume the task, so persist it right away.
        self._save()

    def _note(self, jid: str, text: str) -> None:
        with self._lock:
            job = self._jobs.get(jid)
            if job is None:
                return
            text = str(text)[:300]
            if text and text not in job["notes"]:
                job["notes"].append(text)
                self._dirty.add(jid)

    def _update_payload(self, jid: str, changes: dict) -> None:
        with self._lock:
            job = self._jobs.get(jid)
            if job is None:
                return
            job["payload"].update(copy.deepcopy(changes))
            self._dirty.add(jid)

    # ---------------------------------------------------------------- scheduling
    def _next_order(self) -> float:
        with self._lock:
            self._order = max(self._order + 1.0, time.time())
            return self._order

    def _require(self, jid: str) -> dict:
        job = self._jobs.get(jid)
        if job is None:
            raise AppError("找不到這個工作（可能已從清單清除）", 404)
        return job

    def _dispatch_loop(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(1.0)
            self._wake.clear()
            if self._stop.is_set():
                return
            try:
                self._run_schedule()
                self._dispatch()
            except Exception:  # noqa: BLE001 - keep the scheduler alive whatever happens
                log.exception("Job dispatch failed")

    def _dispatch(self) -> None:
        started: list[JobControl] = []
        with self._lock:
            if self._paused:
                return
            running = Counter(j["lane"] for j in self._jobs.values() if j["status"] == "running")
            queued = sorted((j for j in self._jobs.values() if j["status"] == "queued"), key=queue_key)
            for job in queued:
                lane = job["lane"]
                if running[lane] >= lane_limit(lane) or (job.get("notBefore") or 0) > now():
                    continue
                running[lane] += 1
                job.update(status="running", started=now(), progress=0.0, previewRev=0,
                           message="接續先前送出的任務" if job.get("remote") else "開始執行")
                ctl = JobControl(self, job)
                self._controls[job["id"]] = ctl
                started.append(ctl)
        for ctl in started:
            self._emit(ctl.id, save=True)
            threading.Thread(target=self._run, args=(ctl,), name=f"job-{ctl.id}", daemon=True).start()

    def _run(self, ctl: JobControl) -> None:
        t0 = time.monotonic()
        result: dict | None = None
        error: str | None = None
        retryable = False
        try:
            result = self._executor.execute(ctl) or {}
            status = "done"
        except AppError as exc:
            if exc.status == 499 or ctl.cancelled():
                status = "canceled"
            else:
                status, error = "failed", advise(exc.message)
                retryable = isinstance(exc, HttpError) and exc.code in RETRYABLE
                log.warning("Job %s (%s) failed: %s", ctl.id, ctl.kind, exc.message)
        except Exception as exc:  # noqa: BLE001 - a runner bug must fail the job, not the queue
            log.exception("Job %s (%s) crashed", ctl.id, ctl.kind)
            status, error = "failed", f"內部錯誤：{type(exc).__name__}（詳細內容已寫入記錄檔）"
        with self._lock:
            job = self._jobs.get(ctl.id)
            self._controls.pop(ctl.id, None)
            self._previews.pop(ctl.id, None)
            if job is None:
                return
            tries = int(job.get("autoRetries") or 0)
            if retryable and tries < self._retry_limit():
                # Back in line with its remote task kept, so a cloud job resumes polling instead of paying again.
                wait = RETRY_DELAY * 2 ** tries
                job.update(status="queued", started=None, autoRetries=tries + 1, notBefore=now() + wait,
                           message=f"{error}；{wait} 秒後自動重試（第 {tries + 1} 次）"[:300])
                snapshot = None
            else:
                message = {"done": (result or {}).get("message") or "完成",
                           "canceled": "已取消"}.get(status, error or "失敗")
                job.update(status=status, finished=now(), error=error, result=result, message=str(message)[:300],
                           progress=1.0 if status == "done" else job["progress"],
                           remote=job.get("remote") if status == "failed" else None)
                job["seconds"] = round(time.monotonic() - t0, 1)
                snapshot = self._public(job)
        if snapshot is None:
            log.info("Job %s (%s) will retry automatically after a network error", ctl.id, ctl.kind)
            self._emit(ctl.id, save=True)
            return
        log.info("Job %s (%s) %s in %.1fs", ctl.id, ctl.kind, status, snapshot["seconds"])
        self._emit(ctl.id, save=True)
        self._wake.set()
        self._notify(snapshot)

    def _notify(self, job: dict) -> None:
        active = self.active_count()
        for listener in list(self._listeners):
            try:
                listener(job, active)
            except Exception:  # noqa: BLE001 - a notifier problem must not affect the job
                log.exception("Job listener failed")

    # ---------------------------------------------------------------- publishing / persistence
    def _emit(self, jid: str, save: bool = False) -> None:
        with self._lock:
            job = self._jobs.get(jid)
            self._dirty.discard(jid)
            payload = self._public(job) if job else None
        if payload:
            self._bus.publish("job", payload)
        self._bus.publish("queue", self.state())
        if save:
            self._save_due = True

    def _publish_all(self) -> None:
        self._bus.publish("jobs", self.list())
        self._bus.publish("queue", self.state())
        self._save_due = True

    def _tick_loop(self) -> None:
        last_save = 0.0
        while not self._stop.wait(PUBLISH_INTERVAL):
            with self._lock:
                dirty = [self._public(self._jobs[jid]) for jid in self._dirty if jid in self._jobs]
                self._dirty.clear()
            for job in dirty:
                self._bus.publish("job", job)
            if dirty and any(j["status"] == "running" for j in dirty):
                self._save_due = self._save_due or time.monotonic() - last_save > 10
            if self._save_due and time.monotonic() - last_save >= SAVE_INTERVAL:
                self._save()
                last_save = time.monotonic()

    def _save(self) -> None:
        with self._lock:
            self._save_due = False
            active = [j for j in self._jobs.values() if j["status"] in ACTIVE]
            done = sorted((j for j in self._jobs.values() if j["status"] not in ACTIVE),
                          key=lambda j: j.get("finished") or j.get("created") or 0)
            dropped = done[: max(0, len(done) - KEEP_FINISHED)]
            for job in dropped:
                self._jobs.pop(job["id"], None)
            data = {"version": 1, "paused": self._paused, "resumeAt": self._resume_at, "pauseAt": self._pause_at,
                    "jobs": copy.deepcopy(active + done[len(dropped):])}
        with self._save_lock:
            try:
                write_json_atomic(self._paths.jobs_file, data)
            except OSError as exc:
                log.error("Could not save jobs.json: %s", exc)


def public_job(job: dict) -> dict[str, Any]:
    return JobManager._public(job)
