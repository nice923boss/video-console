"""Thin ComfyUI REST client."""
from __future__ import annotations

import hashlib
import logging
import urllib.parse
from pathlib import Path
from typing import Any, Callable

from .. import net
from ..util import AppError, guess_ext

log = logging.getLogger("vc.comfy")

OOM_MESSAGE = "顯示卡記憶體不足（OOM），請降低解析度或影片長度，或在引擎頁按「釋放記憶體」"
UPLOAD_SUBFOLDER = "vc"

_NODE_ERROR_TYPES = {
    "value_not_in_list": "選項不存在（多半是缺少模型檔，或自訂節點版本不同）",
    "required_input_missing": "缺少必要輸入",
    "value_smaller_than_min": "數值小於下限",
    "value_bigger_than_max": "數值超過上限",
    "invalid_input_type": "輸入類型不符",
    "return_type_mismatch": "連線類型不符",
    "custom_validation_failed": "節點檢查未通過",
    "exception_during_validation": "驗證時發生錯誤",
    "exception_during_inner_validation": "驗證時發生錯誤",
    "dependency_cycle_detected": "節點連線形成循環",
}
_PROMPT_ERROR_TYPES = {
    "prompt_no_outputs": "工作流程沒有任何輸出節點（例如 SaveImage）",
    "invalid_prompt": "工作流程格式不正確",
    "prompt_outputs_failed_validation": "工作流程驗證失敗",
}


def is_oom(text: str) -> bool:
    low = (text or "").lower()
    return any(k in low for k in ("out of memory", "outofmemory", "allocation on device", "cuda error: out"))


def describe_prompt_error(data: Any) -> str:
    """Turn ComfyUI's /prompt 400 body into one readable sentence."""
    if not isinstance(data, dict):
        return "ComfyUI 拒絕了這個工作流程"
    err = data.get("error") or {}
    head = _PROMPT_ERROR_TYPES.get(err.get("type", ""), "") or str(err.get("message") or "ComfyUI 拒絕了這個工作流程")
    parts: list[str] = []
    for nid, info in (data.get("node_errors") or {}).items():
        cls = info.get("class_type", "")
        for e in (info.get("errors") or [])[:2]:
            kind = _NODE_ERROR_TYPES.get(e.get("type", ""), e.get("message", "錯誤"))
            detail = str(e.get("details") or "").strip()
            if len(detail) > 160:
                detail = detail[:160] + "…"
            parts.append(f"節點 #{nid}（{cls}）{kind}" + (f"：{detail}" if detail else ""))
        if len(parts) >= 4:
            break
    if not parts and err.get("details"):
        parts.append(str(err["details"])[:200])
    return head + ("：" + "；".join(parts) if parts else "")


class ComfyClient:
    def __init__(self, url: str, token: str = "", timeout: float = 30):
        self.url = url.rstrip("/")
        self.token = token
        self.timeout = timeout

    # ---------------------------------------------------------------- basics
    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = dict(extra or {})
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _call(self, method: str, path: str, *, body: Any = None, data: bytes | None = None,
              headers: dict[str, str] | None = None, timeout: float | None = None) -> net.Response:
        try:
            return net.request(method, self.url + path, headers=self._headers(headers), json_body=body, data=data,
                               timeout=timeout or self.timeout)
        except net.HttpError as exc:
            if exc.code in (401, 403):
                raise AppError("ComfyUI 拒絕存取：請確認存取權杖（Bearer Token）設定", 502) from None
            if exc.code == 0:
                raise AppError(f"無法連線到 ComfyUI：{exc.message}", 502) from None
            raise

    def json(self, method: str, path: str, **kw: Any) -> Any:
        return self._call(method, path, **kw).json()

    # ---------------------------------------------------------------- endpoints
    def system_stats(self, timeout: float = 6) -> dict:
        return self.json("GET", "/system_stats", timeout=timeout) or {}

    def object_info(self) -> dict:
        return self.json("GET", "/object_info", timeout=90) or {}

    def queue(self) -> dict:
        return self.json("GET", "/queue", timeout=10) or {}

    def history(self, prompt_id: str) -> dict:
        data = self.json("GET", "/history/" + urllib.parse.quote(prompt_id), timeout=15) or {}
        return data.get(prompt_id) or {}

    def submit(self, graph: dict, client_id: str, extra_data: dict | None = None, prompt_id: str = "") -> str:
        """Queue a graph. Newer ComfyUI honours a caller-chosen prompt_id; older ones return their own."""
        body: dict[str, Any] = {"prompt": graph, "client_id": client_id}
        if prompt_id:
            body["prompt_id"] = prompt_id
        if extra_data:
            body["extra_data"] = extra_data
        try:
            data = self.json("POST", "/prompt", body=body, timeout=60)
        except net.HttpError as exc:
            if exc.code == 400:
                raise AppError(describe_prompt_error(exc.json()), 400) from None
            raise
        if not isinstance(data, dict) or not data.get("prompt_id"):
            raise AppError("ComfyUI 沒有回傳任務代號", 502)
        if data.get("node_errors"):
            log.warning("ComfyUI accepted prompt with node_errors: %s", describe_prompt_error(data))
        return str(data["prompt_id"])

    def upload_image(self, data: bytes, ext: str = "") -> str:
        """Upload bytes into input/vc/ and return the LoadImage value ("vc/<name>")."""
        ext = ext or guess_ext(data, ".png")
        name = "vc_" + hashlib.sha1(data).hexdigest()[:20] + ext
        mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}.get(
            ext.lower(), "application/octet-stream")
        body, ctype = net.multipart({"subfolder": UPLOAD_SUBFOLDER, "type": "input", "overwrite": "true"},
                                    [("image", name, data, mime)])
        res = self.json("POST", "/upload/image", data=body, headers={"Content-Type": ctype}, timeout=120) or {}
        stored = res.get("name") or name
        sub = res.get("subfolder") or ""
        return f"{sub}/{stored}" if sub else stored

    def view_url(self, filename: str, subfolder: str = "", type_: str = "output") -> str:
        query = urllib.parse.urlencode({"filename": filename, "subfolder": subfolder, "type": type_})
        return f"{self.url}/view?{query}"

    def download(self, item: dict, dst: Path, cancelled: Callable[[], bool] | None = None) -> None:
        url = self.view_url(item.get("filename", ""), item.get("subfolder", ""), item.get("type", "output"))
        try:
            net.download(url, dst, headers=self._headers(), timeout=120, cancelled=cancelled)
        except net.HttpError as exc:
            raise AppError(f"無法從 ComfyUI 取回輸出檔（{item.get('filename')}）：{exc.message}", 502) from None

    def interrupt(self, prompt_id: str | None = None) -> None:
        body = {"prompt_id": prompt_id} if prompt_id else {}
        self._call("POST", "/interrupt", body=body, timeout=10)

    def delete_queued(self, prompt_ids: list[str]) -> None:
        self._call("POST", "/queue", body={"delete": prompt_ids}, timeout=10)

    def free(self) -> None:
        self._call("POST", "/free", body={"unload_models": True, "free_memory": True}, timeout=30)
