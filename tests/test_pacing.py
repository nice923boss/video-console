"""Detailed script breakdown: story beats split into several sizes, paced by the story and timed to the narration."""
from __future__ import annotations

import re

import pytest

from test_llm import CHAT, completion, llm  # noqa: F401  (llm fixtures)
from test_server import Client, app, client, project  # noqa: F401  (server fixtures)
from vc.pacing import (beat_fields, beat_pace, break_down, fill_coverage, fit_narration, pace_notes, speech_seconds,
                       time_beats)
from vc.script import parse_script

SCRIPT = """1. 深夜來電：夜晚昏暗的客廳，一位六十多歲的阿姨坐在沙發上，茶几上的手機亮起來電，暖黃檯燈。
旁白：一通電話，可能讓一輩子的積蓄不見。

2. 假檢警來電：中景，阿姨把手機貼在耳邊，眉頭緊皺，背景是客廳窗戶與夜色。
旁白：「這裡是地檢署，你的帳戶涉及洗錢，馬上配合調查！」

3. 按下掛斷：特寫：一隻手的手指按下手機螢幕上的紅色掛斷鍵，淺景深。
旁白：第一步，先掛斷。對方再急，也不用馬上回答。

4. 女兒陪伴：三十多歲的女兒坐在阿姨身旁握著她的手，兩人相視而笑，溫暖色調。
旁白：媽，有事一定先跟我說。
"""
SIZES = ("遠景", "全景", "廣角", "中景", "近景", "特寫", "反應鏡頭")


def detailed(text: str = SCRIPT) -> dict:
    return break_down(parse_script(text, "auto"))


def words(text: str) -> str:
    return re.sub(r"\W", "", text)


def size_of(shot: dict) -> str:
    return next(k for k in SIZES if shot["camera"].startswith(k) or shot["camera"].startswith("側面" + k)
                or shot["camera"].startswith("插入" + k))


def test_each_beat_becomes_several_shots_of_different_sizes() -> None:
    plain = parse_script(SCRIPT, "auto")["shots"]
    shots = detailed()["shots"]

    assert len(plain) == 4 and len(shots) >= 2 * len(plain)
    for beat in range(1, 5):
        cover = [s for s in shots if s["beat"] == beat]
        assert 2 <= len(cover) <= 5
        assert len({s["camera"].split("，")[0] for s in cover}) == len(cover)  # no size twice in a beat
    assert {"全景", "中景", "特寫"} <= {size_of(s) for s in shots}
    # the size stands at the start of the camera field and of the picture
    assert all(s["prompt"].startswith(s["camera"].split("，")[0] + "：") for s in shots)


def test_the_size_the_script_asks_for_opens_its_beat_with_the_writers_picture() -> None:
    shots = detailed()["shots"]
    call = [s for s in shots if s["beat"] == 2]
    hangup = [s for s in shots if s["beat"] == 3]

    assert call[0]["camera"].startswith("中景") and call[0]["prompt"] == "中景：阿姨把手機貼在耳邊，眉頭緊皺，背景是客廳窗戶與夜色"
    assert call[0]["name"] == "假檢警來電（中景）"
    # a close-up of a finger is the beat's insert: no face is added to it and no second insert follows
    assert hangup[0]["prompt"] == "特寫：一隻手的手指按下手機螢幕上的紅色掛斷鍵，淺景深"
    assert not any("插入特寫" in s["camera"] for s in hangup)


def test_pace_follows_the_story_and_sets_shot_lengths() -> None:
    shots = detailed()["shots"]
    paces = {s["beat"]: s["pace"] for s in shots}

    assert paces == {1: "slow", 2: "fast", 3: "fast", 4: "slow"}
    slow = [s["duration"] for s in shots if s["pace"] == "slow"]
    fast = [s["duration"] for s in shots if s["pace"] == "fast"]
    assert sum(fast) / len(fast) < sum(slow) / len(slow)
    assert min(slow) >= 2.0 and min(fast) >= 0.8
    assert len({s["duration"] for s in shots}) >= 5


