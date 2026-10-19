"""LLMClient against a local OpenAI-compatible mock: model list, prompt polish, AI shot split, error paths."""
from __future__ import annotations

import socket

import pytest

from vc.llm import LLMClient
from vc.util import AppError

CHAT = "/v1/chat/completions"
STORYBOARD_REPLY = """好的，以下是分鏡 [共兩鏡]：
```json
[
  {"name": "清晨漁港", "prompt": "薄霧中的漁港，漁船靠岸", "motion": "鏡頭緩慢推近", "camera": "遠景",
   "caption": "", "duration": 5},
  {"name": "女孩奔跑", "prompt": "小女孩跑過碼頭", "motion": "跟拍", "camera": "中景",
   "caption": "「爸爸回來了！」", "duration": "4秒"}
]
```"""


def completion(content: str) -> tuple[int, dict]:
    return 200, {"choices": [{"index": 0, "message": {"role": "assistant", "content": content}}]}


@pytest.fixture()
def llm(settings, secrets, mock_http) -> LLMClient:
    mock_http.route("GET", "/v1/models", (200, {"object": "list",
                                               "data": [{"id": "qwen2.5:7b-instruct"}, {"id": "llama3"}]}))
    settings.update({"llm": {"enabled": True, "baseUrl": mock_http.url + "/v1/"}})
    return LLMClient(settings, secrets)


def closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---------------------------------------------------------------- connection test
def test_connection_test_lists_models_even_when_switched_off(llm, settings):
    settings.update({"llm": {"enabled": False}})

    result = llm.test()

    assert result == {"ok": True, "models": ["qwen2.5:7b-instruct", "llama3"], "modelFound": True,
                      "message": "連線成功，找到模型「qwen2.5:7b-instruct」"}


def test_connection_test_flags_a_model_the_server_lacks(llm, settings):
    settings.update({"llm": {"model": "mistral"}})

    result = llm.test()

    assert result["modelFound"] is False
    assert result["message"] == "連線成功，但清單裡沒有「mistral」，請從下方清單挑一個"


def test_connection_test_explains_a_closed_port(llm, settings):
    port = closed_port()
    settings.update({"llm": {"baseUrl": f"http://127.0.0.1:{port}/v1"}})

    with pytest.raises(AppError) as err:
        llm.test()
    assert err.value.status == 502
    assert err.value.message == f"無法連線到 LLM 服務：連線被拒（服務沒有啟動或連接埠錯誤）（127.0.0.1:{port}）"


# ---------------------------------------------------------------- prompt polish
def test_enhance_sends_one_chat_request_and_cleans_the_reply(llm, secrets, mock_http):
    secrets.set("llm.api_key", "sk-test")
    mock_http.route("POST", CHAT, completion("```\nPrompt: 「霧中漁港，晨光灑在漁船上」\n```"))

    assert llm.enhance("  漁港 早上  ") == "霧中漁港，晨光灑在漁船上"

    call = mock_http.calls[-1]
    assert call.headers["authorization"] == "Bearer sk-test"
    body = call.json()
    assert body["model"] == "qwen2.5:7b-instruct"
    assert (body["temperature"], body["stream"]) == (0.7, False)
    system, user = body["messages"]
    assert system["role"] == "system" and "text-to-image" in system["content"]
    assert "Traditional Chinese stays Traditional Chinese" in system["content"]
    assert user == {"role": "user", "content": "漁港 早上"}


def test_enhance_motion_in_english_drops_reasoning(llm, mock_http):
    mock_http.route("POST", CHAT, completion("<think>The user wants motion.</think>\nSlow push-in on the boat."))

    assert llm.enhance("船", kind="motion", target="en") == "Slow push-in on the boat."
    system = mock_http.calls[-1].json()["messages"][0]["content"]
    assert "image-to-video" in system and "Write in English." in system
    assert "authorization" not in mock_http.calls[-1].headers


def test_model_that_rejects_temperature_is_retried_without_it(llm, mock_http):
    def answer(call):
        if "temperature" in call.json():
            return 400, {"error": {"message": "Unsupported value: 'temperature' does not support 0.7."}}
        return completion("A misty harbour at dawn.")

    mock_http.route("POST", CHAT, answer)

    assert llm.enhance("港口") == "A misty harbour at dawn."
    assert ["temperature" in c.json() for c in mock_http.calls] == [True, False]


