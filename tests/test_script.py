"""Script import without AI: tables, numbered lists, labels, free text, durations and limits."""
from __future__ import annotations

import io
import zipfile
from xml.sax.saxutils import escape

import pytest

from vc.script import MAX_SHOTS, docx_to_text, normalize_shot, parse_duration, parse_script
from vc.util import AppError


def brief(shot: dict) -> dict:
    return {k: v for k, v in shot.items() if v}


def make_docx(*blocks: str | list[list[str]]) -> bytes:
    """A minimal Word file: a string is a paragraph, a list of rows a table (a line break in a cell, a paragraph)."""
    def paras(text: str) -> str:
        return "".join(f'<w:p><w:r><w:t xml:space="preserve">{escape(ln)}</w:t></w:r></w:p>' for ln in text.split("\n"))

    def table(rows: list[list[str]]) -> str:
        return "<w:tbl>" + "".join(
            "<w:tr>" + "".join(f"<w:tc>{paras(c)}</w:tc>" for c in row) + "</w:tr>" for row in rows) + "</w:tbl>"

    body = "".join(paras(b) if isinstance(b, str) else table(b) for b in blocks)
    xml = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document xmlns:w='
           f'"http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>{body}</w:body></w:document>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("word/document.xml", xml)
    return buf.getvalue()


# Shot size and camera move in brackets after the description (project "新專案 10/18").
BRACKETED = ("清晨的海港，薄霧中漁船緩緩出港，海鷗盤旋。（遠景，緩慢推進）", "老船長站在船頭，皺紋裡藏著海風，望向遠方。（中景，固定鏡頭）",
             "太陽躍出海平面，金色光芒灑滿甲板。（特寫轉全景，向上搖鏡）")
BRACKETED_SHOTS = [{"prompt": "清晨的海港，薄霧中漁船緩緩出港，海鷗盤旋。（遠景）", "motion": "緩慢推進"},
                   {"prompt": "老船長站在船頭，皺紋裡藏著海風，望向遠方。（中景）", "motion": "固定鏡頭"},
                   {"prompt": "太陽躍出海平面，金色光芒灑滿甲板。（特寫轉全景）", "motion": "向上搖鏡"}]
# Picture / sound shot table from a Word script (07-宜蘭縣政府-我們身邊的隱形圍牆-腳本.docx): 內容 names the
# shot, 影部 is the picture, 聲部 holds the spoken lines with their speakers, music and sound effects.
AV_HEAD = ["內容", "影部", "聲部", "長度", "效果字＆備註"]
AV_ROWS = [
    ["教室\n午餐時間", "【生活情境】\n1. 中午鐘聲響起。\n2. 鏡頭橫搖，同學們併桌吃飯。", "音樂：輕快的校園日常配樂。", "",
     "音效：下課鐘聲"],
    ["教室\n同學邀約", "【溫馨互動】\n1. 陳凱拿出便當。", "同學A：「欸，陳凱，快來我們這邊啦！」\n陳凱：「好啊！」", "", ""],
    ["操場隊伍旁\n耳語", "同學D湊到隊長耳邊。", "同學D：(壓低聲音對隊長說)「不要選那個尤哈啦。」", "4秒",
     "效果字：「名字好奇怪」泡泡框浮現。"],
]
AV_TABLE = "\n".join("\t".join(f'"{c}"' if "\n" in c else c for c in row) for row in [AV_HEAD, *AV_ROWS]) + "\n"
AV_SHOTS = [
    {"name": "教室 午餐時間", "prompt": "【生活情境】\n1. 中午鐘聲響起。\n2. 鏡頭橫搖，同學們併桌吃飯",
     "notes": "音樂：輕快的校園日常配樂。\n音效：下課鐘聲"},
    {"name": "教室 同學邀約", "prompt": "【溫馨互動】\n1. 陳凱拿出便當", "caption": "欸，陳凱，快來我們這邊啦！\n好啊！",
     "notes": "說話者：同學A／陳凱"},
    {"name": "操場隊伍旁 耳語", "prompt": "同學D湊到隊長耳邊", "caption": "不要選那個尤哈啦", "duration": 4.0,
     "notes": "說話者：同學D(壓低聲音對隊長說)\n效果字：「名字好奇怪」泡泡框浮現。"},
]

