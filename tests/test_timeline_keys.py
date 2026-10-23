"""Timeline features behind the FCPX-style shortcuts: keyframes, markers, disabled clips, speed, detached
sound, reverse and freeze frame, from the data model through the export graph to a real ffmpeg render."""
from __future__ import annotations

import copy
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from vc.export import _atempo, active_adjustments, captions_srt, export_options, plan_export, prepare_clips, render
from vc.grade import clean_grade, default_grade
from vc.keyframes import expr, steps, value_at
from vc.linked import follow_ops
from vc.media import Media
from vc.ops import apply_ops, blank_shot
from vc.sequence import build_sequence
from vc.stt import caption_ops
from vc.util import AppError

FFMPEG = shutil.which("ffmpeg")
VID = "t_vid00000"
JS = Path(__file__).resolve().parents[1] / "app" / "web" / "js"


def make_project() -> dict:
    shots = []
    for sid in ("s_aaaaaaaa", "s_bbbbbbbb"):
        shot = blank_shot(sid, {})
        shot["duration"] = 4.0
        shots.append(shot)
    return {"id": "p_test0000", "name": "t", "settings": {"width": 320, "height": 180, "fps": 24},
            "shots": shots, "takes": {}, "media": {}, "audio": [], "captions": [], "overlays": [], "layers": [],
            "tracks": {}, "adjustments": [], "markers": []}


def with_video(p: dict, duration: float = 6.0) -> dict:
    """Give every shot the same video take (a file under media/takes)."""
    p["takes"][VID] = {"id": VID, "kind": "video", "file": f"media/takes/{VID}.mp4", "duration": duration,
                       "hasAudio": True, "width": 320, "height": 180, "shot": "s_aaaaaaaa"}
    for shot in p["shots"]:
        shot["activeVideo"] = VID
    return p


def ff_eval(text: str, **env: float) -> float:
    """Evaluate the small ffmpeg expression subset keyframes.expr writes (if, lt, max, trunc)."""
    py = text.replace("if(", "_if(")
    return eval(py, {"_if": lambda c, a, b: a if c else b, "lt": lambda a, b: a < b, "max": max,  # noqa: S307
                     "trunc": int, "__builtins__": {}}, env)


# ---------------------------------------------------------------- keyframes
def test_keyframe_values_hold_outside_and_move_in_a_line_between() -> None:
    item = {"volume": 0.8, "keyframes": [{"t": 1.0, "volume": 0.0}, {"t": 3.0, "volume": 1.0, "x": 0.5}]}
    assert value_at(item, "volume", 0.0) == 0.0 and value_at(item, "volume", 2.0) == pytest.approx(0.5)
    assert value_at(item, "volume", 9.0) == 1.0
    assert value_at(item, "x", 0.0) == 0.5  # one keyframe holds everywhere
    assert value_at(item, "y", 1.0, 0.2) == 0.2 and value_at({"volume": 0.8}, "volume", 1.0) == 0.8
    for t in (0.0, 1.0, 1.5, 2.0, 2.999, 3.0, 7.0):
        assert ff_eval(expr(item, "volume", "t"), t=t) == pytest.approx(value_at(item, "volume", t), abs=1e-3)
        # Timeline time with the clip starting at 10 s.
        assert ff_eval(expr(item, "volume", "T", 10.0), T=10 + t) == pytest.approx(value_at(item, "volume", t), abs=1e-3)
    assert expr({"volume": 0.8}, "volume", "t") == "0.8000"


def test_grade_keyframes_blend_and_step_for_export() -> None:
    warm, cool = clean_grade({"temp": 9000}), clean_grade({"temp": 3000})
    item = {"keyframes": [{"t": 0.0, "grade": warm}, {"t": 1.0, "grade": cool}]}
    assert value_at(item, "grade", 0.5)["temp"] == pytest.approx(6000)
    spans = steps(item, "grade", 2.0, 5.0, 24)
    assert len(spans) == 24 + 1  # one step per frame across the 1 s change, then the held end value
    assert spans[0][0] == 2.0 and spans[-1] == (3.0, 5.0, cool)
    assert all(a[1] == pytest.approx(b[0]) for a, b in zip(spans, spans[1:]))  # no gaps
    assert len(steps(item, "grade", 0.0, 60.0, 24, max_steps=30)) <= 31
    assert steps({"grade": warm}, "grade", 1.0, 2.0, 24) == [(1.0, 2.0, warm)]