@pytest.mark.parametrize("text, index, count, pace", [
    ("突然一陣大浪打來，他驚慌地往後跑", 1, 3, "fast"),
    ("兩人相視而笑，溫暖色調", 1, 3, "slow"),
    ("阿姨戴上眼鏡拿起手機準備撥號", 1, 3, "medium"),
    ("阿姨戴上眼鏡拿起手機準備撥號", 0, 3, "slow"),  # openings and endings lean slow
    ("撥打 165 反詐騙專線", 1, 3, "medium"),  # 詐騙 names the topic, it is not a chase
])
def test_beat_pace_reads_the_words(text: str, index: int, count: int, pace: str) -> None:
    assert beat_pace(text, index, count) == pace


def test_narration_is_split_across_the_beat_and_each_shot_lasts_long_enough_to_read_it() -> None:
    result = parse_script(SCRIPT, "auto")
    shots = break_down(result)["shots"]

    for beat, source in enumerate(result["shots"], 1):
        parts = [s["caption"] for s in shots if s["beat"] == beat]
        assert words("".join(parts)) == words(source["caption"])  # nothing lost, nothing repeated
        assert sum(bool(p) for p in parts) >= 2
    for s in shots:
        if s["caption"]:
            assert s["duration"] >= speech_seconds(s["caption"]) + 0.2


def test_narration_parts_keep_every_word_in_order() -> None:
    shots = detailed()["shots"]
    joined = "".join(s["caption"] for s in shots if s["beat"] == 3)
    assert [w for w in ("第一步", "先掛斷", "對方再急", "也不用馬上回答") if w in joined] == \
        ["第一步", "先掛斷", "對方再急", "也不用馬上回答"]
    assert joined.index("第一步") < joined.index("先掛斷") < joined.index("對方再急")


def test_a_beat_with_its_own_length_stays_one_shot() -> None:
    result = detailed("1. 清晨的漁港，漁船靜靜停泊。5秒\n2. 小女孩跑過碼頭\n")

    timed = [s for s in result["shots"] if s["beat"] == 1]
    assert len(timed) == 1 and timed[0]["duration"] == 5.0 and timed[0]["prompt"] == "清晨的漁港，漁船靜靜停泊"
    assert len([s for s in result["shots"] if s["beat"] == 2]) >= 2
    assert "1 段已寫明秒數，維持一個鏡頭" in result["notes"]


def test_a_script_without_narration_still_gets_paced_shots() -> None:
    text = "清晨的海邊，一位老人慢慢走在沙灘上。\n\n突然一陣大浪打來，他驚慌地往後跑。\n\n孫女跑過來抱住他，兩人相視而笑。"
    shots = detailed(text)["shots"]

    assert len(shots) >= 6 and all(s["caption"] == "" and s["duration"] for s in shots)
    assert shots[0]["camera"].startswith("遠景")  # the opening sets the place, outdoors
    assert {s["pace"] for s in shots if s["beat"] == 2} == {"fast"}


def test_breakdown_stops_at_the_shot_limit() -> None:
    text = "\n\n".join(f"一位老人坐在海邊的長椅上，望著第 {i} 艘船。" for i in range(80))
    result = detailed(text)
    assert len(result["shots"]) == 200
    assert any("超過 200 個鏡頭" in n for n in result["notes"])


def test_pace_notes_and_beat_fields() -> None:
    shots = [{"beat": 1, "pace": "slow"}, {"beat": 1, "pace": "slow"}, {"beat": 2, "pace": "fast"}]
    assert pace_notes(shots) == ["依劇情節奏分成 2 段、3 個鏡頭：舒緩 1 段、緊湊 1 段"]
    assert pace_notes([{"beat": None}]) == []
    assert beat_fields({"beat": 3, "pace": "FAST"}) == {"beat": 3, "pace": "fast"}
    assert beat_fields({"beat": True, "pace": "quick"}) == {}
    assert beat_fields({"beat": 0}) == {} and beat_fields("雜訊") == {}


