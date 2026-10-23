"""Detailed breakdown of a storyboard table laid out by its writer: numbered actions, spoken lines, camera notes."""
from __future__ import annotations

import pytest

from test_server import Client, app, client  # noqa: F401  (server fixtures)
from vc.beats import plan, share
from vc.pacing import break_down
from vc.script import parse_script

HEAD = ["內容", "影部", "聲部", "長度", "效果字＆備註"]
# Rows written the way the two real scripts are (礁溪社福-兒少安全, 宜蘭縣政府-我們身邊的隱形圍牆).
ROWS = [
    ["學校門口\n放學時分", "【空拍運鏡】\n1. 空拍學校門口，家長在門外等待。\n2. 小宜和小蘭手牽手走出校門。\n3. (特寫) 小蘭的書包吊飾晃動。",
     "旁白：放學了，大家都要平安回家。\n小宜：「妹妹，走慢一點！」", "", ""],
    ["馬路邊\n突發狀況", "【驚險瞬間】\n1. 一輛機車從轉角衝出來。\n2. 小宜一把拉住小蘭。", "小宜：(大喊)「小心！」", "", "音效：喇叭聲"],
    ["馬路邊\n幻想", "【幻想情境】\n1. 小蘭想像自己變成超人。\n2. 飛過馬路。\n3. 擋住機車。\n4. 大家拍手。", "", "", ""],
    ["家中客廳\n資訊圖卡", "【資訊圖卡】\n1. 圖卡一：看左右。\n2. 圖卡二：走斑馬線。", "旁白：過馬路前先看左右，再走斑馬線，就能平安回到家。", "", ""],
    ["教室\n午餐時間", "陳凱拿出便當。\n阮月安獨自坐在角落。", "陳凱：「一起吃吧！」", "", "特寫阮月安的表情\n鏡頭：用廣角顯示整間教室"],
    ["教室角落\n失落", "1. 阮月安低下頭。\n2. (特寫) 阮月安失落的眼神。", "", "", "特寫：失落的眼神"],
    ["教室角落\n分組", "場景：操場上，體育老師正在分組。\n尤哈站在一旁。", "", "", ""],
    ["教室角落\n旁白", "", "旁白：不被理解的便當、被嘲笑的名字、因為身體的限制而被排擠。這些都是看不見的牆。", "", ""],
]


def table(*rows: list[str]) -> str:
    return "\n".join("\t".join(f'"{c}"' if "\n" in c else c for c in row) for row in [HEAD, *rows]) + "\n"


def beat(number: int) -> list[tuple[str, str, str]]:
    """(camera size, picture, caption) of each shot of one beat of ROWS."""
    shots = break_down(parse_script(table(*ROWS)))["shots"]
    return [(s["camera"].split("，")[0], s["prompt"].split("：", 1)[-1], s["caption"])
            for s in shots if s["beat"] == number]


def test_each_numbered_action_is_a_shot_and_each_line_is_heard_over_its_speaker() -> None:
    # the beat opens the story from the air; the written close-up stays a close-up
    assert beat(1) == [("遠景", "空拍學校門口，家長在門外等待", "放學了，大家都要平安回家"),
                       ("中景", "小宜和小蘭手牽手走出校門", "妹妹，走慢一點！"),
                       ("特寫", "小蘭的書包吊飾晃動", "")]


def test_a_new_place_opens_on_a_wide_shot_and_the_same_place_does_not() -> None:
    assert beat(2)[0][0] == "全景"  # 學校門口 to 馬路邊
    assert beat(6)[0][0] == "中景"  # 教室 to 教室角落 is one scene
    assert beat(7)[0][:2] == ("全景", "操場上，體育老師正在分組")  # 場景： opens a scene and is not kept


def test_a_fantasy_is_a_flash_of_at_most_three_short_shots() -> None:
    shots = [s for s in break_down(parse_script(table(*ROWS)))["shots"] if s["beat"] == 3]

    assert [s["prompt"] for s in shots] == ["中景：小蘭想像自己變成超人", "中景：飛過馬路", "中景：擋住機車，大家拍手"]
    assert {s["duration"] for s in shots} == {0.8} and {s["pace"] for s in shots} == {"fast"}


def test_an_information_cards_narration_runs_across_its_panels() -> None:
    assert [c for _, _, c in beat(4)] == ["過馬路前先看左右", "再走斑馬線，就能平安回到家"]


def test_a_camera_note_is_a_shot_after_its_action_unless_an_action_already_shows_it() -> None:
    assert beat(5) == [("全景", "陳凱拿出便當", "一起吃吧！"), ("中景", "阮月安獨自坐在角落", ""),
                       ("特寫", "阮月安的表情", ""), ("遠景", "顯示整間教室", "")]
    assert beat(6) == [("中景", "阮月安低下頭", ""), ("特寫", "阮月安失落的眼神", "")]  # 特寫：失落的眼神 is there


def test_a_narrators_list_gets_a_shot_per_item_and_an_empty_picture_is_left_to_fill_in() -> None:
    shots = [s for s in break_down(parse_script(table(*ROWS)))["shots"] if s["beat"] == 8]

    assert [s["caption"] for s in shots] == ["不被理解的便當", "被嘲笑的名字", "因為身體的限制而被排擠", "這些都是看不見的牆"]
    assert {s["prompt"] for s in shots} == {""}
    assert [s["camera"].split("，")[0] for s in shots] == ["全景", "中景", "全景", "中景"]  # no size twice in a row


def test_shot_lengths_follow_the_role_and_the_line() -> None:
    first = break_down(parse_script(table(*ROWS)))["shots"][:3]

    # medium pace: an establishing shot holds twice the average 2.2, an action the average, a close-up 1.5
    assert [s["duration"] for s in first] == [4.4, 2.2, 1.5]
    assert first[0]["notes"] == "【空拍運鏡】\n說話者：旁白／小宜" and first[1]["notes"] == ""


def test_the_breakdown_keeps_no_working_fields_or_empty_brackets() -> None:
    shots = break_down(parse_script(table(*ROWS, ["結尾", "畫面淡出 ()", "", "", ""])))["shots"]

    assert all("lines" not in s for s in shots)
    assert shots[-1]["prompt"] == "全景：畫面淡出"


@pytest.mark.parametrize("shot", [
    {"prompt": "一位老人坐在海邊的長椅上，望著遠方。", "notes": ""},  # free text: the rules give it several angles
    {"prompt": "老人坐在長椅上。", "notes": "", "lines": []},  # a table row is cut as written, even one action
], ids=["free-text", "table-row"])
def test_only_a_table_row_or_numbered_actions_are_planned(shot: dict) -> None:
    assert (plan(shot) is None) == ("lines" not in shot)


def test_share_cuts_at_pauses_in_order() -> None:
    assert share("過馬路前先看左右，再走斑馬線，就能平安回到家", 2) == ["過馬路前先看左右", "再走斑馬線，就能平安回到家"]
    assert share("先掛斷", 3) == ["先掛斷", "", ""]


def test_parse_api_sends_the_lines_to_no_one(client: Client) -> None:  # noqa: F811
    text = table(*ROWS[:2])
    plain = client.json("POST", "/api/script/parse", {"text": text})
    detail = client.json("POST", "/api/script/parse", {"text": text, "detail": True})

    assert len(plain["shots"]) == 2 and len(detail["shots"]) == 5
    assert all("lines" not in s for s in plain["shots"] + detail["shots"])
