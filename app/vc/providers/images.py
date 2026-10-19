"""Cloud image providers with first-party APIs: OpenAI, Google Gemini, Stability AI."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from .. import net
from ..util import AppError
from . import catalog
from .base import (Context, Provider, Request, b64decode_loose, clean_model, combo, nearest_ratio, select,
                   url_field, with_avoid)


def _orientation(choice: str, req: Request) -> str:
    if choice in ("square", "landscape", "portrait"):
        return choice
    ratio = req.width / max(req.height, 1)
    return "landscape" if ratio > 1.2 else "portrait" if ratio < 0.83 else "square"


class OpenAIImages(Provider):
    id = "openai-images"
    name = "OpenAI 圖片"
    vendor = "OpenAI"
    modes = ("t2i", "i2i")
    docs = "https://platform.openai.com/api-keys"
    notes = ("gpt-image-1 系列可帶參考圖（走 edits 端點），dall-e-3 只能純文字生圖。"
             "輸出尺寸由 OpenAI 固定，會依專案比例挑最接近的橫向、直向或正方形；參考圖強度對 OpenAI 無效。"
             "模型清單會用金鑰向 OpenAI 查詢目前可用的圖片模型。")
    online_test = True
    model_keys = {"t2i": "model", "i2i": "model"}
    expected_seconds = 45
    fields = (
        url_field("baseUrl", "API 網址", "https://api.openai.com/v1",
                  help="使用相容 OpenAI 格式的其他服務時，改成對方提供的網址"),
        combo("model", "模型", ["gpt-image-1", "gpt-image-1-mini", "dall-e-3"], "gpt-image-1"),
        select("quality", "品質", [("auto", "自動"), ("low", "低（快、便宜）"), ("medium", "中"), ("high", "高")],
               "auto"),
        select("size", "輸出比例", [("auto", "依專案比例"), ("landscape", "橫向"), ("portrait", "直向"),
                                    ("square", "正方形")], "auto"),
    )

    SIZES = {
        "gpt": {"landscape": "1536x1024", "portrait": "1024x1536", "square": "1024x1024"},
        "dall-e-3": {"landscape": "1792x1024", "portrait": "1024x1792", "square": "1024x1024"},
    }

    def run(self, ctx: Context, req: Request) -> list[Path]:
        cfg = ctx.config
        key = ctx.require_secret("api_key", "OpenAI API 金鑰")
        model = clean_model(cfg["model"])
        dalle = model.startswith("dall-e")
        if req.image is not None and dalle:
            ctx.notes.append(f"{model} 不支援參考圖，已改成純文字生圖")
            req = replace(req, image=None)
        orient = _orientation(cfg["size"], req)
        size = (self.SIZES["dall-e-3"][orient] if model == "dall-e-3" else "1024x1024" if dalle
                else self.SIZES["gpt"][orient])
        prompt = with_avoid(req.prompt, req.negative)[: 4000 if dalle else 32000]
        auth = {"Authorization": f"Bearer {key}"}
        base = cfg["baseUrl"]
        ctx.progress(0.1, "已送出到 OpenAI，等待生成（通常 20 到 90 秒）")
        if req.image is not None:
            fields = {"model": model, "prompt": prompt, "size": size, "n": "1",
                      "quality": None if cfg["quality"] == "auto" else cfg["quality"]}
            ext = ".jpg" if "jpeg" in req.image_mime else ".webp" if "webp" in req.image_mime else ".png"
            body, ctype = net.multipart(fields, [("image", "reference" + ext, req.image, req.image_mime)])
            resp = net.request("POST", f"{base}/images/edits", headers={**auth, "Content-Type": ctype}, data=body,
                               timeout=300)
        else:
            payload: dict = {"model": model, "prompt": prompt, "size": size, "n": 1}
            if dalle:
                payload["response_format"] = "b64_json"
                if model == "dall-e-3":
                    payload["quality"] = "hd" if cfg["quality"] == "high" else "standard"
            elif cfg["quality"] != "auto":
                payload["quality"] = cfg["quality"]
            resp = net.request("POST", f"{base}/images/generations", headers=auth, json_body=payload, timeout=300)
        item = (resp.json().get("data") or [{}])[0]
        if item.get("b64_json"):
            ctx.save_bytes(b64decode_loose(item["b64_json"]))
        elif item.get("url"):
            ctx.save_url(item["url"])
        else:
            raise AppError("OpenAI 沒有回傳圖片", 502)
        return ctx.files

    def test(self, ctx: Context) -> dict:
        key = ctx.require_secret("api_key", "OpenAI API 金鑰")
        net.request("GET", f"{ctx.config['baseUrl']}/models", headers={"Authorization": f"Bearer {key}"},
                    timeout=20)
        return {"ok": True, "online": True, "message": "連線成功，金鑰有效"}

    def detect_models(self, ctx: Context) -> dict[str, dict]:
        key = ctx.require_secret("api_key", "OpenAI API 金鑰")
        return {"model": catalog.detect_openai(ctx, ctx.config["baseUrl"], key, ctx.config["model"])}


class GeminiImage(Provider):
    id = "gemini-image"
    name = "Google Gemini 圖片"
    vendor = "Google"
    modes = ("t2i", "i2i")
    docs = "https://aistudio.google.com/apikey"
    notes = ("Gemini 圖片模型（俗稱 Nano Banana）擅長依參考圖改圖與保持角色一致。沒有負向提示詞欄位，"
             "會改寫成「Avoid: …」附在提示詞後面。模型清單會用金鑰向 Google 查詢。這把金鑰也會給 Gemini Omni 影片使用。")
    online_test = True
    model_keys = {"t2i": "model", "i2i": "model"}
    expected_seconds = 30
    fields = (
        url_field("baseUrl", "API 網址", "https://generativelanguage.googleapis.com/v1beta"),
        combo("model", "模型", ["gemini-2.5-flash-image", "gemini-3-pro-image-preview"], "gemini-2.5-flash-image"),
        select("imageSize", "解析度（僅 Pro 模型）", ["1K", "2K", "4K"], "1K"),
    )
    ASPECTS = ("1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9", "21:9")

    def run(self, ctx: Context, req: Request) -> list[Path]:
        cfg = ctx.config
        key = ctx.require_secret("api_key", "Google AI Studio API 金鑰")
        model = clean_model(cfg["model"]).removeprefix("models/")
        parts: list[dict] = [{"text": with_avoid(req.prompt, req.negative)}]
        if req.image is not None:
            parts.append({"inlineData": {"mimeType": req.image_mime, "data": req.image_b64()}})
        image_cfg = {"aspectRatio": nearest_ratio(req.width, req.height, self.ASPECTS)}
        if "pro-image" in model or model.startswith("gemini-3"):
            image_cfg["imageSize"] = cfg["imageSize"]
        payload = {"contents": [{"role": "user", "parts": parts}],
                   "generationConfig": {"responseModalities": ["TEXT", "IMAGE"], "imageConfig": image_cfg}}
        ctx.progress(0.1, "已送出到 Gemini，等待生成")
        data = net.request("POST", f"{cfg['baseUrl']}/models/{model}:generateContent",
                           headers={"x-goog-api-key": key}, json_body=payload, timeout=300).json()
        texts: list[str] = []
        candidates = data.get("candidates") or []
        for cand in candidates:
            for part in (cand.get("content") or {}).get("parts") or []:
                inline = part.get("inlineData") or part.get("inline_data") or {}
                if inline.get("data"):
                    ctx.save_bytes(b64decode_loose(inline["data"]))
                    return ctx.files
                if part.get("text"):
                    texts.append(part["text"].strip())
        block = (data.get("promptFeedback") or {}).get("blockReason")
        if block:
            raise AppError(f"Gemini 拒絕這個提示詞（{block}），請調整內容後再試", 422)
        reason = (candidates[0].get("finishReason") or "") if candidates else ""
        said = " ".join(t for t in texts if t)[:200]
        raise AppError("Gemini 沒有回傳圖片" + (f"（{reason}）" if reason else "") + (f"：{said}" if said else ""), 502)

    def test(self, ctx: Context) -> dict:
        key = ctx.require_secret("api_key", "Google AI Studio API 金鑰")
        net.request("GET", f"{ctx.config['baseUrl']}/models?pageSize=1", headers={"x-goog-api-key": key},
                    timeout=20)
        return {"ok": True, "online": True, "message": "連線成功，金鑰有效"}

    def detect_models(self, ctx: Context) -> dict[str, dict]:
        key = ctx.require_secret("api_key", "Google AI Studio API 金鑰")
        return {"model": catalog.detect_google(ctx, ctx.config["baseUrl"], key, "image", ctx.config["model"])}


class StabilityImage(Provider):
    id = "stability-image"
    name = "Stability AI 圖片"
    vendor = "Stability AI"
    modes = ("t2i", "i2i")
    docs = "https://platform.stability.ai/account/keys"
    notes = ("Core 便宜快速但不吃參考圖（帶參考圖時會自動改用 SD 3.5）；Ultra 品質最高，可帶參考圖。"
             "Stability 沒有提供模型清單，SD 3.5 選單不會自動更新；要用較新的模型，請改用「fal.ai 圖片」。")
    online_test = True
    expected_seconds = 20
    fields = (
        url_field("baseUrl", "API 網址", "https://api.stability.ai"),
        select("service", "服務", [("core", "Stable Image Core（快、便宜）"), ("ultra", "Stable Image Ultra（高品質）"),
                                   ("sd3", "Stable Diffusion 3.5")], "core"),
        combo("model", "SD 3.5 模型", ["sd3.5-large", "sd3.5-large-turbo", "sd3.5-medium", "sd3.5-flash"],
              "sd3.5-large", help="服務選 Stable Diffusion 3.5 時使用"),
        select("stylePreset", "風格預設（僅 Core）",
               [("", "不指定"), ("photographic", "攝影"), ("cinematic", "電影感"), ("anime", "動漫"),
                ("digital-art", "數位繪畫"), ("3d-model", "3D 模型"), ("analog-film", "底片"),
                ("comic-book", "漫畫"), ("fantasy-art", "奇幻"), ("line-art", "線稿"), ("neon-punk", "霓虹龐克"),
                ("pixel-art", "像素")], ""),
    )
    ASPECTS = ("16:9", "1:1", "21:9", "2:3", "3:2", "4:5", "5:4", "9:16", "9:21")

    def run(self, ctx: Context, req: Request) -> list[Path]:
        cfg = ctx.config
        key = ctx.require_secret("api_key", "Stability AI API 金鑰")
        service = cfg["service"]
        if service == "core" and req.image is not None:
            ctx.notes.append("Core 不支援參考圖，這次改用 Stable Diffusion 3.5 生成")
            service = "sd3"
        fields: dict = {"prompt": req.prompt[:10000], "negative_prompt": req.negative[:10000] or None,
                        "seed": str(req.seed % 4294967295), "output_format": "png"}
        if service == "sd3":
            fields["model"] = clean_model(cfg["model"])
            fields["mode"] = "image-to-image" if req.image is not None else "text-to-image"
        if service == "core" and cfg["stylePreset"]:
            fields["style_preset"] = cfg["stylePreset"]
        files = []
        if req.image is not None:
            fields["strength"] = f"{req.denoise:.2f}"
            files.append(("image", "reference.png", req.image, req.image_mime))
        else:
            fields["aspect_ratio"] = nearest_ratio(req.width, req.height, self.ASPECTS)
        body, ctype = net.multipart(fields, files)
        ctx.progress(0.1, "已送出到 Stability AI，等待生成")
        resp = net.request("POST", f"{cfg['baseUrl']}/v2beta/stable-image/generate/{service}",
                           headers={"Authorization": f"Bearer {key}", "Accept": "image/*", "Content-Type": ctype},
                           data=body, timeout=300)
        if (resp.headers.get("finish-reason") or "").upper() == "CONTENT_FILTERED":
            raise AppError("Stability AI 的內容過濾擋下了這張圖，請調整提示詞", 422)
        ctx.save_bytes(resp.body, ".png")
        return ctx.files

    def test(self, ctx: Context) -> dict:
        key = ctx.require_secret("api_key", "Stability AI API 金鑰")
        data = net.request("GET", f"{ctx.config['baseUrl']}/v1/user/balance",
                           headers={"Authorization": f"Bearer {key}"}, timeout=20).json()
        credits = data.get("credits")
        tail = f"，剩餘 {credits:g} 點" if isinstance(credits, (int, float)) else ""
        return {"ok": True, "online": True, "message": f"連線成功{tail}"}
