"""User-defined HTTP endpoint for any image/video API that is not built in (self-hosted servers, new vendors).

The request is described with JSON templates containing {{placeholders}}; the result is located with a dotted
path such as "data.0.url". Optional polling covers asynchronous APIs that return a task id first.
"""
from __future__ import annotations

import json
import re
import urllib.parse
from pathlib import Path
from typing import Any

from .. import net
from ..util import AppError, guess_ext
from .base import (Context, Poll, Provider, Request, b64decode_loose, first_url, json_field, nearest_ratio, number,
                   poll, secret, select, text, url_field)

PLACEHOLDER = re.compile(r"\{\{\s*([a-z_0-9]+)\s*\}\}")
KNOWN = ("prompt", "negative", "width", "height", "aspect", "seed", "duration", "fps", "denoise", "image_b64",
         "image_data_uri", "image_mime", "api_key", "id")
ASPECTS = ("16:9", "9:16", "1:1", "4:3", "3:4", "21:9")
TEMPLATE_KEYS = ("url", "headers", "body", "pollUrl")
_DROP = object()

PLACEHOLDER_HELP = ("可用變數：{{prompt}} {{negative}} {{width}} {{height}} {{aspect}} {{seed}} {{duration}} {{fps}} "
                    "{{denoise}} {{image_b64}} {{image_data_uri}} {{image_mime}} {{api_key}}。一格只放一個變數時會保留"
                    "數字型別；沒有值的變數（例如沒有參考圖、沒有負向提示詞）會把整個欄位省略。")


def fill(template: Any, values: dict[str, Any]) -> Any:
    """Replace {{name}} placeholders inside a JSON structure.

    A string that is exactly one placeholder takes the value's native type (numbers stay numbers); when that
    value is None the key or list item is dropped, so optional inputs simply disappear from the request.
    """
    if isinstance(template, dict):
        out = {}
        for key, value in template.items():
            filled = fill(value, values)
            if filled is not _DROP:
                out[key] = filled
        return out
    if isinstance(template, list):
        return [item for item in (fill(v, values) for v in template) if item is not _DROP]
    if isinstance(template, str):
        whole = PLACEHOLDER.fullmatch(template.strip())
        if whole:
            value = values.get(whole.group(1))
            return _DROP if value is None else value
        return PLACEHOLDER.sub(lambda m: _as_text(values.get(m.group(1))), template)
    return template


def fill_url(template: str, values: dict[str, Any]) -> str:
    return PLACEHOLDER.sub(lambda m: urllib.parse.quote(_as_text(values.get(m.group(1))), safe=""), template)


def _as_text(value: Any) -> str:
    return "" if value is None else str(value)


