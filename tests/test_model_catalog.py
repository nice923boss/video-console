"""Model detection: vendor model lists, the saved-model checks and the warnings they produce."""
from __future__ import annotations

import json
import urllib.parse
from pathlib import Path
from typing import Any, Iterator

import pytest

from test_server import Client
from vc.app import App
from vc.providers import catalog, hosted
from vc import providers
from vc.providers.catalog import (ModelCatalog, family_of, model_status, model_warning, pick_newest, same_line,
                                  tier_of)

KEY = "vc-test-key-0123456789abcdef"  # a made-up value, never a real credential
FAL_BASE = {"status": "active", "sort": "recent", "limit": 10, "expand": "openapi-3.0"}
VIDEO_DEFAULTS = providers.get("fal-video").resolve_config({})
DEFAULT_I2V, DEFAULT_T2V = VIDEO_DEFAULTS["model"], VIDEO_DEFAULTS["modelT2V"]
KLING_I2V = "fal-ai/kling-video/v2.1/standard/image-to-video"
KLING_T2V = "fal-ai/kling-video/v2.1/master/text-to-video"
# made-up future versions, so the tests do not depend on what fal lists today
TURBO_STD_I2V = "fal-ai/kling-video/v9/turbo/standard/image-to-video"
TURBO_PRO_I2V = "fal-ai/kling-video/v9/turbo/pro/image-to-video"
TURBO_PRO_T2V = "fal-ai/kling-video/v9/turbo/pro/text-to-video"
LTX_PRO_I2V = "lightricks/ltx-9/image-to-video/pro"
LTX_FAST_I2V = "lightricks/ltx-9/image-to-video/fast"
LTX_FAST_T2V = "lightricks/ltx-9/text-to-video/fast"


@pytest.fixture()
def app(tmp_path: Path) -> Iterator[App]:
    data = tmp_path / "data"
    data.mkdir()
    servers = [{"id": "local", "name": "x", "url": "http://127.0.0.1:9", "enabled": False}]
    (data / "settings.json").write_text(json.dumps({"comfy": {"servers": servers}}), encoding="utf-8")
    instance = App(data)
    instance.start(port=0, watchdog=False)
    yield instance
    instance.stop()


@pytest.fixture()
def client(app: App) -> Client:
    return Client(app.server.port)


@pytest.fixture()
def fal(mock_http, monkeypatch: pytest.MonkeyPatch):
    """fal.ai's model list and queue on the mock server."""
    monkeypatch.setattr(catalog, "FAL_MODELS_API", f"{mock_http.url}/v1/models")
    monkeypatch.setattr(hosted, "FAL_QUEUE", mock_http.url)
    return mock_http


def fal_search(category: str, q: str = "") -> str:
    params = {**FAL_BASE, "category": category, **({"q": q} if q else {})}
    return "/v1/models?" + urllib.parse.urlencode(params)


def fal_lookup(*ids: str) -> str:
    return "/v1/models?" + urllib.parse.urlencode({"endpoint_id": list(ids), "expand": "openapi-3.0"}, doseq=True)


def fal_model(eid: str, date: str = "2025-01-01", *, status: str = "active", required: tuple = ("prompt",),
              props: tuple = ("prompt", "image_url", "negative_prompt"), durations: tuple = ()) -> dict:
    properties: dict[str, Any] = {p: {"type": "string"} for p in props}
    if durations:
        properties["duration"] = {"enum": list(durations)}
    return {"endpoint_id": eid,
            "metadata": {"display_name": eid.split("/")[2] if eid.count("/") > 2 else eid, "status": status,
                         "date": f"{date}T08:00:00.000Z"},
            "openapi": {"components": {"schemas": {"ModelInput": {"required": list(required),
                                                                   "properties": properties}}}}}


def provider(client: Client, pid: str) -> dict:
    return next(p for p in client.json("GET", "/api/providers") if p["id"] == pid)