def test_keyframe_ops_validate_sort_and_undo() -> None:
    p = make_project()
    p["media"]["m_pic00000"] = {"id": "m_pic00000", "kind": "image", "file": "media/import/a.png"}
    p["layers"] = [{"id": "l_top00000", "name": "上方", "hidden": False, "muted": False}]
    apply_ops(p, [{"op": "overlay.add", "item": {"id": "o_aaaaaaaa", "mediaId": "m_pic00000", "layer": "l_top00000",
                                                  "start": 0, "length": 4}}], {})
    back = apply_ops(p, [{"op": "overlay.update", "id": "o_aaaaaaaa", "patch": {"keyframes": [
        {"t": 2, "x": 0.5}, {"t": 0.0001, "opacity": 0.2}, {"t": 2, "x": 0.25, "w": 0.5}]}}], {})
    [o] = p["overlays"]
    assert o["keyframes"] == [{"t": 0.0, "opacity": 0.2}, {"t": 2.0, "x": 0.25, "w": 0.5}]  # later t=2 wins
    apply_ops(p, back, {})
    assert "keyframes" not in o or not o["keyframes"]
    apply_ops(p, [{"op": "overlay.update", "id": "o_aaaaaaaa", "patch": {"keyframes": [{"t": -1, "w": 5}]}}], {})
    assert o["keyframes"] == [{"t": 0.0, "w": 1.0}]  # numbers are clamped like the plain fields
    for bad in ([{"t": 1, "grade": {}}], [{"t": "a", "x": 0}], "x", [1]):
        with pytest.raises(AppError):
            apply_ops(p, [{"op": "overlay.update", "id": "o_aaaaaaaa", "patch": {"keyframes": bad}}], {})


# ---------------------------------------------------------------- markers and flags
def test_markers_add_recolour_and_undo() -> None:
    p = make_project()
    undo = apply_ops(p, [{"op": "marker.add", "item": {"id": "mk_b0000000", "start": 3.0, "color": "blue"}},
                         {"op": "marker.add", "item": {"id": "mk_a0000000", "start": 1.0, "color": "red", "name": "開場"}}], {})
    assert [m["start"] for m in p["markers"]] == [1.0, 3.0]
    apply_ops(p, [{"op": "marker.update", "id": "mk_b0000000", "patch": {"color": "green"}}], {})
    assert p["markers"][1]["color"] == "green"
    with pytest.raises(AppError):
        apply_ops(p, [{"op": "marker.update", "id": "mk_b0000000", "patch": {"color": "pink"}}], {})
    apply_ops(p, undo, {})
    assert p["markers"] == []


def test_shot_flags_and_speed_are_checked() -> None:
    p = make_project()
    apply_ops(p, [{"op": "shot.update", "id": "s_aaaaaaaa",
                   "patch": {"disabled": True, "speed": 9, "audioDetached": True}}], {})
    s = p["shots"][0]
    assert s["disabled"] is True and s["speed"] == 4 and s["audioDetached"] is True
    with pytest.raises(AppError):
        apply_ops(p, [{"op": "shot.update", "id": "s_aaaaaaaa", "patch": {"speed": "fast"}}], {})


def test_undo_removes_fields_the_item_did_not_have() -> None:
    p = make_project()
    p["audio"] = [{"id": "a_old00000", "mediaId": "m_mus00000", "start": 0.0, "volume": 1.0}]  # as uploads write it
    before = copy.deepcopy(p)
    fwd = [{"op": "audio.update", "id": "a_old00000", "patch": {"in": 1.5, "fadeOut": 0.0, "volume": 0.5}},
           {"op": "shot.update", "id": "s_aaaaaaaa", "patch": {"speed": 2}}]
    back = apply_ops(p, fwd, {})
    after = copy.deepcopy(p)
    again = apply_ops(p, back, {})
    assert p == before
    apply_ops(p, again, {})
    assert p == after
    with pytest.raises(AppError):
        apply_ops(p, [{"op": "audio.update", "id": "a_old00000", "patch": {}, "unset": ["id"]}], {})


def test_detached_sound_needs_its_take() -> None:
    p = with_video(make_project())
    item = {"id": "a_det00000", "start": 0, "in": 0, "out": 4, "takeId": VID, "shotId": "s_aaaaaaaa"}
    apply_ops(p, [{"op": "audio.add", "item": item}], {})
    assert p["audio"][0]["takeId"] == VID
    with pytest.raises(AppError):
        apply_ops(p, [{"op": "audio.add", "item": {**item, "id": "a_det00001", "takeId": "t_gone00000"}}], {})


