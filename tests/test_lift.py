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


# ---------------------------------------------------------------- drop (ED-16)
def drop(p: dict, item: dict, media: dict, index: int, take: dict) -> dict:
    body = "console.log(JSON.stringify(m.dropOps(data.p, data.item, data.media, data.index, data.take)));"
    return run_js(JS / "lib" / "lift.js", body, {"p": p, "item": item, "media": media, "index": index, "take": take})


def upper_clip(p: dict, volume: float) -> tuple[dict, dict, dict]:
    media = {"id": "m_clip00001", "kind": "video", "file": "media/import/clip.mp4", "duration": 8.0, "hasAudio": True,
             "width": 320, "height": 180}
    p["media"][media["id"]] = media
    item = {"id": "o_clip00001", "mediaId": media["id"], "layer": "l_top00000", "name": "clip.mp4", "start": 1.0,
            "in": 2.0, "length": 3.0, "x": 0.6, "y": 0.1, "w": 0.3, "opacity": 0.5, "volume": volume,
            "chroma": {"on": True, "color": "#00ff00", "similarity": 0.3, "blend": 0.1},
            "keyframes": [{"t": 1.0, "opacity": 1.0}]}
    p["overlays"].append(item)
    take = {"id": "t_drop00001", "shot": "s_drop00001", "kind": "video", "file": media["file"], "duration": 8.0,
            "hasAudio": True, "width": 320, "height": 180}
    return item, media, take


def test_a_dropped_clip_keeps_its_place_for_a_lift_back() -> None:
    p = project()
    item, media, take = upper_clip(p, 1.0)
    got = drop(p, item, media, 1, take)
    apply_ops(p, got["ops"], {})
    shot = p["shots"][1]
    assert shot["id"] == "s_drop00001" and shot["activeVideo"] == take["id"] and p["overlays"] == []
    assert (shot["trimIn"], shot["trimOut"]) == (2.0, 5.0) and not shot.get("audioDetached") and p["audio"] == []
    assert shot["pip"] == {"x": 0.6, "y": 0.1, "w": 0.3, "opacity": 0.5, "volume": 1.0, "chroma": item["chroma"],
                           "keyframes": [{"t": 1.0, "opacity": 1.0}]}

    back = lift(p, shot["id"], "layer:l_top00000", 6.0)
    apply_ops(p, back["ops"], {})
    over = p["overlays"][0]
    assert {k: over[k] for k in ("x", "y", "w", "opacity", "volume", "in", "length")} == {
        "x": 0.6, "y": 0.1, "w": 0.3, "opacity": 0.5, "volume": 1.0, "in": 2.0, "length": 3.0}
    assert over["chroma"]["on"] and over["keyframes"] == [{"t": 1.0, "opacity": 1.0}]


def test_a_quieter_clip_stays_quieter_on_its_detached_sound() -> None:
    p = project()
    item, media, take = upper_clip(p, 0.5)
    apply_ops(p, drop(p, item, media, 1, take)["ops"], {})
    shot = p["shots"][1]
    [sound] = p["audio"]
    assert shot["audioDetached"] and shot["pip"]["volume"] == 0.5
    # The first shot plays 0..6, so the dropped one starts at 6 with its 2..5 piece of the file.
    assert (sound["takeId"], sound["shotId"], sound["start"], sound["in"], sound["out"], sound["volume"]) == (
        take["id"], shot["id"], 6.0, 2.0, 5.0, 0.5)

    # A lift back takes that sound with it, at the volume it has by then; a moved sound has to be restored first.
    sound["start"] = 6.5
    assert "error" in lift(p, shot["id"], "layer:l_top00000", 6.0)
    sound["start"], sound["volume"] = 6.0, 0.4
    apply_ops(p, lift(p, shot["id"], "layer:l_top00000", 6.0)["ops"], {})
    assert p["audio"] == [] and p["overlays"][0]["volume"] == 0.4 and p["overlays"][0]["opacity"] == 0.5


def test_pip_is_checked_like_an_upper_clip() -> None:
    p = project()
    apply_ops(p, [{"op": "shot.update", "id": p["shots"][0]["id"], "patch": {"pip": {"x": 0.2, "junk": 1}}}], {})
    assert p["shots"][0]["pip"] == {"x": 0.2}
    apply_ops(p, [{"op": "shot.update", "id": p["shots"][0]["id"], "patch": {"pip": {"opacity": 3}}}], {})
    assert p["shots"][0]["pip"] == {"opacity": 1.0}  # clamped like an upper clip's opacity
