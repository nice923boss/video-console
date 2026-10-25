"""SB-14: the storyboard read as one script, and the edited script written back to the shots field by field."""
from __future__ import annotations

from test_server import App, Client, app, client, needs_ffmpeg, project  # noqa: F401  (server fixtures)
from vc import scriptsync


def setup(client: Client, pid: str, shots: list[dict]) -> list[str]:
    """Replace the starting shot with `shots`; returns their ids in order."""
    first = client.json("GET", f"/api/projects/{pid}")["project"]["shots"][0]["id"]
    ops = [{"op": "shot.delete", "id": first}] + [{"op": "shot.add", "shot": {"id": f"s_script{i}", **s}}
                                                  for i, s in enumerate(shots)]
    client.json("POST", f"/api/projects/{pid}/ops", {"ops": ops})
    return [f"s_script{i}" for i in range(len(shots))]


def send(client: Client, pid: str, script: dict, text: str, status: int = 200) -> dict:
    return client.json("POST", f"/api/projects/{pid}/script",
                       {"text": text, "base": script["text"], "ids": script["ids"]}, status=status)


def saved(client: Client, pid: str) -> dict:
    return client.json("GET", f"/api/projects/{pid}")["project"]


def test_parse_reads_labels_continuations_and_blank_lines() -> None:
    text = ("前言不算\n#2 屋頂\n夜晚的屋頂\n  第二行\n\n  空行之後\n運鏡：推近\n字幕：甲\n  換行\n字幕：\n"
            "片長：4 秒\n\n# 新的\n畫面：海邊\n沒縮排的續行\n旁白：乙\n")
    one, two = scriptsync.parse(text)
    assert (one["number"], one["name"], one["prompt"]) == (2, "屋頂", "夜晚的屋頂\n第二行\n\n空行之後")
    assert (one["camera"], one["captions"], one["duration"]) == ("推近", ["甲\n換行", ""], "4 秒")
    assert (two["number"], two["name"], two["prompt"], two["captions"]) == (None, "新的", "海邊\n沒縮排的續行", ["乙"])


def test_render_and_parse_round_trip() -> None:
    project = {"shots": [{"id": "a", "name": "多行\n名稱", "prompt": "一\n\n二", "motion": "", "camera": "搖",
                          "notes": "備註：像標籤\n#3 像標題", "duration": 2.5}],
               "captions": [{"id": "c2", "shot": "a", "start": 1, "text": "後"},
                            {"id": "c1", "shot": "a", "start": 0, "text": "前\n換行"},
                            {"id": "c3", "shot": None, "start": 0, "text": "自由字幕"}]}
    (block,) = scriptsync.parse(scriptsync.render(project))
    assert (block["name"], block["prompt"], block["camera"], block["notes"]) == ("多行 名稱", "一\n\n二", "搖",
                                                                                  "備註：像標籤\n#3 像標題")
    assert (block["captions"], block["duration"]) == (["前\n換行", "後"], "2.5")


def test_unchanged_text_changes_nothing(client: Client, project: dict) -> None:  # noqa: F811
    pid = project["id"]
    setup(client, pid, [{"name": "甲", "prompt": "一\n二"}, {"name": "乙", "camera": "推近"}])
    script = client.json("GET", f"/api/projects/{pid}/script")
    assert script["text"].startswith("#1 甲\n描述：一\n  二\n字幕：\n片長：5\n\n#2 乙\n")
    out = send(client, pid, script, script["text"])
    assert out["changed"] is False and out["updated"] == 0


def test_only_edited_fields_are_written_and_a_shot_edit_meanwhile_stays(client: Client,  # noqa: F811
                                                                         project: dict) -> None:
    pid = project["id"]
    ids = setup(client, pid, [{"name": "甲", "prompt": "舊描述"}, {"name": "乙", "prompt": "乙描述"}])
    script = client.json("GET", f"/api/projects/{pid}/script")
    client.json("POST", f"/api/projects/{pid}/ops", {"ops": [
        {"op": "shot.update", "id": ids[0], "patch": {"camera": "手持", "notes": "分鏡上改的"}}]})
    text = script["text"].replace("描述：舊描述", "描述：新描述\n  第二行").replace("#2 乙", "#2 乙改名")
    out = send(client, pid, script, text)
    assert out["changed"] and out["updated"] == 2
    one, two = saved(client, pid)["shots"]
    assert (one["prompt"], one["camera"], one["notes"]) == ("新描述\n第二行", "手持", "分鏡上改的")
    assert (two["name"], two["prompt"]) == ("乙改名", "乙描述")
    client.json("POST", f"/api/projects/{pid}/undo")  # the whole script is one step
    one, two = saved(client, pid)["shots"]
    assert (one["prompt"], one["camera"], two["name"]) == ("舊描述", "手持", "乙")