def route_kling(mock_http, i2v: str = KLING_I2V, t2v: str = KLING_T2V,
                newer: tuple[str, str, str] = (TURBO_PRO_I2V, TURBO_STD_I2V, TURBO_PRO_T2V)) -> None:
    """The given models are retired on fal; `newer` (two image-to-video tiers and a text-to-video model of the
    same family, v9 turbo by default) replaces them. Two unusable models sit in the general list."""
    pro_i2v, std_i2v, new_t2v = newer
    family = family_of(i2v)
    mock_http.route("GET", fal_lookup(*sorted({i2v, t2v})), (200, {"models": [
        fal_model(t2v, "2025-06-01", status="deprecated"), fal_model(i2v, "2025-06-01", status="deprecated")]}))
    mock_http.route("GET", fal_search("image-to-video"), (200, {"models": [
        fal_model(std_i2v, "2025-09-20", required=("prompt", "image_url"), durations=("5", "10")),
        fal_model("fal-ai/other/first-last-frame", "2025-10-01", required=("prompt", "image_url", "end_image_url"),
                  props=("prompt", "image_url", "end_image_url")),
        fal_model("fal-ai/other/many-images", "2025-10-02", required=("prompt", "image_urls"),
                  props=("prompt", "image_urls"))]}))
    mock_http.route("GET", fal_search("image-to-video", family), (200, {"models": [
        fal_model(pro_i2v, "2025-09-23", required=("prompt", "image_url")),
        fal_model(std_i2v, "2025-09-20", required=("prompt", "image_url"), durations=("5", "10")),
        fal_model(i2v, "2025-06-01", status="deprecated")]}))
    mock_http.route("GET", fal_search("text-to-video"), (200, {"models": []}))
    mock_http.route("GET", fal_search("text-to-video", family), (200, {"models": [
        fal_model(new_t2v, "2025-09-23", props=("prompt", "aspect_ratio", "duration"))]}))


# ---------------------------------------------------------------- naming rules
def test_family_and_tier_come_from_the_model_id() -> None:
    assert family_of(KLING_I2V) == "kling"
    assert family_of("fal-ai/minimax/hailuo-02/standard/image-to-video") == "hailuo"
    assert family_of("fal-ai/flux-pro/v1.1") == "flux"
    assert family_of("fal-ai/wan-i2v") == "wan"
    assert family_of("gpt-image-1-mini") == "gpt-image"
    assert family_of("models/gemini-2.5-flash-image") == "gemini"
    assert family_of("veo-3.0-fast-generate-001") == "veo"
    assert tier_of(KLING_I2V) == {"standard"}
    assert tier_of(TURBO_STD_I2V) == {"turbo", "standard"}
    assert tier_of("gemini-3-pro-image-preview") == {"pro"}


def test_newest_keeps_the_family_and_tier_and_prefers_stable() -> None:
    options = [{"value": TURBO_PRO_I2V}, {"value": TURBO_STD_I2V}, {"value": "fal-ai/wan-25/image-to-video"}]
    assert pick_newest(KLING_I2V, options) == TURBO_STD_I2V  # not the pricier pro tier
    assert pick_newest("fal-ai/kling-video/v2.1/master/image-to-video", options) == TURBO_PRO_I2V  # no master left
    assert pick_newest("fal-ai/hidream/i1", options) is None
    gemini = [{"value": "gemini-3.1-flash-image-preview", "preview": True}, {"value": "gemini-2.5-flash-image"}]
    assert pick_newest("gemini-2.5-flash-image", gemini) == "gemini-2.5-flash-image"
    assert pick_newest("gemini-2.0-flash-image", [gemini[0]]) == "gemini-3.1-flash-image-preview"


def test_same_line_means_a_newer_version_at_the_same_tier() -> None:
    assert same_line(KLING_I2V, TURBO_STD_I2V)  # standard -> turbo/standard keeps the tier
    assert not same_line(KLING_I2V, TURBO_PRO_I2V)
    assert same_line("fal-ai/flux/dev", "fal-ai/flux-2/dev")
    assert not same_line("fal-ai/flux/dev", "blackforestlabs/flux-3/klein/text-to-image")  # a lighter model
    assert not same_line("fal-ai/wan-i2v", "fal-ai/wan-25/turbo/image-to-video")  # no tier -> only untiered ones
    assert not same_line("fal-ai/hidream/i1", "fal-ai/flux/dev")


