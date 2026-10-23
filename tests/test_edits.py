"""Named timeline edits for scripts and Claude (vc/edits.py, GET /timeline and POST /edit): each command
makes the same ops as the edit page's key and applies as one undo step."""
from __future__ import annotations

import copy

import pytest

from test_server import Client, app, client, project  # noqa: F401  (server fixtures)
from test_timeline_keys import VID, make_project, with_video
from vc import edits
from vc.ops import apply_ops
from vc.sequence import build_sequence
from vc.util import AppError

A, B = "s_aaaaaaaa", "s_bbbbbbbb"


def run(p: dict, data: dict) -> tuple[dict, list[dict]]:
    """Plan a command and apply its ops; returns the plan and the undo ops."""
    plan = edits.plan(p, data)
    return plan, apply_ops(p, plan["ops"], {}) if plan["ops"] else []


def spans(p: dict) -> list[tuple[str, float, float]]:
    return [(c["shot"]["id"], c["in"], c["out"]) for c in build_sequence(p)]


def with_audio(p: dict, **extra: object) -> dict:
    apply_ops(p, [{"op": "audio.add", "item": {"id": "a_aaaaaaaa", "takeId": VID, "start": 0, "in": 0, "out": 6, **extra}}], {})
    return p


def with_picture(p: dict) -> dict:
    p["media"]["m_pic00000"] = {"id": "m_pic00000", "kind": "image", "file": "media/import/a.png"}
    p["layers"] = [{"id": "l_top00000", "name": "上方", "hidden": False, "muted": False}]
    apply_ops(p, [{"op": "overlay.add", "item": {"id": "o_aaaaaaaa", "mediaId": "m_pic00000", "layer": "l_top00000",
                                                  "start": 1, "length": 4}}], {})
    return p


# ---------------------------------------------------------------- reading
def test_timeline_lists_clips_items_and_markers() -> None:
    p = with_audio(with_video(make_project()))
    p["markers"] = [{"id": "mk_b000000", "start": 5.0, "color": "red", "name": ""},
                    {"id": "mk_a000000", "start": 1.0, "color": "blue", "name": ""}]
    tl = edits.timeline(p)
    assert tl["duration"] == 12.0
    assert [(c["shotId"], c["index"], c["start"], c["length"], c["source"], c["takeId"]) for c in tl["clips"]] == [
        (A, 0, 0.0, 6.0, "video", VID), (B, 1, 6.0, 6.0, "video", VID)]
    assert tl["items"] == [{"type": "audio", "id": "a_aaaaaaaa", "start": 0.0, "length": 6.0, "name": "", "keyframes": 0}]
    assert [m["id"] for m in tl["markers"]] == ["mk_a000000", "mk_b000000"]


# ---------------------------------------------------------------- split and trim
def test_split_cuts_the_shot_at_the_time_and_undoes() -> None:
    p = with_video(make_project())
    plan, back = run(p, {"cmd": "split", "t": 2.5})
    assert plan["label"] == "分割鏡頭"
    assert spans(p) == [(A, 0.0, 2.5), (plan["id"], 2.5, 6.0), (B, 0.0, 6.0)]
    assert edits.timeline(p)["duration"] == 12.0  # same picture, one more cut
    apply_ops(p, back, {})
    assert spans(p) == [(A, 0.0, 6.0), (B, 0.0, 6.0)]
    with pytest.raises(AppError, match="邊緣"):
        edits.plan(p, {"cmd": "split", "t": 6.05})  # 0.05 s into the second shot


