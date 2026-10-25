"""Cloud providers: the custom HTTP provider end to end against a local mock, plus offline helpers of the vendors."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import subprocess
from pathlib import Path
from typing import Any, Iterator

import pytest

from test_server import Client, needs_ffmpeg, png_bytes
from vc import cloud_cache, net, providers
from vc.app import App
from vc.media import find_ffmpeg
from vc.notext import GUARD
from vc.providers.base import (Context, Request, b64decode_loose, clean_model, decode_data_uri, duration_seconds,
                               first_url, merge_extra, nearest_ratio, nearest_value, parse_numbers, pick_duration,
                               require_host, scaled_size, with_avoid)
from vc.providers.custom import CustomHttp, dig, fill, fill_url
from vc.providers.hosted import FalImage, FalVideo, ReplicateImage, ReplicateVideo, _ext_of, _Replicate
from vc.providers.images import _orientation
from vc.providers.video import GeminiOmni, Kling, MiniMax, Runway, kling_token
from vc.util import AppError

KEY = "vc-test-key-0123456789abcdef"  # a made-up value, never a real credential
PROMPT = "清晨漁港，薄霧，漁船"
OUTPUT_PNG = png_bytes(96, 54, (10, 160, 90))
HEADERS = json.dumps({"X-Api-Key": "{{api_key}}"})
IMAGE_BODY = json.dumps({"prompt": "{{prompt}}", "width": "{{width}}", "height": "{{height}}", "seed": "{{seed}}"})


@pytest.fixture()
def data_dir(tmp_path: Path) -> Path:
    data = tmp_path / "data"
    data.mkdir()
    # a disabled, unreachable ComfyUI, so the default settings (the real machines) are never used
    servers = [{"id": "local", "name": "x", "url": "http://127.0.0.1:9", "enabled": False}]
    (data / "settings.json").write_text(json.dumps({"comfy": {"servers": servers}}), encoding="utf-8")
    return data


@pytest.fixture()
def app(data_dir: Path) -> Iterator[App]:
    instance = App(data_dir)
    instance.start(port=0, watchdog=False)
    yield instance
    instance.stop()


@pytest.fixture()
def client(app: App) -> Client:
    return Client(app.server.port)


@pytest.fixture()
def project(client: Client) -> dict:
    return client.json("POST", "/api/projects", {"name": "雲端測試", "settings": {"width": 1280, "height": 720}})["project"]


@pytest.fixture()
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """For the vendor checks: any attempt to reach a host fails the test instead of leaving the machine."""
    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError(f"network call attempted: {args[:2]}")

    monkeypatch.setattr(net, "request", refuse)
    monkeypatch.setattr(net, "download", refuse)


def configure(client: Client, pid: str, key: str | None = KEY, **config: Any) -> None:
    client.json("PUT", f"/api/providers/{pid}", {"config": {"headers": HEADERS, **config}})
    if key is not None:
        client.json("PUT", "/api/secrets", {"name": f"provider.{pid}.api_key", "value": key})


def run_job(client: Client, project: dict, kind: str, provider: str, timeout: float = 60.0) -> dict:
    body = {"kind": kind, "pid": project["id"], "shotId": project["shots"][0]["id"],
            "payload": {"provider": provider, "prompt": PROMPT}}
    return client.wait_job(client.json("POST", "/api/jobs", body)["job"]["id"], timeout=timeout)


def assert_key_not_kept(job: dict, data_dir: Path, caplog: pytest.LogCaptureFixture) -> None:
    assert KEY not in json.dumps(job, ensure_ascii=False)  # payload, remote, error, result
    assert KEY not in caplog.text
    # secrets.json is DPAPI encrypted, so the plain key must not appear in any file the app wrote
    leaked = [p.name for p in data_dir.rglob("*") if p.is_file() and KEY.encode() in p.read_bytes()]
    assert leaked == []


# ---------------------------------------------------------------- custom HTTP through the app
def test_custom_image_job_sends_the_key_only_in_its_header(client: Client, project: dict, mock_http, data_dir: Path,
                                                           caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    mock_http.route("POST", "/v1/generate", (200, {"data": [{"url": mock_http.url + "/files/out.png"}]}))
    mock_http.route("GET", "/files/out.png", (200, OUTPUT_PNG))
    configure(client, "custom-image", url=mock_http.url + "/v1/generate/", body=IMAGE_BODY)
    done = run_job(client, project, "image", "custom-image")
    assert done["status"] == "done", done["error"]
    assert done["message"] == "生圖完成" and done["remote"] is None

    generate, download = mock_http.calls
    assert (generate.method, generate.path) == ("POST", "/v1/generate")
    assert generate.headers["x-api-key"] == KEY and "authorization" not in generate.headers
    sent = generate.json()
    assert PROMPT in sent["prompt"]
    assert sent["width"] == project["settings"]["width"] and isinstance(sent["width"], int)
    assert isinstance(sent["seed"], int)
    assert download.path == "/files/out.png" and KEY not in json.dumps(download.headers)
    assert_key_not_kept(done, data_dir, caplog)

    saved = client.json("GET", f"/api/projects/{project['id']}")["project"]
    take = saved["takes"][saved["shots"][0]["activeImage"]]
    assert take["source"]["backend"] == "provider" and take["source"]["provider"] == "custom-image"
    assert (take["width"], take["height"]) == (96, 54)


# ---------------------------------------------------------------- spend estimate and reuse (CL-01, CL-02)
def image_api(mock_http) -> dict:
    mock_http.route("POST", "/v1/generate", (200, {"data": [{"url": mock_http.url + "/files/out.png"}]}))
    mock_http.route("GET", "/files/out.png", (200, OUTPUT_PNG))
    return {"url": mock_http.url + "/v1/generate", "body": IMAGE_BODY}


def test_a_unit_price_gives_an_estimate_and_a_monthly_total(client: Client, project: dict, mock_http) -> None:
    config = image_api(mock_http)
    configure(client, "custom-image", **config)
    done = run_job(client, project, "image", "custom-image")
    assert done["status"] == "done" and "cost" not in done["payload"]
    assert client.json("GET", "/api/costs")["jobs"] == 0

    configure(client, "custom-image", **config, price=0.04)
    done = run_job(client, project, "image", "custom-image")
    assert done["payload"]["cost"] == 0.04
    assert any(n.startswith("預估費用約 US$0.04") for n in done["notes"])
    summary = client.json("GET", "/api/costs")
    assert (summary["usd"], summary["jobs"], summary["byProvider"]) == (0.04, 1, {"custom-image": 0.04})
    assert client.json("GET", "/api/bootstrap")["costs"] == summary


def test_the_same_request_reuses_its_take_instead_of_paying_again(client: Client, project: dict, mock_http) -> None:
    configure(client, "custom-image", **image_api(mock_http), price=0.04)
    body = {"kind": "image", "pid": project["id"], "shotId": project["shots"][0]["id"],
            "payload": {"provider": "custom-image", "prompt": PROMPT, "seed": 1234}}
    first = client.wait_job(client.json("POST", "/api/jobs", body)["job"]["id"], timeout=60)
    second = client.wait_job(client.json("POST", "/api/jobs", body)["job"]["id"], timeout=60)
    assert first["status"] == second["status"] == "done"
    assert len([c for c in mock_http.calls if c.path == "/v1/generate"]) == 1
    assert cloud_cache.REUSED_NOTE in second["notes"]
    assert client.json("GET", "/api/costs")["jobs"] == 1  # only the call that went out is counted

    saved = client.json("GET", f"/api/projects/{project['id']}")["project"]
    a, b = (saved["takes"][j["result"]["takeIds"][0]] for j in (first, second))
    assert a["file"] != b["file"] and a["source"]["requestKey"] == b["source"]["requestKey"]
    assert saved["shots"][0]["activeImage"] == b["id"]

    body["payload"]["seed"] = 1235
    client.wait_job(client.json("POST", "/api/jobs", body)["job"]["id"], timeout=60)
    assert len([c for c in mock_http.calls if c.path == "/v1/generate"]) == 2


def async_config(mock_http) -> dict:
    return {"url": mock_http.url + "/v1/generate", "body": IMAGE_BODY, "idPath": "task.id", "resultPath": "output.url",
            "pollUrl": mock_http.url + "/v1/tasks/{{id}}", "pollInterval": 2}


def test_async_custom_api_is_polled_until_it_reports_done(client: Client, project: dict, mock_http, data_dir: Path,
                                                          caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    states = iter([{"status": "queued"}, {"status": "SUCCEEDED", "output": {"url": mock_http.url + "/files/a.png"}}])
    mock_http.route("POST", "/v1/generate", (200, {"task": {"id": "task_123"}}))
    mock_http.route("GET", "/v1/tasks/task_123", lambda call: (200, next(states)))
    mock_http.route("GET", "/files/a.png", (200, OUTPUT_PNG))
    configure(client, "custom-image", **async_config(mock_http))
    done = run_job(client, project, "image", "custom-image")
    assert done["status"] == "done", done["error"]
    assert done["remote"] is None  # the task handle is only kept for failed jobs
    assert [c.path for c in mock_http.calls] == ["/v1/generate", "/v1/tasks/task_123", "/v1/tasks/task_123",
                                                 "/files/a.png"]
    assert all(c.headers["x-api-key"] == KEY for c in mock_http.calls[:3])
    assert_key_not_kept(done, data_dir, caplog)


def test_async_task_failure_is_reported_and_the_handle_kept(client: Client, project: dict, mock_http) -> None:
    reason = {"code": "nsfw", "message": "內容被拒絕"}
    mock_http.route("POST", "/v1/generate", (200, {"task": {"id": "task_123"}}))
    mock_http.route("GET", "/v1/tasks/task_123", (200, {"status": "failed", "error": reason}))
    configure(client, "custom-image", **async_config(mock_http))
    done = run_job(client, project, "image", "custom-image")
    assert done["status"] == "failed"
    assert done["error"] == ("自訂 API 回報任務失敗：" + json.dumps(reason, ensure_ascii=False)
                             + "。內容可能被供應商的安全審查擋下，請修改提示詞或換一張參考圖")
    assert done["remote"] == {"type": "provider", "provider": "custom-image",
                              "data": {"vendor": "custom", "id": "task_123"}}


@pytest.mark.parametrize("status, body, expected", [
    (401, {"error": {"message": "invalid api key"}},
     "金鑰無效或未授權（401）：invalid api key。請檢查金鑰或存取權杖是否正確、是否過期"),
    (429, {"message": "rate limited"}, "請求太頻繁或額度不足（429）：rate limited。請求太頻繁或同時執行的任務太多，請稍等幾分鐘再試"),
    (429, {"error": {"message": "You exceeded your current quota"}},
     "請求太頻繁或額度不足（429）：You exceeded your current quota。帳戶額度或點數不足，請到供應商網站儲值或確認方案"),
    (500, b"upstream exploded", "伺服器錯誤（500）：upstream exploded。供應商服務暫時異常，請稍後再試"),
    (422, {"detail": [{"msg": "duration must be 5 or 10"}]},
     "參數不被接受（422）：duration must be 5 or 10。秒數、比例或解析度不在模型支援的範圍內，請改用模型支援的設定"),
    (418, b"", "HTTP 錯誤（418）"),
])
def test_vendor_http_errors_fail_with_a_readable_message(client: Client, project: dict, mock_http, data_dir: Path,
                                                         caplog: pytest.LogCaptureFixture, status: int, body: Any,
                                                         expected: str) -> None:
    caplog.set_level(logging.DEBUG)
    mock_http.route("POST", "/v1/generate", (status, body))
    configure(client, "custom-image", url=mock_http.url + "/v1/generate", body=IMAGE_BODY)
    done = run_job(client, project, "image", "custom-image")
    assert done["status"] == "failed"
    assert done["error"] == expected
    assert len(mock_http.calls) == 1  # a rejected submit is not retried
    assert_key_not_kept(done, data_dir, caplog)


def test_failure_advice_is_added_once_and_only_for_known_reasons() -> None:
    local = "ComfyUI 執行失敗（節點 #7 KSampler）：Shape mismatch"
    assert net.advise(local) == local
    once = net.advise("fal.ai 任務失敗：Content policy violation.")
    assert once == "fal.ai 任務失敗：Content policy violation。內容可能被供應商的安全審查擋下，請修改提示詞或換一張參考圖"
    assert net.advise(once) == once
    assert net.advise("Replicate 任務失敗：invalid credentials").endswith("。請檢查金鑰或存取權杖是否正確、是否過期")


def test_missing_key_fails_before_anything_is_sent(client: Client, project: dict, mock_http) -> None:
    configure(client, "custom-image", key=None, url=mock_http.url + "/v1/generate", body=IMAGE_BODY)
    done = run_job(client, project, "image", "custom-image")
    assert done["status"] == "failed"
    assert done["error"] == "尚未填寫自訂 API 金鑰，請到「引擎 › 雲端 API」補上"
    assert mock_http.calls == []


@needs_ffmpeg
def test_custom_video_job_downloads_a_real_mp4(client: Client, project: dict, mock_http, tmp_path: Path) -> None:
    imageio_ffmpeg = pytest.importorskip("imageio_ffmpeg")
    clip = tmp_path / "clip.mp4"
    subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-v", "error", "-f", "lavfi",
                    "-i", "color=c=red:s=64x64:d=1:r=24", "-pix_fmt", "yuv420p", "-c:v", "libx264", str(clip)],
                   check=True, capture_output=True, timeout=60)
    mock_http.route("POST", "/v1/video", (200, {"video": {"url": mock_http.url + "/files/clip.mp4"}}))
    mock_http.route("GET", "/files/clip.mp4", (200, clip.read_bytes()))
    body = json.dumps({"prompt": "{{prompt}}", "duration": "{{duration}}", "image": "{{image_data_uri}}"})
    configure(client, "custom-video", url=mock_http.url + "/v1/video", body=body, resultPath="video.url")
    done = run_job(client, project, "video", "custom-video", timeout=120)
    assert done["status"] == "done", done["error"]
    assert done["message"] == "影片完成"
    sent = mock_http.calls[0].json()
    assert isinstance(sent["duration"], int) and "image" not in sent  # no shot picture: the field is dropped

    saved = client.json("GET", f"/api/projects/{project['id']}")["project"]
    take = saved["takes"][saved["shots"][0]["activeVideo"]]
    assert take["kind"] == "video" and take["source"]["provider"] == "custom-video"
    assert 0.5 < take["duration"] < 1.5


def test_a_shot_set_to_skip_its_picture_makes_video_from_text(client: Client, project: dict, mock_http) -> None:
    """The V key and batch runs send no useImage, so the shot's own setting picks image or text to video."""
    pid, sid = project["id"], project["shots"][0]["id"]
    upload = client.json("POST", f"/api/projects/{pid}/upload?name=frame.png&target=shot&shot={sid}", png_bytes())
    assert client.wait_job(upload["job"]["id"])["status"] == "done"
    mock_http.route("POST", "/v1/video", (200, {}))  # no result: the job stops right after the request
    body = json.dumps({"prompt": "{{prompt}}", "image": "{{image_data_uri}}"})
    configure(client, "custom-video", url=mock_http.url + "/v1/video", body=body, resultPath="video.url")

    def sent(patch: dict) -> dict:
        client.json("POST", f"/api/projects/{pid}/ops", {"ops": [{"op": "shot.update", "id": sid, "patch": patch}]})
        before = len(mock_http.calls)
        run_job(client, project, "video", "custom-video")
        return mock_http.calls[before].json()

    assert sent({"videoUseImage": True})["image"].startswith("data:image/")
    assert "image" not in sent({"videoUseImage": False})


