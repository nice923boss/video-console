"""Model hosting platforms: fal.ai and Replicate (one key, many image and video models)."""
from __future__ import annotations

import json
import re
from pathlib import Path

from .. import net
from ..util import AppError
from . import catalog
from .base import (Context, Poll, Provider, Request, clean_model, combo, first_url, json_field, merge_extra,
                   nearest_ratio, parse_numbers, pick_duration, poll, require_host, scaled_size, secret, select, text,
                   toggle)

FAL_QUEUE = "https://queue.fal.run"
REPLICATE = "https://api.replicate.com/v1"
_TASK_ID = re.compile(r"^[A-Za-z0-9_-]{6,100}$")


def _ext_of(mime: str) -> str:
    return ".jpg" if "jpeg" in mime else ".webp" if "webp" in mime else ".png"


def _extra_keys(extra_json: str) -> set[str]:
    """Parameter names the user adds through the extra JSON (they satisfy a model's required inputs)."""
    try:
        extra = json.loads(extra_json or "{}")
    except ValueError:
        return set()
    return set(extra) if isinstance(extra, dict) else set()


def _want(category: str, sends: set[str], model: str, image: bool) -> catalog.FalWant:
    return catalog.FalWant(category, frozenset(sends), (model or "").strip().strip("/"), image)


# ------------------------------------------------------------------ fal.ai
class _Fal(Provider):
    """fal.ai queue API: submit, poll status_url, then fetch response_url."""

    vendor = "fal.ai"
    docs = "https://fal.ai/dashboard/keys"
    resumable = True

    def _auth(self, ctx: Context) -> dict[str, str]:
        return {"Authorization": f"Key {ctx.require_secret('api_key', 'fal.ai API 金鑰')}"}

    def _submit(self, ctx: Context, model: str, payload: dict) -> dict:
        model = clean_model(model)
        ctx.progress(0.05, "上傳到 fal.ai")
        data = net.request("POST", f"{FAL_QUEUE}/{model}", headers=self._auth(ctx), json_body=payload,
                           timeout=180).json()
        rid = str(data.get("request_id") or "")
        if not _TASK_ID.match(rid):
            raise AppError("fal.ai 沒有回傳任務編號", 502)
        base = f"{FAL_QUEUE}/{'/'.join(model.split('/')[:2])}/requests/{rid}"
        remote = {"vendor": "fal", "model": model, "request": rid,
                  "status": data.get("status_url") or f"{base}/status",
                  "response": data.get("response_url") or base,
                  "cancel": data.get("cancel_url") or f"{base}/cancel"}
        ctx.remote(remote)
        return remote

    def _wait(self, ctx: Context, remote: dict) -> list[Path]:
        auth = self._auth(ctx)
        status_url = require_host(remote["status"], "fal.run")
        response_url = require_host(remote["response"], "fal.run")

        def fetch() -> Poll:
            data = net.request("GET", status_url, headers=auth, timeout=30).json()
            state = str(data.get("status") or "").upper()
            if state == "COMPLETED":
                if data.get("error"):
                    raise AppError(f"fal.ai 任務失敗：{str(data['error'])[:300]}", 502)
                return Poll(done=True)
            if state == "IN_QUEUE":
                pos = data.get("queue_position")
                return Poll(message=f"fal.ai 排隊中（前面 {pos} 個）" if isinstance(pos, int) else "fal.ai 排隊中")
            return Poll(message="fal.ai 生成中")

        poll(ctx, fetch, interval=2.5 if self.kind == "image" else 6, expected=self.expected_seconds)
        result = net.request("GET", response_url, headers=auth, timeout=60).json()
        if any(result.get("has_nsfw_concepts") or []):
            raise AppError("fal.ai 的安全過濾判定這張圖不適當（回傳的是黑圖），請調整提示詞", 422)
        url = first_url(result)
        if not url:
            raise AppError("fal.ai 的結果裡找不到檔案網址", 502)
        ctx.save_url(url)
        return ctx.files

    def resume(self, ctx: Context, remote: dict) -> list[Path]:
        if remote.get("vendor") != "fal" or not remote.get("status") or not remote.get("response"):
            return super().resume(ctx, remote)
        return self._wait(ctx, remote)

    def cancel_remote(self, ctx: Context, remote: dict) -> None:
        if remote.get("vendor") == "fal" and remote.get("cancel"):
            net.request("PUT", require_host(remote["cancel"], "fal.run"), headers=self._auth(ctx), timeout=15)


