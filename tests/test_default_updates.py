"""Saved settings still on the pre-2026-10-23 defaults are offered once, and only the chosen ones switch."""
from __future__ import annotations

import copy

from vc import default_updates
from vc.settings import DEFAULTS


def _old_routes() -> list:
    routes = copy.deepcopy(DEFAULTS["routes"])
    routes[1]["video"] = {"backend": "provider", "provider": "kling"}
    return routes


def test_fresh_settings_have_nothing_to_offer(settings):
    assert default_updates.pending(settings.get()) == []


def test_old_kling_and_qwen_picks_are_listed(settings):
    settings.update({
        "routes": _old_routes(),
        "llm": {"model": "qwen2.5:7b-instruct"},
        "providers": {
            "fal-video": {"model": "fal-ai/kling-video/v2.1/pro/image-to-video", "modelT2V": "luma/agent/ray/v3.2/text-to-video"},
            "replicate-video": {"model": "kwaivgi/kling-v2.1", "imageField": "start_image", "durations": "5,10"},
        },
    })

    items = {i["key"]: i for i in default_updates.pending(settings.get())}

    assert set(items) == {"route", "fal-video.model", "replicate-video.model", "llm"}
    assert (items["route"]["current"], items["route"]["suggested"]) == ("可靈 Kling", "Google Gemini Omni")
    assert items["llm"]["suggested"] == "gemma3:4b"


def test_apply_switches_only_the_chosen_items_and_asks_once(settings):
    settings.update({
        "routes": _old_routes(),
        "llm": {"model": "qwen2.5:7b-instruct"},
        "providers": {"replicate-video": {"model": "kwaivgi/kling-v2.1", "imageField": "start_image",
                                          "durations": "5,10", "extra": "{\"go_fast\": true}"}},
    })

    saved = default_updates.apply(settings, ["route", "replicate-video.model", "not-offered"])

    assert saved["routes"][1]["video"] == {"backend": "provider", "provider": "veo"}
    replicate = saved["providers"]["replicate-video"]
    assert (replicate["model"], replicate["imageField"], replicate["durations"]) == ("lightricks/ltx-2.5-fast", "image", "6,8,10")
    assert replicate["extra"] == "{\"go_fast\": true}"
    assert saved["llm"]["model"] == "qwen2.5:7b-instruct"  # kept: not chosen
    assert default_updates.pending(saved) == []


def test_keeping_everything_still_answers_the_question(settings):
    settings.update({"llm": {"model": "qwen2.5:7b-instruct"}})

    saved = default_updates.apply(settings, [])

    assert saved["llm"]["model"] == "qwen2.5:7b-instruct"
    assert default_updates.pending(saved) == []