def test_base64_result_is_decoded_without_any_key(mock_http, tmp_path: Path) -> None:
    prov = providers.get("custom-image")
    config = prov.validate_config({"url": mock_http.url + "/v1/b64", "headers": "{}", "resultPath": "images.0.b64",
                                   "body": json.dumps({"prompt": "{{prompt}}"})})
    mock_http.route("POST", "/v1/b64", (200, {"images": [{"b64": base64.b64encode(OUTPUT_PNG).decode("ascii")}]}))
    ctx = Context(prov.id, config, lambda key: "", dst_dir=tmp_path)
    [path] = prov.run(ctx, Request(kind="image", prompt="港口"))
    assert path.name == "00_custom-image.png" and path.read_bytes() == OUTPUT_PNG
    assert mock_http.calls[0].json() == {"prompt": "港口"}


def test_custom_config_is_validated() -> None:
    prov = CustomHttp("custom-image", "image")
    url = "https://api.example.invalid/v1/generate"
    cases = [
        ({"url": url, "idPath": "id"}, "填了「任務編號路徑」就要一併填「查詢網址」"),
        ({"url": url, "body": json.dumps({"p": "{{promt}}"})}, "不認識的變數：{{promt}}"),
        ({"url": url, "headers": json.dumps({"X-Retry": 3})}, "標頭必須是 JSON 物件，而且每個值都是文字"),
        ({"url": "ftp://example.invalid/x"}, "請求網址 必須是 http:// 或 https:// 開頭的網址"),
        ({"url": url, "idPath": "id", "pollUrl": url + "/{{id}}", "responseType": "binary"},
         "回應格式選「直接回傳檔案」時不能搭配任務輪詢"),
    ]
    for config, message in cases:
        with pytest.raises(AppError) as err:
            prov.validate_config(config)
        assert err.value.message == message
    assert prov.validate_config({"url": url + "/"})["url"] == url


