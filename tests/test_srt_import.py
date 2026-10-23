"""匯入 SRT: an .srt file's subtitles land at the times it gives, in UTF-8, UTF-16 or Big5, as one undo step;
replace=1 drops the project's old captions first."""
from __future__ import annotations

from test_server import Client, app, client, project  # noqa: F401  (server fixtures)
from vc.stt import srt_ops, srt_text

SRT = """1
00:00:01,000 --> 00:00:03,500
<i>港口的早晨</i>

2
00:01:02,250 --> 00:01:04,000
{\\an8}漁船出港
第二行

3
00:00:05,000 --> 00:00:06,000
<font color="#ffffff"> </font>
"""


def url(pid: str, replace: bool = False) -> str:
    return f"/api/projects/{pid}/captions.srt" + ("?replace=1" if replace else "")


def captions(client: Client, pid: str) -> list[dict]:
    return client.json("GET", f"/api/projects/{pid}")["project"]["captions"]


def test_cues_land_at_their_own_times_without_tags(client: Client, project: dict) -> None:
    pid = project["id"]
    code, _, body = client.request("POST", url(pid), SRT.encode("utf-8-sig"))
    assert code == 200, body
    got = sorted(captions(client, pid), key=lambda c: c["start"])
    assert [(c["start"], c["end"], c["text"]) for c in got] == [(1.0, 3.5, "港口的早晨"), (62.25, 64.0, "漁船出港 第二行")]
    assert all(c["position"] == "bottom" and not c.get("shot") for c in got)


def test_import_is_one_undo_step(client: Client, project: dict) -> None:
    pid = project["id"]
    assert client.json("POST", url(pid), SRT.encode("utf-8"))["added"] == 2
    client.json("POST", f"/api/projects/{pid}/undo")
    assert captions(client, pid) == []


def test_replace_drops_old_captions_and_keep_adds_beside_them(client: Client, project: dict) -> None:
    pid = project["id"]
    old = {"id": "c_old000001", "start": 10, "end": 12, "text": "舊字幕", "position": "bottom"}
    client.json("POST", f"/api/projects/{pid}/ops", {"ops": [{"op": "caption.add", "item": old}]})
    client.json("POST", url(pid), SRT.encode("utf-8"))
    assert len(captions(client, pid)) == 3
    client.json("POST", url(pid, replace=True), SRT.encode("utf-8"))
    assert sorted(c["text"] for c in captions(client, pid)) == ["港口的早晨", "漁船出港 第二行"]


def test_file_without_cues_is_refused(client: Client, project: dict) -> None:
    pid = project["id"]
    code, _, body = client.request("POST", url(pid), "這不是字幕檔".encode("utf-8"))
    assert code == 400 and "沒有可匯入的字幕" in body.decode("utf-8")
    old = {"id": "c_old000001", "start": 10, "end": 12, "text": "舊字幕", "position": "bottom"}
    client.json("POST", f"/api/projects/{pid}/ops", {"ops": [{"op": "caption.add", "item": old}]})
    code, _, _ = client.request("POST", url(pid, replace=True), b"")
    assert code == 400
    assert [c["text"] for c in captions(client, pid)] == ["舊字幕"]  # a failed replace keeps them


def test_encodings() -> None:
    assert srt_text(SRT.encode("utf-16")) == SRT  # with its BOM
    assert srt_text(SRT.encode("utf-8-sig")) == SRT
    assert srt_text(SRT.encode("cp950")) == SRT


def test_ops_round_times_and_cut_long_text() -> None:
    text = "1\n00:00:00,1234 --> 00:00:02,0004\n" + "字" * 600 + "\n"
    (op,) = srt_ops({"captions": []}, text, replace=False)
    assert op["item"]["start"] == 0.123 and op["item"]["end"] == 2.0 and len(op["item"]["text"]) == 500