# ---------------------------------------------------------------- speed on the timeline
def test_speed_changes_clip_length_and_linked_items() -> None:
    p = with_video(make_project())
    p["shots"][0]["trimOut"] = 4.0
    p["shots"][1]["trimOut"] = 2.0
    before = build_sequence(p)
    assert [c["length"] for c in before] == [4.0, 2.0]
    p["captions"] = [{"id": "c_aaaaaaaa", "start": 2.0, "end": 3.0, "text": "a"},
                     {"id": "c_bbbbbbbb", "start": 4.5, "end": 5.0, "text": "b"}]
    old = copy.deepcopy(p)
    p["shots"][0]["speed"] = 2.0
    assert [c["length"] for c in build_sequence(p)] == [2.0, 2.0]
    moves = {o["id"]: o["patch"]["start"] for o in follow_ops(old, p)}
    assert moves == {"c_aaaaaaaa": 1.0, "c_bbbbbbbb": 2.5}  # the source frame under each caption keeps it
    # Transcribed cues are in source seconds; a 2x clip shows them in half the time.
    [op] = caption_ops([(2.0, 3.0, "hi")], build_sequence(p)[0])
    assert (op["item"]["start"], op["item"]["end"]) == (1.0, 1.5)


def test_atempo_chain_covers_quarter_speed() -> None:
    assert _atempo(1.0) == ""
    assert _atempo(2.0) == "atempo=2.0000,"
    assert _atempo(0.25) == "atempo=0.5000,atempo=0.5000,"


# ---------------------------------------------------------------- export graph
def plan(p: dict, tmp_path: Path) -> dict:
    (tmp_path / "media" / "takes").mkdir(parents=True, exist_ok=True)
    (tmp_path / "media" / "takes" / f"{VID}.mp4").write_bytes(b"x")
    clips = prepare_clips(p, tmp_path)
    return plan_export(p, tmp_path, clips, export_options(p, {}, {}), None, [])


def test_export_blacks_out_disabled_and_speeds_clips(tmp_path: Path) -> None:
    p = with_video(make_project())
    p["shots"][0].update(speed=2.0, trimOut=4.0)
    p["shots"][1].update(disabled=True, trimOut=2.0)
    out = plan(p, tmp_path)
    assert out["total"] == pytest.approx(4.0)
    assert ["-ss", "0.000", "-t", "4.000", "-i", f"media/takes/{VID}.mp4"] in out["inputs"]  # 2 s at 2x reads 4 s
    assert any(i[-1].startswith("color=c=black:s=320x180") for i in out["inputs"])
    assert "setpts=(PTS-STARTPTS)/2.0000" in out["graph"] and "atempo=2.0000,atrim=duration=2.000" in out["graph"]
    assert "[1:a]" not in out["graph"]  # the disabled shot is silent


def test_export_plays_detached_sound_instead_of_the_shot(tmp_path: Path) -> None:
    p = with_video(make_project())
    p["shots"][0].update(audioDetached=True, trimOut=4.0)
    p["shots"][1]["inSequence"] = False
    p["audio"] = [{"id": "a_det00000", "start": 0.0, "in": 0.0, "out": 4.0, "volume": 1.0, "takeId": VID,
                   "shotId": "s_aaaaaaaa", "keyframes": [{"t": 0.0, "volume": 0.0}, {"t": 2.0, "volume": 1.0}]}]
    out = plan(p, tmp_path)
    assert "[0:a]" not in out["graph"]
    assert ["-i", f"media/takes/{VID}.mp4"] in out["inputs"]
    vol = re.search(r"volume='([^']+)':eval=frame", out["graph"]).group(1)
    assert ff_eval(vol, t=1.0) == pytest.approx(0.5)


def test_export_animates_keyframed_overlays_and_skips_disabled_ones(tmp_path: Path) -> None:
    p = with_video(make_project())
    p["media"]["m_vid00000"] = {"id": "m_vid00000", "kind": "video", "file": f"media/takes/{VID}.mp4",
                                "duration": 6.0, "hasAudio": True, "width": 320, "height": 180}
    p["layers"] = [{"id": "l_top00000", "name": "上方", "hidden": False, "muted": False}]
    base = {"mediaId": "m_vid00000", "layer": "l_top00000", "start": 2.0, "in": 0.0, "length": 3.0,
            "x": 0.0, "y": 0.0, "w": 0.5, "opacity": 1.0, "volume": 1.0}
    p["overlays"] = [{**base, "id": "o_aaaaaaaa", "keyframes": [
        {"t": 0.0, "x": 0.0, "w": 0.25, "opacity": 0.0, "volume": 0.0},
        {"t": 1.0, "x": 0.5, "w": 0.5, "opacity": 1.0, "volume": 1.0}]},
        {**base, "id": "o_bbbbbbbb", "disabled": True}]
    g = plan(p, tmp_path)["graph"]
    assert g.count("overlay=") == 1  # the disabled clip is left out
    x = re.search(r"overlay=x='320\*\(([^']+)\)'", g).group(1)
    assert ff_eval(x, t=2.5) == pytest.approx(0.25)  # halfway through the move, on timeline time
    assert "eval=frame" in g and "geq=" in g and "alpha(X,Y)*(" in g


