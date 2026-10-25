"""Linked edits: items on the other tracks follow the shot they sit on."""
from __future__ import annotations

from test_server import SCRIPT, App, Client, app, client, project  # noqa: F401  (server fixtures)
from vc.opfields import blank_shot
from vc.ops import apply_ops
from vc.sequence import build_sequence
from vc.takes import commit_takes


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
    assert "c_onb0000a" not in s and "a_vo00000a" not in s  # its caption and voice-over went with it
    assert s["a_sfx0000a"] == 4.5  # offset kept from where B began
    assert s["o_onc0000a"] == 5.0


def test_caption_running_past_a_deleted_shot_stays() -> None:
    p = make_project()
    p["captions"][0]["end"] = 9.0  # runs on into C
    linked(p, {"op": "shot.delete", "id": "s_bbbbbbbb"})
    assert starts(p)["c_onb0000a"] == 5.0


def take_out(p: dict, sid: str = "s_bbbbbbbb") -> list[dict]:
    return linked(p, {"op": "shot.update", "id": sid, "patch": {"inSequence": False}})


def put_back(p: dict, sid: str = "s_bbbbbbbb", **extra: object) -> list[dict]:
    return apply_ops(p, [{"op": "linked", "ops": [{"op": "shot.update", "id": sid, "patch": {"inSequence": True}}],
                          **extra}], {})


def test_shot_out_of_sequence_parks_its_items() -> None:
    p = make_project()
    take_out(p)  # A 0..4, C 4..8
    s = starts(p)
    assert "c_onb0000a" not in s and "a_vo00000a" not in s  # the caption and its voice-over went with B
    assert s["a_sfx0000a"] == 4.5  # runs past B, so it stays on the timeline
    assert s["o_onc0000a"] == 5.0
    parked = p["shots"][1]["parked"]
    assert parked["captions"] == [{"id": "c_onb0000a", "start": 1.0, "end": 2.5, "text": "hi"}]
    assert [(a["id"], a["start"]) for a in parked["audio"]] == [("a_vo00000a", 1.0)]


def test_shot_back_in_sequence_restores_its_items() -> None:
    p = make_project()
    take_out(p)
    put_back(p)
    s = starts(p)
    assert s["c_onb0000a"] == 5.0 and p["captions"][0]["end"] == 6.5 and s["a_vo00000a"] == 5.0
    assert s["o_onc0000a"] == 9.0
    assert "parked" not in p["shots"][1]


def test_parked_items_come_back_at_the_new_place() -> None:
    p = make_project()
    take_out(p)
    linked(p, {"op": "shot.move", "id": "s_bbbbbbbb", "index": 0})
    put_back(p)  # B 0..4, A 4..8, C 8..12
    s = starts(p)
    assert s["c_onb0000a"] == 1.0 and s["a_vo00000a"] == 1.0 and s["o_onc0000a"] == 9.0


def test_undo_of_parking_and_restoring() -> None:
    p = make_project()
    before = starts(p)
    inverse = take_out(p)
    apply_ops(p, inverse, {})
    assert starts(p) == before and "parked" not in p["shots"][1]
    take_out(p)
    out = starts(p)
    inverse = put_back(p)
    apply_ops(p, inverse, {})
    assert starts(p) == out and p["shots"][1]["parked"]["captions"][0]["id"] == "c_onb0000a"


def test_restore_skips_items_whose_file_is_gone() -> None:
    p = make_project()
    take_out(p)
    del p["media"]["m_snd0000a"]
    put_back(p)
    s = starts(p)
    assert s["c_onb0000a"] == 5.0 and "a_vo00000a" not in s
    assert "parked" not in p["shots"][1]


def test_unlinked_restore_moves_nothing_else() -> None:
    p = make_project()
    take_out(p)
    put_back(p, shift=False)  # the timeline link is off: parked items still come back
    s = starts(p)
    assert s["c_onb0000a"] == 5.0 and s["a_vo00000a"] == 5.0
    assert s["o_onc0000a"] == 5.0


def test_fixed_sound_stays_put() -> None:
    p = make_project()
    p["audio"][1]["fixed"] = True
    linked(p, {"op": "shot.move", "id": "s_bbbbbbbb", "index": 0})
    s = starts(p)
    assert s["a_sfx0000a"] == 4.5 and s["a_vo00000a"] == 1.0


def test_undo_restores_deleted_captions() -> None:
    p = make_project()
    before = starts(p)
    inverse = linked(p, {"op": "shot.delete", "id": "s_bbbbbbbb"})
    apply_ops(p, inverse, {})
    assert starts(p) == before
    assert next(a for a in p["audio"] if a["id"] == "a_vo00000a")["captionId"] == "c_onb0000a"


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


class Store:
    def __init__(self, p: dict) -> None:
        self.p = p

    def get(self, pid: str) -> dict:
        return self.p

    def apply(self, pid: str, ops: list[dict], **_: object) -> None:
        apply_ops(self.p, ops, {})


def test_new_take_carries_what_follows() -> None:
    """ED-10: a finished 6 s video on B (it was a 4 s still) pushes C's items back; with linking off they stay."""
    for link, want in ((True, 11.0), (False, 9.0)):
        p = make_project()
        take = {"id": "t_long000a", "kind": "video", "file": "media/takes/t_long000a.mp4", "duration": 6.0,
                "hasAudio": False, "width": 320, "height": 180, "shot": "s_bbbbbbbb"}
        commit_takes(Store(p), "p_test", [take], "activeVideo", link=link)
        assert p["shots"][1]["activeVideo"] == "t_long000a"
        assert starts(p)["o_onc0000a"] == want and starts(p)["c_onb0000a"] == 5.0


def test_script_put_in_front_pushes_captions_back(client: Client, project: dict) -> None:
    """ED-11: a caption on the first shot moves with it past the imported shots, unless linking is off."""
    pid, sid = project["id"], project["shots"][0]["id"]
    client.json("POST", f"/api/projects/{pid}/ops", {"ops": [
        {"op": "shot.update", "id": sid, "patch": {"prompt": "日出"}},
        {"op": "caption.add", "item": {"id": "c_mine0000a", "start": 1.0, "end": 2.0, "text": "早安"}}]})
    parsed = client.json("POST", "/api/script/parse", {"text": SCRIPT})
    client.json("POST", f"/api/projects/{pid}/script/apply", {"shots": parsed["shots"], "index": 0, "captions": False})
    saved = client.json("GET", f"/api/projects/{pid}")["project"]
    mine = next(c for c in build_sequence(saved) if c["shot"]["id"] == sid)
    assert mine["start"] > 0 and saved["captions"][0]["start"] == round(mine["start"] + 1.0, 3)
    client.json("POST", f"/api/projects/{pid}/script/apply",
                {"shots": parsed["shots"], "index": 0, "captions": False, "link": False})
    after = client.json("GET", f"/api/projects/{pid}")["project"]
    assert after["captions"][0]["start"] == saved["captions"][0]["start"]