def dig(data: Any, path: str) -> Any:
    """Follow a path such as "data.0.url" or "output[0]" through decoded JSON; None when it does not exist."""
    for part in re.findall(r"[^.\[\]]+", path or ""):
        if isinstance(data, list):
            try:
                data = data[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(data, dict):
            data = data.get(part)
        else:
            return None
        if data is None:
            return None
    return data


def _names(cfg: dict) -> set[str]:
    return {name for key in TEMPLATE_KEYS for name in PLACEHOLDER.findall(cfg.get(key) or "")}


class CustomHttp(Provider):
    vendor = "自訂"
    resumable = True
    expected_seconds = 60
    secrets = (secret("api_key", "自訂 API 金鑰", help="在網址、標頭或內容裡用 {{api_key}} 引用；不需要金鑰的服務可留空"),)

    def __init__(self, pid: str, kind: str):
        self.id = pid
        self.kind = kind
        video = kind == "video"
        self.name = "自訂 HTTP 影片" if video else "自訂 HTTP 圖片"
        self.modes = ("i2v", "t2v") if video else ("t2i", "i2i")
        self.notes = ("串接沒有內建的服務或自架伺服器。用 JSON 樣板描述請求，用路徑（例如 data.0.url）指出結果在哪裡；"
                      "非同步的服務可設定任務編號與查詢網址，程式會自動輪詢。")
        body = ({"prompt": "{{prompt}}", "image": "{{image_data_uri}}", "duration": "{{duration}}",
                 "aspect_ratio": "{{aspect}}"} if video else
                {"prompt": "{{prompt}}", "negative_prompt": "{{negative}}", "width": "{{width}}",
                 "height": "{{height}}", "seed": "{{seed}}", "image": "{{image_data_uri}}"})
        self.fields = (
            url_field("url", "請求網址", "", help="例如 https://example.com/v1/generate，網址內也可用變數"),
            select("method", "請求方法", ["POST", "PUT", "GET"], "POST", help="GET 不會送出請求內容，參數請寫在網址裡"),
            json_field("headers", "標頭（JSON）", json.dumps({"Authorization": "Bearer {{api_key}}"}),
                       help="不需要金鑰時改成 {}"),
            json_field("body", "請求內容（JSON 樣板）", json.dumps(body, ensure_ascii=False, indent=2),
                       help=PLACEHOLDER_HELP),
            select("responseType", "回應格式", [("json", "JSON（從路徑取得網址或 base64）"),
                                                ("binary", "直接回傳檔案")], "json"),
            text("resultPath", "結果路徑", "data.0.url",
                 help="JSON 裡圖片或影片網址（或 base64）的位置；留空則自動找第一個網址"),
            text("idPath", "任務編號路徑（非同步服務才填）", "", help="例如 id 或 data.task_id；留空表示請求直接回傳結果"),
            url_field("pollUrl", "查詢網址", "", help="用 {{id}} 代表任務編號，例如 https://example.com/v1/tasks/{{id}}"),
            text("statusPath", "狀態路徑", "status"),
            text("doneValues", "完成狀態值", "succeeded,success,completed,done", help="多個值用逗號分隔，不分大小寫"),
            text("failValues", "失敗狀態值", "failed,error,canceled,cancelled"),
            text("errorPath", "錯誤訊息路徑", "error"),
            number("pollInterval", "查詢間隔（秒）", 5, 2, 60),
        )

    # config --------------------------------------------------------------------
    def validate_config(self, values: Any) -> dict:
        cfg = super().validate_config(values)
        headers = json.loads(cfg["headers"] or "{}")
        if not isinstance(headers, dict) or not all(isinstance(v, str) for v in headers.values()):
            raise AppError("標頭必須是 JSON 物件，而且每個值都是文字")
        body = json.loads(cfg["body"] or "{}")
        if not isinstance(body, (dict, list)):
            raise AppError("請求內容必須是 JSON 物件或陣列")
        unknown = sorted(_names(cfg) - set(KNOWN))
        if unknown:
            raise AppError("不認識的變數：" + "、".join("{{" + name + "}}" for name in unknown))
        if cfg["idPath"]:
            if not cfg["pollUrl"]:
                raise AppError("填了「任務編號路徑」就要一併填「查詢網址」")
            if not cfg["statusPath"]:
                raise AppError("非同步服務需要「狀態路徑」才能判斷任務是否完成")
            if cfg["responseType"] == "binary":
                raise AppError("回應格式選「直接回傳檔案」時不能搭配任務輪詢")
        return cfg

    # run -----------------------------------------------------------------------
    def _values(self, ctx: Context, req: Request | None) -> dict[str, Any]:
        cfg = ctx.config
        values: dict[str, Any] = {}
        if "api_key" in _names(cfg):
            values["api_key"] = ctx.require_secret("api_key", "自訂 API 金鑰")
        if req is not None:
            values.update({
                "prompt": req.prompt,
                "negative": req.negative or None,
                "width": req.width,
                "height": req.height,
                "aspect": nearest_ratio(req.width, req.height, ASPECTS),
                "seed": req.seed,
                "duration": int(round(req.duration)),
                "fps": req.fps,
                "denoise": req.denoise,
                "image_b64": req.image_b64() or None,
                "image_data_uri": req.image_data_uri() or None,
                "image_mime": req.image_mime if req.image is not None else None,
            })
        return values

    def _headers(self, cfg: dict, values: dict[str, Any]) -> dict[str, str]:
        filled = fill(json.loads(cfg["headers"] or "{}"), values)
        return {str(k): str(v) for k, v in filled.items()}

    def run(self, ctx: Context, req: Request) -> list[Path]:
        cfg = ctx.config
        if not cfg["url"]:
            raise AppError("尚未填寫自訂 API 的網址，請到「引擎 › 雲端 API」補上", 400)
        values = self._values(ctx, req)
        headers = self._headers(cfg, values)
        body = None
        if cfg["method"] != "GET":
            body = fill(json.loads(cfg["body"] or "{}"), values)
            body = None if body is _DROP else body
        ctx.progress(0.05, f"已送出到 {urllib.parse.urlparse(cfg['url']).hostname or '自訂服務'}")
        resp = net.request(cfg["method"], fill_url(cfg["url"], values), headers=headers, json_body=body, timeout=600)
        if cfg["responseType"] == "binary":
            ctype = (resp.headers.get("content-type") or "").lower()
            if ctype.startswith(("application/json", "text/")):
                raise AppError(f"自訂 API 回傳的是文字而不是檔案：{resp.text()[:200]}", 502)
            ctx.save_bytes(resp.body, ".mp4" if self.kind == "video" else ".png")
            return ctx.files
        data = resp.json()
        if not cfg["idPath"]:
            return self._save_result(ctx, data)
        task = dig(data, cfg["idPath"])
        if not isinstance(task, (str, int)) or isinstance(task, bool) or not str(task).strip():
            raise AppError(f"回應裡找不到任務編號（{cfg['idPath']}）", 502)
        remote = {"vendor": "custom", "id": str(task).strip()[:500]}
        ctx.remote(remote)
        return self._wait(ctx, remote)

    def _wait(self, ctx: Context, remote: dict) -> list[Path]:
        cfg = ctx.config
        task = str(remote.get("id") or "")
        if not task or not cfg["pollUrl"]:
            raise AppError("自訂 API 的任務資料或查詢網址不完整，無法接續", 409)
        values = {**self._values(ctx, None), "id": task}
        url = fill_url(cfg["pollUrl"], values)
        headers = self._headers(cfg, values)
        done = {v.strip().lower() for v in cfg["doneValues"].split(",") if v.strip()}
        failed = {v.strip().lower() for v in cfg["failValues"].split(",") if v.strip()}

        def fetch() -> Poll:
            data = net.request("GET", url, headers=headers, timeout=60).json()
            status = _as_text(dig(data, cfg["statusPath"])).strip().lower()
            if status in done:
                return Poll(done=True, result=data)
            if status in failed:
                reason = dig(data, cfg["errorPath"]) if cfg["errorPath"] else None
                reason = json.dumps(reason, ensure_ascii=False) if isinstance(reason, (dict, list)) else reason
                raise AppError(f"自訂 API 回報任務失敗：{_as_text(reason)[:300] or status}", 502)
            return Poll(message=f"自訂服務狀態：{status or '處理中'}")

        data = poll(ctx, fetch, interval=cfg["pollInterval"], expected=self.expected_seconds)
        return self._save_result(ctx, data)

    def _save_result(self, ctx: Context, data: Any) -> list[Path]:
        path = ctx.config["resultPath"]
        value = dig(data, path) if path else first_url(data)
        if isinstance(value, (list, dict)):
            value = first_url(value)
        if not isinstance(value, str) or not value.strip():
            raise AppError(f"回應裡找不到結果（{path or '自動尋找網址'}），請檢查「結果路徑」", 502)
        value = value.strip()
        if value.startswith(("http://", "https://", "data:")):
            ctx.save_url(value)
            return ctx.files
        raw = b64decode_loose(value)
        if not guess_ext(raw, ""):
            raise AppError(f"「{path}」的內容不是網址，也不是可辨識的圖片或影片", 502)
        ctx.save_bytes(raw)
        return ctx.files

    def resume(self, ctx: Context, remote: dict) -> list[Path]:
        return self._wait(ctx, remote)

    def test(self, ctx: Context) -> dict:
        if not ctx.config["url"]:
            raise AppError("尚未填寫自訂 API 的網址", 400)
        self._values(ctx, None)  # raises when {{api_key}} is used but empty
        return {"ok": True, "online": False, "message": "網址已設定；自訂 HTTP 不會自動送測試請求（避免產生費用）"}