def test_split_other_tracks_keeps_keyframes_in_place() -> None:
    p = with_picture(with_video(make_project()))
    apply_ops(p, [{"op": "overlay.update", "id": "o_aaaaaaaa", "patch": {"keyframes": [
        {"t": 0, "opacity": 0.0}, {"t": 4, "opacity": 1.0}]}}], {})
    plan, _ = run(p, {"cmd": "split", "t": 3, "type": "overlay", "id": "o_aaaaaaaa"})
    head, tail = p["overlays"]
    assert (head["start"], head["length"], tail["id"], tail["start"], tail["length"]) == (1, 2.0, plan["id"], 3.0, 2.0)
    assert head["keyframes"] == [{"t": 0.0, "opacity": 0.0}, {"t": 2.0, "opacity": 0.5}]
    assert tail["keyframes"] == [{"t": 0.0, "opacity": 0.5}, {"t": 2.0, "opacity": 1.0}]
    p["captions"] = [{"id": "c_aaaaaaaa", "start": 1.0, "end": 3.0, "text": "嗨", "shot": A}]
    run(p, {"cmd": "split", "t": 2, "type": "caption"})  # no id: the caption under t
    assert [(c["start"], c["end"], c.get("shot")) for c in p["captions"]] == [(1.0, 2.0, A), (2.0, 3.0, None)]
    with pytest.raises(AppError, match="type"):
        edits.plan(p, {"cmd": "split", "t": 2, "type": "nope"})


def test_split_all_cuts_every_track_under_the_time() -> None:
    p = with_audio(with_video(make_project()))
    p["captions"] = [{"id": "c_aaaaaaaa", "start": 1.0, "end": 3.0, "text": "嗨"}]
    p["adjustments"] = [{"id": "adj_aaaaaaa", "start": 8.0, "length": 2.0}]
    plan, _ = run(p, {"cmd": "splitAll", "t": 2})
    assert plan["label"] == "分割所有軌道"
    assert len(p["shots"]) == 3 and len(p["audio"]) == 2 and len(p["captions"]) == 2
    assert len(p["adjustments"]) == 1  # not under t
    assert [(a["start"], a["in"], a.get("out")) for a in p["audio"]] == [(0, 0, 2.0), (2.0, 2.0, 6)]
    with pytest.raises(AppError, match="沒有可以分割"):
        edits.plan(p, {"cmd": "splitAll", "t": 11.95})


def test_trim_removes_before_or_after_the_time() -> None:
    p = with_audio(with_video(make_project()), keyframes=[{"t": 0, "volume": 0.0}, {"t": 4, "volume": 1.0}])
    run(p, {"cmd": "trim", "t": 2, "side": "start"})
    assert spans(p) == [(A, 2.0, 6.0), (B, 0.0, 6.0)]  # the next shot closes up
    run(p, {"cmd": "trim", "t": 1, "side": "start", "type": "audio", "id": "a_aaaaaaaa"})
    [a] = p["audio"]
    assert (a["start"], a["in"]) == (1.0, 1.0)
    assert a["keyframes"] == [{"t": 0.0, "volume": 0.25}, {"t": 3.0, "volume": 1.0}]
    run(p, {"cmd": "trim", "t": 4, "side": "end", "type": "audio"})
    assert a["out"] == 4.0
    with pytest.raises(AppError, match="side"):
        edits.plan(p, {"cmd": "trim", "t": 2, "side": "left"})


# ---------------------------------------------------------------- picture and sound
def test_freeze_puts_a_still_between_the_halves_and_queues_the_frame() -> None:
    p = with_video(make_project())
    plan, _ = run(p, {"cmd": "freeze", "t": 3})
    sid = plan["id"]
    assert [(c["shot"]["id"], c["source"], c["length"]) for c in build_sequence(p)] == [
        (A, "video", 3.0), (sid, "empty", 2.0), (p["shots"][2]["id"], "video", 3.0), (B, "video", 6.0)]
    assert plan["job"] == {"kind": "lastframe", "shotId": A,
                           "payload": {"source": VID, "target": "shot", "targetShot": sid, "at": 3.0}}
    plan, _ = run(p, {"cmd": "freeze", "t": 0.05})  # at the head: the still goes in front, nothing is cut
    assert [o["op"] for o in plan["ops"]] == ["shot.add"] and p["shots"][0]["id"] == plan["id"]
    p["shots"][1]["audioDetached"] = True
    with pytest.raises(AppError, match="還原原聲"):
        edits.plan(p, {"cmd": "freeze", "t": 3})


