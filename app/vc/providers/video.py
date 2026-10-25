"""First-party image-to-video APIs: Kling, Runway, MiniMax (Hailuo), Google Gemini Omni."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import time
import urllib.parse
from pathlib import Path

from .. import net
from ..util import AppError
from . import catalog, published
from .base import (Context, Poll, Provider, Request, b64decode_loose, clean_model, combo, nearest_ratio, number,
                   pick_duration, poll, same_host, secret, select, text, toggle, url_field, with_avoid)

_TASK_ID = re.compile(r"^[A-Za-z0-9_-]{4,120}$")
_DURATION = select("duration", "秒數", [("auto", "依鏡頭的影片秒數（5 或 10 秒）"), ("5", "固定 5 秒"), ("10", "固定 10 秒")],
                   "auto")


def _five_or_ten(choice: str) -> list[int]:
    return [5, 10] if choice == "auto" else [int(choice)]


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def kling_token(access_key: str, secret_key: str, now: float | None = None) -> str:
    """HS256 JWT as required by the Kling API (valid 30 minutes)."""
    issued = int(time.time() if now is None else now)
    head = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    body = _b64url(json.dumps({"iss": access_key, "exp": issued + 1800, "nbf": issued - 5},
                              separators=(",", ":")).encode())
    sig = hmac.new(secret_key.encode(), f"{head}.{body}".encode(), hashlib.sha256).digest()
    return f"{head}.{body}.{_b64url(sig)}"


# ------------------------------------------------------------------ Kling
class Kling(Provider):
    id = "kling"
    name = "可靈 Kling"
    kind = "video"
    vendor = "Kling AI"
    modes = ("i2v", "t2v")
    docs = "https://app.klingai.com/global/dev"
    notes = ("可靈的金鑰是一組 Access Key 與 Secret Key，兩個都要填。標準模式較便宜，專業模式畫質較好。"
             "可靈沒有提供模型清單 API，選單來自 Video Console 在 GitHub 上維護的清單，按「偵測模型」取得最新版；也可以改用「fal.ai 影片」。")
    resumable = True
    online_test = True
    expected_seconds = 240
    secrets = (secret("access_key", "Access Key"), secret("secret_key", "Secret Key"))
    fields = (
        url_field("baseUrl", "API 網址", "https://api-singapore.klingai.com",
                  help="早期申請的帳號若連不上，改成 https://api.klingai.com"),
        combo("model", "圖轉影片模型", published.options("kling", "model"), "kling-v2-1"),
        combo("modelT2V", "文字轉影片模型", published.options("kling", "modelT2V"), "kling-v2-1-master"),
        select("mode", "生成模式", [("std", "標準（較便宜）"), ("pro", "專業（畫質較好）")], "std"),
        _DURATION,
        number("cfgScale", "提示詞貼合度", 0.5, 0.0, 1.0, 0.05, help="越高越貼近提示詞，越低越自由"),
    )
    model_keys = {"i2v": "model", "t2v": "modelT2V"}

    def durations(self, config: dict, mode: str, models: dict | None) -> list:
        return [str(v) for v in _five_or_ten(config["duration"])]

    def detect_models(self, ctx: Context) -> dict[str, dict]:
        return published.detect(self.id, ctx.config, self.vendor)

    def _call(self, ctx: Context, method: str, path: str, body: dict | None = None) -> dict:
        ak = ctx.require_secret("access_key", "可靈 Access Key")
        sk = ctx.require_secret("secret_key", "可靈 Secret Key")
        data = net.request(method, ctx.config["baseUrl"] + path,
                           headers={"Authorization": f"Bearer {kling_token(ak, sk)}"}, json_body=body,
                           timeout=120).json()
        if data.get("code") not in (0, None):
            raise AppError(f"可靈回報錯誤：{data.get('message') or '未知錯誤'}（代碼 {data.get('code')}）", 502)
        return data

    def run(self, ctx: Context, req: Request) -> list[Path]:
        cfg = ctx.config
        path = "image2video" if req.image is not None else "text2video"
        body: dict = {"model_name": clean_model(cfg["model"] if req.image is not None else cfg["modelT2V"]),
                      "prompt": req.prompt[:2500], "cfg_scale": cfg["cfgScale"], "mode": cfg["mode"],
                      "duration": pick_duration(req.duration, self.durations(cfg, req.mode, None))}
        if req.negative:
            body["negative_prompt"] = req.negative[:2500]
        if req.image is not None:
            body["image"] = req.image_b64()  # raw base64, Kling rejects the data: prefix
        else:
            body["aspect_ratio"] = nearest_ratio(req.width, req.height, ("16:9", "9:16", "1:1"))
        ctx.progress(0.05, "上傳到可靈")
        task = str((self._call(ctx, "POST", f"/v1/videos/{path}", body).get("data") or {}).get("task_id") or "")
        if not _TASK_ID.match(task):
            raise AppError("可靈沒有回傳任務編號", 502)
        remote = {"vendor": "kling", "task": task, "path": path}
        ctx.remote(remote)
        return self._wait(ctx, remote)

    def _wait(self, ctx: Context, remote: dict) -> list[Path]:
        task, path = str(remote.get("task") or ""), remote.get("path")
        if not _TASK_ID.match(task) or path not in ("image2video", "text2video"):
            raise AppError("可靈任務資料不完整，無法接續", 409)

        def fetch() -> Poll:
            data = self._call(ctx, "GET", f"/v1/videos/{path}/{task}").get("data") or {}
            status = data.get("task_status")
            if status == "succeed":
                videos = (data.get("task_result") or {}).get("videos") or []
                url = videos[0].get("url") if videos else None
                if not url:
                    raise AppError("可靈任務完成但沒有影片網址", 502)
                return Poll(done=True, result=url)
            if status == "failed":
                raise AppError(f"可靈生成失敗：{data.get('task_status_msg') or '未提供原因'}", 502)
            return Poll(message="可靈排隊中" if status == "submitted" else "可靈生成中")

        ctx.save_url(poll(ctx, fetch, interval=8, expected=self.expected_seconds))
        return ctx.files

    def resume(self, ctx: Context, remote: dict) -> list[Path]:
        return self._wait(ctx, remote)

    def test(self, ctx: Context) -> dict:
        self._call(ctx, "GET", "/v1/videos/image2video?pageNum=1&pageSize=1")
        return {"ok": True, "online": True, "message": "連線成功，金鑰有效"}


# ------------------------------------------------------------------ Runway
class Runway(Provider):
    id = "runway"
    name = "Runway"
    kind = "video"
    vendor = "Runway"
    modes = ("i2v",)
    docs = "https://dev.runwayml.com/"
    notes = ("Runway 只能圖轉影片，鏡頭要先有圖片。輸出比例會挑最接近專案比例的官方選項。"
             "Runway 沒有提供模型清單 API，選單來自 Video Console 在 GitHub 上維護的清單，按「偵測模型」取得最新版；清單還沒收錄的新模型，可到 Runway 開發者文件查代號後手動填入"
             "（輸出比例會先套用 gen4_turbo 的選項）。")
    resumable = True
    online_test = True
    expected_seconds = 90
    fields = (
        url_field("baseUrl", "API 網址", "https://api.dev.runwayml.com"),
        combo("model", "模型", published.options("runway", "model"), "gen4_turbo"),
        _DURATION,
    )
    model_keys = {"i2v": "model"}
    RATIOS = {
        "gen4_turbo": ("1280:720", "720:1280", "1104:832", "832:1104", "960:960", "1584:672"),
        "gen3a_turbo": ("1280:768", "768:1280"),
    }

    def durations(self, config: dict, mode: str, models: dict | None) -> list:
        return _five_or_ten(config["duration"])

    def detect_models(self, ctx: Context) -> dict[str, dict]:
        return published.detect(self.id, ctx.config, self.vendor)

    def _headers(self, ctx: Context) -> dict[str, str]:
        return {"Authorization": f"Bearer {ctx.require_secret('api_key', 'Runway API 金鑰')}",
                "X-Runway-Version": "2024-11-06"}

    def run(self, ctx: Context, req: Request) -> list[Path]:
        req = self.check_mode(ctx, req)
        cfg = ctx.config
        model = clean_model(cfg["model"])
        body = {"model": model, "promptImage": req.image_data_uri(), "promptText": req.prompt[:1000],
                "ratio": nearest_ratio(req.width, req.height, self.RATIOS.get(model, self.RATIOS["gen4_turbo"])),
                "duration": pick_duration(req.duration, self.durations(cfg, req.mode, None)),
                "seed": req.seed % 4294967295}
        ctx.progress(0.05, "上傳到 Runway")
        data = net.request("POST", f"{cfg['baseUrl']}/v1/image_to_video", headers=self._headers(ctx),
                           json_body=body, timeout=120).json()
        task = str(data.get("id") or "")
        if not _TASK_ID.match(task):
            raise AppError("Runway 沒有回傳任務編號", 502)
        remote = {"vendor": "runway", "task": task}
        ctx.remote(remote)
        return self._wait(ctx, remote)

    def _wait(self, ctx: Context, remote: dict) -> list[Path]:
        task = str(remote.get("task") or "")
        if not _TASK_ID.match(task):
            raise AppError("Runway 任務資料不完整，無法接續", 409)
        url = f"{ctx.config['baseUrl']}/v1/tasks/{task}"
        headers = self._headers(ctx)

        def fetch() -> Poll:
            data = net.request("GET", url, headers=headers, timeout=30).json()
            status = data.get("status")
            if status == "SUCCEEDED":
                output = data.get("output") or []
                if not output:
                    raise AppError("Runway 任務完成但沒有影片網址", 502)
                return Poll(done=True, result=output[0])
            if status == "FAILED":
                raise AppError(f"Runway 生成失敗：{data.get('failure') or data.get('failureCode') or '未提供原因'}", 502)
            if status == "CANCELLED":
                raise AppError("Runway 任務已被取消", 499)
            progress = data.get("progress")
            return Poll(message="Runway 排隊中" if status in ("PENDING", "THROTTLED") else "Runway 生成中",
                        progress=float(progress) if isinstance(progress, (int, float)) else None)

        ctx.save_url(poll(ctx, fetch, interval=5, expected=self.expected_seconds))
        return ctx.files

    def resume(self, ctx: Context, remote: dict) -> list[Path]:
        return self._wait(ctx, remote)

    def cancel_remote(self, ctx: Context, remote: dict) -> None:
        task = str(remote.get("task") or "")
        if _TASK_ID.match(task):
            net.request("DELETE", f"{ctx.config['baseUrl']}/v1/tasks/{task}", headers=self._headers(ctx), timeout=15)

    def test(self, ctx: Context) -> dict:
        data = net.request("GET", f"{ctx.config['baseUrl']}/v1/organization", headers=self._headers(ctx),
                           timeout=20).json()
        credits = data.get("creditBalance")
        tail = f"，剩餘 {credits} 點" if isinstance(credits, (int, float)) else ""
        return {"ok": True, "online": True, "message": f"連線成功{tail}"}


# ------------------------------------------------------------------ MiniMax
class MiniMax(Provider):
    id = "minimax"
    name = "MiniMax 海螺"
    kind = "video"
    vendor = "MiniMax"
    modes = ("i2v", "t2v")
    docs = "https://www.minimax.io/platform/user-center/basic-information/interface-key"
    notes = ("Hailuo 02 的 1080P 只能 6 秒；768P 與 512P 可選 6 或 10 秒（依鏡頭的影片秒數挑選），其他模型的長度由模型決定。"
             "MiniMax 沒有提供模型清單 API，選單來自 Video Console 在 GitHub 上維護的清單，按「偵測模型」取得最新版；也可以改用「fal.ai 影片」。")
    resumable = True
    expected_seconds = 240
    fields = (
        url_field("baseUrl", "API 網址", "https://api.minimax.io", help="中國大陸帳號請改成 https://api.minimaxi.com"),
        combo("model", "模型", published.options("minimax", "model"), "MiniMax-Hailuo-02"),
        select("resolution", "解析度（Hailuo 系列）", ["768P", "1080P", "512P"], "768P"),
        toggle("promptOptimizer", "讓 MiniMax 自動優化提示詞", True),
        text("groupId", "Group ID（選填）", "", help="舊版帳號下載影片時需要，一般留空"),
    )
    model_keys = {"i2v": "model", "t2v": "model"}

    def durations(self, config: dict, mode: str, models: dict | None) -> list:
        if "hailuo" not in clean_model(config["model"]).lower():
            return []
        return [6] if config["resolution"] == "1080P" else [6, 10]

    def detect_models(self, ctx: Context) -> dict[str, dict]:
        return published.detect(self.id, ctx.config, self.vendor)

    def _call(self, ctx: Context, method: str, path: str, body: dict | None = None) -> dict:
        key = ctx.require_secret("api_key", "MiniMax API 金鑰")
        data = net.request(method, ctx.config["baseUrl"] + path, headers={"Authorization": f"Bearer {key}"},
                           json_body=body, timeout=120).json()
        base = data.get("base_resp") or {}
        if base.get("status_code") not in (0, None):
            raise AppError(f"MiniMax 回報錯誤：{base.get('status_msg') or '未知錯誤'}（代碼 {base.get('status_code')}）",
                           502)
        return data

    def run(self, ctx: Context, req: Request) -> list[Path]:
        cfg = ctx.config
        model = clean_model(cfg["model"])
        body: dict = {"model": model, "prompt": req.prompt[:2000], "prompt_optimizer": cfg["promptOptimizer"]}
        if "hailuo" in model.lower():
            body["resolution"] = cfg["resolution"]
            body["duration"] = pick_duration(req.duration, self.durations(cfg, req.mode, None))
        if req.image is not None:
            body["first_frame_image"] = req.image_data_uri()
        ctx.progress(0.05, "上傳到 MiniMax")
        task = str(self._call(ctx, "POST", "/v1/video_generation", body).get("task_id") or "")
        if not _TASK_ID.match(task):
            raise AppError("MiniMax 沒有回傳任務編號", 502)
        remote = {"vendor": "minimax", "task": task}
        ctx.remote(remote)
        return self._wait(ctx, remote)

    def _wait(self, ctx: Context, remote: dict) -> list[Path]:
        task = str(remote.get("task") or "")
        if not _TASK_ID.match(task):
            raise AppError("MiniMax 任務資料不完整，無法接續", 409)

        def fetch() -> Poll:
            data = self._call(ctx, "GET", f"/v1/query/video_generation?task_id={urllib.parse.quote(task)}")
            status = str(data.get("status") or "")
            if status == "Success":
                if not data.get("file_id"):
                    raise AppError("MiniMax 任務完成但沒有檔案編號", 502)
                return Poll(done=True, result=str(data["file_id"]))
            if status == "Fail":
                raise AppError("MiniMax 生成失敗（可能是內容審查未通過），請調整提示詞或圖片", 502)
            return Poll(message="MiniMax 排隊中" if status in ("Preparing", "Queueing") else "MiniMax 生成中")

        file_id = poll(ctx, fetch, interval=8, expected=self.expected_seconds)
        query = {"file_id": file_id}
        if ctx.config["groupId"]:
            query["GroupId"] = ctx.config["groupId"]
        data = self._call(ctx, "GET", "/v1/files/retrieve?" + urllib.parse.urlencode(query))
        url = (data.get("file") or {}).get("download_url")
        if not url:
            raise AppError("MiniMax 沒有回傳影片下載網址", 502)
        ctx.save_url(url)
        return ctx.files

    def resume(self, ctx: Context, remote: dict) -> list[Path]:
        return self._wait(ctx, remote)


# ------------------------------------------------------------------ Google Gemini Omni
class GeminiOmni(Provider):
    # The id stays "veo" so saved settings, routes and job handles from the Veo days still point here.
    id = "veo"
    name = "Google Gemini Omni"
    kind = "video"
    vendor = "Google"
    modes = ("i2v", "t2v")
    docs = "https://ai.google.dev/gemini-api/docs/omni"
    notes = ("共用「Google Gemini 圖片」的金鑰，需要付費方案（沒有免費額度）。透過 Interactions API 呼叫 Gemini Omni，"
             "Gemini API 上的 Veo 模型 2026-10-22 起全部停用。長度可選 3 到 10 秒，送出最接近鏡頭長度的整數秒，"
             "輸出含聲音。比例只有 16:9 與 9:16。依影片秒數計費，720p 約每秒 0.10 美元（3 秒約 0.30 美元），"
             "1080p 與 4K 是放大輸出，Google 未列出每秒價格。沒有負面提示詞欄位，會改寫成「Avoid: …」附在提示詞後面，"
             "提示詞以英文最穩定。模型清單會用金鑰向 Google 查詢。")
    resumable = True
    online_test = True
    expected_seconds = 120
    key_owner = "gemini-image"
    model_keys = {"i2v": "model", "t2v": "model"}
    fields = (
        url_field("baseUrl", "API 網址", "https://generativelanguage.googleapis.com/v1beta"),
        combo("model", "模型", ["gemini-omni-1.1-flash"], "gemini-omni-1.1-flash"),
        select("resolution", "解析度", [("360p", "360p"), ("720p", "720p"), ("1080p", "1080p（放大）"),
                                       ("4k", "4K（放大）")], "720p"),
    )
    LENGTHS = tuple(f"{n}s" for n in range(3, 11))  # response_format.duration: whole seconds 3 to 10 with "s"
    _ID = re.compile(r"^[A-Za-z0-9_-]{4,300}$")
    _FILE = re.compile(r"files/([A-Za-z0-9_-]{1,200})")

    def durations(self, config: dict, mode: str, models: dict | None) -> list:
        return list(self.LENGTHS)  # the same for both modes, every resolution and both Omni models

    def _headers(self, ctx: Context) -> dict[str, str]:
        return {"x-goog-api-key": ctx.require_secret("api_key", "Google AI Studio API 金鑰")}

    def run(self, ctx: Context, req: Request) -> list[Path]:
        cfg = ctx.config
        headers = self._headers(ctx)
        model = clean_model(cfg["model"]).removeprefix("models/")
        if model.startswith("veo"):
            raise AppError(f"Gemini API 已停用 Veo 模型（{model}），請到「引擎 › 雲端 API」改用 gemini-omni-1.1-flash", 400)
        content: list[dict] = [{"type": "text", "text": with_avoid(req.prompt, req.negative[:2000])}]
        if req.image is not None:
            content.insert(0, {"type": "image", "data": req.image_b64(), "mime_type": req.image_mime})
        body = {
            "model": model,
            "input": content,
            "background": True,  # returns an interaction id at once, so the job can be polled, resumed and cancelled
            "response_format": {"type": "video", "delivery": "uri",
                                "aspect_ratio": nearest_ratio(req.width, req.height, ("16:9", "9:16")),
                                "resolution": cfg["resolution"],
                                "duration": pick_duration(req.duration, self.durations(cfg, req.mode, None))},
            # image_to_video makes the image the first frame instead of a loose reference
            "generation_config": {"video_config": {"task": "image_to_video" if req.image is not None
                                                   else "text_to_video"}},
        }
        ctx.progress(0.05, "上傳到 Google Gemini Omni")
        # A long timeout in case Google answers synchronously and only replies once the video is done.
        data = net.request("POST", f"{cfg['baseUrl']}/interactions", headers=headers, json_body=body,
                           timeout=600).json()
        interaction = str(data.get("id") or "")
        if not self._ID.match(interaction):
            raise AppError("Gemini Omni 沒有回傳任務編號", 502)
        remote = {"vendor": "omni", "interaction": interaction}
        ctx.remote(remote)
        state = self._state(data)
        if state.done:
            return self._save(ctx, state.result, headers)
        return self._wait(ctx, remote)

    def _wait(self, ctx: Context, remote: dict) -> list[Path]:
        interaction = str(remote.get("interaction") or "")
        if not self._ID.match(interaction):
            raise AppError("Gemini Omni 任務資料不完整，無法接續（Veo 時期的任務無法接續）", 409)
        headers = self._headers(ctx)
        url = f"{ctx.config['baseUrl']}/interactions/{interaction}"
        video = poll(ctx, lambda: self._state(net.request("GET", url, headers=headers, timeout=120).json()),
                     interval=10, expected=self.expected_seconds)
        return self._save(ctx, video, headers)

    @staticmethod
    def _outputs(data: dict, kind: str) -> list[dict]:
        return [item for step in data.get("steps") or [] if isinstance(step, dict) and step.get("type") == "model_output"
                for item in step.get("content") or [] if isinstance(item, dict) and item.get("type") == kind]

    def _state(self, data: dict) -> Poll:
        status = str(data.get("status") or "")
        if status in ("in_progress", "queued"):
            return Poll(message="Gemini Omni 排隊中" if status == "queued" else "Gemini Omni 生成中")
        if status == "cancelled":
            raise AppError("Gemini Omni 任務已被取消", 499)
        video = next((v for v in self._outputs(data, "video") if v.get("data") or v.get("uri")), None)
        if status in ("completed", "incomplete") and video:
            return Poll(done=True, result=video)
        said = "；".join(str(t.get("text") or "") for t in self._outputs(data, "text")).strip()[:300]
        if status in ("completed", "incomplete"):
            raise AppError("Gemini Omni 沒有產生影片" + (f"：{said}" if said else "（可能被安全過濾擋下，請調整提示詞）"),
                           422)
        errors = "；".join(str(e.get("message") or e.get("code") or "") for e in data.get("errors") or []
                          if isinstance(e, dict)).strip()[:300]
        raise AppError(f"Gemini Omni 生成失敗：{errors or said or status or '未提供原因'}", 502)

    def _save(self, ctx: Context, video: dict, headers: dict[str, str]) -> list[Path]:
        if video.get("data"):
            ctx.save_bytes(b64decode_loose(str(video["data"])), ".mp4")
            return ctx.files
        base = ctx.config["baseUrl"]
        uri = str(video.get("uri") or "")
        found = self._FILE.search(uri)
        if not found or (uri.startswith(("http://", "https://")) and not same_host(uri, base)):
            ctx.save_url(uri)
            return ctx.files
        name = found.group(1)

        def fetch() -> Poll:
            state = net.request("GET", f"{base}/files/{name}", headers=headers, timeout=30).json().get("state")
            if state == "FAILED":
                raise AppError("Google 處理影片檔失敗", 502)
            return Poll(done=state == "ACTIVE", message="Google 正在處理影片檔")

        poll(ctx, fetch, interval=5, expected=30)
        ctx.save_url(f"{base}/files/{name}:download?alt=media", headers=headers)
        return ctx.files

    def resume(self, ctx: Context, remote: dict) -> list[Path]:
        return self._wait(ctx, remote)

    def cancel_remote(self, ctx: Context, remote: dict) -> None:
        interaction = str(remote.get("interaction") or "")
        if self._ID.match(interaction):
            net.request("POST", f"{ctx.config['baseUrl']}/interactions/{interaction}/cancel",
                        headers=self._headers(ctx), json_body={}, timeout=15)

    def test(self, ctx: Context) -> dict:
        net.request("GET", f"{ctx.config['baseUrl']}/models?pageSize=1", headers=self._headers(ctx), timeout=20)
        return {"ok": True, "online": True, "message": "連線成功，金鑰有效（Gemini Omni 另需付費方案，送出後才會確認）"}

    def detect_models(self, ctx: Context) -> dict[str, dict]:
        key = ctx.require_secret("api_key", "Google AI Studio API 金鑰")
        return {"model": catalog.detect_google(ctx, ctx.config["baseUrl"], key, "video", ctx.config["model"])}