@pytest.mark.parametrize("status, payload, expected", [
    (401, {"error": {"message": "Invalid API key"}}, "LLM 呼叫失敗：金鑰無效或未授權（401）：Invalid API key"),
    (200, b"<html>gateway</html>", "LLM 呼叫失敗：回應不是有效的 JSON"),
    (200, {"result": "no choices"}, "LLM 回應格式不正確（不是 OpenAI 相容格式）"),
    (200, completion("   ")[1], "LLM 沒有回傳內容，請換個模型再試"),
])
def test_enhance_failures_read_clearly(llm, mock_http, status, payload, expected):
    mock_http.route("POST", CHAT, (status, payload))

    with pytest.raises(AppError) as err:
        llm.enhance("港口")
    assert err.value.status == 502
    assert err.value.message.startswith(expected)
    assert len(mock_http.calls) == 1


def test_switched_off_model_is_never_called(llm, settings, mock_http):
    settings.update({"llm": {"enabled": False}})

    with pytest.raises(AppError) as err:
        llm.enhance("港口")
    assert err.value.status == 409
    assert mock_http.calls == []


@pytest.mark.parametrize("text, kind, target, expected", [
    ("港口", "audio", "same", "潤飾的類型不正確"),
    ("港口", "image", "jp", "潤飾的類型不正確"),
    ("   ", "image", "same", "請先輸入要潤飾的描述"),
])
def test_enhance_rejects_bad_input_before_calling(llm, mock_http, text, kind, target, expected):
    with pytest.raises(AppError, match=expected):
        llm.enhance(text, kind=kind, target=target)
    assert mock_http.calls == []


# ---------------------------------------------------------------- translate for English-only templates
def test_translate_sends_one_chat_request_and_cleans_the_reply(llm, mock_http):
    mock_http.route("POST", CHAT, completion("<think>harbour</think>\nTranslation: \"Misty harbour at dawn, cinematic\""))

    assert llm.translate("  清晨的海港，薄霧，cinematic  ") == "Misty harbour at dawn, cinematic"

    [call] = mock_http.calls
    system, user = call.json()["messages"]
    assert "only understands English" in system["content"] and "add nothing" in system["content"]
    assert user == {"role": "user", "content": "清晨的海港，薄霧，cinematic"}


def test_translate_refuses_empty_text_and_a_switched_off_model(llm, settings, mock_http):
    with pytest.raises(AppError, match="沒有要翻譯的提示詞"):
        llm.translate("   ")
    settings.update({"llm": {"enabled": False}})
    with pytest.raises(AppError) as err:
        llm.translate("清晨的海港")
    assert err.value.status == 409
    assert mock_http.calls == []


# ---------------------------------------------------------------- AI shot split
def test_script_to_shots_reads_json_wrapped_in_prose_and_fences(llm, mock_http):
    mock_http.route("POST", CHAT, completion(STORYBOARD_REPLY))

    shots = llm.script_to_shots("清晨的漁港，小女孩跑過碼頭喊爸爸回來了。")

    assert [(s["name"], s["prompt"], s["motion"], s["camera"], s["caption"], s["duration"]) for s in shots] == [
        ("清晨漁港", "薄霧中的漁港，漁船靠岸", "鏡頭緩慢推近", "遠景", "", 5.0),
        ("女孩奔跑", "小女孩跑過碼頭", "跟拍", "中景", "爸爸回來了！", 4.0),
    ]
    system = mock_http.calls[-1].json()["messages"][0]["content"]
    assert "storyboard" in system and "at most 60 shots" in system


def test_script_to_shots_accepts_an_object_and_skips_empty_items(llm, mock_http):
    mock_http.route("POST", CHAT, completion('{"shots": [{"prompt": "燈塔"}, {"prompt": ""}, "雜訊"]}'))

    shots = llm.script_to_shots("燈塔")

    assert [s["prompt"] for s in shots] == ["燈塔"]


@pytest.mark.parametrize("reply, expected", [
    ("抱歉，我無法處理這段文字。", "LLM 回傳的內容不是 JSON"),
    ("[]", "LLM 沒有拆出任何鏡頭"),
])
def test_script_to_shots_failures_read_clearly(llm, mock_http, reply, expected):
    mock_http.route("POST", CHAT, completion(reply))

    with pytest.raises(AppError, match=expected) as err:
        llm.script_to_shots("燈塔")
    assert err.value.status == 502


def test_script_to_shots_refuses_long_text_before_calling(llm, mock_http):
    with pytest.raises(AppError, match="最多 12,000 字"):
        llm.script_to_shots("字" * 12_001)
    assert mock_http.calls == []
