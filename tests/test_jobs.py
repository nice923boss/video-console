"""Job queue scheduling on one lane: first come first served, "move to top", and voice-over first (fake executor)."""
from __future__ import annotations

import threading
import time

import pytest

from vc.events import EventBus
from vc.jobs import JobControl, JobManager
from vc.net import HttpError
from vc.paths import Paths
from vc.util import AppError, now


class RecordingExecutor:
    """Every job runs on the "comfy" lane and only records the order the jobs started in."""

    def __init__(self, expected: int) -> None:
        self.ran: list[str] = []
        self.expected = expected
        self.finished = threading.Event()

    def prepare(self, kind: str, pid: str, shot_id: str | None, payload: dict) -> dict:
        return {"title": payload["name"], "lane": "comfy", "payload": payload}

    def execute(self, ctl: JobControl) -> dict:
        self.ran.append(ctl.payload["name"])
        if len(self.ran) == self.expected:
            self.finished.set()
        return {}

    def admit(self, prepared: dict) -> None:
        pass

    def after_restart(self, job: dict) -> str:
        return "interrupt"


def test_voice_lines_jump_ahead_of_renders_waiting_on_the_same_lane(tmp_path) -> None:
    paths = Paths(tmp_path)
    paths.ensure()
    executor = RecordingExecutor(expected=5)
    jobs = JobManager(paths, EventBus(), executor)
    jobs.pause(True)
    jobs.start()
    try:
        ids = {name: jobs.submit(kind, "p_test000001", None, {"name": name})["id"]
               for kind, name in (("image", "img1"), ("video", "vid1"), ("voice", "line1"),
                                  ("image", "img2"), ("voice", "line2"))}
        jobs.move_top(ids["img2"])  # a manual "move to top" still orders the renders among themselves
        jobs.pause(False)
        assert executor.finished.wait(5), executor.ran
    finally:
        jobs.stop()
    assert executor.ran == ["line1", "line2", "img2", "img1", "vid1"]


def test_night_run_starts_the_queue_at_its_time_and_pauses_it_again(tmp_path) -> None:
    paths = Paths(tmp_path)
    paths.ensure()
    executor = RecordingExecutor(expected=1)
    jobs = JobManager(paths, EventBus(), executor)
    jobs.start()
    try:
        with pytest.raises(AppError):
            jobs.schedule(now() - 1, None)
        with pytest.raises(AppError):
            jobs.schedule(now() + 5, now() + 4)
        state = jobs.schedule(now() + 0.3, now() + 1.6)
        assert state["paused"] is True and state["resumeAt"] and state["pauseAt"]
        jobs.submit("image", "p_test000001", None, {"name": "night"})
        assert executor.ran == []
        assert executor.finished.wait(3), "the queue did not start at its time"
        assert jobs.state()["paused"] is False and jobs.state()["resumeAt"] is None
        deadline = time.monotonic() + 4
        while not jobs.state()["paused"] and time.monotonic() < deadline:
            time.sleep(0.1)
        assert jobs.state() | {"counts": None} == {"paused": True, "resumeAt": None, "pauseAt": None, "counts": None,
                                                     "active": 0}
        jobs.schedule(now() + 60, None)
        assert jobs.pause(False)["resumeAt"] is None  # resuming by hand drops the schedule
    finally:
        jobs.stop()
    assert JobManager(paths, EventBus(), executor).state()["paused"] is False


def test_dragging_a_waiting_job_puts_it_before_another_or_last(tmp_path) -> None:
    paths = Paths(tmp_path)
    paths.ensure()
    executor = RecordingExecutor(expected=4)
    jobs = JobManager(paths, EventBus(), executor)
    jobs.pause(True)
    jobs.start()
    try:
        ids = {name: jobs.submit("image", "p_test000001", None, {"name": name})["id"] for name in ("a", "b", "c", "d")}
        jobs.move(ids["d"], ids["b"])  # a, d, b, c
        jobs.move(ids["a"], None)  # d, b, c, a
        jobs.move(ids["c"], ids["d"])  # c, d, b, a
        with pytest.raises(AppError):
            jobs.move(ids["a"], "j_nothere000")
        jobs.pause(False)
        assert executor.finished.wait(5), executor.ran
        with pytest.raises(AppError):
            jobs.move(ids["a"], None)  # finished jobs keep their place
    finally:
        jobs.stop()
    assert executor.ran == ["c", "d", "b", "a"]