# ---------------------------------------------------------------- Google Gemini Omni against a local mock
CLIP = b"\x00\x00\x00\x18ftypmp42" + bytes(64)  # mp4 magic bytes: enough for the file type check
OMNI_ID = "v1_omni123"


def omni_answer(status: str, *content: dict) -> dict:
    return {"id": OMNI_ID, "status": status,
            "steps": [{"type": "user_input"}, {"type": "model_output", "content": list(content)}]}


def omni_video(**fields: str) -> dict:
    return {"type": "video", "mime_type": "video/mp4", **fields}


def omni(mock_http, tmp_path: Path, remotes: list | None = None, **config: Any) -> tuple[GeminiOmni, Context]:
    prov = GeminiOmni()
    cfg = prov.resolve_config({"baseUrl": mock_http.url + "/v1beta", **config})
    on_remote = remotes.append if remotes is not None else None
    return prov, Context(prov.id, cfg, lambda key: KEY if key == "api_key" else "", dst_dir=tmp_path,
                         on_remote=on_remote)


def test_omni_text_to_video_sends_its_length_and_polls_the_interaction(mock_http, tmp_path: Path) -> None:
    mock_http.route("POST", "/v1beta/interactions", (200, {"id": OMNI_ID, "status": "in_progress"}))
    data = base64.b64encode(CLIP).decode("ascii")
    mock_http.route("GET", f"/v1beta/interactions/{OMNI_ID}", (200, omni_answer("completed", omni_video(data=data))))
    remotes: list = []
    prov, ctx = omni(mock_http, tmp_path, remotes)
    [path] = prov.run(ctx, Request(kind="video", prompt="清晨漁港", negative="blurry", width=1280, height=720,
                                   duration=4.0))
    assert path.suffix == ".mp4" and path.read_bytes() == CLIP
    assert remotes == [{"vendor": "omni", "interaction": OMNI_ID}]

    create, status = mock_http.calls
    assert create.headers["x-goog-api-key"] == KEY and KEY not in create.path
    assert create.json() == {
        "model": "gemini-omni-1.1-flash",
        "input": [{"type": "text", "text": "清晨漁港\n\nAvoid: blurry"}],
        "background": True,
        "response_format": {"type": "video", "delivery": "uri", "aspect_ratio": "16:9", "resolution": "720p",
                            "duration": "4s"},
        "generation_config": {"video_config": {"task": "text_to_video"}},
    }
    assert (status.method, status.path, status.headers["x-goog-api-key"]) == (
        "GET", f"/v1beta/interactions/{OMNI_ID}", KEY)