CASES = {
    "numbered": (
        "海港的一天\n\n1. 清晨的漁港，薄霧籠罩，漁船靜靜停泊。鏡頭緩慢推進。5秒\n"
        "2. 穿黃色雨衣的小女孩跑過碼頭，她說：「爸爸回來了！」\n3、燈塔在夕陽下發光，海鷗飛過，空拍緩慢前進，約8秒\n",
        "numbered",
        [{"prompt": "清晨的漁港，薄霧籠罩，漁船靜靜停泊", "motion": "鏡頭緩慢推進", "duration": 5.0},
         {"prompt": "穿黃色雨衣的小女孩跑過碼頭", "caption": "爸爸回來了！"},
         {"prompt": "燈塔在夕陽下發光，海鷗飛過", "motion": "空拍緩慢前進", "duration": 8.0}],
    ),
    "markdown table": (
        "| 鏡號 | 景別 | 畫面描述 | 運鏡 | 秒數 | 旁白 |\n|---|---|---|---|---|---|\n"
        "| 1 | 遠景 | 清晨漁港，薄霧 | 緩慢推進 | 5 | 新的一天開始了 |\n"
        "| 2 | 特寫 | 小女孩的笑臉<br>雨滴落下 | 固定 | 3秒 | |\n"
        "| 3 | 全景 | 燈塔夕照 | 空拍 | 00:10-00:18 | 燈塔守護著港口 |\n",
        "table",
        [{"prompt": "遠景，清晨漁港，薄霧", "camera": "緩慢推進", "caption": "新的一天開始了", "duration": 5.0},
         {"prompt": "特寫，小女孩的笑臉\n雨滴落下", "camera": "固定", "duration": 3.0},
         {"prompt": "全景，燈塔夕照", "camera": "空拍", "caption": "燈塔守護著港口", "duration": 8.0}],
    ),
    "spreadsheet paste": (
        "編號\t畫面\t動作\t時長\n1\t清晨漁港\t漁船緩緩出港\t4\n2\t市場叫賣\t人群走動\t6\n",
        "table",
        [{"prompt": "清晨漁港", "motion": "漁船緩緩出港", "duration": 4.0},
         {"prompt": "市場叫賣", "motion": "人群走動", "duration": 6.0}],
    ),
    "spreadsheet without header": (
        "1\t開場\t清晨的漁港，薄霧籠罩，漁船靜靜停泊\t漁船緩緩出港\t5\n2\t市場\t熱鬧的魚市場，攤販叫賣\t人群走動\t3\n",
        "table",
        [{"name": "開場", "prompt": "清晨的漁港，薄霧籠罩，漁船靜靜停泊", "motion": "漁船緩緩出港", "duration": 5.0},
         {"name": "市場", "prompt": "熱鬧的魚市場，攤販叫賣", "motion": "人群走動", "duration": 3.0}],
    ),
    "spreadsheet cell with line break": (
        "編號\t畫面\t旁白\n1\t\"清晨漁港\n薄霧\"\t早安\n2\t燈塔\t晚安\n",
        "table",
        [{"prompt": "清晨漁港\n薄霧", "caption": "早安"}, {"prompt": "燈塔", "caption": "晚安"}],
    ),
    "labelled fields": (
        "鏡頭1：港口清晨\n畫面：清晨的漁港，薄霧籠罩\n運鏡：緩慢推進\n旁白：新的一天開始了\n秒數：5\n\n"
        "鏡頭2\n畫面：小女孩奔跑\n動作：她跑過碼頭\n時間：黃昏\n",
        "numbered",
        [{"name": "港口清晨", "prompt": "清晨的漁港，薄霧籠罩", "camera": "緩慢推進", "caption": "新的一天開始了",
          "duration": 5.0},
         {"prompt": "黃昏，小女孩奔跑", "motion": "她跑過碼頭"}],
    ),
    "labels for one shot": (
        "畫面：清晨漁港\n運鏡：推進\n旁白：早安\n",
        "single",
        [{"prompt": "清晨漁港", "camera": "推進", "caption": "早安"}],
    ),
    "labels on each line": (
        "畫面：清晨漁港 運鏡：推進\n畫面：小女孩奔跑 運鏡：跟拍\n",
        "lines",
        [{"prompt": "清晨漁港", "camera": "推進"}, {"prompt": "小女孩奔跑", "camera": "跟拍"}],
    ),
    "english shots": (
        "Shot 1: A foggy harbor at dawn. Slow dolly in.\nShot 2: A girl in a yellow raincoat runs. Camera pans right.\n",
        "numbered",
        [{"prompt": "A foggy harbor at dawn.", "motion": "Slow dolly in."},
         {"prompt": "A girl in a yellow raincoat runs.", "motion": "Camera pans right."}],
    ),
    "chinese numerals": (
        "一、清晨漁港\n二、小女孩奔跑，鏡頭跟拍\n三、燈塔夕照\n",
        "numbered",
        [{"prompt": "清晨漁港"}, {"prompt": "小女孩奔跑", "motion": "鏡頭跟拍"}, {"prompt": "燈塔夕照"}],
    ),
    "bracketed numbers": (
        "【鏡頭1】清晨漁港\n【鏡頭2】小女孩奔跑\n",
        "numbered",
        [{"prompt": "清晨漁港"}, {"prompt": "小女孩奔跑"}],
    ),
    "dash after shot number": (
        "鏡頭1–清晨漁港\n鏡頭2—小女孩奔跑\n",
        "numbered",
        [{"prompt": "清晨漁港"}, {"prompt": "小女孩奔跑"}],
    ),
    "markdown bullets": (
        "- **鏡頭 1**：清晨漁港，鏡頭緩慢推進\n- **鏡頭 2**：小女孩奔跑\n",
        "numbered",
        [{"prompt": "清晨漁港", "motion": "鏡頭緩慢推進"}, {"prompt": "小女孩奔跑"}],
    ),
    "scene headings around numbered shots": (
        "第一場 港口\n1. 清晨漁港\n2. 小女孩奔跑\n第二場 燈塔\n3. 燈塔夕照\n",
        "numbered",
        [{"prompt": "清晨漁港"}, {"prompt": "小女孩奔跑"}, {"prompt": "燈塔夕照"}],
    ),
    "scene headings only": (
        "第一場 港口\n清晨漁港，薄霧\n第二場 燈塔\n燈塔夕照\n",
        "numbered",
        [{"name": "港口", "prompt": "清晨漁港，薄霧"}, {"name": "燈塔", "prompt": "燈塔夕照"}],
    ),
    "timecodes": (
        "00:00-00:05 清晨漁港\n00:05-00:12 小女孩奔跑，鏡頭跟拍\n",
        "numbered",
        [{"prompt": "清晨漁港", "duration": 5.0}, {"prompt": "小女孩奔跑", "motion": "鏡頭跟拍", "duration": 7.0}],
    ),
    "timecodes with dashes": (
        "00:00–00:05 清晨漁港\n00:05—00:12 小女孩奔跑\n",
        "numbered",
        [{"prompt": "清晨漁港", "duration": 5.0}, {"prompt": "小女孩奔跑", "duration": 7.0}],
    ),
    "paragraphs": (
        "清晨的漁港，薄霧籠罩。\n\n小女孩跑過碼頭，鏡頭向右平移。\n\n燈塔在夕陽下發光。",
        "paragraphs",
        [{"prompt": "清晨的漁港，薄霧籠罩"}, {"prompt": "小女孩跑過碼頭", "motion": "鏡頭向右平移"},
         {"prompt": "燈塔在夕陽下發光"}],
    ),
    "sentences": (
        "清晨的漁港薄霧籠罩，漁船靜靜停泊。小女孩跑過碼頭，喊著爸爸。燈塔在夕陽下發光。",
        "sentences",
        [{"prompt": "清晨的漁港薄霧籠罩，漁船靜靜停泊"}, {"prompt": "小女孩跑過碼頭，喊著爸爸"},
         {"prompt": "燈塔在夕陽下發光"}],
    ),
    "english sentences": (
        "A foggy harbor at dawn. A girl in a yellow raincoat runs along the pier. The lighthouse glows.",
        "sentences",
        [{"prompt": "A foggy harbor at dawn."}, {"prompt": "A girl in a yellow raincoat runs along the pier."},
         {"prompt": "The lighthouse glows."}],
    ),
    "indented lines": (
        "\t清晨漁港\n\t小女孩奔跑\n",
        "lines",
        [{"prompt": "清晨漁港"}, {"prompt": "小女孩奔跑"}],
    ),
    "one sentence": ("一個穿黃色雨衣的小女孩在雨中奔跑", "single", [{"prompt": "一個穿黃色雨衣的小女孩在雨中奔跑"}]),
    "decimal is not a shot number": ("3.5 秒的畫面很長\n", "single", [{"prompt": "3.5 秒的畫面很長"}]),
    "words that look like a number": ("鏡頭十分穩定，畫面很美\n", "single", [{"prompt": "鏡頭十分穩定，畫面很美"}]),
    "camera move first": (
        "鏡頭緩慢推進，清晨的漁港，薄霧籠罩", "single", [{"prompt": "清晨的漁港，薄霧籠罩", "motion": "鏡頭緩慢推進"}],
    ),
    "length after the last sentence": (
        "An 80s style diner at night, neon signs. 5s\n",
        "single",
        [{"prompt": "An 80s style diner at night, neon signs.", "duration": 5.0}],
    ),
    "size and move in brackets": ("\n".join(BRACKETED), "lines", BRACKETED_SHOTS),
    "size and move in brackets on one line": ("".join(BRACKETED), "sentences", BRACKETED_SHOTS),
    "half-width brackets": (
        "A foggy harbor at dawn (wide shot, slow dolly in).",
        "single",
        [{"prompt": "A foggy harbor at dawn (wide shot).", "motion": "slow dolly in"}],
    ),
    "square brackets": (
        "1. 清晨的海港，海鷗盤旋。【遠景，緩慢推進】\n2. 燈塔夕照\n",
        "numbered",
        [{"prompt": "清晨的海港，海鷗盤旋。【遠景】", "motion": "緩慢推進"}, {"prompt": "燈塔夕照"}],
    ),
    "camera move with a bracketed path": (
        "清晨的漁港，薄霧籠罩。鏡頭緩慢推進（越過碼頭，停在燈塔）。小女孩跑過碼頭。",
        "sentences",
        [{"prompt": "清晨的漁港，薄霧籠罩", "motion": "鏡頭緩慢推進（越過碼頭，停在燈塔）"}, {"prompt": "小女孩跑過碼頭"}],
    ),
    "picture and sound table": (AV_TABLE, "table", AV_SHOTS),
    "內容 column alone is the picture": (
        "鏡號\t內容\t秒數\n1\t清晨漁港\t5\n2\t燈塔夕照\t3\n",
        "table",
        [{"prompt": "清晨漁港", "duration": 5.0}, {"prompt": "燈塔夕照", "duration": 3.0}],
    ),
    "影像／聲音 header": (
        "鏡號\t影像\t聲音\n1\t清晨漁港\t陳凱：「早安！」\n2\t燈塔夕照\t音樂：輕快的配樂\n",
        "table",
        [{"prompt": "清晨漁港", "caption": "早安！", "notes": "說話者：陳凱"},
         {"prompt": "燈塔夕照", "notes": "音樂：輕快的配樂"}],
    ),
    "畫面／聲音 header": (
        "畫面\t聲音\n小女孩跑過碼頭\t小女孩：(大喊)「爸爸回來了！」\n燈塔夕照\t燈塔守護著港口\n",
        "table",
        [{"prompt": "小女孩跑過碼頭", "caption": "爸爸回來了！", "notes": "說話者：小女孩(大喊)"},
         {"prompt": "燈塔夕照", "caption": "燈塔守護著港口"}],
    ),
    "Video／Audio header in any case": (
        "VIDEO\taudio\nA foggy harbor at dawn\tSFX: waves on the pier\nThe lighthouse glows\tThe harbor sleeps.\n",
        "table",
        [{"prompt": "A foggy harbor at dawn", "notes": "SFX: waves on the pier"},
         {"prompt": "The lighthouse glows", "caption": "The harbor sleeps"}],
    ),
    "聲音： in free text is not a label": (
        "畫面：清晨漁港\n聲音：海浪拍岸\n旁白：早安\n",
        "single",
        [{"prompt": "清晨漁港\n聲音：海浪拍岸", "caption": "早安"}],
    ),
    # A title-card direction is an editing note: out of the picture, into the notes, even when it is all there is.
    "title card direction in the 影像 column": (
        '鏡號\t影像\t聲音\t秒數\n1\t"校園全景，陽光灑落。\n5. 標題字卡浮現"\t新學期開始了\t5\n2\t片名字卡浮現\t\t3\n',
        "table",
        [{"prompt": "校園全景，陽光灑落", "caption": "新學期開始了", "notes": "標題字卡浮現", "duration": 5.0},
         {"notes": "片名字卡浮現", "duration": 3.0}],
    ),
    "title card direction in a numbered list": (
        "1. 清晨漁港，字幕打上地名，鏡頭緩慢推進\n2. 燈塔夕照\n",
        "numbered",
        [{"prompt": "清晨漁港", "motion": "鏡頭緩慢推進", "notes": "字幕打上地名"}, {"prompt": "燈塔夕照"}],
    ),
    "title card direction after the camera move": (
        "1. 清晨漁港，鏡頭緩慢推進，字幕浮現\n2. 燈塔夕照，運鏡：推進，標題字卡浮現\n",
        "numbered",
        [{"prompt": "清晨漁港", "motion": "鏡頭緩慢推進", "notes": "字幕浮現"},
         {"prompt": "燈塔夕照", "camera": "推進", "notes": "標題字卡浮現"}],
    ),
}