def test_detach_and_restore_sound() -> None:
    p = with_video(make_project())
    plan, _ = run(p, {"cmd": "detach", "id": A})
    [a] = p["audio"]
    assert a["id"] == plan["id"] and (a["takeId"], a["shotId"], a["start"], a["in"], a["out"]) == (VID, A, 0.0, 0.0, 6.0)
    assert p["shots"][0]["audioDetached"] is True
    with pytest.raises(AppError, match="已經分離"):
        edits.plan(p, {"cmd": "detach", "t": 1})
    run(p, {"cmd": "restore", "id": a["id"]})
    assert p["audio"] == [] and p["shots"][0]["audioDetached"] is False
    p["shots"][1]["speed"] = 2.0
    with pytest.raises(AppError, match="變速"):
        edits.plan(p, {"cmd": "detach", "id": B})
    p["takes"][VID]["hasAudio"] = False
    with pytest.raises(AppError, match="沒有聲音"):
        edits.plan(p, {"cmd": "detach", "id": A})
    with pytest.raises(AppError, match="不是已分離"):
        edits.plan(p, {"cmd": "restore", "id": A})


def test_reverse_queues_once_then_switches_and_mirrors_trims() -> None:
    p = with_video(make_project())
    plan = edits.plan(p, {"cmd": "reverse", "id": A})
    assert plan["ops"] == [] and plan["job"] == {"kind": "reverse", "shotId": A, "payload": {"source": VID}}
    p["takes"]["t_rev00000"] = {"id": "t_rev00000", "kind": "video", "file": "media/takes/t_rev00000.mp4", "duration": 6.0,
                                "hasAudio": True, "parent": VID, "source": {"tool": "reverse"}, "shot": A}
    apply_ops(p, [{"op": "shot.update", "id": A, "patch": {"trimIn": 1.0, "trimOut": 4.0}}], {})
    plan, _ = run(p, {"cmd": "reverse", "t": 1})
    shot = p["shots"][0]
    assert plan["label"] == "倒放" and (shot["activeVideo"], shot["trimIn"], shot["trimOut"]) == ("t_rev00000", 2.0, 5.0)
    plan, _ = run(p, {"cmd": "reverse", "id": A})
    assert plan["label"] == "取消倒放" and (shot["activeVideo"], shot["trimIn"], shot["trimOut"]) == (VID, 1.0, 4.0)
    shot["audioDetached"] = True
    with pytest.raises(AppError, match="還原原聲"):
        edits.plan(p, {"cmd": "reverse", "id": A})


def test_volume_steps_in_db_and_ranges_use_keyframes() -> None:
    p = with_audio(with_video(make_project()))
    run(p, {"cmd": "volume", "type": "audio", "id": "a_aaaaaaaa", "db": 6})
    assert p["audio"][0]["volume"] == 1.9953
    p = with_audio(with_video(make_project()))
    plan, _ = run(p, {"cmd": "volume", "type": "audio", "id": "a_aaaaaaaa", "db": -6, "in": 2, "out": 4})
    assert plan["label"] == "調低音量"
    assert p["audio"][0]["keyframes"] == [{"t": 1.9, "volume": 1.0}, {"t": 2.0, "volume": 0.5012},
                                          {"t": 4.0, "volume": 0.5012}, {"t": 4.1, "volume": 1.0}]
    run(p, {"cmd": "volume", "type": "audio", "id": "a_aaaaaaaa", "db": 6})  # whole clip: every key moves
    assert [k["volume"] for k in p["audio"][0]["keyframes"]] == [1.9953, 1.0, 1.0, 1.9953]
    with_picture(p)
    for data, text in (({"type": "shot", "id": A}, "先分離"), ({"type": "overlay", "id": "o_aaaaaaaa"}, "圖片"),
                       ({"type": "audio", "id": "a_aaaaaaaa", "in": 1}, "一起給")):
        with pytest.raises(AppError, match=text):
            edits.plan(p, {"cmd": "volume", "db": 1, **data})