def test_omni_sends_the_closest_length_it_accepts(mock_http, tmp_path: Path) -> None:
    data = base64.b64encode(CLIP).decode("ascii")
    mock_http.route("POST", "/v1beta/interactions", (200, omni_answer("completed", omni_video(data=data))))
    prov, ctx = omni(mock_http, tmp_path)
    for seconds, sent in ((1.0, "3s"), (3.4, "3s"), (6.5, "6s"), (8.0, "8s"), (15.0, "10s")):
        prov.run(ctx, Request(kind="video", prompt="港口", duration=seconds))
        assert mock_http.calls[-1].json()["response_format"]["duration"] == sent
    assert {call.method for call in mock_http.calls} == {"POST"}  # a finished answer needs no polling


def test_omni_image_to_video_puts_the_frame_first(mock_http, tmp_path: Path) -> None:
    data = base64.b64encode(CLIP).decode("ascii")
    mock_http.route("POST", "/v1beta/interactions", (200, omni_answer("completed", omni_video(data=data))))
    prov, ctx = omni(mock_http, tmp_path, resolution="1080p")
    frame = png_bytes(45, 80)
    prov.run(ctx, Request(kind="video", prompt="港口", image=frame, width=448, height=768, duration=5.0))
    sent = mock_http.calls[0].json()
    assert sent["input"] == [{"type": "image", "data": base64.b64encode(frame).decode("ascii"), "mime_type": "image/png"},
                             {"type": "text", "text": "港口"}]
    assert sent["generation_config"] == {"video_config": {"task": "image_to_video"}}
    assert sent["response_format"] == {"type": "video", "delivery": "uri", "aspect_ratio": "9:16",
                                       "resolution": "1080p", "duration": "5s"}


