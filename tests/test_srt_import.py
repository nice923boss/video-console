"""匯入 SRT: an .srt file's subtitles land at the times it gives (plus offset=), keeping their line breaks, in UTF-8,
UTF-16 or Big5, as one undo step even past the per-batch op limit; replace=1 drops the project's old captions first,
and voices= says what happens to their voice-overs."""
from __future__ import annotations

from test_server import Client, app, client, project  # noqa: F401  (server fixtures)
from vc.opfields import MAX_OPS
from vc.sequence import VOICE_TRACK
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
    assert [(c["start"], c["end"], c["text"]) for c in got] == [(1.0, 3.5, "港口的早晨"), (62.25, 64.0, "漁船出港\n第二行")]
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
    assert sorted(c["text"] for c in captions(client, pid)) == ["港口的早晨", "漁船出港\n第二行"]


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


def test_offset_moves_the_cues_to_the_playhead_and_bad_options_are_refused(client: Client, project: dict) -> None:
    pid = project["id"]
    res = client.json("POST", url(pid) + "?offset=10", SRT.encode("utf-8"))
    assert res["added"] == 2 and res["voiced"] == []
    assert sorted((c["start"], c["end"]) for c in captions(client, pid)) == [(11.0, 13.5), (72.25, 74.0)]
    for bad in ("?offset=abc", "?voices=all"):
        code, _, _ = client.request("POST", url(pid) + bad, SRT.encode("utf-8"))
        assert code == 400


def test_more_cues_than_one_batch_holds_are_still_one_undo_step(client: Client, project: dict) -> None:
    pid = project["id"]
    n = MAX_OPS + 100
    text = "".join(f"{i + 1}\n00:00:{i // 100:02d},{i % 100 * 10:03d} --> 00:00:{i // 100:02d},{i % 100 * 10 + 5:03d}\n句{i}\n\n"
                   for i in range(n))
    assert client.json("POST", url(pid), text.encode("utf-8"))["added"] == n
    assert len(captions(client, pid)) == n
    client.json("POST", f"/api/projects/{pid}/undo")
    assert captions(client, pid) == []
    client.json("POST", f"/api/projects/{pid}/redo")
    assert len(captions(client, pid)) == n


def voiced_project() -> dict:
    """One old caption spoken by the narrator with a generated voice-over, one without a speaker."""
    return {
        "captions": [{"id": "c_old000001", "start": 0.5, "end": 3.0, "text": "舊", "speaker": "c_narrator0"},
                     {"id": "c_old000002", "start": 60.0, "end": 70.0, "text": "舊二"}],
        "audio": [{"id": "a_vo0000001", "mediaId": "m_vo0000001", "captionId": "c_old000001", "start": 0.5}],
        "media": {"m_vo0000001": {"id": "m_vo0000001", "source": {"backend": "audio8voice"}}},
    }


def added(ops: list[dict]) -> list[tuple[str, str | None]]:
    return [(op["item"]["text"], op["item"].get("speaker")) for op in ops if op["op"] == "caption.add"]


def test_replace_moves_the_old_voice_overs_to_the_trash_by_default() -> None:
    ops = srt_ops(voiced_project(), SRT, replace=True)
    assert {"op": "audio.delete", "id": "a_vo0000001"} in ops
    assert {"op": "media.update", "id": "m_vo0000001", "patch": {"trashed": True}} in ops  # AU-14
    assert added(ops) == [("港口的早晨", None), ("漁船出港\n第二行", None)]


def test_replace_can_keep_the_voice_overs_as_plain_clips() -> None:
    ops = srt_ops(voiced_project(), SRT, replace=True, voices="keep")
    assert ops[0] == {"op": "audio.update", "id": "a_vo0000001", "patch": {"captionId": None, "track": VOICE_TRACK}}
    assert not any(op["op"] in ("audio.delete", "media.delete", "media.update") for op in ops)


def test_regen_gives_each_new_caption_the_speaker_it_overlaps_most() -> None:
    ops = srt_ops(voiced_project(), SRT, replace=True, voices="regen")
    assert {"op": "audio.delete", "id": "a_vo0000001"} in ops
    assert added(ops) == [("港口的早晨", "c_narrator0"), ("漁船出港\n第二行", None)]  # the 60 s caption has no speaker
    # shifted past the old caption, nothing overlaps it any more
    assert added(srt_ops(voiced_project(), SRT, replace=True, offset=5, voices="regen"))[0] == ("港口的早晨", None)
