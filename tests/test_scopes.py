"""GR-07 scopes (lib/scopes.js): the luma waveform and the vectorscope worked out from RGBA pixels, Rec.709 as the
export writes it."""
from __future__ import annotations

import math
import shutil

import pytest

from test_timeline_keys import JS, run_js

KR, KB = 0.2126, 0.0722
SCOPES = JS / "lib" / "scopes.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


def chroma(r: float, g: float, b: float) -> tuple[float, float]:
    y = KR * r + (1 - KR - KB) * g + KB * b
    return (b - y) / (2 * (1 - KB)), (r - y) / (2 * (1 - KR))


def pixels(*rgb: tuple[int, int, int]) -> list[int]:
    return [v for p in rgb for v in (*p, 255)]


def lit(counts: list[int], cols: int) -> dict[tuple[int, int], int]:
    """The cells with pixels in them, as (row, col): count."""
    return {(k // cols, k % cols): n for k, n in enumerate(counts) if n}


def test_waveform_rows_run_white_to_black() -> None:
    # 4 x 2 picture, left half black, right half white, top right mid grey.
    data = pixels((0, 0, 0), (0, 0, 0), (128, 128, 128), (255, 255, 255),
                  (0, 0, 0), (0, 0, 0), (255, 255, 255), (255, 255, 255))
    got = run_js(SCOPES, "console.log(JSON.stringify(Array.from(m.waveform(data, 4, 2, 2))));", data)
    assert lit(got, 2) == {(255, 0): 4, (0, 1): 3, (255 - 128, 1): 1}


def test_vectorscope_places_colours_by_cb_and_cr() -> None:
    data = pixels((255, 0, 0), (128, 128, 128), (0, 0, 255))
    got = run_js(SCOPES, "console.log(JSON.stringify(Array.from(m.vectorscope(data, 256))));", data)
    want: dict[tuple[int, int], int] = {}
    for rgb in [(1, 0, 0), (128 / 255,) * 3, (0, 0, 1)]:
        cb, cr = chroma(*rgb)
        cell = (math.floor((0.5 - cr) * 255 + 0.5), math.floor((0.5 + cb) * 255 + 0.5))
        want[cell] = want.get(cell, 0) + 1
    assert lit(got, 256) == want
    assert (0, 98) in want  # pure red: Cr at the top edge, a little left of the middle
    assert (128, 128) in want  # grey has no colour


def test_targets_are_the_75_percent_bars() -> None:
    got = run_js(SCOPES, "console.log(JSON.stringify({ targets: m.TARGETS, skin: m.SKIN_ANGLE }));", None)
    targets = {label: (cb, cr) for label, cb, cr in got["targets"]}
    assert targets["紅"] == pytest.approx(chroma(0.75, 0, 0))
    assert targets["黃"] == pytest.approx(chroma(0.75, 0.75, 0))
    for a, b in [("紅", "青"), ("綠", "洋紅"), ("藍", "黃")]:  # complements sit opposite each other
        assert targets[a] == pytest.approx(tuple(-v for v in targets[b]))
    assert all(math.hypot(cb, cr) < 0.5 for cb, cr in targets.values())  # inside the circle
    skin = math.degrees(math.atan2(*reversed(chroma(0.87, 0.65, 0.53))))
    assert abs(skin - got["skin"]) < 5  # a typical skin tone lies on the skin line


def test_paint_lights_cells_by_count() -> None:
    got = run_js(SCOPES, """console.log(JSON.stringify({
      one: Array.from(m.paint(new Uint32Array([0, 1, 10, 40]), 10, [10, 20, 30])),
      each: Array.from(m.paint(new Uint32Array([1, 1]), 1, [[1, 2, 3], [4, 5, 6]])),
      colors: m.vectorColors(3) }));""", None)
    one = [got["one"][i:i + 4] for i in range(0, 16, 4)]
    assert one[0] == [0, 0, 0, 0]  # nothing there stays clear
    assert one[1][:3] == [10, 20, 30] and 64 <= one[1][3] < 255  # one pixel still shows
    assert one[2][3] == one[3][3] == 255  # full at `full` pixels, no brighter past it
    assert got["each"] == [1, 2, 3, 255, 4, 5, 6, 255]
    assert got["colors"][4] == [153, 153, 153]  # the middle cell has no colour
    assert got["colors"][1][0] > got["colors"][1][2]  # top middle (Cr up, Cb 0) leans red over blue
