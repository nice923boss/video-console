"""AI voice-over: the cast table, the Audio8Voice proxy endpoints and the "voice" job (Audio8Voice mocked)."""
from __future__ import annotations

import io
import json
import math
import struct
import threading
import wave
import zipfile

from conftest import MockServer
from test_server import Client, _package, app, client, needs_ffmpeg, project  # noqa: F401  (shared fixtures)
from vc import net, voice as voice_module
from vc.app import App

NARRATOR = {"id": "c_narrator0", "name": "旁白", "voice": "dirb6edcb1a:凡萱", "voiceName": "凡萱", "speed": 1.2,
            "volume": 0.8}


def wav_bytes(seconds: float = 1.5, rate: int = 22050) -> bytes:
    frames = b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * 220 * i / rate)))
                      for i in range(int(seconds * rate)))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(frames)
    return buf.getvalue()


def use_audio8voice(client: Client, url: str) -> None:
    client.json("PUT", "/api/settings", {"voice": {"url": url}})


def add_line(client: Client, pid: str, text: str = "爸爸回來了！", speaker: str | None = "c_narrator0") -> str:
    cid = "x_line000001"
    client.json("POST", f"/api/projects/{pid}/ops", {"ops": [
        {"op": "cast.add", "item": NARRATOR},
        {"op": "caption.add", "item": {"id": cid, "start": 2.0, "end": 3.0, "text": text, "speaker": speaker}}]})
    return cid


def run_voice(client: Client, pid: str, cid: str) -> dict:
    job = client.json("POST", "/api/jobs", {"kind": "voice", "pid": pid, "payload": {"captionId": cid}})["job"]
    assert job["lane"] == "comfy"  # never on the GPU together with a ComfyUI render
    return client.wait_job(job["id"])


def test_deleting_a_cast_member_clears_and_undo_restores_its_captions(client: Client, project: dict) -> None:
    pid = project["id"]
    assert project["cast"] == []
    cid = add_line(client, pid)
    client.json("POST", f"/api/projects/{pid}/ops",
                {"ops": [{"op": "cast.update", "id": "c_narrator0", "patch": {"speed": 9, "name": "主角"}}]})
    saved = client.json("GET", f"/api/projects/{pid}")["project"]
    assert saved["cast"][0]["speed"] == 2.0 and saved["cast"][0]["name"] == "主角"
    client.json("POST", f"/api/projects/{pid}/ops", {"ops": [{"op": "cast.delete", "id": "c_narrator0"}]})
    saved = client.json("GET", f"/api/projects/{pid}")["project"]
    assert saved["cast"] == [] and saved["captions"][0]["speaker"] is None
    client.json("POST", f"/api/projects/{pid}/undo")
    saved = client.json("GET", f"/api/projects/{pid}")["project"]
    assert saved["cast"][0]["id"] == "c_narrator0" and saved["captions"][0]["speaker"] == "c_narrator0"
    client.json("POST", f"/api/projects/{pid}/ops",
                {"ops": [{"op": "caption.update", "id": cid, "patch": {"speaker": "bad id!"}}]}, status=400)


@needs_ffmpeg
def test_voice_job_puts_the_line_on_the_track_and_regenerating_replaces_it(
        app: App, client: Client, project: dict, mock_http: MockServer) -> None:
    pid = project["id"]
    mock_http.route("POST", "/synthesize", (200, wav_bytes(1.5)))
    use_audio8voice(client, mock_http.url + "/")
    cid = add_line(client, pid)

    done = run_voice(client, pid, cid)
    assert done["status"] == "done", done["error"]
    assert "比字幕長 0.5 秒" in done["message"]
    assert mock_http.calls[-1].json() == {"text": "爸爸回來了！", "voice": "dirb6edcb1a:凡萱", "speed": 1.2}
    saved = client.json("GET", f"/api/projects/{pid}")["project"]
    [clip] = saved["audio"]
    assert (clip["captionId"], clip["start"], clip["volume"]) == (cid, 2.0, 0.8)
    first = saved["media"][clip["mediaId"]]
    assert first["kind"] == "audio" and abs(first["duration"] - 1.5) < 0.05
    assert first["source"] == {"backend": "audio8voice", "captionId": cid, "castId": "c_narrator0",
                               "voice": "dirb6edcb1a:凡萱", "voiceName": "凡萱", "text": "爸爸回來了！", "speed": 1.2}
    assert (app.store.project_dir(pid) / first["file"]).read_bytes()[:4] == b"RIFF"

    client.json("POST", f"/api/projects/{pid}/ops",
                {"ops": [{"op": "caption.update", "id": cid, "patch": {"text": "  我回來了  "}}]})
    done = run_voice(client, pid, cid)
    assert done["status"] == "done", done["error"]
    saved = client.json("GET", f"/api/projects/{pid}")["project"]
    [clip] = saved["audio"]
    assert first["id"] not in saved["media"]
    assert saved["media"][clip["mediaId"]]["source"]["text"] == "我回來了"
    assert not list((app.store.project_dir(pid) / "media" / "cache").glob("job-*"))


