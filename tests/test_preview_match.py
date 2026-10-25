"""The preview shows what the export writes (QA-04). A grid of colours goes through a colour grade three ways
(the preview's SVG filter in Chromium, the export's 3D LUT in ffmpeg, and vc.grade.grader as the reference
model) and then once end to end: the edit page's preview against a frame of a draft export. Skipped when
Playwright or its Chromium is missing (pip install playwright, then python -m playwright install chromium)."""
from __future__ import annotations

import re
import struct
import subprocess
import zlib
from pathlib import Path
from typing import Any, Iterator

import pytest

from test_server import Client, needs_ffmpeg
from test_web_ui import app, client  # noqa: F401  (fixtures)
from vc.grade import build_cube, clean_grade, grader
from vc.media import Media, find_ffmpeg

sync_api = pytest.importorskip("playwright.sync_api")
pytestmark = needs_ffmpeg

WEB = Path(__file__).resolve().parents[1] / "app" / "web"
LEVELS = [0, 64, 128, 191, 255]
COLOURS = [(r, g, b) for b in LEVELS for g in LEVELS for r in LEVELS] + [(40, 40, 40), (110, 110, 110), (220, 220, 220)]
NX, NY = 16, 8  # 128 cells
GRADES = {
    "warm": {"temp": 8000, "tint": 20},
    "wheels": {"shadows": {"r": 0.3, "bright": 0.1}, "highlights": {"bright": -0.3, "b": 0.2}, "midtones": {"bright": 0.4}},
    "mixed": {"temp": 7500, "shadows": {"r": 0.2}, "midtones": {"bright": 0.2}, "highlights": {"sat": 0.6}},
}


