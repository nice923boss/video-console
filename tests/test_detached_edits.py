"""Freeze and reverse on a shot whose sound is detached (ED-21): a freeze cuts the detached sound at the still and
moves the rest past it whatever the link toggle; a reverse switches the picture and leaves the sound playing
forward, and the reversed half still restores with the others."""
from __future__ import annotations

from test_edits import A, run
from test_timeline_keys import VID, make_project, with_video
from vc.edits import FREEZE_LEN
from vc.ops import apply_ops
from vc.sequence import build_sequence

REV = "t_rev00000a"


def detached() -> tuple[dict, str]:
    p = with_video(make_project())
    plan, _ = run(p, {"cmd": "detach", "id": A})
    return p, plan["id"]


def test_freeze_cuts_the_sound_and_moves_its_tail_past_the_still() -> None:
    p, aid = detached()
    _, back = run(p, {"cmd": "freeze", "t": 2})
    head, tail = p["audio"]
    assert head["id"] == aid and (head["start"], head["in"], head["out"]) == (0, 0, 2)
    assert (tail["start"], tail["in"], tail["out"]) == (2 + FREEZE_LEN, 2, 6) and tail["shotId"] == A
    clips = build_sequence(p)
    assert [c["start"] for c in clips[:3]] == [0, 2, 2 + FREEZE_LEN]  # head, still, tail: the sound sits under it
    apply_ops(p, back, {})
    assert [(a["id"], a["start"], a["out"]) for a in p["audio"]] == [(aid, 0, 6)]


def test_freeze_at_the_start_moves_the_whole_sound() -> None:
    p, _ = detached()
    run(p, {"cmd": "freeze", "t": 0.05})
    assert [(a["start"], a["out"]) for a in p["audio"]] == [(FREEZE_LEN, 6)]


def test_reverse_switches_the_picture_and_keeps_the_sound() -> None:
    p, _ = detached()
    p["takes"][REV] = {**p["takes"][VID], "id": REV, "parent": VID, "file": "media/takes/rev.mp4",
                       "source": {"tool": "reverse"}}
    before = [dict(a) for a in p["audio"]]
    run(p, {"cmd": "reverse", "id": A})
    assert p["shots"][0]["activeVideo"] == REV and p["audio"] == before


def test_a_reversed_half_restores_with_the_others() -> None:
    p, aid = detached()
    p["takes"][REV] = {**p["takes"][VID], "id": REV, "parent": VID, "file": "media/takes/rev.mp4",
                       "source": {"tool": "reverse"}}
    plan, _ = run(p, {"cmd": "split", "t": 2.5})
    run(p, {"cmd": "reverse", "id": plan["id"]})
    tail = p["shots"][1]
    assert p["takes"][tail["activeVideo"]]["source"]["tool"] == "reverse"
    run(p, {"cmd": "restore", "id": aid})
    assert p["audio"] == [] and not any(s.get("audioDetached") for s in p["shots"])