def test_voice_job_refuses_lines_that_are_not_ready(client: Client, project: dict) -> None:
    pid = project["id"]
    cid = add_line(client, pid, speaker=None)

    def refuse(caption_id: str, status: int = 400) -> str:
        code, _, body = client.request("POST", "/api/jobs",
                                       {"kind": "voice", "pid": pid, "payload": {"captionId": caption_id}})
        assert code == status, body
        return json.loads(body)["error"]

    assert "指定人物" in refuse(cid)
    client.json("POST", f"/api/projects/{pid}/ops", {"ops": [
        {"op": "caption.update", "id": cid, "patch": {"speaker": "c_narrator0"}},
        {"op": "cast.update", "id": "c_narrator0", "patch": {"voice": ""}}]})
    assert "還沒有設定配音聲音" in refuse(cid)
    assert refuse("x_missing0001", 404)


def test_voice_job_explains_audio8voice_failures(client: Client, project: dict, mock_http: MockServer) -> None:
    pid = project["id"]
    cid = add_line(client, pid)
    use_audio8voice(client, "http://127.0.0.1:9")
    done = run_voice(client, pid, cid)
    assert done["status"] == "failed" and "請先啟動 Audio8Voice" in done["error"]
    mock_http.route("POST", "/synthesize", (404, {"detail": "找不到聲音：dirb6edcb1a:凡萱"}))
    use_audio8voice(client, mock_http.url)
    done = run_voice(client, pid, cid)
    assert done["status"] == "failed" and done["error"] == "Audio8Voice：找不到聲音：dirb6edcb1a:凡萱"
    assert client.json("GET", f"/api/projects/{pid}")["project"]["audio"] == []


def test_status_voices_and_url_checks(client: Client, mock_http: MockServer) -> None:
    health = {"ready": True, "status": "就緒", "voice": "dirb6edcb1a:凡萱", "voice_name": "凡萱"}
    voices = [{"id": "dirb6edcb1a:凡萱", "name": "凡萱", "group": "我的聲音", "prepared": True}]
    presets = [{"name": "旁白", "voice": "dirb6edcb1a:凡萱", "speed": 1.2, "voice_name": "凡萱"}]
    for path, answer in (("/health", health), ("/voices", voices), ("/presets", presets)):
        mock_http.route("GET", path, (200, answer))
    use_audio8voice(client, mock_http.url)
    assert client.json("GET", "/api/voice/status") == {"online": True, "url": mock_http.url, **health}
    assert client.json("GET", "/api/voice/voices") == {"voices": voices, "presets": presets}
    assert client.json("POST", "/api/voice/test", {"url": mock_http.url}) == health
    client.json("POST", "/api/voice/test", {"url": "127.0.0.1:18301"}, status=400)
    client.json("PUT", "/api/settings", {"voice": {"url": "not a url"}}, status=400)

    use_audio8voice(client, "http://127.0.0.1:9")
    status = client.json("GET", "/api/voice/status")
    assert status["online"] is False and "請先啟動 Audio8Voice" in status["error"]
    client.json("GET", "/api/voice/voices", status=502)


