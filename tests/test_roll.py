"""ED-08 roll, slip and slide (vc/edits.py, lib/roll.js): a roll moves the cut between two shots, a slip shows
other footage in the same slot, a slide moves a shot between its neighbours; none changes the total length."""
from __future__ import annotations

import shutil

import pytest

from test_server import Client, app, client, project  # noqa: F401  (server fixtures)
from test_timeline_keys import JS, VID, make_project, run_js, with_video
from vc import edits
from vc.ops import apply_ops
from vc.opfields import blank_shot
from vc.sequence import build_sequence, total_duration
from vc.util import AppError

A, B, C = "s_aaaaaaaa", "s_bbbbbbbb", "s_cccccccc"


def setup(trims: dict[str, tuple[float, float | None]], middle: bool = False) -> dict:
    """Shots A (B) on a 10 s video, with C between them when `middle`; trims maps a shot to (in, out)."""
    p = with_video(make_project(), 10.0)
    if middle:
        shot = {**blank_shot(C, {}), "duration": 4.0, "activeVideo": VID}
        p["shots"].insert(1, shot)
    for shot in p["shots"]:
        shot["trimIn"], shot["trimOut"] = trims.get(shot["id"], (0.0, 6.0))
    return p


def run(p: dict, data: dict) -> dict:
    """Plan a command and apply it the way POST /edit does (linked unless the plan says not)."""
    plan = edits.plan(p, data)
    apply_ops(p, [{"op": "linked", "ops": plan["ops"]}] if plan.get("link", True) else plan["ops"], {})
    return plan


def spans(p: dict) -> list[tuple[str, float, float, float]]:
    return [(c["shot"]["id"], round(c["start"], 3), round(c["in"], 3), round(c["out"], 3)) for c in build_sequence(p)]


def caption(p: dict, cid: str, start: float) -> dict:
    apply_ops(p, [{"op": "caption.add", "item": {"id": cid, "start": start, "end": start + 0.5, "text": "字"}}], {})
    return p


def cap_start(p: dict, cid: str) -> float:
    return next(c["start"] for c in p["captions"] if c["id"] == cid)


def test_roll_moves_the_cut_and_keeps_the_rest_in_place() -> None:
    p = caption(caption(setup({B: (2.0, 8.0)}), "c_onb00000", 9.0), "c_ona00000", 1.0)
    plan = run(p, {"cmd": "roll", "id": A, "by": 1.0})
    assert plan["label"] == "滾動剪接點" and plan["by"] == 1.0
    assert spans(p) == [(A, 0.0, 0.0, 7.0), (B, 7.0, 3.0, 8.0)]
    assert total_duration(build_sequence(p)) == pytest.approx(12.0)
    assert (cap_start(p, "c_ona00000"), cap_start(p, "c_onb00000")) == (1.0, 9.0)  # both stay over their footage


def test_roll_stops_where_the_footage_or_the_clip_runs_out() -> None:
    p = setup({B: (2.0, 8.0)})
    assert run(p, {"cmd": "roll", "id": A, "by": -5.0})["by"] == -2.0  # B has only 2 s before its in point
    assert spans(p) == [(A, 0.0, 0.0, 4.0), (B, 4.0, 0.0, 8.0)]
    assert run(p, {"cmd": "roll", "id": A, "by": 9.0})["by"] == 6.0  # A reaches the end of its 10 s video
    assert spans(p) == [(A, 0.0, 0.0, 10.0), (B, 10.0, 6.0, 8.0)]
    assert p["shots"][0]["trimOut"] is None  # to the end again
    with pytest.raises(AppError, match="到底"):
        edits.plan(p, {"cmd": "roll", "id": A, "by": 1.0})
    with pytest.raises(AppError, match="最後一個鏡頭"):
        edits.plan(p, {"cmd": "roll", "id": B, "by": 1.0})


def test_roll_works_on_stills_and_sped_clips() -> None:
    p = setup({B: (2.0, 10.0)})
    del p["shots"][0]["activeVideo"]  # A is an empty 4 s shot
    p["shots"][1]["speed"] = 2.0  # B plays 2..10 of its video in 4 s
    assert run(p, {"cmd": "roll", "id": A, "by": 1.0})["by"] == 1.0
    assert spans(p) == [(A, 0.0, 0.0, 5.0), (B, 5.0, 4.0, 10.0)]  # B starts 2 s of footage later
    assert p["shots"][0]["duration"] == 5.0
    assert total_duration(build_sequence(p)) == pytest.approx(8.0)
    assert run(p, {"cmd": "roll", "id": A, "by": -9.0})["by"] == -2.0  # B is back at its first frame
    p = setup({B: (8.0, 10.0)})
    del p["shots"][0]["activeVideo"]
    assert run(p, {"cmd": "roll", "id": A, "by": -9.0})["by"] == -3.8  # A down to its shortest 0.2 s
    assert spans(p)[0] == (A, 0.0, 0.0, 0.2)


