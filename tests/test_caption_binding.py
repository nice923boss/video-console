"""Captions bound to a storyboard shot: the binding carries the text only, never the timing."""
from __future__ import annotations

import pytest

from vc.ops import apply_ops, blank_shot
from vc.storyboard import render_storyboard
from vc.util import AppError


def make_project() -> dict:
    # Two 4 s stills: A 0..4, B 4..8; one caption on B, bound to it.
    shots = []
    for sid in ("s_aaaaaaaa", "s_bbbbbbbb"):
        shot = blank_shot(sid, {})
        shot["duration"] = 4.0
        shots.append(shot)
    return {
        "shots": shots, "takes": {}, "media": {}, "layers": [], "audio": [], "overlays": [], "cast": [],
        "captionStyles": [], "settings": {"width": 1920, "height": 1080},
        "captions": [{"id": "c_onb0000a", "start": 4.5, "end": 6.0, "text": "你好", "shot": "s_bbbbbbbb"}],
    }


def test_add_and_unbind() -> None:
    p = make_project()
    apply_ops(p, [{"op": "caption.add", "item": {"id": "c_ona0000a", "start": 0.0, "end": 1.0, "text": "早",
                                                  "shot": "s_aaaaaaaa"}}], {})
    assert p["captions"][0]["shot"] == "s_aaaaaaaa"
    inverse = apply_ops(p, [{"op": "caption.update", "id": "c_ona0000a", "patch": {"shot": None}}], {})
    assert p["captions"][0]["shot"] is None
    apply_ops(p, inverse, {})
    assert p["captions"][0]["shot"] == "s_aaaaaaaa"


def test_bad_shot_id_is_rejected() -> None:
    p = make_project()
    with pytest.raises(AppError):
        apply_ops(p, [{"op": "caption.update", "id": "c_onb0000a", "patch": {"shot": "not an id"}}], {})


def test_deleting_the_shot_unbinds_and_undo_rebinds() -> None:
    p = make_project()
    inverse = apply_ops(p, [{"op": "shot.delete", "id": "s_bbbbbbbb"}], {})
    cap = p["captions"][0]
    assert cap["shot"] is None and cap["text"] == "你好" and cap["start"] == 4.5
    apply_ops(p, inverse, {})
    assert [s["id"] for s in p["shots"]] == ["s_aaaaaaaa", "s_bbbbbbbb"]
    assert p["captions"][0]["shot"] == "s_bbbbbbbb"


def test_linked_delete_undo_rebinds() -> None:
    p = make_project()
    inverse = apply_ops(p, [{"op": "linked", "ops": [{"op": "shot.delete", "id": "s_bbbbbbbb"}]}], {})
    assert p["captions"] == []  # it lay wholly on B, so the linked delete took it along
    apply_ops(p, inverse, {})
    assert p["captions"][0]["shot"] == "s_bbbbbbbb" and p["captions"][0]["start"] == 4.5


def test_binding_does_not_tie_timing() -> None:
    p = make_project()
    apply_ops(p, [{"op": "shot.update", "id": "s_bbbbbbbb", "patch": {"duration": 1.0}}], {})
    assert (p["captions"][0]["start"], p["captions"][0]["end"]) == (4.5, 6.0)


def test_storyboard_shows_bound_text() -> None:
    p = make_project()
    page = render_storyboard(p, lambda shot: None)
    assert page.count("<span>字幕</span>") == 1 and "你好" in page
