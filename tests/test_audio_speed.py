"""The detached sound of a sped shot (ED-20): the audio clip keeps the shot's speed, so it stays as long as the
picture, cuts at the matching source time and exports through atempo."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from test_edits import A, run
from test_timeline_keys import JS, make_project, plan, run_js, with_video
from vc.edits import audio_length


def sped() -> dict:
    p = with_video(make_project())
    p["shots"][0].update(speed=2.0, trimOut=4.0)  # 4 s of source in 2 s
    return p


def test_detach_keeps_the_speed() -> None:
    p = sped()
    got, _ = run(p, {"cmd": "detach", "id": A})
    item = p["audio"][0]
    assert item["id"] == got["id"] and item["speed"] == 2 and (item["in"], item["out"]) == (0, 4)
    assert audio_length(p, item) == pytest.approx(2.0)  # as long as the picture
    assert p["shots"][0]["audioDetached"] is True


def test_split_and_trim_cut_at_the_source_time() -> None:
    p = sped()
    got, _ = run(p, {"cmd": "detach", "id": A})
    run(p, {"cmd": "split", "t": 0.5, "type": "audio", "id": got["id"]})
    head, tail = p["audio"]
    assert head["out"] == 1.0 and tail["in"] == 1.0 and tail["start"] == 0.5 and tail["speed"] == 2
    run(p, {"cmd": "trim", "t": 1.5, "side": "end", "type": "audio", "id": tail["id"]})
    assert p["audio"][1]["out"] == 3.0


def test_export_reads_the_source_span_and_speeds_it_up(tmp_path: Path) -> None:
    p = sped()
    run(p, {"cmd": "detach", "id": A})
    p["audio"][0]["out"] = 3.0  # 3 s of source: 1.5 s on the timeline
    g = plan(p, tmp_path)["graph"]
    assert "atrim=start=0.000:duration=3.000,asetpts=PTS-STARTPTS,atempo=2.0000,volume=" in g


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_timeline_length_divides_by_speed() -> None:
    body = "console.log(JSON.stringify([m.audioLength(data.a, data.m), m.audioLength({ ...data.a, speed: 0.5 }, data.m)]));"
    got = run_js(JS / "lib" / "sequence.js", body, {"a": {"in": 1, "out": 5, "speed": 2}, "m": {"duration": 6}})
    assert got == [2, 8]
