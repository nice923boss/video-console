"""Caption styles: project ops with undo, the app-wide library, ASS output and a real libass render."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from vc import opfields, ops
from vc.captions import _family_names, caption_ass, resolve_style, stack_dragged, wrap_text, wrap_width
from vc.events import EventBus
from vc.export import build_ass
from vc.media import find_ffmpeg
from vc.paths import Paths
from vc.settings import SettingsStore
from vc.store import ProjectStore
from vc.util import AppError

needs_ffmpeg = pytest.mark.skipif(find_ffmpeg() is None, reason="ffmpeg is not installed")
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
PRESETS_JS = Path(__file__).resolve().parents[1] / "app" / "web" / "js" / "lib" / "caption-presets.js"


def _presets() -> list[dict]:
    script = "const m = await import(process.argv[1]); console.log(JSON.stringify(m.PRESET_STYLES));"
    proc = subprocess.run(["node", "--input-type=module", "-e", script, PRESETS_JS.as_uri()],
                          capture_output=True, check=True, text=True, encoding="utf-8")
    return json.loads(proc.stdout)


def _project() -> dict:
    return {
        "id": "p_test000001", "name": "t", "settings": {"width": 1920, "height": 1080, "fps": 24},
        "shots": [], "takes": {}, "media": {}, "audio": [], "cast": [], "layers": [], "overlays": [],
        "tracks": {}, "captionStyles": [],
        "captions": [
            {"id": "c_aaaaaa01", "start": 0.0, "end": 1.0, "text": "第一句", "position": "bottom"},
            {"id": "c_aaaaaa02", "start": 1.0, "end": 2.0, "text": "第二句", "position": "top"},
        ],
    }


def _undo(project: dict, inverse: list[dict]) -> None:
    ops.apply_ops(project, inverse, {})


def test_style_ops_fill_defaults_validate_and_undo() -> None:
    p = _project()
    inv_add = ops.apply_ops(p, [{"op": "captionStyle.add", "item": {
        "id": "cs_title01", "name": "標題", "color": "#FFCC00", "font": "DFKai-SB, x", "box": True}}], {})
    [style] = p["captionStyles"]
    assert style["color"] == "#ffcc00" and style["font"] == "DFKai-SB x" and style["box"] is True
    assert style["outlineWidth"] == opfields.DEFAULT_CAPTION_STYLE["outlineWidth"]  # missing fields take defaults
    with pytest.raises(AppError):
        ops.apply_ops(p, [{"op": "captionStyle.update", "id": "cs_title01", "patch": {"color": "red"}}], {})
    with pytest.raises(AppError):
        ops.apply_ops(p, [{"op": "captionStyle.update", "id": "cs_title01", "patch": {"align": "middle"}}], {})

    inv = ops.apply_ops(p, [{"op": "captionStyle.update", "id": "cs_title01",
                             "patch": {"size": 999, "x": 0.3, "y": 0.2}}], {})
    assert p["captionStyles"][0]["size"] == 200 and p["captionStyles"][0]["x"] == 0.3
    _undo(p, inv)
    assert p["captionStyles"][0]["size"] == 52 and p["captionStyles"][0]["x"] is None

    _undo(p, inv_add)
    assert p["captionStyles"] == []


def test_deleting_a_style_unlinks_captions_and_the_default_and_undo_restores_them() -> None:
    p = _project()
    ops.apply_ops(p, [
        {"op": "captionStyle.add", "item": {"id": "cs_aaaaaa01", "name": "A"}},
        {"op": "captionStyle.add", "item": {"id": "cs_bbbbbb01", "name": "B"}},
        {"op": "caption.update", "id": "c_aaaaaa01", "patch": {"style": "cs_bbbbbb01", "x": 0.5, "y": 0.4}},
        {"op": "project.update", "patch": {"settings": {"captionStyle": "cs_bbbbbb01"}}},
    ], {})
    inv = ops.apply_ops(p, [{"op": "captionStyle.delete", "id": "cs_bbbbbb01"}], {})
    assert [s["id"] for s in p["captionStyles"]] == ["cs_aaaaaa01"]
    assert p["captions"][0]["style"] is None and p["settings"]["captionStyle"] is None
    _undo(p, inv)
    assert [s["id"] for s in p["captionStyles"]] == ["cs_aaaaaa01", "cs_bbbbbb01"]  # back in its place
    assert p["captions"][0]["style"] == "cs_bbbbbb01" and p["settings"]["captionStyle"] == "cs_bbbbbb01"


def test_resolve_prefers_caption_style_then_project_default_then_legacy_size() -> None:
    p = _project()
    cap = p["captions"][0]
    assert resolve_style(p, cap, 70)["size"] == 70  # no styles yet: the old export caption size
    ops.apply_ops(p, [
        {"op": "captionStyle.add", "item": {"id": "cs_aaaaaa01", "size": 40}},
        {"op": "captionStyle.add", "item": {"id": "cs_bbbbbb01", "size": 90}},
        {"op": "project.update", "patch": {"settings": {"captionStyle": "cs_aaaaaa01"}}},
    ], {})
    assert resolve_style(p, cap, 70)["size"] == 40
    cap["style"] = "cs_bbbbbb01"
    assert resolve_style(p, cap, 70)["size"] == 90
    cap["style"] = "cs_missing01"  # a stale link falls back to the default
    assert resolve_style(p, cap, 70)["size"] == 40


def test_ass_layers_alignment_and_dragged_position() -> None:
    p = _project()
    ops.apply_ops(p, [
        {"op": "captionStyle.add", "item": {
            "id": "cs_aaaaaa01", "align": "left", "box": True, "shadow": True, "shadowDistance": 3,
            "shadowBlur": 4, "outline": False, "color": "#112233", "opacity": 0.5}},
        {"op": "project.update", "patch": {"settings": {"captionStyle": "cs_aaaaaa01"}}},
        {"op": "caption.update", "id": "c_aaaaaa02", "patch": {"x": 0.25, "y": 0.5}},
    ], {})
    styles, events = caption_ass(p, p["captions"], 1920, 52)
    assert [s.split(",")[0] for s in styles] == ["Style: C1T", "Style: C1S", "Style: C1B"]
    text = styles[0].split(",")
    assert text[3] == "&H80332211"  # 50 % opaque #112233 as &HAABBGGRR
    assert text[16] == "0" and text[17] == "0"  # no outline, the text layer casts no shadow
    assert styles[1].split(",")[16:18] == ["1", "3"]  # shadow copy keeps a border so it casts at all
    assert styles[2].split(",")[15:17] == ["3", "12"]  # box: opaque box style padded by boxPadding
    first = [e for e in events if "第一句" in e]
    assert [e.split(",")[0] for e in first] == ["Dialogue: 1", "Dialogue: 2", "Dialogue: 3"]
    assert all("{\\an1" in e for e in first)  # bottom row, left aligned
    assert "\\1a&HFF&\\3a&HFF&\\blur4" in first[1]
    second = [e for e in events if "第二句" in e]
    assert all("{\\an4\\pos(480,540)" in e for e in second)  # dragged: middle-left anchor at (0.25, 0.5)


def test_dragged_captions_shown_together_move_apart() -> None:
    def box(key: str, start: float, end: float, top: float) -> dict:
        return {"key": key, "start": start, "end": end, "left": 900, "right": 1020, "top": top, "bottom": top + 60}
    shifts = stack_dragged([box("a", 0, 2, 800), box("b", 1, 3, 800), box("c", 0, 2, 100), box("d", 0.5, 1, 120),
                            box("e", 2, 3, 100), box("f", 1.5, 3, 800)])
    assert shifts["a"] == 0 and shifts["c"] == 0 and shifts["e"] == 0  # nothing earlier in the way
    assert shifts["b"] == -60  # lower half: moves up off "a"
    assert shifts["d"] == 40  # upper half: its top (120) moves down to the bottom of "c" (160)
    assert shifts["f"] == -120  # clear of "a" at -60 still meets "b" there, so it goes one more up

    p = _project()
    ops.apply_ops(p, [{"op": "caption.update", "id": "c_aaaaaa01", "patch": {"x": 0.5, "y": 0.8, "end": 2.0}},
                      {"op": "caption.update", "id": "c_aaaaaa02", "patch": {"x": 0.5, "y": 0.8, "start": 0.5}}], {})
    _, events = caption_ass(p, p["captions"], 1920, 52)
    assert all("\\pos(960,864)" in e for e in events if "第一句" in e)
    assert all("\\pos(960,812)" in e for e in events if "第二句" in e)  # one 52 px line up
    ops.apply_ops(p, [{"op": "caption.update", "id": "c_aaaaaa01", "patch": {"end": 0.5}}], {})
    _, events = caption_ass(p, p["captions"], 1920, 52)
    assert all("\\pos(960,864)" in e for e in events)  # one after the other: both stay put


def test_glow_keeps_the_outline_of_the_shadow_copy() -> None:
    p = _project()
    ops.apply_ops(p, [{"op": "captionStyle.add", "item": {
        "id": "cs_aaaaaa01", "shadowDistance": 0, "shadowBlur": 6}},
        {"op": "project.update", "patch": {"settings": {"captionStyle": "cs_aaaaaa01"}}}], {})
    _, events = caption_ass(p, p["captions"][:1], 1920, 52)
    shadow = [e for e in events if e.startswith("Dialogue: 2")][0]
    assert "\\1a&HFF&\\blur6}" in shadow and "\\3a" not in shadow


def test_library_lives_in_app_settings_and_is_validated(tmp_path: Path) -> None:
    store = SettingsStore(tmp_path / "settings.json")
    assert store.get()["captionStyles"] == []
    saved = store.update({"captionStyles": [{"id": "cs_lib00001", "name": "  黃字  ", "color": "#FFFF00"}]})
    [style] = saved["captionStyles"]
    assert style["name"] == "黃字" and style["color"] == "#ffff00" and style["font"] == "Microsoft JhengHei"
    assert SettingsStore(tmp_path / "settings.json").get()["captionStyles"][0]["id"] == "cs_lib00001"
    with pytest.raises(AppError):
        store.update({"captionStyles": [{"id": "cs_lib00001"}, {"id": "cs_lib00001"}]})
    with pytest.raises(AppError):
        store.update({"captionStyles": [{"id": "bad id"}]})
    assert store.get()["captionStyles"][0]["name"] == "黃字"  # a rejected patch leaves the library alone


def test_new_projects_can_start_with_the_library(tmp_path: Path) -> None:
    settings = SettingsStore(tmp_path / "settings.json")
    settings.update({"captionStyles": [{"id": "cs_lib00001", "name": "黃字"}, {"id": "cs_lib00002", "name": "白字"}]})
    store = ProjectStore(Paths(tmp_path / "data"), EventBus(), settings)
    assert store.create("a")["captionStyles"] == []  # off by default
    settings.update({"captionStylesAuto": True})
    p = store.create("b")
    assert [s["name"] for s in p["captionStyles"]] == ["黃字", "白字"]
    assert p["captionStyles"][0]["id"] not in ("cs_lib00001", "cs_lib00002")  # the project keeps its own copies
    assert p["settings"]["captionStyle"] == p["captionStyles"][0]["id"]
    assert resolve_style(p, {})["name"] == "黃字"


def test_font_registry_names_become_families() -> None:
    assert _family_names("Microsoft JhengHei & Microsoft JhengHei UI (TrueType)", "msjh.ttc") == [
        "Microsoft JhengHei", "Microsoft JhengHei UI"]
    assert _family_names("Noto Sans TC Bold (TrueType)", "NotoSansTC-Bold.otf") == ["Noto Sans TC"]
    assert _family_names("Courier 10,12,15", "COURE.FON") == []


@needs_ffmpeg
def test_burnt_in_box_lands_where_the_caption_was_dragged(tmp_path: Path) -> None:
    p = _project()
    ops.apply_ops(p, [
        {"op": "captionStyle.add", "item": {
            "id": "cs_aaaaaa01", "size": 80, "color": "#ff0000", "outline": False, "shadow": False,
            "box": True, "boxColor": "#00ff00", "boxOpacity": 1, "boxPadding": 30}},
        {"op": "project.update", "patch": {"settings": {"captionStyle": "cs_aaaaaa01"}}},
        {"op": "caption.update", "id": "c_aaaaaa01", "patch": {"text": "一一一一", "x": 0.25, "y": 0.25}},
    ], {})
    w, h = 640, 360
    ass = build_ass(p, [], {"burnCaptions": True, "captionSize": 52}, w, h)
    (tmp_path / "t.ass").write_text(ass, encoding="utf-8")
    proc = subprocess.run([find_ffmpeg(), "-v", "error", "-f", "lavfi", "-i", f"color=c=0x0000ff:s={w}x{h}:d=1",
                           "-vf", "ass=filename=t.ass", "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                          cwd=tmp_path, capture_output=True, check=True)
    frame = proc.stdout

    def pixel(fx: float, fy: float) -> tuple[int, ...]:
        i = (int(fy * h) * w + int(fx * w)) * 3
        return tuple(frame[i:i + 3])

    def near(got: tuple[int, ...], want: tuple[int, int, int]) -> bool:
        return all(abs(a - b) < 60 for a, b in zip(got, want))

    # Just above the text the green box shows (padding); the bottom row, where it used to be, stays blue.
    assert near(pixel(0.25, 0.25 - 50 / 1080), (0, 255, 0)), pixel(0.25, 0.25 - 50 / 1080)
    assert near(pixel(0.25, 0.92), (0, 0, 255))
    assert near(pixel(0.75, 0.25), (0, 0, 255))


@needs_node
def test_every_preset_is_a_valid_style_with_a_unique_name() -> None:
    presets = _presets()
    assert len(presets) >= 5
    assert len({p["name"] for p in presets}) == len(presets)
    for i, preset in enumerate(presets):
        style = opfields.clean_caption_style({**preset, "id": f"cs_preset{i:03d}"})
        assert {k: style[k] for k in preset} == preset  # nothing clamped or dropped


@needs_node
@needs_ffmpeg
def test_common_preset_matches_the_reference_frame(tmp_path: Path) -> None:
    # Reference: the owner's usual captions, glyphs 54px high with their bottom at y=1025 on a 1080p frame.
    preset = _presets()[0]
    p = _project()
    ops.apply_ops(p, [
        {"op": "captionStyle.add", "item": {**preset, "id": "cs_aaaaaa01"}},
        {"op": "project.update", "patch": {"settings": {"captionStyle": "cs_aaaaaa01"}}},
        {"op": "caption.update", "id": "c_aaaaaa01", "patch": {"text": "第 31 條第 6 項"}},
    ], {})
    w, h = 1920, 1080
    (tmp_path / "t.ass").write_text(build_ass(p, [], {"burnCaptions": True, "captionSize": 52}, w, h), encoding="utf-8")
    proc = subprocess.run([find_ffmpeg(), "-v", "error", "-f", "lavfi", "-i", f"color=c=0x808080:s={w}x{h}:d=1",
                           "-vf", "ass=filename=t.ass", "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                          cwd=tmp_path, capture_output=True, check=True)
    frame = proc.stdout
    rows = [y for y in range(h // 2, h)
            if any(min(frame[(y * w + x) * 3:(y * w + x) * 3 + 3]) > 235 for x in range(w // 4, w * 3 // 4))]
    assert 50 <= rows[-1] - rows[0] + 1 <= 58, (rows[0], rows[-1])
    assert 1020 <= rows[-1] <= 1030, rows[-1]


def test_wrap_breaks_chinese_anywhere_words_at_spaces_and_keeps_punctuation_off_line_starts() -> None:
    width = lambda text: 10.0 * len(text)  # noqa: E731  (every character 10 px)
    assert wrap_text("一二三四五六七", 50, width) == ["一二三四五", "六七"]
    assert wrap_text("一二三四五，六", 50, width) == ["一二三四", "五，六"]  # "，" pulls 五 down with it
    assert wrap_text("一二三「四五六", 40, width) == ["一二三", "「四五六"]  # an opening bracket never ends a line
    assert wrap_text("hello world foo", 60, width) == ["hello", "world", "foo"]
    assert wrap_text("supercalifragilistic", 60, width) == ["supercalifragilistic"]  # a long word stays whole
    assert wrap_text("短\n第二行", 500, width) == ["短", "第二行"]


def test_dragged_captions_wrap_toward_the_nearer_edge() -> None:
    s = {"marginH": 80, "size": 50, "align": "center"}
    assert wrap_width(s, None, 1920) == 1760
    assert wrap_width(s, (0.5, 0.5), 1920) == 1760
    assert wrap_width(s, (0.8, 0.5), 1920) == 2 * (384 - 80)
    assert wrap_width(s, (0.99, 0.5), 1920) == 150  # at least three font sizes
    assert wrap_width({**s, "align": "left"}, (0.75, 0.5), 1920) == 480 - 80
    assert wrap_width({**s, "align": "right"}, (0.25, 0.5), 1920) == 480 - 80


@needs_ffmpeg
def test_long_burnt_in_captions_stay_inside_the_frame(tmp_path: Path) -> None:
    long = "這是一句很長很長的字幕用來確認輸出時會自動換行而且不會超出畫面的左右兩側邊緣" * 2
    p = _project()
    p["captions"][1].update(start=0.0, end=1.0)  # both on screen in the rendered frame
    ops.apply_ops(p, [
        {"op": "captionStyle.add", "item": {"id": "cs_aaaaaa01", "size": 60, "outline": False, "shadow": False}},
        {"op": "project.update", "patch": {"settings": {"captionStyle": "cs_aaaaaa01"}}},
        {"op": "caption.update", "id": "c_aaaaaa01", "patch": {"text": long}},
        {"op": "caption.update", "id": "c_aaaaaa02", "patch": {"text": long, "x": 0.85, "y": 0.3}},
    ], {})
    w, h = 1920, 1080
    (tmp_path / "t.ass").write_text(build_ass(p, [], {"burnCaptions": True, "captionSize": 52}, w, h), encoding="utf-8")
    proc = subprocess.run([find_ffmpeg(), "-v", "error", "-f", "lavfi", "-i", f"color=c=black:s={w}x{h}:d=1",
                           "-vf", "ass=filename=t.ass", "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
                          cwd=tmp_path, capture_output=True, check=True)
    frame = proc.stdout
    lit = [x for y in range(h) for x in range(0, w, 2) if frame[y * w + x] > 100]
    assert min(lit) >= 80 - 4 and max(lit) <= w - 80 + 4, (min(lit), max(lit))  # inside the side margins
    rows = [y for y in range(h) if any(frame[y * w + x] > 100 for x in range(0, w, 4))]
    bands = sum(1 for a, b in zip(rows, rows[1:]) if b - a > 4) + 1
    assert bands >= 2 + 3  # the bottom caption takes two lines, the narrow dragged one at least three


@needs_node
def test_styles_added_under_a_taken_name_get_the_next_number() -> None:
    script = ("const m = await import(process.argv[1]);"
              "const p = {captionStyles: [{name: '標題'}, {name: '標題 2'}]};"
              "console.log(JSON.stringify(['標題', '白字'].map((n) => m.addStyleOps(p, null, n).list[0].item.name)));")
    module = PRESETS_JS.with_name("caption-style.js").as_uri()
    proc = subprocess.run(["node", "--input-type=module", "-e", script, module],
                          capture_output=True, check=True, text=True, encoding="utf-8")
    assert json.loads(proc.stdout) == ["標題 3", "白字"]