def test_omni_uri_result_waits_for_the_file_then_downloads_it(mock_http, tmp_path: Path,
                                                              monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Context, "sleep", lambda self, seconds: None)
    uri = f"{mock_http.url}/v1beta/files/clip01:download?alt=media"
    mock_http.route("POST", "/v1beta/interactions", (200, {"id": OMNI_ID, "status": "queued"}))
    answers = iter([omni_answer("in_progress"), omni_answer("completed", omni_video(uri=uri))])
    mock_http.route("GET", f"/v1beta/interactions/{OMNI_ID}", lambda call: (200, next(answers)))
    states = iter(["PROCESSING", "ACTIVE"])
    mock_http.route("GET", "/v1beta/files/clip01", lambda call: (200, {"name": "files/clip01", "state": next(states)}))
    mock_http.route("GET", "/v1beta/files/clip01:download?alt=media", (200, CLIP))
    prov, ctx = omni(mock_http, tmp_path)
    [path] = prov.run(ctx, Request(kind="video", prompt="港口"))
    assert path.suffix == ".mp4" and path.read_bytes() == CLIP
    assert [call.path for call in mock_http.calls] == [
        "/v1beta/interactions", f"/v1beta/interactions/{OMNI_ID}", f"/v1beta/interactions/{OMNI_ID}",
        "/v1beta/files/clip01", "/v1beta/files/clip01", "/v1beta/files/clip01:download?alt=media"]
    assert all(call.headers["x-goog-api-key"] == KEY for call in mock_http.calls)


def test_omni_failures_and_blocked_prompts_are_readable(mock_http, tmp_path: Path) -> None:
    answer: dict = {}
    mock_http.route("POST", "/v1beta/interactions", lambda call: (200, answer))
    prov, ctx = omni(mock_http, tmp_path)
    cases = [
        ({"id": OMNI_ID, "status": "failed", "errors": [{"code": "INTERNAL", "message": "backend overloaded"}]},
         502, "Gemini Omni 生成失敗：backend overloaded"),
        (omni_answer("completed", {"type": "text", "text": "I can't create that video."}),
         422, "Gemini Omni 沒有產生影片：I can't create that video."),
        (omni_answer("completed"), 422, "Gemini Omni 沒有產生影片（可能被安全過濾擋下，請調整提示詞）"),
        ({"id": OMNI_ID, "status": "cancelled"}, 499, "Gemini Omni 任務已被取消"),
    ]
    for answer, status, message in cases:
        with pytest.raises(AppError) as err:
            prov.run(ctx, Request(kind="video", prompt="港口"))
        assert (err.value.status, err.value.message) == (status, message)

    answer = omni_answer("completed", omni_video(uri="files/broken1"))
    mock_http.route("GET", "/v1beta/files/broken1", (200, {"name": "files/broken1", "state": "FAILED"}))
    with pytest.raises(AppError) as failed:
        prov.run(ctx, Request(kind="video", prompt="港口"))
    assert (failed.value.status, failed.value.message) == (502, "Google 處理影片檔失敗")