def grid_png(w: int, h: int) -> bytes:
    rows = []
    for y in range(h):
        row = bytearray(b"\0")
        for x in range(w):
            row += bytes(COLOURS[(y * NY // h) * NX + x * NX // w])
        rows.append(bytes(row))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(b"".join(rows))) + chunk(b"IEND", b"")


def png_size(png: bytes) -> tuple[int, int]:
    return struct.unpack(">II", png[16:24])  # the screenshot's real size, not the element box


def rgb(args: list[str], data: bytes | None = None) -> bytes:
    """Decode with ffmpeg to raw RGB (a video by its colour tags)."""
    out = subprocess.run([find_ffmpeg(), "-v", "error", *args, "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                         input=data, capture_output=True, check=True)
    return out.stdout


def cell(raw: bytes, w: int, h: int, i: int) -> list[int]:
    """Mean colour of a 5x5 patch in the middle of grid cell i."""
    cx, cy = int((i % NX + 0.5) * w / NX), int((i // NX + 0.5) * h / NY)
    acc = [0, 0, 0]
    for dy in range(-2, 3):
        for dx in range(-2, 3):
            o = ((cy + dy) * w + cx + dx) * 3
            for k in range(3):
                acc[k] += raw[o + k]
    return [round(a / 25) for a in acc]


def expected(grade: dict) -> list[list[int]]:
    ref = grader(grade)
    return [[round(v * 255) for v in ref(*(c / 255 for c in colour))] for colour in COLOURS]


def worst(a: list[list[int]], b: list[list[int]]) -> int:
    return max(abs(x - y) for p, q in zip(a, b) for x, y in zip(p, q))


@pytest.fixture(scope="module")
def browser() -> Iterator[Any]:
    with sync_api.sync_playwright() as pw:
        try:
            chromium = pw.chromium.launch(args=["--force-color-profile=srgb"])
        except sync_api.Error as err:
            pytest.skip(f"Playwright's Chromium is not installed: {err}")
        yield chromium
        chromium.close()


def test_preview_filter_and_export_lut_follow_the_grade_model(browser: Any, tmp_path: Path) -> None:
    w, h = NX * 8, NY * 8
    picture = grid_png(w, h)
    page = browser.new_page(viewport={"width": 400, "height": 300})
    html = ("<!doctype html><body style='margin:0'><svg width='0' height='0' style='position:absolute'>"
            "<defs id='defs'></defs></svg><img id='pic' src='/grid.png'></body>")

    def serve(route: Any) -> None:
        url = route.request.url
        if url.endswith("/grid.png"):
            route.fulfill(body=picture, content_type="image/png")
        elif url.endswith("/grade.js"):
            route.fulfill(body=(WEB / "js/lib/grade.js").read_text(encoding="utf-8"), content_type="text/javascript")
        else:
            route.fulfill(body=html, content_type="text/html")

    page.route("http://grade.test/**", serve)
    page.goto("http://grade.test/")
    page.wait_for_function("document.getElementById('pic').complete")
    raw = rgb(["-i", "-"], picture)
    for name, settings in GRADES.items():
        grade = clean_grade(settings)
        page.evaluate("""async (g) => { const m = await import('/grade.js');
            document.getElementById('defs').innerHTML = m.filterMarkup('f', g);
            document.getElementById('pic').style.filter = 'url(#f)'; }""", grade)
        page.wait_for_timeout(100)
        shot = page.locator("#pic").screenshot()
        sw, sh = png_size(shot)
        preview = [cell(rgb(["-i", "-"], shot), sw, sh, i) for i in range(len(COLOURS))] if (sw, sh) == (w, h) else None
        assert preview, (sw, sh)
        cube = tmp_path / f"{name}.cube"
        cube.write_text(build_cube(grade), encoding="utf-8")
        lut_path = str(cube).replace("\\", "/").replace(":", "\\:")
        out = rgb(["-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}", "-i", "-",
                   "-vf", f"lut3d=file='{lut_path}':interp=tetrahedral"], raw)
        lut = [cell(out, w, h, i) for i in range(len(COLOURS))]
        model = expected(grade)
        # measured: preview <= 3 levels off the model, the 33-point LUT <= 8 on strong grades
        assert worst(preview, model) <= 4, name
        assert worst(lut, model) <= 8, name
    page.close()


def test_edit_preview_matches_the_exported_frame(browser: Any, app: Any, client: Client) -> None:
    grade = clean_grade(GRADES["mixed"])
    project = client.json("POST", "/api/projects", {"name": "色彩比對"})["project"]
    pid, sid = project["id"], project["shots"][0]["id"]
    w, h = project["settings"]["width"], project["settings"]["height"]
    job = client.json("POST", f"/api/projects/{pid}/upload?name=grid.png&target=shot&shot={sid}", grid_png(w, h))["job"]
    assert client.wait_job(job["id"])["status"] == "done"
    client.json("POST", f"/api/projects/{pid}/ops", {"ops": [{"op": "shot.update", "id": sid, "patch": {"grade": grade}}]})
    job = client.json("POST", "/api/jobs", {"kind": "export", "pid": pid, "payload": {"options": {"quality": "draft"}}})["job"]
    done = client.wait_job(job["id"], timeout=300)
    assert done["status"] == "done", done
    video = app.store.project_dir(pid) / done["result"]["file"]

    info = subprocess.run([find_ffmpeg(), "-hide_banner", "-i", str(video)], capture_output=True, text=True,
                          encoding="utf-8", errors="replace").stderr
    stream = next(line for line in info.splitlines() if "Video:" in line)
    assert "yuv420p(tv, bt709," in stream  # tagged, so every player reads it the same way
    vw, vh = (int(v) for v in re.search(r", (\d+)x(\d+)", stream).groups())
    frame = rgb(["-ss", "1", "-i", str(video), "-frames:v", "1"])
    exported = [cell(frame, vw, vh, i) for i in range(len(COLOURS))]

    page = browser.new_page(viewport={"width": 1440, "height": 900})
    page.goto(f"http://127.0.0.1:{app.server.port}/#edit")
    page.wait_for_selector(".boot-screen", state="detached", timeout=15000)
    page.wait_for_timeout(1500)
    shot = page.locator(".pl-host").screenshot()
    sw, sh = png_size(shot)
    preview = [cell(rgb(["-i", "-"], shot), sw, sh, i) for i in range(len(COLOURS))]
    page.close()
    # measured 6 levels (4:2:0 chroma and the LUT); before QA-04 the export read as players read it was 35-39 off
    assert worst(exported, expected(grade)) <= 8
    assert worst(preview, exported) <= 10


def test_ken_burns_and_frame_grabs_keep_the_colours(tmp_path: Path) -> None:
    media = Media(find_ffmpeg())
    src = tmp_path / "grid.png"
    src.write_bytes(grid_png(320, 160))
    clip = tmp_path / "kb.mp4"
    media.kenburns(src, clip, 320, 160, 24, 1.0, "static")
    info = subprocess.run([find_ffmpeg(), "-hide_banner", "-i", str(clip)], capture_output=True, text=True,
                          encoding="utf-8", errors="replace").stderr
    assert "yuv420p(tv, bt709," in info
    still = tmp_path / "still.png"
    media.frame_at(clip, 0.5, still)
    grabbed = rgb(["-i", str(still)])
    got = [cell(grabbed, 320, 160, i) for i in range(len(COLOURS))]
    want = [list(c) for c in COLOURS]
    assert worst(got, want) <= 12  # 4:2:0 chroma on 20 px cells; a BT.601/709 mix-up is 35+ off
