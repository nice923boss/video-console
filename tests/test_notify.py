"""Notifier: toast events, one ntfy push per drained queue, and the test push. Local mock server only."""
from __future__ import annotations

import json
import time

import pytest

from vc.notify import MAX_LISTED, Notifier, summarize
from vc.util import AppError


def job(kind: str, status: str, title: str, *, message: str = "", error: str | None = None) -> dict:
    return {"id": f"j-{title}", "kind": kind, "pid": "p1", "status": status, "title": title,
            "message": message, "error": error}


@pytest.fixture()
def notifier(settings, secrets, bus, mock_http) -> Notifier:
    mock_http.route("POST", "/", (200, {"id": "msg1"}))
    settings.update({"notify": {"ntfy": {"enabled": True, "server": mock_http.url, "topic": "vc-test"}}})
    secrets.set("ntfy.token", "tk_test")
    return Notifier(settings, secrets, bus)


def test_finished_job_shows_toast_but_waits_for_queue_to_empty(notifier, events, mock_http):
    notifier.on_job(job("image", "done", "生圖：鏡頭 1", message="完成"), 1)

    toasts = events("notify")
    assert len(toasts) == 1
    assert toasts[0] == {"level": "success", "title": "生圖：鏡頭 1", "message": "完成", "jobId": "j-生圖：鏡頭 1",
                         "pid": "p1", "kind": "image", "status": "done"}
    assert mock_http.wait_for_calls(1, timeout=0.5) == []


def test_drained_queue_sends_one_summary_push(notifier, events, mock_http):
    notifier.on_job(job("image", "done", "生圖：鏡頭 1"), 1)
    notifier.on_job(job("video", "failed", "影片：鏡頭 2", error="缺少模型"), 0)

    calls = mock_http.wait_for_calls(1)
    assert len(calls) == 1
    call = calls[0]
    assert (call.method, call.path) == ("POST", "/")
    assert call.headers["authorization"] == "Bearer tk_test"
    assert call.headers["content-type"] == "application/json"
    assert call.json() == {
        "topic": "vc-test",
        "title": "Video Console",
        "message": "1 個工作完成，1 個失敗\n✗ 影片：鏡頭 2\n✓ 生圖：鏡頭 1",
        "tags": ["warning"],
    }
    error_toast = events("notify")[1]
    assert (error_toast["level"], error_toast["message"]) == ("error", "缺少模型")
    assert "tk_test" not in json.dumps(events("notify"), ensure_ascii=False)


def test_each_drain_starts_a_new_batch(notifier, mock_http):
    notifier.on_job(job("image", "done", "生圖：鏡頭 1"), 0)
    assert len(mock_http.wait_for_calls(1)) == 1
    notifier.on_job(job("export", "done", "輸出影片：短片"), 0)

    calls = mock_http.wait_for_calls(2)
    assert [c.json()["message"] for c in calls] == ["完成：生圖：鏡頭 1", "完成：輸出影片：短片"]
    assert calls[1].json()["tags"] == ["white_check_mark"]


def test_failures_only_mode_skips_clean_batches_and_lists_failures(notifier, settings, events, mock_http):
    settings.update({"notify": {"ntfy": {"when": "failed"}}})
    notifier.on_job(job("image", "done", "生圖：鏡頭 1"), 0)
    assert mock_http.wait_for_calls(1, timeout=0.5) == []
    assert len(events("notify")) == 1  # the open window still gets its toast

    notifier.on_job(job("image", "done", "生圖：鏡頭 2"), 2)
    notifier.on_job(job("video", "failed", "影片：鏡頭 3", error="逾時"), 1)
    notifier.on_job(job("video", "failed", "影片：鏡頭 4", error="缺少模型"), 0)

    calls = mock_http.wait_for_calls(1)
    assert calls[0].json()["message"] == "2 個工作失敗\n✗ 影片：鏡頭 3\n✗ 影片：鏡頭 4"


def test_transcription_is_announced(notifier, events, mock_http):
    notifier.on_job(job("transcribe", "done", "語音轉字幕：鏡頭 1", message="已加入 3 句字幕"), 0)

    assert events("notify")[0]["message"] == "已加入 3 句字幕"
    assert mock_http.wait_for_calls(1)[0].json()["message"] == "完成：語音轉字幕：鏡頭 1"


@pytest.mark.parametrize("finished", [
    job("ingest", "done", "匯入：a.png"),     # bookkeeping jobs stay quiet
    job("image", "canceled", "生圖：鏡頭 1"),  # the user stopped it on purpose
])
def test_quiet_jobs_send_nothing(notifier, events, mock_http, finished):
    notifier.on_job(finished, 0)

    assert events("notify") == []
    assert mock_http.wait_for_calls(1, timeout=0.5) == []


def test_push_switched_off_keeps_toasts_only(notifier, settings, events, mock_http):
    settings.update({"notify": {"ntfy": {"enabled": False}}})
    notifier.on_job(job("image", "done", "生圖：鏡頭 1"), 0)

    assert len(events("notify")) == 1
    assert mock_http.wait_for_calls(1, timeout=0.5) == []


def test_failed_background_push_is_logged_without_raising(notifier, mock_http, caplog):
    mock_http.route("POST", "/", (500, {"error": "boom"}))
    notifier.on_job(job("image", "done", "生圖：鏡頭 1"), 0)

    assert len(mock_http.wait_for_calls(1)) == 1
    for _ in range(40):  # the log line follows the response on the push thread
        if "ntfy push failed" in caplog.text:
            break
        time.sleep(0.05)
    assert "ntfy push failed: 伺服器錯誤（500）：boom" in caplog.text
    assert "tk_test" not in caplog.text


def test_test_push_uses_saved_settings(notifier, mock_http):
    result = notifier.test()

    assert result == {"ok": True, "message": "已送出測試通知到主題「vc-test」，請查看手機"}
    body = mock_http.calls[0].json()
    assert (body["message"], body["tags"]) == ("測試通知：手機推播設定成功", ["bell"])


def test_test_push_reports_server_refusal(notifier, mock_http):
    mock_http.route("POST", "/", (401, {"code": 40101, "http": 401, "error": "unauthorized"}))

    with pytest.raises(AppError) as err:
        notifier.test()
    assert err.value.status == 502
    assert err.value.message == "ntfy 推播失敗：金鑰無效或未授權（401）：unauthorized。請檢查金鑰或存取權杖是否正確、是否過期"


def test_test_push_needs_a_topic(settings, secrets, bus):
    with pytest.raises(AppError, match="請先填寫 ntfy 主題"):
        Notifier(settings, secrets, bus).test()


def test_summary_of_one_failure_includes_trimmed_error():
    message, tags = summarize([{"status": "failed", "title": "影片：鏡頭 3", "error": "x" * 300}])

    assert message == "失敗：影片：鏡頭 3\n" + "x" * 200
    assert tags == ["warning"]


def test_summary_lists_failures_first_and_caps_the_list():
    batch = [{"status": "done", "title": f"生圖：鏡頭 {i}", "error": ""} for i in range(1, 7)]
    batch.append({"status": "failed", "title": "影片：鏡頭 7", "error": "逾時"})

    message, tags = summarize(batch)

    lines = message.split("\n")
    assert lines[0] == "6 個工作完成，1 個失敗"
    assert lines[1] == "✗ 影片：鏡頭 7"
    assert len(lines) == 1 + MAX_LISTED + 1
    assert lines[-1] == "…還有 2 個"
    assert tags == ["warning"]
