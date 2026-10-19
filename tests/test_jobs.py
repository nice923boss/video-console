"""Job queue scheduling on one lane: first come first served, "move to top", and voice-over first (fake executor)."""
from __future__ import annotations

import threading

from vc.events import EventBus
from vc.jobs import JobControl, JobManager
from vc.paths import Paths


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