def test_omni_resumes_and_cancels_the_stored_interaction(mock_http, tmp_path: Path) -> None:
    data = base64.b64encode(CLIP).decode("ascii")
    mock_http.route("GET", f"/v1beta/interactions/{OMNI_ID}", (200, omni_answer("completed", omni_video(data=data))))
    mock_http.route("POST", f"/v1beta/interactions/{OMNI_ID}/cancel", (200, {"id": OMNI_ID, "status": "cancelled"}))
    prov, ctx = omni(mock_http, tmp_path)
    remote = {"vendor": "omni", "interaction": OMNI_ID}
    [path] = prov.resume(ctx, remote)
    assert path.read_bytes() == CLIP
    prov.cancel_remote(ctx, remote)
    assert [(call.method, call.path) for call in mock_http.calls] == [
        ("GET", f"/v1beta/interactions/{OMNI_ID}"), ("POST", f"/v1beta/interactions/{OMNI_ID}/cancel")]
    assert mock_http.calls[1].headers["x-goog-api-key"] == KEY


@pytest.mark.usefixtures("no_network")
def test_omni_refuses_retired_veo_models_and_veo_era_handles(tmp_path: Path) -> None:
    prov = GeminiOmni()
    ctx = Context(prov.id, prov.resolve_config({"model": "veo-3.1-generate-preview"}), lambda key: KEY,
                  dst_dir=tmp_path)
    with pytest.raises(AppError) as retired:
        prov.run(ctx, Request(kind="video", prompt="港口"))
    assert retired.value.status == 400 and "gemini-omni-1.1-flash" in retired.value.message
    veo_handle = {"op": "models/veo-3.0-generate-001/operations/abc123"}
    with pytest.raises(AppError) as old:
        prov.resume(ctx, veo_handle)
    assert old.value.status == 409
    prov.cancel_remote(ctx, veo_handle)  # nothing it can cancel, so nothing is sent
    no_key = Context(prov.id, prov.resolve_config({}), lambda key: "", dst_dir=tmp_path)
    with pytest.raises(AppError, match="尚未填寫Google AI Studio API 金鑰"):
        prov.run(no_key, Request(kind="video", prompt="港口"))


@needs_ffmpeg
def test_omni_job_uses_the_gemini_image_key_and_the_nearest_length(client: Client, project: dict, mock_http,
                                                                   data_dir: Path, tmp_path: Path,
                                                                   caplog: pytest.LogCaptureFixture) -> None:
    clip = tmp_path / "clip.mp4"
    subprocess.run([find_ffmpeg(), "-y", "-v", "error", "-f", "lavfi",
                    "-i", "color=c=blue:s=64x64:d=1:r=24", "-pix_fmt", "yuv420p", "-c:v", "libx264", str(clip)],
                   check=True, capture_output=True, timeout=60)
    caplog.set_level(logging.DEBUG)
    mock_http.route("POST", "/v1beta/interactions", (200, {"id": OMNI_ID, "status": "in_progress"}))
    data = base64.b64encode(clip.read_bytes()).decode("ascii")
    mock_http.route("GET", f"/v1beta/interactions/{OMNI_ID}", (200, omni_answer("completed", omni_video(data=data))))
    client.json("PUT", "/api/providers/veo", {"config": {"baseUrl": mock_http.url + "/v1beta"}})
    client.json("PUT", "/api/secrets", {"name": "provider.gemini-image.api_key", "value": KEY})
    body = {"kind": "video", "pid": project["id"], "shotId": project["shots"][0]["id"],
            "payload": {"provider": "veo", "prompt": PROMPT, "duration": 2}}
    done = client.wait_job(client.json("POST", "/api/jobs", body)["job"]["id"], timeout=120)
    assert done["status"] == "done", done["error"]
    create = mock_http.calls[0]
    assert create.headers["x-goog-api-key"] == KEY
    sent = create.json()
    assert sent["response_format"]["duration"] == "3s"  # 2 seconds is shorter than Omni allows
    assert PROMPT in sent["input"][-1]["text"] and sent["generation_config"]["video_config"]["task"] == "text_to_video"
    assert_key_not_kept(done, data_dir, caplog)

    saved = client.json("GET", f"/api/projects/{project['id']}")["project"]
    take = saved["takes"][saved["shots"][0]["activeVideo"]]
    assert take["source"]["provider"] == "veo" and 0.5 < take["duration"] < 1.5