def test_status_and_warning_of_a_saved_model() -> None:
    entry = {"fields": {"model": {"options": [{"value": "gpt-image-1.5"}], "suggested": "gpt-image-1.5",
                                  "known": {"dall-e-2": "deprecated"}, "complete": True}}}
    assert model_status(None, "model", "gpt-image-1") == "unknown"
    assert model_status(entry, "model", "gpt-image-1.5") == "ok"
    assert model_status(entry, "model", "dall-e-2") == "deprecated"
    assert model_status(entry, "model", "gpt-image-1") == "missing"
    assert model_warning(entry, "model", "gpt-image-1.5", "OpenAI") is None
    warning = model_warning(entry, "model", "gpt-image-1", "OpenAI")
    assert "gpt-image-1" in warning and "改用 gpt-image-1.5" in warning
    partial = {"fields": {"model": {"options": [], "known": {}, "complete": False}}}
    assert model_status(partial, "model", "anything") == "unknown"


def test_catalog_entries_belong_to_their_source_and_survive_a_restart(tmp_path: Path) -> None:
    path = tmp_path / "model-catalog.json"
    store = ModelCatalog(path)
    store.put("openai-images", "https://api.openai.com/v1", {"model": {"options": []}})
    assert store.get("openai-images", "https://other.example/v1") is None
    assert ModelCatalog(path).get("openai-images", "https://api.openai.com/v1")["fields"] == {"model": {"options": []}}
    path.write_text("{broken", encoding="utf-8")
    assert ModelCatalog(path).get("openai-images", "https://api.openai.com/v1") is None


# ---------------------------------------------------------------- fal.ai through the app
def test_fal_lists_current_models_and_moves_retired_defaults_to_the_newest(client: Client, fal) -> None:
    # the day fal retires today's built-in defaults
    route_kling(fal, DEFAULT_I2V, DEFAULT_T2V, (LTX_PRO_I2V, LTX_FAST_I2V, LTX_FAST_T2V))
    info = provider(client, "fal-video")
    assert info["modelKeys"] == {"i2v": "model", "t2v": "modelT2V"} and info["models"] is None

    result = client.json("POST", "/api/providers/fal-video/models", {})
    i2v = result["models"]["fields"]["model"]
    assert [o["value"] for o in i2v["options"]] == [LTX_PRO_I2V, LTX_FAST_I2V]  # newest first, unusable ones gone
    assert i2v["options"][1]["hint"] == "秒數 5、10" and i2v["options"][1]["date"] == "2025-09-20"
    assert i2v["known"] == {DEFAULT_I2V: "deprecated"} and i2v["suggested"] == LTX_FAST_I2V
    assert result["models"]["fields"]["modelT2V"]["suggested"] == LTX_FAST_T2V
    # nothing was saved for this provider, so its retired defaults follow the newest models
    assert result["pinned"] == {"model": LTX_FAST_I2V, "modelT2V": LTX_FAST_T2V}
    config = provider(client, "fal-video")["config"]
    assert (config["model"], config["modelT2V"]) == (LTX_FAST_I2V, LTX_FAST_T2V)
    assert provider(client, "fal-video")["models"]["fields"]["model"]["suggested"] == LTX_FAST_I2V
    assert all("authorization" not in c.headers for c in fal.calls)  # the model list needs no key


def test_a_saved_retired_model_is_kept_and_flagged_when_a_job_goes_out(client: Client, fal, app: App) -> None:
    route_kling(fal)
    client.json("PUT", "/api/providers/fal-video", {"config": {"model": KLING_I2V, "modelT2V": KLING_T2V}})
    client.json("PUT", "/api/secrets", {"name": "provider.fal-image.api_key", "value": KEY})
    result = client.json("POST", "/api/providers/fal-video/models", {})
    assert result["pinned"] == {}
    assert provider(client, "fal-video")["config"]["modelT2V"] == KLING_T2V  # never switched behind the user's back
    assert {c.headers.get("authorization") for c in fal.calls} == {f"Key {KEY}"}
    assert KEY not in app.paths.model_catalog_file.read_text(encoding="utf-8")

    project = client.json("POST", "/api/projects", {"name": "模型檢查"})["project"]
    body = {"kind": "video", "pid": project["id"], "shotId": project["shots"][0]["id"],
            "payload": {"provider": "fal-video", "prompt": "清晨漁港"}}
    job = client.wait_job(client.json("POST", "/api/jobs", body)["job"]["id"], timeout=30)
    assert any(KLING_T2V in n and "停用" in n and f"改用 {TURBO_PRO_T2V}" in n for n in job["notes"]), job["notes"]
    assert any(c.method == "POST" and c.path == f"/{KLING_T2V}" for c in fal.calls)  # the job still went out