def test_new_blocks_become_shots_and_nothing_is_deleted(client: Client, project: dict) -> None:  # noqa: F811
    pid = project["id"]
    ids = setup(client, pid, [{"name": "甲"}, {"name": "乙"}, {"name": "丙"}])
    script = client.json("GET", f"/api/projects/{pid}/script")
    first, second, third = script["text"].rstrip("\n").split("\n\n")
    text = "\n\n".join(["#開場\n描述：最前面", first, "#\n描述：甲之後\n片長：3秒", third])  # #2 left out
    out = send(client, pid, script, text)
    assert out["added"] == 2 and len(out["shotIds"]) == 2
    shots = saved(client, pid)["shots"]
    assert [s["name"] or s["prompt"] for s in shots] == ["開場", "甲", "甲之後", "乙", "丙"]
    assert [s["id"] for s in shots if s["id"] in ids] == ids and shots[2]["duration"] == 3.0


def test_a_longer_shot_moves_the_captions_after_it(client: Client, project: dict) -> None:  # noqa: F811
    pid = project["id"]
    setup(client, pid, [{"name": "甲", "duration": 4}, {"name": "乙", "duration": 4}])
    client.json("POST", f"/api/projects/{pid}/ops", {"ops": [{"op": "caption.add", "item": {
        "id": "c_freeline", "start": 5, "end": 6, "text": "自由字幕", "position": "bottom", "shot": None}}]})
    script = client.json("GET", f"/api/projects/{pid}/script")
    send(client, pid, script, script["text"].replace("片長：4", "片長：6", 1))
    (cap,) = saved(client, pid)["captions"]
    assert (cap["start"], cap["end"]) == (7.0, 8.0)


def test_caption_lines_add_change_and_remove_captions(client: Client, project: dict) -> None:  # noqa: F811
    pid = project["id"]
    ids = setup(client, pid, [{"name": "甲", "duration": 4}, {"name": "乙", "duration": 2}])
    script = client.json("GET", f"/api/projects/{pid}/script")
    out = send(client, pid, script, script["text"].replace("字幕：", "字幕：第一句\n字幕：第二句", 1))
    assert out["captions"] == 2
    caps = scriptsync.shot_captions(saved(client, pid), ids[0])
    assert [(c["text"], c["start"], c["end"]) for c in caps] == [("第一句", 0, 2), ("第二句", 2, 4)]
    script = client.json("GET", f"/api/projects/{pid}/script")
    text = script["text"].replace("字幕：第一句\n", "字幕：\n").replace("第二句", "改過的第二句")
    send(client, pid, script, text)
    caps = scriptsync.shot_captions(saved(client, pid), ids[0])
    assert [(c["text"], c["start"], c["end"]) for c in caps] == [("改過的第二句", 2, 4)]
    script = client.json("GET", f"/api/projects/{pid}/script")  # no room after it: the last caption shares its time
    send(client, pid, script, script["text"].replace("字幕：改過的第二句", "字幕：改過的第二句\n字幕：第三句"))
    caps = scriptsync.shot_captions(saved(client, pid), ids[0])
    assert [(c["text"], c["start"], c["end"]) for c in caps] == [("改過的第二句", 2, 3), ("第三句", 3, 4)]


def test_mistakes_are_reported_with_their_line(client: Client, project: dict) -> None:  # noqa: F811
    pid = project["id"]
    ids = setup(client, pid, [{"name": "甲", "duration": 1}, {"name": "乙"}])
    script = client.json("GET", f"/api/projects/{pid}/script")
    first, second = script["text"].rstrip("\n").split("\n\n")
    err = send(client, pid, script, "\n\n".join([first, first]), status=400)
    assert "#1" in err["error"] and "兩次" in err["error"]
    err = send(client, pid, script, script["text"].replace("片長：5", "片長：很久"), status=400)
    assert "第 6 行" in err["error"] and "很久" in err["error"]
    text = script["text"].replace("字幕：", "字幕：一\n字幕：二\n字幕：三", 1)
    err = send(client, pid, script, text, status=400)
    assert "#1 甲" in err["error"] and "時間不夠" in err["error"]
    client.json("POST", f"/api/projects/{pid}/ops", {"ops": [{"op": "shot.delete", "id": ids[1]}]})
    out = send(client, pid, script, script["text"].replace("#2 乙", "#2 乙改名"))
    assert out["changed"] is False and "已被刪除" in out["notes"][0]
