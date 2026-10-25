"""Music tracks in the order the user set (ED-04): project.tracks.musicOrder."""
from __future__ import annotations

import shutil

import pytest

from test_timeline_keys import JS, make_project, run_js
from vc.util import AppError
from vc.ops import apply_ops

node = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")

AUDIO = [{"id": "a_first00aa", "mediaId": "m_snd0000a", "start": 0.0},
         {"id": "a_second0aa", "mediaId": "m_snd0000a", "start": 2.0},
         {"id": "a_third00aa", "mediaId": "m_snd0000a", "start": 4.0},
         {"id": "a_join000aa", "mediaId": "m_snd0000a", "start": 6.0, "track": "a_first00aa"}]


def keys(order: list[str] | None) -> list[str]:
    body = "console.log(JSON.stringify(m.musicTracks(data.audio, data.order).map((t) => t.key)));"
    return run_js(JS / "lib" / "sequence.js", body, {"audio": AUDIO, "order": order})


@node
def test_time_order_without_a_saved_order() -> None:
    assert keys(None) == ["a_first00aa", "a_second0aa", "a_third00aa"]


@node
def test_saved_order_first_then_the_rest_in_time_order() -> None:
    assert keys(["a_third00aa", "a_gone0000aa"]) == ["a_third00aa", "a_first00aa", "a_second0aa"]


def test_order_is_saved_and_undone() -> None:
    p = make_project()
    inverse = apply_ops(p, [{"op": "project.update", "patch": {"tracks": {"musicOrder": ["a_third00aa", "a_third00aa"]}}}], {})
    assert p["tracks"]["musicOrder"] == ["a_third00aa"]
    apply_ops(p, inverse, {})
    assert p["tracks"]["musicOrder"] == []


def test_bad_order_is_refused() -> None:
    p = make_project()
    for bad in ("a_third00aa", ["../x"]):
        with pytest.raises(AppError):
            apply_ops(p, [{"op": "project.update", "patch": {"tracks": {"musicOrder": bad}}}], {})