class FalImage(_Fal):
    id = "fal-image"
    name = "fal.ai 圖片"
    kind = "image"
    modes = ("t2i", "i2i")
    expected_seconds = 15
    notes = ("一把金鑰可用 fal.ai 上的各種模型，模型清單會自動向 fal.ai 查詢最新可用的版本，"
             "也可以從 fal.ai 模型頁的 API 說明複製代號貼上。Stability（Stable Diffusion）等沒有模型清單的廠商，"
             "較新的模型可從這裡選。這把金鑰也會給「fal.ai 影片」使用。")
    model_keys = {"t2i": "model", "i2i": "modelI2I"}
    fields = (
        combo("model", "文字生圖模型", ["fal-ai/flux/dev", "fal-ai/flux/schnell", "fal-ai/flux-pro/v1.1"],
              "fal-ai/flux/dev"),
        combo("modelI2I", "參考圖生圖模型", ["fal-ai/flux/dev/image-to-image"], "fal-ai/flux/dev/image-to-image"),
        select("resolution", "輸出尺寸", [("1mp", "約 100 萬像素（依專案比例）"), ("project", "與專案相同")], "1mp"),
        toggle("sendNegative", "送出負向提示詞", False, help="模型有 negative_prompt 參數時再開啟（Flux 沒有）"),
        json_field("extra", "額外參數（JSON）", "{}", help='會合併進送出的參數，例如 {"num_inference_steps": 28}'),
    )

    def run(self, ctx: Context, req: Request) -> list[Path]:
        cfg = ctx.config
        if cfg["resolution"] == "1mp":
            width, height = scaled_size(req.width, req.height, 1.0, 16)
        else:
            width, height = max(64, req.width // 16 * 16), max(64, req.height // 16 * 16)
        payload: dict = {"prompt": req.prompt, "image_size": {"width": width, "height": height},
                         "seed": req.seed % 4294967296, "num_images": 1, "enable_safety_checker": True}
        if cfg["sendNegative"] and req.negative:
            payload["negative_prompt"] = req.negative
        model = cfg["model"]
        if req.image is not None:
            model = cfg["modelI2I"]
            payload["image_url"] = req.image_data_uri()
            payload["strength"] = round(req.denoise, 2)
        return self._wait(ctx, self._submit(ctx, model, merge_extra(payload, cfg["extra"])))

    def detect_models(self, ctx: Context) -> dict[str, dict]:
        cfg = ctx.config
        sends = {"prompt", "image_size", "seed", "num_images", "enable_safety_checker"}
        sends |= {"negative_prompt"} if cfg["sendNegative"] else set()
        return catalog.detect_fal(ctx, {
            "model": _want("text-to-image", sends, cfg["model"], False),
            "modelI2I": _want("image-to-image", sends | {"image_url", "strength"}, cfg["modelI2I"], True),
        }, _extra_keys(cfg["extra"]))


class FalVideo(_Fal):
    id = "fal-video"
    name = "fal.ai 影片"
    kind = "video"
    modes = ("i2v", "t2v")
    key_owner = "fal-image"
    expected_seconds = 180
    notes = ("共用「fal.ai 圖片」的金鑰。可靈（Kling）、海螺（MiniMax Hailuo）、Wan 等較新的版本都從這裡選，"
             "模型清單會自動向 fal.ai 查詢，每個模型能設定的秒數也一起偵測，鏡頭的「影片秒數」只會列出該模型支援的值。"
             "Wan 沒有秒數參數，用影格數控制長度，請在額外參數填 num_frames。")
    model_keys = {"i2v": "model", "t2v": "modelT2V"}
    fields = (
        combo("model", "圖轉影片模型",
              ["fal-ai/kling-video/v3/turbo/standard/image-to-video", "fal-ai/kling-video/v3/turbo/pro/image-to-video",
               "fal-ai/minimax/hailuo-02/standard/image-to-video", "fal-ai/wan-i2v"],
              "fal-ai/kling-video/v3/turbo/standard/image-to-video"),
        combo("modelT2V", "文字轉影片模型",
              ["fal-ai/kling-video/v3/turbo/pro/text-to-video", "fal-ai/kling-video/v3/turbo/standard/text-to-video",
               "fal-ai/wan-t2v"],
              "fal-ai/kling-video/v3/turbo/pro/text-to-video"),
        text("durations", "可用秒數", "5,10",
             help="偵測不到模型的秒數時才使用：以逗號分隔，送出時取最接近的值；清空表示不送秒數"),
        toggle("sendNegative", "送出負向提示詞", False, help="模型有 negative_prompt 參數時再開啟（Kling v3、海螺沒有）"),
        json_field("extra", "額外參數（JSON）", "{}"),
    )

    def run(self, ctx: Context, req: Request) -> list[Path]:
        cfg = ctx.config
        payload: dict = {"prompt": req.prompt[:2500]}
        allowed = self.durations(cfg, req.mode, ctx.models)
        if allowed:
            payload["duration"] = pick_duration(req.duration, allowed)
        if cfg["sendNegative"] and req.negative:
            payload["negative_prompt"] = req.negative[:2500]
        if req.image is not None:
            model = cfg["model"]
            payload["image_url"] = req.image_data_uri()
        else:
            model = cfg["modelT2V"]
            payload["aspect_ratio"] = nearest_ratio(req.width, req.height, ("16:9", "9:16", "1:1"))
        return self._wait(ctx, self._submit(ctx, model, merge_extra(payload, cfg["extra"])))

    def durations(self, config: dict, mode: str, models: dict | None) -> list:
        key = self.model_keys.get(mode, "model")
        detected = catalog.model_durations(models, key, config.get(key, ""))
        return detected if detected is not None else [f"{v:g}" for v in parse_numbers(config["durations"])]

    def detect_models(self, ctx: Context) -> dict[str, dict]:
        cfg = ctx.config
        sends = {"prompt", "duration"}  # duration goes out whenever the model has the parameter
        sends |= {"negative_prompt"} if cfg["sendNegative"] else set()
        return catalog.detect_fal(ctx, {
            "model": _want("image-to-video", sends | {"image_url"}, cfg["model"], True),
            "modelT2V": _want("text-to-video", sends | {"aspect_ratio"}, cfg["modelT2V"], False),
        }, _extra_keys(cfg["extra"]))


# ------------------------------------------------------------------ Replicate
class _Replicate(Provider):
    """Replicate predictions API: create, poll urls.get, download the first output URL."""

    vendor = "Replicate"
    docs = "https://replicate.com/account/api-tokens"
    resumable = True
    online_test = True
    secrets = (secret("api_key", "API Token"),)

    def _auth(self, ctx: Context) -> dict[str, str]:
        return {"Authorization": f"Bearer {ctx.require_secret('api_key', 'Replicate API Token')}"}

    def _upload(self, ctx: Context, req: Request) -> str:
        ctx.progress(0.03, "上傳參考圖到 Replicate")
        body, ctype = net.multipart({}, [("content", "reference" + _ext_of(req.image_mime), req.image or b"",
                                          req.image_mime)])
        data = net.request("POST", f"{REPLICATE}/files", headers={**self._auth(ctx), "Content-Type": ctype},
                           data=body, timeout=180).json()
        url = (data.get("urls") or {}).get("get")
        if not url:
            raise AppError("Replicate 沒有回傳上傳檔案的網址", 502)
        return url

    def _create(self, ctx: Context, model: str, inputs: dict, *, wait: bool) -> tuple[dict, dict]:
        model = clean_model(model)
        headers = self._auth(ctx)
        if wait:
            headers["Prefer"] = "wait=30"
        if ":" in model:
            url, body = f"{REPLICATE}/predictions", {"version": model.split(":", 1)[1], "input": inputs}
        elif re.fullmatch(r"[0-9a-f]{64}", model):
            url, body = f"{REPLICATE}/predictions", {"version": model, "input": inputs}
        elif model.count("/") == 1:
            url, body = f"{REPLICATE}/models/{model}/predictions", {"input": inputs}
        else:
            raise AppError("Replicate 模型格式應為「擁有者/模型名稱」或「擁有者/模型名稱:版本」")
        ctx.progress(0.06, "已送出到 Replicate")
        data = net.request("POST", url, headers=headers, json_body=body, timeout=120).json()
        pid = str(data.get("id") or "")
        if not _TASK_ID.match(pid):
            raise AppError("Replicate 沒有回傳任務編號", 502)
        urls = data.get("urls") or {}
        get_url = urls.get("get") or f"{REPLICATE}/predictions/{pid}"
        remote = {"vendor": "replicate", "id": pid, "get": get_url, "cancel": urls.get("cancel") or f"{get_url}/cancel"}
        ctx.remote(remote)
        return data, remote

    @staticmethod
    def _state(data: dict) -> Poll:
        status = data.get("status")
        if status == "succeeded":
            return Poll(done=True, result=data.get("output"))
        if status == "canceled":
            raise AppError("Replicate 任務已被取消", 499)
        if status in ("failed", "aborted"):
            raise AppError(f"Replicate 任務失敗：{str(data.get('error') or '未提供原因')[:300]}", 502)
        found = re.findall(r"(\d{1,3})%\|", str(data.get("logs") or "")[-4000:])
        progress = min(int(found[-1]), 100) / 100 if found else None
        return Poll(message="Replicate 啟動中" if status == "starting" else "Replicate 生成中", progress=progress)

    def _finish(self, ctx: Context, data: dict, remote: dict) -> list[Path]:
        auth = self._auth(ctx)
        get_url = require_host(remote["get"], "api.replicate.com")
        first = self._state(data) if data else Poll()
        output = first.result if first.done else poll(
            ctx, lambda: self._state(net.request("GET", get_url, headers=auth, timeout=30).json()),
            interval=2.5 if self.kind == "image" else 8, expected=self.expected_seconds)
        url = first_url(output)
        if not url:
            raise AppError("Replicate 的結果裡找不到檔案網址", 502)
        ctx.save_url(url)
        return ctx.files

    def resume(self, ctx: Context, remote: dict) -> list[Path]:
        if remote.get("vendor") != "replicate" or not remote.get("get"):
            return super().resume(ctx, remote)
        return self._finish(ctx, {}, remote)

    def cancel_remote(self, ctx: Context, remote: dict) -> None:
        if remote.get("vendor") == "replicate" and remote.get("cancel"):
            net.request("POST", require_host(remote["cancel"], "api.replicate.com"), headers=self._auth(ctx),
                        timeout=15)

    def test(self, ctx: Context) -> dict:
        data = net.request("GET", f"{REPLICATE}/account", headers=self._auth(ctx), timeout=20).json()
        who = data.get("username")
        return {"ok": True, "online": True, "message": f"連線成功（帳號 {who}）" if who else "連線成功，Token 有效"}


class ReplicateImage(_Replicate):
    id = "replicate-image"
    name = "Replicate 圖片"
    kind = "image"
    modes = ("t2i", "i2i")
    expected_seconds = 20
    notes = ("Replicate 會檢查參數名稱，換模型時請到模型頁的 API 說明確認參考圖欄位名稱。"
             "這把 Token 也會給「Replicate 影片」使用。")
    fields = (
        combo("model", "模型", ["black-forest-labs/flux-dev", "black-forest-labs/flux-schnell",
                                "black-forest-labs/flux-1.1-pro"], "black-forest-labs/flux-dev",
              help="格式：擁有者/模型名稱，或擁有者/模型名稱:版本"),
        text("imageField", "參考圖欄位名稱", "image", help="flux-dev 是 image；清空表示不送參考圖"),
        text("strengthField", "參考圖強度欄位名稱", "prompt_strength"),
        toggle("sendNegative", "送出負向提示詞", False, help="模型有 negative_prompt 參數時再開啟（Flux 沒有）"),
        json_field("extra", "額外參數（JSON）", "{}"),
    )
    ASPECTS = ("1:1", "16:9", "21:9", "3:2", "2:3", "4:5", "5:4", "3:4", "4:3", "9:16", "9:21")

    def run(self, ctx: Context, req: Request) -> list[Path]:
        cfg = ctx.config
        inputs: dict = {"prompt": req.prompt, "aspect_ratio": nearest_ratio(req.width, req.height, self.ASPECTS),
                        "seed": req.seed % 2147483647, "output_format": "png"}
        if cfg["sendNegative"] and req.negative:
            inputs["negative_prompt"] = req.negative
        if req.image is not None:
            if cfg["imageField"]:
                inputs[cfg["imageField"]] = self._upload(ctx, req)
                if cfg["strengthField"]:
                    inputs[cfg["strengthField"]] = round(req.denoise, 2)
            else:
                ctx.notes.append("沒有設定參考圖欄位名稱，已略過參考圖")
        data, remote = self._create(ctx, cfg["model"], merge_extra(inputs, cfg["extra"]), wait=True)
        return self._finish(ctx, data, remote)


class ReplicateVideo(_Replicate):
    id = "replicate-video"
    name = "Replicate 影片"
    kind = "video"
    modes = ("i2v", "t2v")
    key_owner = "replicate-image"
    expected_seconds = 240
    notes = ("共用「Replicate 圖片」的 Token。首格圖片欄位：Kling 是 start_image，Hailuo 02 是 first_frame_image，"
             "Wan 是 image；秒數選項：Kling 5、10，Hailuo 02 6、10，Wan 沒有秒數參數請清空。")
    fields = (
        combo("model", "模型", ["kwaivgi/kling-v2.1", "minimax/hailuo-02", "wan-video/wan-2.2-i2v-fast"],
              "kwaivgi/kling-v2.1"),
        text("imageField", "首格圖片欄位名稱", "start_image"),
        text("durations", "可用秒數", "5,10", help="以逗號分隔，鏡頭的「影片秒數」從這些值挑選；清空表示不送秒數"),
        toggle("sendNegative", "送出負向提示詞", False, help="模型有 negative_prompt 參數時再開啟"),
        json_field("extra", "額外參數（JSON）", "{}"),
    )

    def durations(self, config: dict, mode: str, models: dict | None) -> list:
        return [int(v) for v in parse_numbers(config["durations"])]

    def run(self, ctx: Context, req: Request) -> list[Path]:
        cfg = ctx.config
        inputs: dict = {"prompt": req.prompt}
        allowed = self.durations(cfg, req.mode, ctx.models)
        if allowed:
            inputs["duration"] = pick_duration(req.duration, allowed)
        if cfg["sendNegative"] and req.negative:
            inputs["negative_prompt"] = req.negative
        if req.image is not None:
            if not cfg["imageField"]:
                raise AppError("請先在設定填寫「首格圖片欄位名稱」")
            inputs[cfg["imageField"]] = self._upload(ctx, req)
        data, remote = self._create(ctx, cfg["model"], merge_extra(inputs, cfg["extra"]), wait=False)
        return self._finish(ctx, data, remote)
