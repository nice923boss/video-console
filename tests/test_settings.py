"""SettingsStore: defaults stay pristine, rejected patches change nothing, ntfy and LLM fields are checked."""
from __future__ import annotations

import pytest

from vc.settings import DEFAULTS, SettingsStore
from vc.util import AppError, read_json


def test_stores_never_share_or_mutate_the_defaults(tmp_path):
    first = SettingsStore(tmp_path / "a.json")
    first.set_last_project("p1")
    first.update({"notify": {"ntfy": {"topic": "vc-a"}}})

    assert DEFAULTS["ui"]["lastProjectId"] is None
    assert DEFAULTS["notify"]["ntfy"]["topic"] == ""
    second = SettingsStore(tmp_path / "b.json")
    assert second.get()["ui"]["lastProjectId"] is None
    assert second.section("notify")["ntfy"]["topic"] == ""


def test_saved_settings_survive_a_restart(tmp_path, settings):
    settings.update({"llm": {"model": "llama3"}, "notify": {"sound": False}})

    reloaded = SettingsStore(tmp_path / "settings.json")
    assert reloaded.section("llm")["model"] == "llama3"
    assert reloaded.section("notify")["sound"] is False
    assert read_json(tmp_path / "settings.json", {})["llm"]["model"] == "llama3"


def test_update_publishes_a_snapshot(settings, events):
    settings.update({"notify": {"desktop": False}})

    assert events("settings")[-1]["notify"]["desktop"] is False


@pytest.mark.parametrize("patch, message", [
    ({"notify": {"ntfy": {"enabled": True}}}, "開啟手機推播前，請先填寫 ntfy 主題"),
    ({"notify": {"ntfy": {"topic": "my topic"}}}, "ntfy 主題只能用英數字、底線、連字號"),
    ({"notify": {"ntfy": {"server": "ftp://ntfy.example"}}}, "ntfy 伺服器位址格式不正確"),
    ({"llm": {"baseUrl": "localhost:11434"}}, "LLM 服務位址格式不正確"),
    ({"llm": {"temperature": "hot"}}, "LLM 溫度必須是 0 到 2 之間的數字"),
    ({"llm": {"temperature": None}}, "LLM 溫度必須是 0 到 2 之間的數字"),
    ({"history": {"keep": "many"}}, "版本紀錄或鏡頭長度必須是數字"),
    ({"defaults": {"shotDuration": None}}, "版本紀錄或鏡頭長度必須是數字"),
    ({"bogus": {}}, "未知的設定項目：bogus"),
])
def test_bad_values_are_rejected_and_nothing_changes(settings, events, patch, message):
    before = settings.get()

    with pytest.raises(AppError, match=message):
        settings.update({"comfy": {"timeoutMin": 45}, **patch})

    assert settings.get() == before
    assert events("settings") == []


def test_values_are_tidied_on_save(settings):
    saved = settings.update({
        "notify": {"ntfy": {"enabled": 1, "topic": " vc-alerts_01 ", "server": " https://ntfy.example.com/ "}},
        "llm": {"baseUrl": "http://127.0.0.1:1234/v1/", "model": "  m " + "x" * 200, "temperature": "1.5",
                "enabled": "yes"},
    })

    assert saved["notify"]["ntfy"] == {"enabled": True, "topic": "vc-alerts_01", "server": "https://ntfy.example.com",
                                       "when": "all"}
    llm = saved["llm"]
    assert (llm["baseUrl"], llm["temperature"], llm["enabled"]) == ("http://127.0.0.1:1234/v1", 1.5, True)
    assert llm["model"] == ("m " + "x" * 200)[:120]


@pytest.mark.parametrize("given, kept", [(5, 2.0), (-1, 0.0), (0, 0.0)])
def test_temperature_is_clamped(settings, given, kept):
    assert settings.update({"llm": {"temperature": given}})["llm"]["temperature"] == kept


@pytest.mark.parametrize("given, kept", [
    ({"keep": 0, "intervalSec": 1}, {"keep": 5, "intervalSec": 10.0}),
    ({"keep": 9999, "intervalSec": 99999}, {"keep": 500, "intervalSec": 3600.0}),
    ({"keep": "80", "intervalSec": "60"}, {"keep": 80, "intervalSec": 60.0}),
])
def test_history_limits_are_clamped(settings, given, kept):
    assert settings.update({"history": given})["history"] == kept


@pytest.mark.parametrize("given, kept", [("failed", "failed"), ("sometimes", "all"), (None, "all")])
def test_push_condition_falls_back_to_every_batch(settings, given, kept):
    assert settings.update({"notify": {"ntfy": {"when": given}}})["notify"]["ntfy"]["when"] == kept


@pytest.mark.parametrize("given, kept", [(1.3, 1.3), (1.15, 1.15), (2, 1), ("1.3", 1), (True, 1)])
def test_text_scale_keeps_only_the_offered_sizes(settings, given, kept):
    assert settings.update({"ui": {"textScale": given}})["ui"]["textScale"] == kept


def test_shot_duration_default_is_clamped(settings):
    assert settings.update({"defaults": {"shotDuration": 0}})["defaults"]["shotDuration"] == 0.2


def test_label_colors_keep_only_well_formed_entries(settings):
    colors = settings.update({"ui": {"labelColors": [
        {"value": "#AABBCC", "label": "  待重拍  "},
        {"value": "#aabbcc", "label": "重複"},
        {"value": "red", "label": "不是色碼"},
        {"value": "#123456"},
        *({"value": f"#0000{i:02x}", "label": str(i)} for i in range(20)),
    ]}})["ui"]["labelColors"]

    assert colors[:2] == [{"value": "#aabbcc", "label": "待重拍"}, {"value": "#123456", "label": "#123456"}]
    assert len(colors) == 12


def test_returned_snapshot_is_a_copy(settings):
    snapshot = settings.update({"llm": {"model": "llama3"}})
    snapshot["llm"]["model"] = "changed"
    settings.get()["llm"]["model"] = "changed too"

    assert settings.section("llm")["model"] == "llama3"


def test_corrupt_stored_settings_fall_back_to_defaults(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text('{"llm": {"baseUrl": "not a url"}}', encoding="utf-8")

    assert SettingsStore(path).section("llm") == DEFAULTS["llm"]


def test_defaults_pair_local_ltx_video_with_a_cloud_route_at_1920x1080():
    local, cloud = DEFAULTS["routes"]
    assert local["video"] == {"backend": "comfy", "server": "auto", "workflow": "ltxv-2b-i2v"}
    assert cloud["video"]["backend"] == "provider"
    assert (DEFAULTS["defaults"]["width"], DEFAULTS["defaults"]["height"]) == (1920, 1080)