def test_keyframe_records_the_current_values() -> None:
    p = with_picture(with_audio(with_video(make_project()), volume=0.5))
    run(p, {"cmd": "keyframe", "type": "audio", "id": "a_aaaaaaaa", "t": 1})
    run(p, {"cmd": "keyframe", "type": "audio", "id": "a_aaaaaaaa", "t": 3, "values": {"volume": 2}})
    assert p["audio"][0]["keyframes"] == [{"t": 1.0, "volume": 0.5}, {"t": 3.0, "volume": 2.0}]
    run(p, {"cmd": "keyframe", "type": "overlay", "id": "o_aaaaaaaa", "t": 2, "values": {"x": 0.5}})
    [key] = p["overlays"][0]["keyframes"]
    assert key["t"] == 1.0 and key["x"] == 0.5 and set(key) == {"t", "x", "y", "w", "opacity"}  # a still has no volume
    apply_ops(p, [{"op": "adjust.add", "item": {"id": "adj_aaaaaaa", "start": 0, "length": 6}}], {})
    run(p, {"cmd": "keyframe", "type": "adjust", "id": "adj_aaaaaaa", "t": 1, "values": {"grade": {"temp": 3000}}})
    assert p["adjustments"][0]["keyframes"][0]["grade"]["temp"] == 3000
    for data, text in (({"type": "caption", "id": "c_x"}, "不能加"), ({"type": "audio", "id": "a_aaaaaaaa", "t": 9}, "不在"),
                       ({"type": "audio", "id": "a_aaaaaaaa", "values": {"x": 1}}, "只能記")):
        with pytest.raises(AppError, match=text):
            edits.plan(p, {"cmd": "keyframe", "t": 1, **data})


def test_bad_commands_are_refused_in_chinese() -> None:
    p = with_video(make_project())
    for data, text in (({"cmd": "zoom"}, "不支援"), ({"cmd": "split"}, "t 要是數字"), ({"cmd": "split", "t": "1"}, "數字"),
                       ({"cmd": "restore"}, "要給 id"), ({"cmd": "split", "t": 99, "type": "shot", "id": "s_gone0000"}, "找不到")):
        with pytest.raises(AppError, match=text):
            edits.plan(p, data)
    assert edits.plan(copy.deepcopy(p), {"cmd": "split", "t": 1})["ops"]  # planning leaves the project alone
    assert spans(p) == [(A, 0.0, 6.0), (B, 0.0, 6.0)]


# ---------------------------------------------------------------- over HTTP
def test_edit_endpoint_is_one_undo_step_and_items_follow(client: Client, project: dict) -> None:  # noqa: F811
    pid = project["id"]
    first = client.json("GET", f"/api/projects/{pid}/timeline")["clips"][0]
    client.json("POST", f"/api/projects/{pid}/ops", {"ops": [
        {"op": "shot.add", "shot": {"id": "s_second00a", "duration": 4.0}, "index": 1},
        {"op": "caption.add", "item": {"id": "c_late00000", "start": first["length"] + 1, "end": first["length"] + 2,
                                       "text": "後面"}}]})
    out = client.json("POST", f"/api/projects/{pid}/edit", {"cmd": "trim", "t": 1, "side": "start"})
    assert out["changed"] and out["label"] == "向左裁剪"
    saved = client.json("GET", f"/api/projects/{pid}")["project"]
    assert saved["captions"][0]["start"] == pytest.approx(first["length"])  # moved up with the shot it sits on
    out = client.json("POST", f"/api/projects/{pid}/edit", {"cmd": "split", "t": 2})
    tl = client.json("GET", f"/api/projects/{pid}/timeline")
    assert [c["shotId"] for c in tl["clips"]][1] == out["id"] and len(tl["clips"]) == 3
    client.json("POST", f"/api/projects/{pid}/undo")
    assert len(client.json("GET", f"/api/projects/{pid}/timeline")["clips"]) == 2
    err = client.json("POST", f"/api/projects/{pid}/edit", {"cmd": "freeze", "t": 1}, status=400)
    assert "影片鏡頭" in err["error"]
