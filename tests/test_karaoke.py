"""CP-13 karaoke captions: the voice-over's per-character times on the timeline, the ASS \\k export and a real
libass render, the preview's lit count (node), the charTimes field and the backfill of older voice-overs."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from vc import ops
from vc.backfill import missing
from vc.export import build_ass
from vc.karaoke import voice_starts
from vc.media import find_ffmpeg
from vc.util import AppError

needs_ffmpeg = pytest.mark.skipif(find_ffmpeg() is None, reason="ffmpeg is not installed")
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
KARAOKE_JS = Path(__file__).resolve().parents[1] / "app" / "web" / "js" / "lib" / "karaoke.js"


def _project(text: str = "你好，世界", times: list | None = None, highlight: bool = True) -> dict:
    """A 1 s caption at 2 s voiced by a clip that starts at 2 s; 你 0.0, 好 0.2, 世 0.5, 界 0.7 into the file."""
    return {
        "id": "p_test000001", "name": "t", "settings": {"width": 1920, "height": 1080, "fps": 24,
                                                        "captionStyle": "cs_aaaaaa01"},
        "shots": [], "takes": {}, "layers": [], "overlays": [], "tracks": {}, "cast": [],
        "captionStyles": [{"id": "cs_aaaaaa01", "name": "卡拉", "size": 120, "color": "#ffffff", "outline": False,
                           "shadow": False, "highlight": highlight, "highlightColor": "#ff0000", "x": 0.5, "y": 0.5}],
        "media": {"m_voice00001": {"id": "m_voice00001", "kind": "audio", "file": "media/import/v.wav",
                                   "source": {"backend": "voai", "text": text},
                                   "charTimes": times if times is not None else [0.0, 0.2, None, 0.5, 0.7]}},
        "audio": [{"id": "a_voice00001", "mediaId": "m_voice00001", "start": 2.0, "captionId": "c_aaaaaa01"}],
        "captions": [{"id": "c_aaaaaa01", "start": 2.0, "end": 3.0, "text": text}],
    }


def _text_layer(p: dict) -> str:
    ass = build_ass(p, [], {"burnCaptions": True, "captionSize": 52}, 1920, 1080)
    return next(line for line in ass.splitlines() if line.startswith("Dialogue: 3,"))


def test_starts_follow_the_clip_and_punctuation_lights_with_the_character_before() -> None:
    p = _project()
    assert voice_starts(p, p["captions"][0]) == [2.0, 2.2, 2.2, 2.5, 2.7]
    p["audio"][0].update(start=1.0, **{"in": 0.5, "speed": 2.0})  # trimmed and played twice as fast
    assert voice_starts(p, p["captions"][0]) == [0.75, 0.85, 0.85, 1.0, 1.1]


def test_no_karaoke_without_a_current_voice() -> None:
    cap = lambda p: p["captions"][0]  # noqa: E731
    p = _project()
    p["captions"][0]["text"] = "你好，地球"  # edited after voicing: the times belong to other words
    assert voice_starts(p, cap(p)) is None
    p = _project()
    p["audio"][0]["disabled"] = True
    assert voice_starts(p, cap(p)) is None
    p = _project()
    del p["media"]["m_voice00001"]["charTimes"]  # not analysed yet
    assert voice_starts(p, cap(p)) is None
    assert voice_starts(_project(times=[None] * 5), cap(_project())) is None  # silent voice


def test_export_puts_a_k_tag_before_every_character() -> None:
    line = _text_layer(_project())
    assert r"\1c&H0000FF&\1a&H00&\2c&HFFFFFF&\2a&H00&" in line  # lit red, unlit white
    tags = re.findall(r"\{[^}]*\\k(\d+)\}([^{]*)", line)
    assert tags[0] == ("0", "")  # nothing before the first character
    assert [t for t in tags[1:]] == [("20", "你"), ("0", "好"), ("30", "，"), ("20", "世"), ("30", "界")]
    plain = _text_layer(_project(highlight=False))
    assert "\\k" not in plain and plain.endswith("你好，世界")
    stale = _project()
    stale["media"]["m_voice00001"]["source"]["text"] = "別的話"
    assert "\\k" not in _text_layer(stale)


def test_spaces_dropped_at_a_line_break_keep_their_time() -> None:
    text = "AAAA BBBB AAAA BBBB AAAA BBBB AAAA BBBB AAAA BBBB AAAA BBBB AAAA BBBB AAAA BBBB AAAA BBBB AAAA BBBB AAAA BBBB AAAA BBBB"
    times = [round(i * 0.005, 3) for i in range(len(text))]
    p = _project(text, times)
    p["captions"][0]["end"] = 4.0
    line = _text_layer(p)
    assert "\\N" in line  # wrapped
    total = sum(int(k) for k in re.findall(r"\\k(\d+)", line))
    assert total == 200  # every \k adds up to the caption's length, whatever the wrap dropped


@needs_ffmpeg
def test_libass_lights_the_characters_already_said(tmp_path: Path) -> None:
    text = "一二三四"
    p = _project(text, [0.0, 0.25, 0.75, 0.9])
    w, h = 640, 360
    ass = build_ass(p, [], {"burnCaptions": True, "captionSize": 52}, w, h)
    (tmp_path / "t.ass").write_text(ass, encoding="utf-8")
    # 2.5 s: 一 and 二 said, 三 and 四 not yet.
    proc = subprocess.run([find_ffmpeg(), "-v", "error", "-f", "lavfi", "-i", f"color=c=0x0000ff:s={w}x{h}:d=3",
                           "-vf", "ass=filename=t.ass", "-ss", "2.5", "-frames:v", "1", "-f", "rawvideo",
                           "-pix_fmt", "rgb24", "-"], cwd=tmp_path, capture_output=True, check=True)
    frame = proc.stdout

    def count(x0: int, x1: int, want: tuple[int, int, int]) -> int:
        n = 0
        for y in range(h // 2 - 30, h // 2 + 30):
            for x in range(x0, x1):
                i = (y * w + x) * 3
                n += all(abs(a - b) < 60 for a, b in zip(frame[i:i + 3], want))
        return n

    red, white = (255, 0, 0), (255, 255, 255)
    assert count(240, 320, red) > 50 and count(240, 320, white) == 0, (count(240, 320, red), count(240, 320, white))
    assert count(320, 400, white) > 50 and count(320, 400, red) == 0


@needs_node
def test_preview_lights_the_same_characters() -> None:
    p = _project()
    script = ("const m = await import(process.argv[1]); const p = JSON.parse(process.argv[2]);"
              "const c = p.captions[0];"
              "console.log(JSON.stringify([m.voiceStarts(p, c), ...[1.9, 2.0, 2.3, 2.6, 2.95].map((t) => m.litCount(p, c, t))]));")
    proc = subprocess.run(["node", "--input-type=module", "-e", script, KARAOKE_JS.as_uri(), json.dumps(p)],
                          capture_output=True, check=True, text=True, encoding="utf-8")
    starts, *lit = json.loads(proc.stdout)
    assert starts == voice_starts(p, p["captions"][0])
    assert lit == [0, 1, 3, 4, 5]


def test_char_times_are_derived_facts_with_checked_values() -> None:
    p = _project()
    del p["media"]["m_voice00001"]["charTimes"]
    ops.apply_ops(p, [{"op": "item.derive", "id": "m_voice00001", "patch": {"charTimes": [0, 0.2, None, 0.5, 0.7]}}], {})
    assert p["media"]["m_voice00001"]["charTimes"] == [0.0, 0.2, None, 0.5, 0.7]
    for bad in ([-1], ["0.1"], [True], "0.1", [0.1] * 1001):
        with pytest.raises(AppError):
            ops.apply_ops(p, [{"op": "item.derive", "id": "m_voice00001", "patch": {"charTimes": bad}}], {})


def test_backfill_queues_voice_overs_without_times() -> None:
    p = _project()
    assert missing(p) == []
    del p["media"]["m_voice00001"]["charTimes"]
    assert missing(p) == ["m_voice00001"]
    p["media"]["m_voice00001"]["source"] = {"backend": "import"}  # a plain sound file: nothing to align
    assert missing(p) == []