@pytest.mark.parametrize("text, mode, shots", CASES.values(), ids=CASES.keys())
def test_auto_mode_reads_common_formats(text, mode, shots):
    result = parse_script(text)

    assert result["mode"] == mode
    assert [brief(s) for s in result["shots"]] == shots


def test_untitled_heading_line_is_reported():
    result = parse_script(CASES["numbered"][0])

    assert result["notes"] == ["開頭 1 行沒有編號，已當成標題略過"]


def test_quoted_speech_becomes_the_caption():
    shots = parse_script("1. 小明說：「你好」\n2. 她大喊：「爸爸！」，鏡頭推近\n")["shots"]

    assert [(s["caption"], s["motion"]) for s in shots] == [("你好", ""), ("爸爸！", "鏡頭推近")]


@pytest.mark.parametrize("mode", ["lines", "paragraphs", "sentences"])
def test_forced_split_modes(mode):
    result = parse_script(CASES["paragraphs"][0], mode)

    assert result["mode"] == mode
    assert [(s["prompt"], s["motion"]) for s in result["shots"]] == [
        ("清晨的漁港，薄霧籠罩", ""), ("小女孩跑過碼頭", "鏡頭向右平移"), ("燈塔在夕陽下發光", "")]


def test_forced_single_keeps_the_whole_text_in_one_shot():
    [shot] = parse_script(CASES["paragraphs"][0], "single")["shots"]

    assert shot["prompt"] == "清晨的漁港，薄霧籠罩。\n小女孩跑過碼頭\n燈塔在夕陽下發光"
    assert shot["motion"] == "鏡頭向右平移"