def test_slip_shows_other_footage_without_moving_anything() -> None:
    p = caption(setup({A: (2.0, 8.0)}), "c_ona00000", 1.0)
    plan = run(p, {"cmd": "slip", "id": A, "by": 1.0})
    assert plan["label"] == "滑動內容" and plan["link"] is False
    assert spans(p) == [(A, 0.0, 3.0, 9.0), (B, 6.0, 0.0, 6.0)]
    assert cap_start(p, "c_ona00000") == 1.0  # the caption stays on the timeline, not on the footage
    assert run(p, {"cmd": "slip", "id": A, "by": 5.0})["by"] == 1.0
    assert spans(p)[0] == (A, 0.0, 4.0, 10.0) and p["shots"][0]["trimOut"] is None
    assert run(p, {"cmd": "slip", "id": A, "by": -9.0})["by"] == -4.0
    assert spans(p)[0] == (A, 0.0, 0.0, 6.0)
    del p["shots"][1]["activeVideo"]
    with pytest.raises(AppError, match="影片鏡頭"):
        edits.plan(p, {"cmd": "slip", "id": B, "by": 1.0})


def test_slide_moves_a_shot_between_its_neighbours() -> None:
    p = setup({A: (0.0, 4.0), C: (1.0, 3.0), B: (2.0, 8.0)}, middle=True)
    caption(caption(caption(p, "c_ona00000", 1.0), "c_onc00000", 4.5), "c_onb00000", 9.0)
    plan = run(p, {"cmd": "slide", "id": C, "by": 1.0})
    assert plan["label"] == "滑移鏡頭" and plan["by"] == 1.0
    assert spans(p) == [(A, 0.0, 0.0, 5.0), (C, 5.0, 1.0, 3.0), (B, 7.0, 3.0, 8.0)]
    assert total_duration(build_sequence(p)) == pytest.approx(12.0)
    # The caption on C goes with it; the ones on A and B stay over their footage.
    assert [cap_start(p, c) for c in ("c_ona00000", "c_onc00000", "c_onb00000")] == [1.0, 5.5, 9.0]
    assert run(p, {"cmd": "slide", "id": C, "by": -9.0})["by"] == -3.0  # B is back at its first frame
    with pytest.raises(AppError, match="前後都有鏡頭"):
        edits.plan(p, {"cmd": "slide", "id": A, "by": 1.0})


def test_commands_need_a_shot_id_and_a_number() -> None:
    p = setup({})
    for data, text in (({"cmd": "roll", "by": 1}, "要給鏡頭 id"), ({"cmd": "slip", "id": A}, "by 要是數字"),
                       ({"cmd": "slide", "id": "s_gone0000", "by": 1}, "找不到")):
        with pytest.raises(AppError, match=text):
            edits.plan(p, data)


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_preview_ranges_match_the_server() -> None:
    p = setup({A: (2.0, 7.0), C: (0.0, 3.0), B: (1.0, 8.0)}, middle=True)
    p["shots"][1]["speed"] = 2.0
    del p["shots"][2]["activeVideo"]
    clips = build_sequence(p)
    got = run_js(JS / "lib" / "roll.js", """const clips = data.clips;
      console.log(JSON.stringify([m.cutRange(clips[0], clips[1]), m.cutRange(clips[1], clips[2]),
                                  m.slipRange(clips[0]), m.slipRange(clips[1])].flat()));""", {"clips": clips})
    want = [*edits.cut_range(clips[0], clips[1]), *edits.cut_range(clips[1], clips[2]),
            *edits.slip_range(clips[0]), *edits.slip_range(clips[1])]
    assert got == pytest.approx(want)


def test_edit_endpoint_slips_without_moving_items_and_returns_the_amount(client: Client, project: dict) -> None:  # noqa: F811
    pid = project["id"]
    shots = client.json("GET", f"/api/projects/{pid}")["project"]["shots"]
    client.json("POST", f"/api/projects/{pid}/ops", {"ops": [
        {"op": "shot.add", "shot": {"id": "s_second00a", "duration": 4.0}, "index": len(shots)},
        {"op": "shot.add", "shot": {"id": "s_third000a", "duration": 4.0}, "index": len(shots) + 1}]})
    tl = client.json("GET", f"/api/projects/{pid}/timeline")
    second = next(c for c in tl["clips"] if c["shotId"] == "s_second00a")
    out = client.json("POST", f"/api/projects/{pid}/edit", {"cmd": "roll", "id": "s_second00a", "by": 1.5})
    assert out["changed"] and out["by"] == 1.5 and out["label"] == "滾動剪接點"
    after = client.json("GET", f"/api/projects/{pid}/timeline")
    assert after["duration"] == pytest.approx(tl["duration"])
    assert next(c for c in after["clips"] if c["shotId"] == "s_second00a")["length"] == pytest.approx(second["length"] + 1.5)
    err = client.json("POST", f"/api/projects/{pid}/edit", {"cmd": "slip", "id": "s_second00a", "by": 1}, status=400)
    assert "影片鏡頭" in err["error"]