def test_fit_narration_lengthens_a_shot_too_short_for_its_caption() -> None:
    shots = [{"caption": "一通電話可能讓一輩子的積蓄不見", "duration": 1.0}, {"caption": "", "duration": 0.8},
             {"caption": "先掛斷", "duration": 3.0}]
    # 15 characters at 4 a second plus a beat of 0.2, rounded up: 3.95 -> 4.0
    assert [s["duration"] for s in fit_narration(shots)] == [4.0, 0.8, 3.0]
    assert fit_narration([{"caption": "先掛斷再說", "duration": 1.2}])[0]["duration"] == 1.5  # 1.45 rounds up


# ---------------------------------------------------------------- API
def test_parse_api_breaks_down_only_when_asked(client: Client, project: dict) -> None:
    plain = client.json("POST", "/api/script/parse", {"text": SCRIPT})
    detail = client.json("POST", "/api/script/parse", {"text": SCRIPT, "detail": True})

    assert len(plain["shots"]) == 4 and "beat" not in plain["shots"][0]
    assert len(detail["shots"]) >= 8 and detail["mode"] == plain["mode"]
    assert detail["notes"][0].startswith("依劇情節奏分成 4 段")
    applied = client.json("POST", f"/api/projects/{project['id']}/script/apply", {"shots": detail["shots"]})
    saved = client.json("GET", f"/api/projects/{project['id']}")["project"]
    assert applied["added"] == len(detail["shots"])
    assert all("beat" not in s and "pace" not in s for s in saved["shots"])
    assert [s["duration"] for s in saved["shots"]] == [s["duration"] for s in detail["shots"]]


# ---------------------------------------------------------------- text model
DETAIL_REPLY = """```json
[
  {"beat": 1, "pace": "slow", "name": "客廳", "prompt": "全景：昏暗客廳", "camera": "全景，緩慢推進",
   "caption": "一通電話", "duration": 3},
  {"beat": 1, "pace": "slow", "name": "阿姨", "prompt": "特寫：阿姨的臉", "camera": "特寫，緩慢推近",
   "caption": "可能讓一輩子的積蓄不見", "duration": 1.5},
  {"beat": 2, "pace": "fast", "name": "掛斷", "prompt": "插入特寫：手指按下掛斷鍵", "camera": "插入特寫",
   "caption": "", "duration": 0.8}
]
```"""


def test_text_model_detail_keeps_beats_and_fits_the_narration(llm, mock_http) -> None:  # noqa: F811
    mock_http.route("POST", CHAT, completion(DETAIL_REPLY))

    shots = llm.script_to_shots("深夜來電，阿姨接起電話。", detail=True)

    # a slow beat runs at least 7 seconds, shared 3 : 1.5; the second shot's 11 characters need 3.0 seconds
    assert [(s["beat"], s["pace"], s["duration"]) for s in shots] == [(1, "slow", 4.7), (1, "slow", 3.0),
                                                                       (2, "fast", 3.0)]
    system = mock_http.calls[-1].json()["messages"][0]["content"]
    assert "At most 120 shots" in system and '"pace"' in system and "2 to 5 shots" in system


def model_shot(beat: int | None, pace: str, prompt: str, camera: str, caption: str, duration: float) -> dict:
    shot = {"name": "鏡頭", "prompt": prompt, "motion": "", "camera": camera, "caption": caption, "notes": "",
            "duration": duration}
    return {**shot, "beat": beat, "pace": pace} if beat else shot