def test_disabled_captions_stay_out_of_srt() -> None:
    p = make_project()
    p["captions"] = [{"id": "c_aaaaaaaa", "start": 0.0, "end": 1.0, "text": "看得到"},
                     {"id": "c_bbbbbbbb", "start": 1.0, "end": 2.0, "text": "停用", "disabled": True}]
    srt = captions_srt(p)
    assert "看得到" in srt and "停用" not in srt


def test_keyframed_adjustment_layer_becomes_steps() -> None:
    p = make_project()
    warm = clean_grade({"temp": 9000})
    p["adjustments"] = [{"id": "adj_a0000000", "start": 1.0, "length": 4.0, "hidden": False, "grade": default_grade(),
                         "keyframes": [{"t": 0.0, "grade": default_grade()}, {"t": 1.0, "grade": warm}]}]
    spans = active_adjustments(p, 8.0)
    # Each step holds the grade at its middle, climbing to the warm grade and holding it to the end.
    assert spans[0][0] == 1.0 and 5000 < spans[0][2]["temp"] < 5200 and spans[-1] == (2.0, 5.0, warm)
    assert spans[1][2]["temp"] > spans[0][2]["temp"]


# ---------------------------------------------------------------- preview mirrors and keys
def run_js(module: Path, body: str, data: object) -> object:
    """Run body (with `m` the imported module and `data` the input) under node; return what it prints."""
    script = f"const m = await import(process.argv[1]); const data = JSON.parse(process.argv[2]); {body}"
    proc = subprocess.run(["node", "--input-type=module", "-e", script, module.as_uri(), json.dumps(data)],
                          capture_output=True, check=True, text=True, encoding="utf-8")
    return json.loads(proc.stdout)


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_preview_keyframes_match_export() -> None:
    warm, cool = clean_grade({"temp": 9000}), clean_grade({"temp": 3000})
    item = {"volume": 0.8, "keyframes": [{"t": 0.5, "volume": 0.0, "grade": warm}, {"t": 1.5, "volume": 1.0},
                                         {"t": 2.5, "volume": 0.4, "grade": cool}]}
    # Inside the export's steps (an exact step edge can round either way), plus the first key itself.
    times = [0.5] + [i / 10 + 0.013 for i in range(30)]
    got = run_js(JS / "lib" / "keyframes.js", """console.log(JSON.stringify({
      volume: data.times.map((t) => m.valueAt(data.item, "volume", t)),
      stepped: data.times.map((t) => m.steppedAt(data.item, "grade", t, 3, 24).temp),
      split: m.splitKeys(data.item, 1.0) }));""", {"item": item, "times": times})
    assert got["volume"] == pytest.approx([value_at(item, "volume", t) for t in times])
    spans = steps(item, "grade", 0.0, 3.0, 24)
    for t, temp in zip(times, got["stepped"]):
        held = next(v for a, b, v in spans if a <= t < b)
        assert temp == pytest.approx(held["temp"])
    head, tail = got["split"]
    assert head[-1]["t"] == 1.0 and tail[0]["t"] == 0.0 and head[-1]["volume"] == tail[0]["volume"] == pytest.approx(0.5)
    for t in (0.0, 0.7, 1.2, 1.9):  # each half plays what the whole clip played there
        whole = value_at(item, "volume", t + 1.0)
        assert value_at({"keyframes": tail}, "volume", t) == pytest.approx(whole)


def default_keys() -> list[tuple[str, str, list[str], list[str]]]:
    """(scope, id, Video Console keys, Final Cut Pro keys) of every built-in command in lib/keymap.js."""
    lst = r'\[((?:"[^"]*"(?:, )?)*)\]'
    out = []
    for m in re.finditer(r'cmd\("(\w+)", "([\w.]+)", "[^"]*", ' + lst + r"(?:, " + lst + r")?\)",
                         (JS / "lib" / "keymap.js").read_text(encoding="utf-8")):
        keys = re.findall(r'"([^"]*)"', m.group(3))
        out.append((m.group(1), m.group(2), keys, keys if m.group(4) is None else re.findall(r'"([^"]*)"', m.group(4))))
    return out


