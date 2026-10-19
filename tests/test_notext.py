"""Generated pictures carry no lettering: title and subtitle directions are dropped, the sent prompt forbids text."""
from __future__ import annotations

import pytest

from vc.notext import GUARD, NEGATIVE_TERMS, strip_text_directions, with_guard, with_text_negative


@pytest.mark.parametrize("prompt, cleaned, dropped", [
    ("【空拍運鏡】\n1. 高空俯瞰稻田與校舍。\n5. 標題字卡浮現\ncinematic film still, natural light",
     "【空拍運鏡】\n1. 高空俯瞰稻田與校舍。\ncinematic film still, natural light", ["標題字卡浮現"]),
    ("【字卡】\n1. 畫面淡出，出現標語字卡。", "1. 畫面淡出", ["【字卡】", "出現標語字卡"]),
    ("1. 孩子們圍坐。\n3. 跳出關鍵字卡「平等」、「不受歧視」。", "1. 孩子們圍坐。", ["跳出關鍵字卡「平等」、「不受歧視」"]),
    ("海港清晨，下方上字幕：早安", "海港清晨", ["下方上字幕：早安"]),
    ("Harbour at dawn. A title card appears, boats drift", "Harbour at dawn. boats drift", ["A title card appears"]),
    ("Boats at dusk with on-screen text and subtitles", "", ["Boats at dusk with on-screen text and subtitles"]),
], ids=["title-card-line", "bracket-and-clause", "keyword-card", "subtitle", "english-clause", "english-all"])
def test_title_and_subtitle_directions_are_dropped(prompt: str, cleaned: str, dropped: list[str]) -> None:
    assert strip_text_directions(prompt) == (cleaned, dropped)


def test_a_prompt_without_directions_is_unchanged() -> None:
    prompt = "清晨漁港，薄霧，漁船\n\ncinematic film still, photorealistic, shallow depth of field"
    assert strip_text_directions(prompt) == (prompt, [])


def test_the_sent_prompt_ends_with_the_no_text_clause() -> None:
    assert with_guard("清晨漁港") == f"清晨漁港\n{GUARD}"
    assert with_guard("") == ""  # a picture-only video sends no prompt at all
    assert "subtitles" in GUARD and "title cards" in GUARD and "watermarks" in GUARD


def test_negative_gains_only_the_missing_no_text_terms() -> None:
    default = "blurry, low quality, watermark, text, logo, signature"
    assert with_text_negative(default) == f"{default}, subtitles, captions, title card, letters"
    assert with_text_negative("") == ", ".join(NEGATIVE_TERMS)
    assert with_text_negative("rough texture").endswith(", text, subtitles, captions, title card, letters, watermark, logo")
    assert with_text_negative(with_text_negative("cartoon")) == with_text_negative("cartoon")