def test_beats_the_text_model_left_as_one_shot_are_filled_by_the_rules() -> None:
    model = [
        model_shot(1, "slow", "全景，昏暗客廳", "全景，緩慢推進", "一通電話", 3.0),
        model_shot(1, "slow", "特寫，阿姨的臉", "特寫，緩慢推近", "可能讓一輩子的積蓄不見", 3.0),
        model_shot(2, "fast", "中景，阿姨把手機貼在耳邊，眉頭緊皺", "中景，極緩慢推近",
                   "這裡是地檢署，你的帳戶涉及洗錢，馬上配合調查", 4.0),
        model_shot(None, "", "警察局外觀", "", "", 3.0),
    ]
    shots, notes = fill_coverage(model)
    call = [s for s in shots if s.get("beat") == 2]

    assert shots[:2] == model[:2] and shots[-1] == model[-1]  # covered beats and shots without a beat stay
    assert 2 <= len(call) <= 5 and {s["pace"] for s in call} == {"fast"}
    assert call[0]["camera"] == "中景，極緩慢推近" and call[0]["prompt"] == "中景：阿姨把手機貼在耳邊，眉頭緊皺"
    assert words("".join(s["caption"] for s in call)) == words(model[2]["caption"])
    assert notes == ["文字模型有 1 段只給一個鏡頭，已依規則補拆"]
    assert fill_coverage(model[:2]) == (model[:2], [])


def test_text_model_shots_are_timed_by_pace_and_a_repeated_caption_is_split() -> None:
    line = "第一步，先掛斷。對方再急，也不用馬上回答"
    model = [
        model_shot(1, "fast", "特寫，手指按下掛斷鍵", "特寫，手持", f"旁白：{line}", 5.0),
        model_shot(1, "fast", "特寫，螢幕變黑", "特寫，固定鏡頭", f"旁白：{line}", 5.0),
        model_shot(2, "slow", "中景，阿姨深呼吸", "中景，固定鏡頭", "冷靜下來", 5.0),
        model_shot(2, "slow", "特寫，阿姨的臉", "特寫，緩慢推近", "才看得出破綻", 5.0),
        model_shot(None, "", "警察局外觀", "", "旁白：青峰市政府警察局，關心您", 1.0),
    ]
    shots = time_beats(model)
    fast, slow = [s["duration"] for s in shots[:2]], [s["duration"] for s in shots[2:4]]

    assert [s["caption"] for s in shots[:2]] == ["第一步，先掛斷。對方再急", "也不用馬上回答"]
    assert shots[4]["caption"] == "青峰市政府警察局，關心您" and shots[4]["duration"] == 3.0  # read, not cut
    # the fast beat's 4.75 seconds are shared evenly, the first part's 10 characters need 2.7; slow runs 7 seconds
    assert fast == [2.7, 2.4] and slow == [3.5, 3.5]


@pytest.mark.parametrize("prompt, size, picture", [
    ("Wide shot, dim living room, an elderly woman on the sofa", "全景", "dim living room, an elderly woman on the sofa"),
    ("Medium side shot, she holds the phone", "側面中景", "she holds the phone"),
    ("Close-up: finger pressing the red button", "特寫", "finger pressing the red button"),
])
def test_an_english_picture_from_the_text_model_keeps_its_size(prompt: str, size: str, picture: str) -> None:
    shots, _ = fill_coverage([model_shot(1, "slow", prompt, "", "一通電話", 3.0)])

    assert shots[0]["camera"].startswith(size) and shots[0]["prompt"] == f"{size}：{picture}"
    assert all(s["prompt"].split("：")[0] == s["camera"].split("，")[0] for s in shots)  # no 中景 on a wide shot


def test_parse_api_fills_the_text_models_single_shot_beats(app, client: Client, mock_http) -> None:  # noqa: F811
    mock_http.route("POST", CHAT, completion(DETAIL_REPLY))
    app.settings.update({"llm": {"enabled": True, "baseUrl": mock_http.url + "/v1"}})

    result = client.json("POST", "/api/script/parse", {"text": "深夜來電", "mode": "llm", "detail": True})

    assert [s["beat"] for s in result["shots"]] == [1, 1, 2, 2, 2]
    assert result["shots"][2]["camera"].startswith("插入特寫")  # the model's insert leads its beat
    assert result["notes"] == ["依劇情節奏分成 2 段、5 個鏡頭：舒緩 1 段、緊湊 1 段",
                               "文字模型有 1 段只給一個鏡頭，已依規則補拆"]