@pytest.mark.parametrize("value, seconds", [
    ("5", 5.0), ("5秒", 5.0), ("5 s", 5.0), ("約5秒", 5.0), ("0:05", 5.0), ("1.5s", 1.5),
    ("3-5秒", 5.0), ("3–5秒", 5.0), ("2~4", 4.0), ("3到6秒", 6.0), ("00:05-00:10", 5.0),
    ("1:30", 60.0), ("120", 60.0),
    ("黃昏", None), ("0", None), ("4—", None),
])
def test_parse_duration(value, seconds):
    assert parse_duration(value) == seconds


def test_normalize_shot_cleans_ai_output():
    shot = normalize_shot({"name": "A", "prompt": ["清晨", "漁港"], "caption": "「早安。」", "duration": "6秒"})

    assert shot == {"name": "A", "prompt": "清晨\n漁港", "motion": "", "camera": "", "caption": "早安",
                    "notes": "", "duration": 6.0}


@pytest.mark.parametrize("data", [{"prompt": "", "motion": None}, "x", None, []])
def test_normalize_shot_drops_unusable_items(data):
    assert normalize_shot(data) is None


def test_normalize_shot_ignores_boolean_duration():
    assert normalize_shot({"prompt": "p", "duration": True})["duration"] is None


@pytest.mark.parametrize("text, mode, message", [
    ("", "auto", "請先貼上劇本或分鏡文字"),
    ("   ", "auto", "請先貼上劇本或分鏡文字"),
    ("x" * 100_001, "auto", "文字太長了"),
    ("abc", "weird", "拆分方式不正確"),
], ids=["empty", "blank", "too-long", "bad-mode"])
def test_bad_input_is_refused(text, mode, message):
    with pytest.raises(AppError, match=message):
        parse_script(text, mode)