def test_picking_a_voice_prepares_it_once_on_the_comfy_lane(
        client: Client, project: dict, mock_http: MockServer) -> None:
    pid = project["id"]
    mock_http.route("GET", "/voices", (200, [
        {"id": "dirb6edcb1a:凡萱", "name": "凡萱", "group": "我的聲音", "prepared": False},
        {"id": "dirb6edcb1a:俊傑", "name": "俊傑", "group": "我的聲音", "prepared": True}]))
    release = threading.Event()

    def slow_prepare(call):  # Whisper on the GPU: the job stays active until released
        release.wait(10)
        return 200, {"id": call.json()["voice"], "name": "凡萱", "prepared": True, "text": "你好", "sec": 27.4}

    mock_http.route("POST", "/prepare", slow_prepare)
    use_audio8voice(client, mock_http.url)

    first = client.json("POST", "/api/voice/prepare", {"pid": pid, "voice": "dirb6edcb1a:凡萱"})
    assert first["prepared"] is False
    job = first["job"]
    assert (job["kind"], job["lane"], job["title"]) == ("voice", "comfy", "準備聲音：凡萱")
    again = client.json("POST", "/api/voice/prepare", {"pid": pid, "voice": "dirb6edcb1a:凡萱"})
    assert again["job"]["id"] == job["id"]  # picked twice while preparing: still one job
    release.set()
    done = client.wait_job(job["id"])
    assert done["status"] == "done", done["error"]
    assert done["message"] == "聲音已準備好：凡萱（27 秒）"
    assert [c.json() for c in mock_http.calls if c.path == "/prepare"] == [{"voice": "dirb6edcb1a:凡萱"}]

    assert client.json("POST", "/api/voice/prepare", {"pid": pid, "voice": "dirb6edcb1a:俊傑"}) == {
        "prepared": True, "job": None}
    client.json("POST", "/api/voice/prepare", {"pid": pid, "voice": "dirb6edcb1a:不存在"}, status=404)
    assert len(client.json("GET", f"/api/jobs?pid={pid}")["jobs"]) == 1

    mock_http.route("POST", "/prepare", (400, {"detail": "Whisper 聽不到人聲"}))
    failed = client.wait_job(client.json("POST", "/api/voice/prepare", {"pid": pid, "voice": "dirb6edcb1a:凡萱"})["job"]["id"])
    assert failed["status"] == "failed" and failed["error"] == "Audio8Voice：Whisper 聽不到人聲"


def test_a_slow_audio8voice_is_reported_as_busy_not_offline() -> None:
    base = "http://127.0.0.1:18301"
    slow = voice_module._explain(net._network_error(TimeoutError("timed out"), base + "/prepare"), base)
    assert "太久沒有回應" in slow.message and "請先啟動" not in slow.message
    down = voice_module._explain(net._network_error(ConnectionRefusedError(), base + "/prepare"), base)
    assert "請先啟動 Audio8Voice" in down.message


def test_cast_and_speakers_travel_with_a_package(app: App, client: Client, project: dict) -> None:
    add_line(client, project["id"])
    data = _package(app, client, project["id"], fullBackup=True, includeExport=False)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        assert json.loads(zf.read("project.json"))["cast"] == [NARRATOR]
    imported = client.json("POST", "/api/projects/import?name=backup.zip", data)["project"]
    assert imported["cast"] == [NARRATOR] and imported["captions"][0]["speaker"] == "c_narrator0"


# ---------------------------------------------------------------- VoAI (cloud, mocked)
VOAI_MEMBER = {"id": "c_cloud00000", "name": "雨榛", "engine": "voai", "voice": "雨榛", "voiceName": "雨榛",
               "style": "聊天", "model": "Neo", "speed": 1.8, "volume": 1.0}
VOAI_KEY = "voai-test-key-0123456789"


def use_voai(client: Client, mock_http: MockServer, monkeypatch, key: str | None = VOAI_KEY) -> None:
    monkeypatch.setattr(voice_module, "VOAI_URL", mock_http.url)
    monkeypatch.setattr(voice_module, "VOAI_BACKOFF", 0.0)
    if key:
        client.json("PUT", "/api/secrets", {"name": "voai.api_key", "value": key})


def add_cloud_line(client: Client, pid: str) -> str:
    cid = "x_cloud00001"
    client.json("POST", f"/api/projects/{pid}/ops", {"ops": [
        {"op": "cast.add", "item": VOAI_MEMBER},
        {"op": "caption.add", "item": {"id": cid, "start": 1.0, "end": 3.0, "text": "今天天氣真好", "speaker": "c_cloud00000"}}]})
    return cid


def run_cloud_voice(client: Client, pid: str, cid: str) -> dict:
    job = client.json("POST", "/api/jobs", {"kind": "voice", "pid": pid, "payload": {"captionId": cid}})["job"]
    assert job["lane"] == "provider:voai"  # a cloud line never waits behind a ComfyUI render
    return client.wait_job(job["id"])


