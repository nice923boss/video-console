"""Linked edits: items on the other tracks follow the shot they sit on."""
from __future__ import annotations

from vc.ops import apply_ops, blank_shot


def make_project() -> dict:
    # Three 4 s stills: A 0..4, B 4..8, C 8..12.
    shots = []
    for sid in ("s_aaaaaaaa", "s_bbbbbbbb", "s_cccccccc"):
        shot = blank_shot(sid, {})
        shot["duration"] = 4.0
        shots.append(shot)
    return {
        "shots": shots,
        "takes": {},
        "media": {"m_snd0000a": {"id": "m_snd0000a", "kind": "audio", "duration": 10.0},
                  "m_pic0000a": {"id": "m_pic0000a", "kind": "image"}},
        "layers": [{"id": "l_top0000a", "name": "", "hidden": False, "muted": False}],
        # One caption on B with its voice-over, a sound on B, a picture on C.
        "captions": [{"id": "c_onb0000a", "start": 5.0, "end": 6.5, "text": "hi"}],
        "audio": [{"id": "a_vo00000a", "mediaId": "m_snd0000a", "captionId": "c_onb0000a", "start": 5.0, "in": 0.0},
                  {"id": "a_sfx0000a", "mediaId": "m_snd0000a", "start": 4.5, "in": 0.0}],
        "overlays": [{"id": "o_onc0000a", "mediaId": "m_pic0000a", "layer": "l_top0000a", "start": 9.0,
                      "in": 0.0, "length": 1.0}],
    }


def starts(p: dict) -> dict[str, float]:
    return {it["id"]: it["start"] for key in ("captions", "audio", "overlays") for it in p[key]}


def linked(p: dict, *ops: dict) -> list[dict]:
    return apply_ops(p, [{"op": "linked", "ops": list(ops)}], {})


def test_moving_a_shot_carries_its_items() -> None:
    p = make_project()
    linked(p, {"op": "shot.move", "id": "s_bbbbbbbb", "index": 0})  # B 0..4, A 4..8, C 8..12
    s = starts(p)
    assert s["c_onb0000a"] == 1.0 and p["captions"][0]["end"] == 2.5
    assert s["a_vo00000a"] == 1.0 and s["a_sfx0000a"] == 0.5
    assert s["o_onc0000a"] == 9.0


def test_longer_shot_pushes_later_items() -> None:
    p = make_project()
    linked(p, {"op": "shot.update", "id": "s_aaaaaaaa", "patch": {"duration": 6.0}})
    s = starts(p)
    assert s["c_onb0000a"] == 7.0 and s["a_sfx0000a"] == 6.5 and s["o_onc0000a"] == 11.0


def test_deleted_shot_items_land_where_it_began() -> None:
    p = make_project()
    linked(p, {"op": "shot.delete", "id": "s_bbbbbbbb"})  # A 0..4, C 4..8
    s = starts(p)
    assert s["c_onb0000a"] == 5.0 and s["a_sfx0000a"] == 4.5  # offset kept from where B began
    assert s["o_onc0000a"] == 5.0


def test_split_leaves_items_in_place() -> None:
    p = make_project()
    before = starts(p)
    tail = {**blank_shot("s_btail000", {}), "duration": 2.0}
    linked(p, {"op": "shot.update", "id": "s_bbbbbbbb", "patch": {"duration": 2.0}},
           {"op": "shot.add", "shot": tail, "index": 2})
    assert starts(p) == before


def test_undo_restores_positions() -> None:
    p = make_project()
    before = starts(p)
    inverse = linked(p, {"op": "shot.move", "id": "s_cccccccc", "index": 0})
    assert starts(p) != before
    apply_ops(p, inverse, {})
    assert starts(p) == before
    assert [s["id"] for s in p["shots"]] == ["s_aaaaaaaa", "s_bbbbbbbb", "s_cccccccc"]


def test_items_edited_in_the_same_batch_are_left_alone() -> None:
    p = make_project()
    linked(p, {"op": "shot.update", "id": "s_aaaaaaaa", "patch": {"duration": 6.0}},
           {"op": "caption.update", "id": "c_onb0000a", "patch": {"start": 0.5, "end": 1.0}})
    s = starts(p)
    assert s["c_onb0000a"] == 0.5
    assert s["a_vo00000a"] == 7.0  # the voice-over still follows the shot


def test_plain_ops_do_not_link() -> None:
    p = make_project()
    apply_ops(p, [{"op": "shot.update", "id": "s_aaaaaaaa", "patch": {"duration": 6.0}}], {})
    assert starts(p)["o_onc0000a"] == 9.0