def test_default_keys_follow_the_table_and_never_clash() -> None:
    cmds = default_keys()
    by_id = {c[1]: c for c in cmds}
    assert len(by_id) > 70
    # The Video Console preset follows the CapCut "Final Cut Pro X" timeline table.
    for cid, key in [("app.palette", "mod+shift+p"), ("edit.keyframePanel", "mod+k"), ("edit.keyframeAdd", "k"),
                     ("edit.split", "alt+b"), ("edit.splitAll", "alt+shift+b"), ("edit.toolBlade", "b"),
                     ("edit.toggleEnabled", "v"), ("edit.volumeUp", "mod+."), ("edit.detachAudio", "mod+shift+s"),
                     ("edit.speed", "mod+r"), ("edit.freeze", "alt+shift+f"), ("edit.trackTaller", "+"),
                     ("edit.delete", "f2")]:
        assert key in by_id[cid][2], cid
    assert by_id["edit.stop"][2] == [] and by_id["edit.stop"][3] == ["k"]
    for preset in (2, 3):
        # The palette's own entries (commands.js) take Alt+1..9 and "?" everywhere.
        assert not [k for c in cmds for k in c[preset] if re.fullmatch(r"alt\+\d|\?", k)]
        seen: dict[str, list[tuple[str, str]]] = {}
        for c in cmds:
            for key in c[preset]:
                for scope, other in seen.get(key, []):
                    assert not (scope == c[0] or "global" in (scope, c[0])), (key, other, c[1])
                seen.setdefault(key, []).append((c[0], c[1]))


# ---------------------------------------------------------------- real ffmpeg
@pytest.mark.skipif(not FFMPEG, reason="ffmpeg not installed")
def test_render_with_speed_keyframes_and_detached_sound(tmp_path: Path) -> None:
    media = Media(FFMPEG)
    takes = tmp_path / "media" / "takes"
    takes.mkdir(parents=True)
    media.run(["-f", "lavfi", "-i", "testsrc=size=320x180:rate=24:duration=6", "-f", "lavfi",
               "-i", "sine=frequency=440:duration=6", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
               "-shortest", str(takes / f"{VID}.mp4")])
    p = with_video(make_project())
    third, fourth = blank_shot("s_cccccccc", {}), blank_shot("s_dddddddd", {})
    p["shots"] += [third, fourth]
    for shot in p["shots"]:
        shot["activeVideo"] = VID
    p["shots"][0].update(trimOut=2.0, audioDetached=True)  # 2 s, sound on the audio track
    p["shots"][1].update(trimOut=2.0, speed=0.5)  # 4 s
    p["shots"][2].update(trimOut=1.0, speed=0.25)  # 4 s
    p["shots"][3].update(trimOut=2.0, disabled=True)  # 2 s of black
    p["audio"] = [{"id": "a_det00000", "start": 0.0, "in": 0.0, "out": 2.0, "volume": 1.0, "takeId": VID,
                   "keyframes": [{"t": 0.0, "volume": 0.0}, {"t": 2.0, "volume": 1.0}]}]
    p["media"]["m_vid00000"] = {"id": "m_vid00000", "kind": "video", "file": f"media/takes/{VID}.mp4",
                                "duration": 6.0, "hasAudio": True, "width": 320, "height": 180}
    p["layers"] = [{"id": "l_top00000", "name": "上方", "hidden": False, "muted": False}]
    p["overlays"] = [{"id": "o_aaaaaaaa", "mediaId": "m_vid00000", "layer": "l_top00000", "start": 1.0, "in": 0.0,
                      "length": 3.0, "x": 0.1, "y": 0.1, "w": 0.3, "opacity": 1.0, "volume": 0.5,
                      "keyframes": [{"t": 0.0, "x": 0.0, "w": 0.2, "opacity": 0.2, "volume": 0.0},
                                    {"t": 2.0, "x": 0.6, "w": 0.4, "opacity": 1.0, "volume": 1.0}]}]
    p["adjustments"] = [{"id": "adj_a0000000", "start": 6.0, "length": 4.0, "hidden": False, "grade": default_grade(),
                         "keyframes": [{"t": 0.0, "grade": default_grade()},
                                       {"t": 0.5, "grade": clean_grade({"temp": 9000})}]}]
    result = render(media, p, tmp_path, export_options(p, {}, {"quality": "draft"}), "libx264")
    info = media.probe(tmp_path / result["file"])
    assert result["duration"] == pytest.approx(12.0)
    assert info["duration"] == pytest.approx(12.0, abs=0.15) and info["hasAudio"]
