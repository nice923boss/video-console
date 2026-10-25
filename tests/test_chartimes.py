"""CP-13: per-character start times estimated from a voice-over WAV and its known text."""
from __future__ import annotations

import math
import struct
import wave
from pathlib import Path

from vc.chartimes import align, frame_db, number_syllables, weights

RATE = 24000


def write_wav(path: Path, parts: list[tuple[float, float]]) -> None:
    """parts: (seconds, amplitude) runs of a 220 Hz tone; amplitude 0 is silence."""
    samples: list[int] = []
    for seconds, amp in parts:
        for k in range(int(seconds * RATE)):
            samples.append(int(amp * 32767 * math.sin(2 * math.pi * 220 * k / RATE)))
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(struct.pack(f"<{len(samples)}h", *samples))


def test_each_syllable_starts_near_where_it_sounds(tmp_path: Path) -> None:
    # 你 0.20, 好 0.43 after a soft dip, a real pause, then 世 0.93 and 界 1.16 after another dip.
    wav = tmp_path / "line.wav"
    write_wav(wav, [(0.2, 0), (0.2, 0.5), (0.03, 0.02), (0.2, 0.5), (0.3, 0), (0.2, 0.5), (0.03, 0.02), (0.2, 0.5),
                    (0.2, 0)])
    times = align("你好，世界。", frame_db(wav))
    assert times[2] is None and times[5] is None  # punctuation takes no time
    for got, want in zip([times[0], times[1], times[3], times[4]], [0.2, 0.43, 0.93, 1.16]):
        assert abs(got - want) <= 0.06, (times, want)


def test_silent_audio_or_nothing_to_say_gives_no_times(tmp_path: Path) -> None:
    wav = tmp_path / "quiet.wav"
    write_wav(wav, [(1.0, 0)])
    assert align("你好。", frame_db(wav)) == [None, None, None]
    write_wav(wav, [(0.5, 0.5)])
    assert align("。。", frame_db(wav)) == [None, None]


def test_numbers_and_english_count_their_spoken_syllables() -> None:
    assert [number_syllables(n) for n in (7, 10, 15, 60, 25, 105, 2026)] == [1, 1, 2, 2, 3, 4, 6]
    w = weights("用 AI 做 2 個影片\n好")
    assert w[2] == w[3] == 1.0  # A, I spelled out
    assert w[1] == w[4] == w[12] == 0.0  # spaces and the line break
    assert sum(weights("P5.js")) == 5.0  # P, 5, dot, j, s
