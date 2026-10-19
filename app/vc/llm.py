"""Optional text model behind an OpenAI-compatible API (Ollama, LM Studio, OpenAI, ...).

Used to polish shot prompts and to split a script into shots. Nothing here runs unless the user enables it.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from .net import HttpError, request
from .script import MAX_SHOTS, normalize_shot
from .secrets import SecretStore
from .settings import SettingsStore
from .util import AppError

log = logging.getLogger("vc.llm")

LLM_KEY = "llm.api_key"
MAX_INPUT = 12_000
_THINK_RE = re.compile(r"<think>.*?(?:</think>|$)", re.IGNORECASE | re.DOTALL)
_FENCE_RE = re.compile(r"^```[a-zA-Z]*\s*|\s*```$")
_LEAD_RE = re.compile(r"^(?:prompt|提示詞|描述|motion|動態|translation|譯文)\s*[:：]\s*", re.IGNORECASE)
_QUOTES = "\"'「」『』“”"

_LANG = {
    "same": "Write in the same language as the user's text; Traditional Chinese stays Traditional Chinese.",
    "en": "Write in English.",
}
_ENHANCE = {
    "image": (
        "You write prompts for text-to-image models. Rewrite the user's shot description as one vivid, concrete "
        "image prompt covering subject, action, setting, lighting, composition and mood. Keep every fact the user "
        "gave and add no text, logos or watermarks. Keep it under 80 words or 150 Chinese characters."
    ),
    "motion": (
        "You write motion prompts for image-to-video models. In one or two sentences, describe what moves in the "
        "shot and how the camera moves. Describe motion only, not the static look. Keep every fact the user gave."
    ),
}
_TRANSLATE = (
    "You translate prompts for an AI video model that only understands English. Translate the user's text into "
    "natural English. Keep every subject, action, camera move and style term, keep parts already in English as "
    "they are, and add nothing."
)
_REPLY_RULE = "Reply with the prompt text only: no preamble, no quotes, no markdown."
_SHOTS = (
    "You are a storyboard artist. Split the user's script into shots for a short AI-generated video, in story order, "
    "at most {limit} shots. Reply with a JSON array only, no markdown. Each item is an object with: "
    '"name" (short title, at most 12 characters), "prompt" (what the frame shows: subject, setting, lighting, '
    'composition; never titles, subtitles or other on-screen text, which go in "caption"), "motion" (how subjects and camera move), "camera" (shot size and camera move), '
    '"caption" (narration or dialogue to show as a subtitle, or ""), "duration" (seconds, a number from 2 to 10). '
    "Use the same language as the script; Traditional Chinese stays Traditional Chinese."
)


class LLMClient:
    def __init__(self, settings: SettingsStore, secrets: SecretStore) -> None:
        self._settings = settings
        self._secrets = secrets

    # ---------------------------------------------------------------- public
    def test(self) -> dict[str, Any]:
        """List the server's models; works before the feature is switched on so the form can be checked."""
        cfg = self._config(require_enabled=False)
        try:
            data = request("GET", cfg["baseUrl"] + "/models", headers=self._headers(), timeout=10).json()
        except AppError as exc:
            raise AppError(f"無法連線到 LLM 服務：{exc.message}", 502) from None
        items = data.get("data") if isinstance(data, dict) else None
        models = [str(m["id"]) for m in items or [] if isinstance(m, dict) and m.get("id")]
        found = cfg["model"] in models
        if not models:
            message = "連線成功，但服務沒有列出模型，請直接試用"
        elif found:
            message = f"連線成功，找到模型「{cfg['model']}」"
        else:
            message = f"連線成功，但清單裡沒有「{cfg['model']}」，請從下方清單挑一個"
        return {"ok": True, "models": models[:200], "modelFound": found, "message": message}

    def enhance(self, text: str, kind: str = "image", target: str = "same") -> str:
        if kind not in _ENHANCE or target not in _LANG:
            raise AppError("潤飾的類型不正確")
        text = (text or "").strip()
        if not text:
            raise AppError("請先輸入要潤飾的描述")
        system = " ".join((_ENHANCE[kind], _LANG[target], _REPLY_RULE))
        result = _clean_reply(self.chat(system, text[:4000], timeout=120))
        if not result:
            raise AppError("LLM 沒有回傳內容，請換個模型再試", 502)
        return result[:2000]

    def translate(self, text: str) -> str:
        """The prompt in English, for templates whose text encoder reads English only (LTX-Video's T5)."""
        text = (text or "").strip()
        if not text:
            raise AppError("沒有要翻譯的提示詞")
        result = _clean_reply(self.chat(" ".join((_TRANSLATE, _REPLY_RULE)), text[:8000], timeout=60))
        if not result:
            raise AppError("LLM 沒有回傳譯文，請換個模型再試", 502)
        return result[:8000]

    def script_to_shots(self, text: str) -> list[dict]:
        text = (text or "").strip()
        if not text:
            raise AppError("請先貼上劇本或分鏡文字")
        if len(text) > MAX_INPUT:
            raise AppError(f"AI 拆解一次最多 {MAX_INPUT:,} 字，請分段處理或改用一般拆解")
        reply = self.chat(_SHOTS.format(limit=60), text, timeout=300)
        shots = [s for s in (normalize_shot(item) for item in _shot_list(_extract_json(reply))) if s]
        if not shots:
            raise AppError("LLM 沒有拆出任何鏡頭，請換個模型或改用一般拆解", 502)
        return shots[:MAX_SHOTS]

    def chat(self, system: str, user: str, *, timeout: float = 120) -> str:
        cfg = self._config()
        body: dict[str, Any] = {
            "model": cfg["model"],
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": cfg["temperature"],
            "stream": False,
        }
        url = cfg["baseUrl"] + "/chat/completions"
        try:
            try:
                data = request("POST", url, headers=self._headers(), json_body=body, timeout=timeout).json()
            except HttpError as exc:
                # Some hosted reasoning models accept only their default temperature.
                if exc.code != 400 or "temperature" not in exc.message.lower():
                    raise
                body.pop("temperature")
                data = request("POST", url, headers=self._headers(), json_body=body, timeout=timeout).json()
        except AppError as exc:
            raise AppError(f"LLM 呼叫失敗：{exc.message}", 502) from None
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise AppError("LLM 回應格式不正確（不是 OpenAI 相容格式）", 502) from None
        return _THINK_RE.sub("", str(content or "")).strip()

    # ---------------------------------------------------------------- helpers
    def _config(self, require_enabled: bool = True) -> dict:
        cfg = self._settings.section("llm")
        if require_enabled and not cfg.get("enabled"):
            raise AppError("尚未開啟文字模型，請到「引擎 › 文字模型」開啟", 409)
        if not cfg.get("model"):
            raise AppError("請先填寫 LLM 模型名稱")
        return cfg

    def _headers(self) -> dict[str, str]:
        key = self._secrets.get(LLM_KEY)
        return {"Authorization": f"Bearer {key}"} if key else {}


def _clean_reply(text: str) -> str:
    text = _FENCE_RE.sub("", text.strip()).strip()
    text = _LEAD_RE.sub("", text).strip()
    if len(text) >= 2 and text[0] in _QUOTES and text[-1] in _QUOTES:
        text = text[1:-1].strip()
    return text


def _extract_json(text: str) -> Any:
    """First JSON array or object in the reply; models often wrap it in prose or code fences."""
    decoder = json.JSONDecoder()
    for attempt, match in enumerate(re.finditer(r"[\[{]", text)):
        if attempt >= 50:
            break
        try:
            value, _ = decoder.raw_decode(text, match.start())
        except ValueError:
            continue
        if isinstance(value, (list, dict)):
            return value
    log.info("LLM reply had no JSON (%d chars)", len(text))
    raise AppError("LLM 回傳的內容不是 JSON，請換個模型或改用一般拆解", 502)


def _shot_list(value: Any) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        for key in ("shots", "storyboard", "scenes", "items", "data"):
            if isinstance(value.get(key), list):
                return value[key]
        if "prompt" in value:
            return [value]
    return []