def test_fal_lengths_come_from_each_models_schema() -> None:
    def with_duration(spec: dict) -> dict:
        model = fal_model("fal-ai/x/image-to-video")
        model["openapi"]["components"]["schemas"]["ModelInput"]["properties"]["duration"] = spec
        return model

    assert catalog._fal_durations(with_duration({"enum": ["5", "10"]})) == ["5", "10"]
    assert catalog._fal_durations(with_duration({"enum": ["4s", "6s", "8s"]})) == ["4s", "6s", "8s"]
    assert catalog._fal_durations(with_duration({"type": "integer", "minimum": 3, "maximum": 6})) == [3, 4, 5, 6]
    assert catalog._fal_durations(with_duration({"type": "string"})) is None  # open-ended: the configured lengths
    assert catalog._fal_durations(fal_model("fal-ai/wan-i2v")) == []  # no duration parameter: Wan counts frames
    assert catalog._fal_durations({"endpoint_id": "fal-ai/x"}) is None  # no schema, nothing proven


def test_a_shot_asks_the_model_for_one_of_its_own_lengths(client: Client, fal) -> None:
    i2v, t2v = TURBO_STD_I2V, TURBO_PRO_T2V
    fal.route("GET", fal_lookup(*sorted({i2v, t2v})), (200, {"models": [
        fal_model(i2v, required=("prompt", "image_url")),
        fal_model(t2v, props=("prompt", "aspect_ratio"), durations=("4s", "6s", "8s"))]}))
    for category in ("image-to-video", "text-to-video"):
        fal.route("GET", fal_search(category), (200, {"models": []}))
        fal.route("GET", fal_search(category, "kling"), (200, {"models": []}))
    client.json("PUT", "/api/providers/fal-video", {"config": {"model": i2v, "modelT2V": t2v}})
    client.json("PUT", "/api/secrets", {"name": "provider.fal-image.api_key", "value": KEY})
    assert provider(client, "fal-video")["durations"] == {"i2v": [6.0, 8.0, 10.0], "t2v": [6.0, 8.0, 10.0]}  # not detected yet
    fields = client.json("POST", "/api/providers/fal-video/models", {})["models"]["fields"]
    assert fields["modelT2V"]["durations"] == {t2v: ["4s", "6s", "8s"]} and fields["model"]["durations"] == {i2v: []}
    assert provider(client, "fal-video")["durations"] == {"i2v": [], "t2v": [4.0, 6.0, 8.0]}

    project = client.json("POST", "/api/projects", {"name": "秒數"})["project"]
    pid, sid = project["id"], project["shots"][0]["id"]
    body = {"kind": "video", "pid": pid, "shotId": sid, "payload": {"provider": "fal-video", "prompt": "清晨漁港"}}

    def sent_duration(patch: dict) -> tuple[Any, list[str]]:
        client.json("POST", f"/api/projects/{pid}/ops", {"ops": [{"op": "shot.update", "id": sid, "patch": patch}]})
        before = len(fal.calls)
        job = client.wait_job(client.json("POST", "/api/jobs", body)["job"]["id"], timeout=30)
        post = next(c for c in fal.calls[before:] if c.method == "POST" and c.path == f"/{t2v}")
        return post.json().get("duration"), job["notes"]

    duration, notes = sent_duration({"videoDuration": 10})
    assert duration == "8s" and any("沒有 10 秒，改用 8 秒" in n for n in notes), notes
    duration, notes = sent_duration({"videoDuration": None, "duration": 6.2})  # automatic: nearest the shot length
    assert duration == "6s" and not any("改用" in n for n in notes), notes


def test_a_draft_is_checked_without_touching_the_settings(client: Client, fal) -> None:
    route_kling(fal)
    result = client.json("POST", "/api/providers/fal-video/models",
                         {"config": {"model": KLING_I2V, "modelT2V": KLING_T2V}})
    assert result["pinned"] == {} and result["models"]["fields"]["model"]["known"] == {KLING_I2V: "deprecated"}
    assert provider(client, "fal-video")["config"]["model"] == DEFAULT_I2V  # still the built-in default