# ---------------------------------------------------------------- offline helpers
def test_fill_keeps_native_types_and_drops_missing_values() -> None:
    template = {"prompt": "{{prompt}}", "size": "{{width}}x{{height}}", "w": "{{ width }}", "neg": "{{negative}}",
                "refs": ["{{image_b64}}", "fixed"], "n": 1}
    out = fill(template, {"prompt": "港口", "width": 1280, "height": 720, "negative": None})
    assert out == {"prompt": "港口", "size": "1280x720", "w": 1280, "refs": ["fixed"], "n": 1}
    url = fill_url("https://h.invalid/v1/tasks/{{id}}?q={{prompt}}", {"id": "a/b c", "prompt": "港"})
    assert url == "https://h.invalid/v1/tasks/a%2Fb%20c?q=%E6%B8%AF"
    data = {"data": [{"url": "u"}], "output": ["o"]}
    assert dig(data, "data.0.url") == "u" and dig(data, "output[0]") == "o"
    assert dig(data, "data.5.url") is None and dig(data, "data.0.url.x") is None and dig(data, "missing") is None


def test_size_ratio_and_number_helpers() -> None:
    assert nearest_ratio(1280, 720, ("16:9", "9:16", "1:1")) == "16:9"
    assert nearest_ratio(1000, 990, ("16:9", "9:16", "1:1")) == "1:1"
    assert nearest_ratio(720, 1280, ("1280:768", "768:1280")) == "768:1280"
    assert scaled_size(1280, 720) == (1328, 752)
    assert scaled_size(10, 10, megapixels=0.000001) == (16, 16)
    assert nearest_value(7, [5, 10]) == 5 and nearest_value(9, [5, 10]) == 10 and nearest_value(3, []) == 3
    assert parse_numbers("5, 10，15、 x 20") == [5.0, 10.0, 15.0, 20.0]
    assert with_avoid("港口", " 文字 ") == "港口\n\nAvoid: 文字" and with_avoid("港口", "") == "港口"
    assert pick_duration(8, [5, 10]) == 10 and pick_duration(7.5, ["5", "10"]) == "5" and pick_duration(3, [10]) == 10
    assert pick_duration(7, ["4s", "6s", "8s"]) == "6s"  # a tie goes to the shorter length
    assert [duration_seconds(v) for v in (5, "10", "8s", 2.5, True, "auto", None)] == [5.0, 10.0, 8.0, 2.5, None, None, None]
    assert [_orientation("auto", Request("image", "p", width=w, height=h)) for w, h in
            ((1280, 720), (720, 1280), (1000, 1000))] == ["landscape", "portrait", "square"]
    assert _orientation("portrait", Request("image", "p", width=1280, height=720)) == "portrait"
    assert (_ext_of("image/jpeg"), _ext_of("image/webp"), _ext_of("image/png")) == (".jpg", ".webp", ".png")


def test_each_vendor_offers_its_own_video_lengths() -> None:
    def lengths(prov, mode: str, config: dict | None = None, models: dict | None = None) -> list | None:
        return prov.durations(prov.resolve_config(config), mode, models)

    assert lengths(Kling(), "i2v") == ["5", "10"] and lengths(Kling(), "t2v", {"duration": "10"}) == ["10"]
    assert lengths(Runway(), "i2v") == [5, 10] and lengths(Runway(), "i2v", {"duration": "5"}) == [5]
    assert lengths(MiniMax(), "i2v") == [6, 10] and lengths(MiniMax(), "t2v", {"resolution": "1080P"}) == [6]
    assert lengths(MiniMax(), "i2v", {"model": "I2V-01"}) == []  # older models set their own length
    omni = ["3s", "4s", "5s", "6s", "7s", "8s", "9s", "10s"]  # whole seconds, 3 to 10, in every mode
    assert lengths(GeminiOmni(), "t2v") == omni and lengths(GeminiOmni(), "i2v", {"resolution": "4k"}) == omni
    assert lengths(ReplicateVideo(), "i2v", {"durations": "6, 10"}) == [6, 10]
    model = "fal-ai/x/image-to-video"
    detected = {"fields": {"model": {"durations": {model: ["4s", "6s", "8s"]}}}}
    assert lengths(FalVideo(), "i2v", {"model": f" {model}/"}, detected) == ["4s", "6s", "8s"]
    assert lengths(FalVideo(), "i2v", {"model": model}) == ["6", "8", "10"]  # never detected: the configured lengths
    assert lengths(FalVideo(), "t2v", {"durations": ""}, detected) == []
    assert lengths(providers.get("custom-video"), "t2v") is None  # the shot's seconds go out as they are


def test_model_names_extra_json_and_result_urls() -> None:
    assert clean_model("/fal-ai/flux/dev/") == "fal-ai/flux/dev"
    for bad in ("a/../b", "bad name", "x?y=1", ""):
        with pytest.raises(AppError, match="模型名稱格式不正確"):
            clean_model(bad)
    assert merge_extra({"a": 1}, '{"b": 2, "a": 3}') == {"a": 3, "b": 2}
    assert merge_extra({"a": 1}, "  ") == {"a": 1}
    with pytest.raises(AppError, match="額外參數必須是 JSON 物件"):
        merge_extra({}, "[1]")
    with pytest.raises(AppError, match="額外參數不是有效的 JSON"):
        merge_extra({}, "{")
    assert first_url({"images": [{"url": "https://cdn.invalid/a.png"}]}) == "https://cdn.invalid/a.png"
    assert first_url({"output": ["text", {"video": {"url": "data:video/mp4;base64,AA=="}}]}).startswith("data:")
    assert first_url({"output": "not a url"}) is None


