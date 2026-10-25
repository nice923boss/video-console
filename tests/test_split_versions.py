"""Split halves with versions of their own (ED-18): the tail of a cut gets copies of the shot's takes (same
files), so either half can switch or regenerate without changing the other."""
from __future__ import annotations

from test_edits import A, run
from test_timeline_keys import VID, make_project, with_video
from vc.ops import apply_ops

REV = "t_rev00000a"


def versions(p: dict, shot_id: str) -> list[dict]:
    return [t for t in p["takes"].values() if t.get("shot") == shot_id]


def test_tail_gets_its_own_copy_of_each_version() -> None:
    p = with_video(make_project())
    p["takes"][REV] = {**p["takes"][VID], "id": REV, "parent": VID, "source": {"tool": "reverse"}}
    plan, back = run(p, {"cmd": "split", "t": 2.5})
    head, tail = p["shots"][0], p["shots"][1]
    assert head["activeVideo"] == VID and {t["id"] for t in versions(p, A)} == {VID, REV}
    assert len(versions(p, plan["id"])) == 2
    video = p["takes"][tail["activeVideo"]]
    assert tail["id"] == plan["id"] and video["id"] != VID and video["shot"] == tail["id"]
    assert video["file"] == p["takes"][VID]["file"]
    [rev] = [t for t in versions(p, tail["id"]) if t.get("source", {}).get("tool") == "reverse"]
    assert rev["parent"] == video["id"]  # the reversed pair stays a pair
    apply_ops(p, [{"op": "shot.update", "id": tail["id"], "patch": {"activeVideo": rev["id"]}}], {})
    assert p["shots"][0]["activeVideo"] == VID  # switching one half leaves the other alone
    apply_ops(p, back, {})
    assert set(p["takes"]) == {VID, REV} and len(p["shots"]) == 2


def test_halves_of_a_detached_shot_restore_together() -> None:
    p = with_video(make_project())
    plan, _ = run(p, {"cmd": "detach", "id": A})
    audio_id = plan["id"]
    run(p, {"cmd": "split", "t": 2.5})
    assert p["shots"][1]["audioDetached"] is True and p["shots"][1]["activeVideo"] != VID
    run(p, {"cmd": "restore", "id": audio_id})
    assert p["audio"] == [] and not any(s.get("audioDetached") for s in p["shots"])


def test_split_all_and_freeze_copy_versions_too() -> None:
    p = with_video(make_project())
    plan, _ = run(p, {"cmd": "splitAll", "t": 2})
    tail = p["shots"][1]
    assert tail["activeVideo"] != VID and p["takes"][tail["activeVideo"]]["shot"] == tail["id"]
    plan, _ = run(p, {"cmd": "freeze", "t": 1})
    kinds = [o["op"] for o in plan["ops"]]
    assert kinds[:2] == ["shot.update", "take.add"] and kinds[-2:] == ["shot.add", "shot.add"]