def test_retry_keeps_the_seed_unless_a_new_one_is_asked_for(tmp_path) -> None:
    paths = Paths(tmp_path)
    paths.ensure()
    jobs = JobManager(paths, EventBus(), RecordingExecutor(expected=99))
    jobs.pause(True)
    seeded = jobs.submit("image", "p_test000001", None, {"name": "img", "seed": 5, "seedLocked": True})["id"]
    plain = jobs.submit("export", "p_test000001", None, {"name": "out"})["id"]
    for jid in (seeded, plain):
        jobs.cancel(jid)

    again = jobs.retry(seeded)
    assert (again["status"], again["attempt"], again["payload"]["seed"]) == ("queued", 2, 5)
    jobs.cancel(seeded)
    fresh = jobs.retry(seeded, new_seed=True)
    assert fresh["payload"]["seed"] != 5 and fresh["payload"]["seedLocked"] is True
    with pytest.raises(AppError, match="沒有種子"):
        jobs.retry(plain, new_seed=True)
    assert jobs.get(plain)["status"] == "canceled"


class FlakyExecutor(RecordingExecutor):
    """Fails each job's first `failures` runs with `error`, after recording a remote task on the first one."""

    def __init__(self, failures: int, error: AppError) -> None:
        super().__init__(expected=99)
        self.failures = failures
        self.error = error
        self.resumed: list[bool] = []

    def execute(self, ctl: JobControl) -> dict:
        self.resumed.append(ctl.resuming)
        if len(self.resumed) == 1:
            ctl.remote({"taskId": "t1"})
        if len(self.resumed) <= self.failures:
            raise self.error
        self.finished.set()
        return {"message": "好了"}


def run_flaky(tmp_path, monkeypatch, executor: FlakyExecutor, limit: int) -> tuple[dict, list[str]]:
    monkeypatch.setattr("vc.jobs.RETRY_DELAY", 0)
    paths = Paths(tmp_path)
    paths.ensure()
    jobs = JobManager(paths, EventBus(), executor, retry_limit=lambda: limit)
    finals: list[str] = []
    done = threading.Event()
    jobs.add_listener(lambda job, active: (finals.append(job["status"]), done.set()))
    jobs.start()
    try:
        jid = jobs.submit("image", "p_test000001", None, {"name": "img"})["id"]
        assert done.wait(10)
    finally:
        jobs.stop()
    return jobs.get(jid), finals


def test_network_errors_are_retried_and_resume_the_remote_task(tmp_path, monkeypatch) -> None:
    executor = FlakyExecutor(2, HttpError("連線逾時（api.example）", 0))

    job, finals = run_flaky(tmp_path, monkeypatch, executor, limit=2)

    assert (job["status"], job["message"], job["autoRetries"]) == ("done", "好了", 2)
    assert executor.resumed == [False, True, True]  # no second submission after the first one got through
    assert finals == ["done"]  # listeners (toasts, ntfy) only hear the final outcome


def test_retries_stop_at_the_limit(tmp_path, monkeypatch) -> None:
    executor = FlakyExecutor(5, HttpError("伺服器暫時無法使用", 503))

    job, finals = run_flaky(tmp_path, monkeypatch, executor, limit=1)

    assert (job["status"], job["autoRetries"]) == ("failed", 1)
    assert len(executor.resumed) == 2 and finals == ["failed"]


@pytest.mark.parametrize("error", [AppError("提示詞被拒"), HttpError("請求格式錯誤", 400)])
def test_other_errors_fail_at_once(tmp_path, monkeypatch, error) -> None:
    executor = FlakyExecutor(1, error)

    job, finals = run_flaky(tmp_path, monkeypatch, executor, limit=3)

    assert job["status"] == "failed" and len(executor.resumed) == 1 and finals == ["failed"]