def test_base64_and_data_uri_decoding() -> None:
    urlsafe = base64.urlsafe_b64encode(OUTPUT_PNG).decode("ascii").rstrip("=")
    assert b64decode_loose(urlsafe[:40] + "\n" + urlsafe[40:]) == OUTPUT_PNG
    assert decode_data_uri("data:image/png;base64," + base64.b64encode(OUTPUT_PNG).decode("ascii")) == OUTPUT_PNG
    assert decode_data_uri("data:text/plain,hi%20there") == b"hi there"


def test_require_host_only_accepts_https_on_the_vendor_domain() -> None:
    assert require_host("https://queue.fal.run/x", "fal.run") == "https://queue.fal.run/x"
    assert require_host("https://fal.run/x", "fal.run") == "https://fal.run/x"
    for url in ("http://queue.fal.run/x", "https://evilfal.run/x", "https://fal.run.evil.invalid/x",
                "https://127.0.0.1/x", "queue.fal.run/x"):
        with pytest.raises(AppError, match="任務網址不在預期的服務網域") as err:
            require_host(url, "fal.run")
        assert err.value.status == 502


def test_replicate_states_are_parsed() -> None:
    done = _Replicate._state({"status": "succeeded", "output": ["https://replicate.delivery/a.png"]})
    assert done.done and done.result == ["https://replicate.delivery/a.png"]
    running = _Replicate._state({"status": "processing", "logs": " 10%|#  \n 42%|####  "})
    assert not running.done and running.progress == 0.42 and running.message == "Replicate 生成中"
    starting = _Replicate._state({"status": "starting"})
    assert starting.message == "Replicate 啟動中" and starting.progress is None
    with pytest.raises(AppError) as canceled:
        _Replicate._state({"status": "canceled"})
    assert canceled.value.status == 499
    with pytest.raises(AppError, match="Replicate 任務失敗：未提供原因"):
        _Replicate._state({"status": "failed", "error": None})


def test_kling_token_is_a_signed_hs256_jwt() -> None:
    def unpad(part: str) -> bytes:
        return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))

    head, body, sig = kling_token("ak-test", "sk-test", now=1_700_000_000).split(".")
    assert json.loads(unpad(head)) == {"alg": "HS256", "typ": "JWT"}
    assert json.loads(unpad(body)) == {"iss": "ak-test", "exp": 1_700_001_800, "nbf": 1_699_999_995}
    assert unpad(sig) == hmac.new(b"sk-test", f"{head}.{body}".encode(), hashlib.sha256).digest()


@pytest.mark.usefixtures("no_network")
def test_stored_task_urls_off_the_vendor_domain_are_refused(tmp_path: Path) -> None:
    ctx = Context("offline", {}, lambda key: "not-a-real-key", dst_dir=tmp_path)
    fal = {"vendor": "fal", "status": "https://fal.run.evil.invalid/s", "response": "https://queue.fal.run/r"}
    with pytest.raises(AppError, match="任務網址不在預期的服務網域：fal.run.evil.invalid"):
        FalImage().resume(ctx, fal)
    with pytest.raises(AppError, match="任務網址不在預期的服務網域：api.replicate.com.evil.invalid"):
        ReplicateImage().resume(ctx, {"vendor": "replicate", "get": "https://api.replicate.com.evil.invalid/p/1"})
    with pytest.raises(AppError, match="可靈任務資料不完整") as kling:
        Kling().resume(ctx, {"task": "x", "path": "image2video"})
    assert kling.value.status == 409
    with pytest.raises(AppError, match="自訂 API 的任務資料或查詢網址不完整"):
        CustomHttp("custom-image", "image").resume(Context("custom-image", {"pollUrl": ""}, lambda key: ""), {})


def test_cloud_request_drops_title_directions_and_forbids_text(client: Client, project: dict, mock_http) -> None:
    mock_http.route("POST", "/v1/generate", (200, {"data": [{"url": mock_http.url + "/files/out.png"}]}))
    mock_http.route("GET", "/files/out.png", (200, OUTPUT_PNG))
    body = json.dumps({"prompt": "{{prompt}}", "negative": "{{negative}}"})
    configure(client, "custom-image", url=mock_http.url + "/v1/generate", body=body)
    job = {"kind": "image", "pid": project["id"], "shotId": project["shots"][0]["id"],
           "payload": {"provider": "custom-image", "prompt": f"{PROMPT}\n5. 標題字卡浮現"}}
    done = client.wait_job(client.json("POST", "/api/jobs", job)["job"]["id"], timeout=60)
    assert done["status"] == "done", done["error"]

    sent = mock_http.calls[0].json()
    assert sent["prompt"] == f"{PROMPT}\n{GUARD}"
    assert all(term in sent["negative"] for term in ("text", "subtitles", "title card", "watermark"))
    assert any("標題字卡浮現" in note for note in done["notes"])