def test_a_working_default_is_not_swapped_for_a_different_tier(client: Client, fal) -> None:
    t2i, i2i = "fal-ai/flux/dev", "fal-ai/flux/dev/image-to-image"
    fal.route("GET", fal_lookup(t2i, i2i), (200, {"models": [
        fal_model(t2i, props=("prompt", "image_size", "seed")),
        fal_model(i2i, required=("prompt", "image_url", "mask_url"), props=("prompt", "image_url", "mask_url"))]}))
    fal.route("GET", fal_search("text-to-image"), (200, {"models": [
        fal_model("blackforestlabs/flux-3/klein/text-to-image", "2026-10-14")]}))
    fal.route("GET", fal_search("image-to-image"), (200, {"models": [
        fal_model("fal-ai/flux-2/klein/edit", "2026-10-01", required=("prompt", "image_url"))]}))
    for category in ("text-to-image", "image-to-image"):
        fal.route("GET", fal_search(category, "flux"), (200, {"models": []}))
    result = client.json("POST", "/api/providers/fal-image/models", {})
    fields = result["models"]["fields"]
    assert fields["model"]["known"] == {t2i: "ok"} and fields["model"]["suggested"].endswith("klein/text-to-image")
    assert fields["modelI2I"]["known"] == {i2i: "incompatible"}  # needs a mask the app never sends
    # flux/dev still works, so it stays; the unusable image-to-image default moves to the suggestion
    assert result["pinned"] == {"modelI2I": "fal-ai/flux-2/klein/edit"}


def test_one_unknown_fal_id_does_not_hide_the_others(client: Client, fal) -> None:
    i2i = "fal-ai/flux/dev/image-to-image"
    client.json("PUT", "/api/providers/fal-image", {"config": {"model": "fal-ai/flux/typo"}})
    # fal answers 404 for a batch with one unknown id, so each id is looked up on its own; a lookup can also
    # come back without the model's OpenAPI document, which proves nothing either way
    fal.route("GET", fal_lookup(i2i), (200, {"models": [
        {k: v for k, v in fal_model(i2i).items() if k != "openapi"}]}))
    for category in ("text-to-image", "image-to-image"):
        fal.route("GET", fal_search(category), (200, {"models": []}))
        fal.route("GET", fal_search(category, "flux"), (200, {"models": []}))
    fields = client.json("POST", "/api/providers/fal-image/models", {})["models"]["fields"]
    assert fields["model"]["known"] == {"fal-ai/flux/typo": "unlisted"}
    assert fields["modelI2I"]["known"] == {i2i: "ok"}
    assert any(c.path == fal_lookup(i2i, "fal-ai/flux/typo") for c in fal.calls)


def test_fal_rate_limit_reads_as_a_retry_hint(client: Client, fal) -> None:
    fal.route("GET", fal_lookup(*sorted({DEFAULT_I2V, DEFAULT_T2V})), (429, {"detail": "rate limited"}))
    for category in ("image-to-video", "text-to-video"):
        fal.route("GET", fal_search(category), (200, {"models": []}))
        fal.route("GET", fal_search(category, family_of(DEFAULT_I2V)), (200, {"models": []}))
    error = client.json("POST", "/api/providers/fal-video/models", {}, status=429)
    assert "重新偵測" in error["error"]


# ---------------------------------------------------------------- OpenAI and Google through the app
def test_openai_lists_image_models_and_marks_shutdowns(client: Client, mock_http) -> None:
    client.json("PUT", "/api/providers/openai-images", {"config": {"baseUrl": f"{mock_http.url}/v1", "model": "dall-e-2"}})
    client.json("PUT", "/api/secrets", {"name": "provider.openai-images.api_key", "value": KEY})
    mock_http.route("GET", "/v1/models", (200, {"data": [
        {"id": "gpt-4o", "created": 1715000000}, {"id": "gpt-image-1", "created": 1745000000},
        {"id": "gpt-image-1.5", "created": 1765000000}, {"id": "dall-e-3", "created": 1698000000},
        {"id": "dall-e-2", "created": 1698000001, "shutdown_date": "2026-05-12"}]}))
    field = client.json("POST", "/api/providers/openai-images/models", {})["models"]["fields"]["model"]
    assert [o["value"] for o in field["options"]] == ["gpt-image-1.5", "gpt-image-1", "dall-e-3"]
    assert field["known"] == {"dall-e-2": "deprecated"} and field["complete"] is True
    assert mock_http.calls[-1].headers["authorization"] == f"Bearer {KEY}"
    assert model_status({"fields": {"model": field}}, "model", "gpt-image-0") == "missing"