@needs_ffmpeg
def test_voai_job_speaks_the_line_in_the_cloud_and_regenerating_replaces_it(
        app: App, client: Client, project: dict, mock_http: MockServer, monkeypatch) -> None:
    pid = project["id"]
    mock_http.route("POST", "/TTS/Speech", (200, wav_bytes(1.0, 44100)))
    use_voai(client, mock_http, monkeypatch)
    cid = add_cloud_line(client, pid)

    done = run_cloud_voice(client, pid, cid)
    assert done["status"] == "done", done["error"]
    call = mock_http.calls[-1]
    assert call.json() == {"version": "Neo", "text": "今天天氣真好", "speaker": "雨榛", "style": "聊天", "speed": 1.5}
    assert call.headers["x-api-key"] == VOAI_KEY and call.headers["x-output-format"] == "wav"
    saved = client.json("GET", f"/api/projects/{pid}")["project"]
    [clip] = saved["audio"]
    first = saved["media"][clip["mediaId"]]
    assert (clip["captionId"], clip["start"]) == (cid, 1.0)
    assert first["source"] == {"backend": "voai", "captionId": cid, "castId": "c_cloud00000", "voice": "雨榛",
                               "voiceName": "雨榛", "text": "今天天氣真好", "speed": 1.8, "style": "聊天", "model": "Neo"}

    done = run_cloud_voice(client, pid, cid)
    assert done["status"] == "done", done["error"]
    saved = client.json("GET", f"/api/projects/{pid}")["project"]
    assert len(saved["audio"]) == 1 and first["id"] not in saved["media"]


def test_voai_job_needs_a_key_and_explains_cloud_failures(
        client: Client, project: dict, mock_http: MockServer, monkeypatch) -> None:
    pid = project["id"]
    use_voai(client, mock_http, monkeypatch, key=None)
    cid = add_cloud_line(client, pid)
    code, _, body = client.request("POST", "/api/jobs", {"kind": "voice", "pid": pid, "payload": {"captionId": cid}})
    assert code == 400 and "VoAI API 金鑰" in json.loads(body)["error"]

    client.json("PUT", "/api/secrets", {"name": "voai.api_key", "value": VOAI_KEY})
    mock_http.route("POST", "/TTS/Speech", (401, {"msg": "invalid api key"}))
    done = run_cloud_voice(client, pid, cid)
    assert done["status"] == "failed" and "金鑰無效" in done["error"]
    mock_http.route("POST", "/TTS/Speech", (400, {"msg": "style 錯誤。可選 style: ['預設']"}))
    done = run_cloud_voice(client, pid, cid)
    assert done["status"] == "failed" and done["error"] == "VoAI：style 錯誤。可選 style: ['預設']"
    mock_http.route("POST", "/TTS/Speech", (529, {"msg": "too many requests"}))
    before = len(mock_http.calls)
    done = run_cloud_voice(client, pid, cid)
    assert done["status"] == "failed" and "請求太多" in done["error"]
    assert len(mock_http.calls) - before == voice_module.VOAI_RETRIES
    assert client.json("GET", f"/api/projects/{pid}")["project"]["audio"] == []

    client.json("POST", f"/api/projects/{pid}/ops",
                {"ops": [{"op": "cast.update", "id": "c_cloud00000", "patch": {"style": ""}}]})
    code, _, body = client.request("POST", "/api/jobs", {"kind": "voice", "pid": pid, "payload": {"captionId": cid}})
    assert code == 400 and "還沒有設定配音聲音" in json.loads(body)["error"]


def test_voai_speakers_are_listed_and_the_key_is_never_echoed(
        client: Client, mock_http: MockServer, monkeypatch) -> None:
    use_voai(client, mock_http, monkeypatch, key=None)
    assert client.json("GET", "/api/voice/voai") == {"configured": False, "online": False, "error": "", "models": []}
    speaker = {"name": "雨榛", "styles": ["預設", "聊天"], "age": 25, "gender": "女聲", "category": "真實聲線",
               "pov": "旁白", "tags": ["廣播"]}
    mock_http.route("GET", "/TTS/GetSpeaker", (200, {"data": {"models": [{"info": {"version": "Neo"}, "speakers": [speaker]}]}}))
    status, _, body = client.request("PUT", "/api/secrets", {"name": "voai.api_key", "value": VOAI_KEY})
    assert status == 200 and VOAI_KEY.encode() not in body
    got = client.json("GET", "/api/voice/voai")
    assert got == {"configured": True, "online": True, "error": "", "models": [{"version": "Neo", "remark": "", "speakers": [
        {"name": "雨榛", "styles": ["預設", "聊天"], "gender": "女聲", "age": 25, "category": "真實聲線"}]}]}
    assert mock_http.calls[-1].headers["x-api-key"] == VOAI_KEY
    calls = len(mock_http.calls)
    client.json("GET", "/api/voice/voai")
    assert len(mock_http.calls) == calls  # cached
    mock_http.route("GET", "/TTS/GetSpeaker", (401, {"msg": "invalid api key"}))
    bad = client.json("GET", "/api/voice/voai?refresh=1")
    assert bad["configured"] is True and bad["online"] is False and "金鑰無效" in bad["error"]
    for path in ("/api/voice/voai", "/api/secrets", "/api/bootstrap"):
        assert VOAI_KEY.encode() not in client.request("GET", path)[2]
