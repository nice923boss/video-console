"""Lift (ED-17): a picture-lane shot dragged onto an upper track becomes a clip there; an empty shot keeps its place."""
from __future__ import annotations

import shutil

import pytest

from test_timeline_keys import JS, make_project, run_js, with_video
from vc.ops import apply_ops

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


def lift(p: dict, shot_id: str, target: str, start: float) -> dict:
    body = """
      const clips = seq.buildSequence(data.p);
      const clip = clips.find((c) => c.shot.id === data.id);
      console.log(JSON.stringify(m.liftOps(data.p, clip, data.target, data.start)));
    """
    body = 'const seq = await import(data.seq);\n' + body
    return run_js(JS / "lib" / "lift.js", body,
                  {"p": p, "id": shot_id, "target": target, "start": start, "seq": (JS / "lib" / "sequence.js").as_uri()})


def project() -> dict:
    p = with_video(make_project())
    p["settings"].update(width=320, height=180)
    p["layers"] = [{"id": "l_top00000", "name": ""}]
    return p


def starts(p: dict) -> list[tuple[str, float]]:
    body = "console.log(JSON.stringify(m.buildSequence(data.p).map((c) => [c.source, c.start, c.length])));"
    return [tuple(x) for x in run_js(JS / "lib" / "sequence.js", body, {"p": p})]


def test_shot_becomes_an_upper_clip_and_leaves_a_gap() -> None:
    p = project()
    first = p["shots"][0]
    before = starts(p)
    got = lift(p, first["id"], "layer:l_top00000", 0.0)
    apply_ops(p, got["ops"], {})
    over = p["overlays"][0]
    media = p["media"][over["mediaId"]]
    take = p["takes"][first["activeVideo"] or first["activeImage"]]
    assert over["id"] == got["id"] and over["layer"] == "l_top00000" and media["file"] == take["file"]
    assert over["length"] == pytest.approx(before[0][2], abs=1e-3)
    assert p["shots"][0]["name"] == "空白" and not p["shots"][0]["activeVideo"] and not p["shots"][0]["activeImage"]
    after = starts(p)
    assert after[0][0] == "empty" and [s[1:] for s in after] == pytest.approx([s[1:] for s in before], abs=1e-3)


def test_new_track_and_an_existing_library_item() -> None:
    p = project()
    first = p["shots"][0]
    take = p["takes"][first["activeVideo"] or first["activeImage"]]
    p["media"]["m_same0000a"] = {"id": "m_same0000a", "kind": take["kind"], "file": take["file"]}
    got = lift(p, first["id"], "new-layer", 1.5)
    kinds = [o["op"] for o in got["ops"]]
    assert "media.add" not in kinds and kinds[0] == "layer.add"
    apply_ops(p, got["ops"], {})
    assert p["overlays"][0]["mediaId"] == "m_same0000a" and p["overlays"][0]["start"] == 1.5 and len(p["layers"]) == 2


def test_refuses_what_an_upper_clip_cannot_carry() -> None:
    p = project()
    p["shots"][0]["speed"] = 2
    assert "error" in lift(p, p["shots"][0]["id"], "layer:l_top00000", 0)
    p["shots"][0]["speed"] = 1
    p["shots"][0]["audioDetached"] = True
    assert "error" in lift(p, p["shots"][0]["id"], "layer:l_top00000", 0)


def test_undo_puts_the_shot_back() -> None:
    p = project()
    first = dict(p["shots"][0])
    inverse = apply_ops(p, lift(p, first["id"], "layer:l_top00000", 0)["ops"], {})
    apply_ops(p, inverse, {})
    assert p["shots"][0]["id"] == first["id"] and p["overlays"] == [] and len(p["media"]) == 0