def test_google_lists_gemini_image_and_omni_video_models_from_every_page(client: Client, mock_http) -> None:
    base = f"{mock_http.url}/v1beta"
    client.json("PUT", "/api/providers/gemini-image", {"config": {"baseUrl": base}})
    # a model saved in the Veo days, before Google shut Veo down on the Gemini API
    client.json("PUT", "/api/providers/veo", {"config": {"baseUrl": base, "model": "veo-3.0-fast-generate-001"}})
    client.json("PUT", "/api/secrets", {"name": "provider.gemini-image.api_key", "value": KEY})
    gen, lro = ["generateContent", "countTokens"], ["predictLongRunning"]
    mock_http.route("GET", "/v1beta/models?pageSize=1000", (200, {"nextPageToken": "p2", "models": [
        {"name": "models/gemini-2.5-flash", "supportedGenerationMethods": gen},
        {"name": "models/gemini-2.5-flash-image", "displayName": "Nano Banana", "supportedGenerationMethods": gen},
        {"name": "models/gemini-omni-1.1-flash", "displayName": "Gemini Omni Flash", "supportedGenerationMethods": gen},
        {"name": "models/veo-3.0-fast-generate-001", "supportedGenerationMethods": lro}]}))
    mock_http.route("GET", "/v1beta/models?pageSize=1000&pageToken=p2", (200, {"models": [
        {"name": "models/gemini-3-pro-image-preview", "supportedGenerationMethods": gen},
        {"name": "models/gemini-omni-flash-preview", "supportedGenerationMethods": []},  # listed methods are ignored
        {"name": "models/veo-3.1-fast-generate-preview", "supportedGenerationMethods": lro}]}))

    image = client.json("POST", "/api/providers/gemini-image/models", {})["models"]["fields"]["model"]
    assert [o["value"] for o in image["options"]] == ["gemini-3-pro-image-preview", "gemini-2.5-flash-image"]
    assert image["options"][1]["label"] == "Nano Banana" and image["suggested"] == "gemini-2.5-flash-image"
    entry = client.json("POST", "/api/providers/veo/models", {})["models"]
    video = entry["fields"]["model"]
    assert [o["value"] for o in video["options"]] == ["gemini-omni-1.1-flash", "gemini-omni-flash-preview"]
    assert video["options"][0]["label"] == "Gemini Omni Flash" and video["complete"] is True
    assert video["known"] == {"veo-3.0-fast-generate-001": "deprecated", "veo-3.1-fast-generate-preview": "deprecated"}
    assert video["suggested"] == "gemini-omni-1.1-flash"  # a saved Veo id points at the stable Omni
    assert {c.headers["x-goog-api-key"] for c in mock_http.calls} == {KEY}

    warning = model_warning(entry, "model", "veo-3.0-fast-generate-001", "Google")
    assert warning == "模型 veo-3.0-fast-generate-001 已被Google標記為停用，建議到「引擎 › 雲端 API」改用 gemini-omni-1.1-flash"
    gone = model_warning(entry, "model", "veo-2.0-generate-001", "Google")  # already off the list
    assert gone == "Google的模型清單裡已找不到 veo-2.0-generate-001（可能已下架），建議到「引擎 › 雲端 API」改用 gemini-omni-1.1-flash"
    assert model_warning(entry, "model", "gemini-omni-1.1-flash", "Google") is None


def test_google_key_without_omni_lists_no_video_model(client: Client, mock_http) -> None:
    client.json("PUT", "/api/providers/veo", {"config": {"baseUrl": f"{mock_http.url}/v1beta"}})
    client.json("PUT", "/api/secrets", {"name": "provider.gemini-image.api_key", "value": KEY})
    mock_http.route("GET", "/v1beta/models?pageSize=1000", (200, {"models": [
        {"name": "models/gemini-2.5-flash-image", "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/veo-3.1-generate-preview", "supportedGenerationMethods": ["predictLongRunning"]}]}))
    video = client.json("POST", "/api/providers/veo/models", {})["models"]["fields"]["model"]
    assert video["options"] == [] and video["complete"] is False
    assert video["message"] == "這把金鑰的模型清單裡沒有Gemini Omni 影片模型"
    assert video["known"] == {"veo-3.1-generate-preview": "deprecated"}


def test_vendors_without_a_model_list_refuse_detection(client: Client) -> None:
    info = provider(client, "kling")
    assert info["modelKeys"] == {} and "models" not in info
    error = client.json("POST", "/api/providers/kling/models", {}, status=400)
    assert "沒有可查詢的模型清單" in error["error"]