def test_long_lists_are_capped_with_a_note():
    result = parse_script("\n".join(f"{i}. 畫面 {i}" for i in range(1, 251)))

    assert len(result["shots"]) == MAX_SHOTS
    assert result["notes"] == [f"一次最多匯入 {MAX_SHOTS} 個鏡頭，其餘已略過"]


def test_word_shot_tables_are_read_as_one_table():
    data = make_docx("宜蘭縣政府-兒童權利公約系列-我們身邊的隱形圍牆", "●角色設定：",
                     [["長度：約 3 分鐘", "製作：喵魔人"]],
                     "【開場】", [AV_HEAD, AV_ROWS[0]],
                     "【第一幕：午餐時間的距離】", [AV_HEAD, *AV_ROWS[1:]])

    result = docx_to_text(data)

    assert [brief(s) for s in parse_script(result["text"])["shots"]] == AV_SHOTS
    assert result["notes"] == ["讀入 2 個分鏡表，共 3 列", "另有 1 個表格沒有匯入（沒有標題列或欄位不同）",
                               "表格外的 4 段文字沒有匯入（例如標題、說明、段落標題）"]


def test_word_file_without_a_shot_table_gives_its_paragraphs():
    result = docx_to_text(make_docx("1. 清晨漁港", "", "2. 燈塔夕照"))

    assert result == {"text": "1. 清晨漁港\n\n2. 燈塔夕照", "notes": []}


@pytest.mark.parametrize("data, message", [
    (b"not a zip", "無法讀取這個 Word 檔"),
    (make_docx(""), "這個 Word 檔沒有文字"),
], ids=["not-docx", "empty"])
def test_bad_word_files_are_refused(data, message):
    with pytest.raises(AppError, match=message):
        docx_to_text(data)
