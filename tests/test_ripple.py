"""Ripple delete (Shift+Delete): web/js/lib/ripple.js builds the ops, the server applies them."""
from __future__ import annotations

import shutil

import pytest

from test_timeline_keys import JS, make_project, run_js
from vc.ops import apply_ops

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


def project() -> dict:
    p = make_project()
    p["media"] = {"m_snd0000a": {"id": "m_snd0000a", "kind": "audio", "duration": 2.0},
                  "m_pic0000a": {"id": "m_pic0000a", "kind": "image"}}
    p["layers"] = [{"id": "l_one0000a", "name": ""}, {"id": "l_two0000a", "name": ""}]
    p["captions"] = [{"id": "c_one0000a", "start": 1.0, "end": 2.0, "text": "一"},
                     {"id": "c_two0000a", "start": 3.0, "end": 4.0, "text": "二"}]
    p["audio"] = [{"id": "a_vo1000aa", "mediaId": "m_snd0000a", "captionId": "c_one0000a", "start": 1.0, "in": 0.0},
                  {"id": "a_vo2000aa", "mediaId": "m_snd0000a", "captionId": "c_two0000a", "start": 3.0, "in": 0.0},
                  {"id": "a_mus100aa", "mediaId": "m_snd0000a", "start": 0.0, "in": 0.0},
                  {"id": "a_mus200aa", "mediaId": "m_snd0000a", "start": 2.5, "in": 0.0, "track": "a_mus100aa"}]
    p["overlays"] = [{"id": f"o_{name}", "mediaId": "m_pic0000a", "layer": layer, "start": start, "in": 0.0,
                      "length": 1.5} for name, layer, start in (("one0000a", "l_one0000a", 0.5),
                                                                ("two0000a", "l_one0000a", 4.0),
                                                                ("oth0000a", "l_two0000a", 4.0))]
    return p


def ripple(p: dict, kind: str, item_id: str) -> dict:
    got = run_js(JS / "lib" / "ripple.js", "console.log(JSON.stringify(m.rippleOps(data.p, data.sel)));",
                 {"p": p, "sel": {"type": kind, "id": item_id}})
    apply_ops(p, got, {})
    return {it["id"]: it["start"] for key in ("captions", "audio", "overlays") for it in p[key]}


def test_caption_goes_with_its_voice_and_the_next_pair_moves_up() -> None:
    p = project()
    s = ripple(p, "caption", "c_one0000a")
    assert "c_one0000a" not in s and "a_vo1000aa" not in s
    assert s["c_two0000a"] == 2.0 and p["captions"][0]["end"] == 3.0 and s["a_vo2000aa"] == 2.0
    assert s["a_mus200aa"] == 2.5  # the soundtrack is another track


def test_music_closes_up_on_its_own_track_only() -> None:
    p = project()
    s = ripple(p, "audio", "a_mus100aa")
    assert s["a_mus200aa"] == 0.5 and s["a_vo1000aa"] == 1.0


def test_overlay_closes_up_on_its_own_layer_only() -> None:
    p = project()
    s = ripple(p, "overlay", "o_one0000a")
    assert s["o_two0000a"] == 2.5 and s["o_oth0000a"] == 4.0


def test_later_item_never_moves_before_the_gap() -> None:
    p = project()
    p["overlays"][1]["start"] = 1.0  # starts inside the deleted clip
    s = ripple(p, "overlay", "o_one0000a")
    assert s["o_two0000a"] == 0.5
