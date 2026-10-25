"""Small HTTP helpers on top of urllib (no third-party dependencies)."""
from __future__ import annotations

import json
import re
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .util import AppError

USER_AGENT = "VideoConsole/1.0"
_SENSITIVE_HEADERS = {"authorization", "x-goog-api-key", "x-api-key", "api-key", "x-key", "cookie"}
_CHUNK = 256 * 1024


class HttpError(AppError):
    """Upstream HTTP failure. `code` is the upstream status (0 = no response)."""

    def __init__(self, message: str, code: int, body: bytes = b"", url: str = ""):
        super().__init__(message, 502)
        self.code = code
        self.body = body
        self.url = url

    def json(self) -> Any:
        try:
            return json.loads(self.body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None


@dataclass(frozen=True)
class Response:
    status: int
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""

    def json(self) -> Any:
        if not self.body:
            return None
        try:
            return json.loads(self.body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise AppError("回應不是有效的 JSON：" + self.text(200), 502) from None

    def text(self, limit: int = 2000) -> str:
        return self.body[:limit].decode("utf-8", "replace")


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    """Follow redirects but never forward credentials to a different host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None:
            old_host = urllib.parse.urlsplit(req.full_url).netloc.lower()
            new_host = urllib.parse.urlsplit(newurl).netloc.lower()
            if old_host != new_host:
                for store in (new.headers, new.unredirected_hdrs):
                    for key in list(store):
                        if key.lower() in _SENSITIVE_HEADERS:
                            del store[key]
        return new


_OPENER = urllib.request.build_opener(_SafeRedirect)


def upstream_message(body: bytes | str | Any, limit: int = 300) -> str:
    """Pull a human readable message out of a typical JSON error body."""
    data = body
    if isinstance(body, (bytes, str)):
        text = body.decode("utf-8", "replace") if isinstance(body, bytes) else body
        try:
            data = json.loads(text)
        except ValueError:
            return text.strip()[:limit]
    candidates: list[Any] = []
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            candidates += [err.get("message"), err.get("detail")]
        candidates += [err, data.get("message"), data.get("detail"), data.get("msg"), data.get("errors"),
                       data.get("base_resp", {}).get("status_msg") if isinstance(data.get("base_resp"), dict) else None]
    for c in candidates:
        if isinstance(c, list) and c:
            c = c[0]
            if isinstance(c, dict):
                c = c.get("message") or c.get("msg") or json.dumps(c, ensure_ascii=False)
        if isinstance(c, str) and c.strip():
            return c.strip()[:limit]
    return ""


def describe_status(code: int, body: bytes = b"") -> str:
    base = {
        400: "請求內容有誤（400）",
        401: "金鑰無效或未授權（401）",
        402: "帳戶額度不足（402）",
        403: "沒有權限（403）",
        404: "找不到資源（404）",
        408: "請求逾時（408）",
        413: "上傳內容太大（413）",
        422: "參數不被接受（422）",
        429: "請求太頻繁或額度不足（429）",
    }.get(code) or (f"伺服器錯誤（{code}）" if code >= 500 else f"HTTP 錯誤（{code}）")
    detail = upstream_message(body)
    hint = _reason_advice(detail) or _STATUS_ADVICE.get(code) or (_SERVER_ADVICE if code >= 500 else "")
    return _with_hint(f"{base}：{detail}" if detail else base, hint)


# English reasons vendors give, checked before the status code's general advice.
_REASON_ADVICE = [
    (re.compile(r"safety|content.?policy|moderat|nsfw|flagged|sensitive (content|word|information)|inappropriate"
                r"|policy violation", re.I),
     "內容可能被供應商的安全審查擋下，請修改提示詞或換一張參考圖"),
    (re.compile(r"api.?key|unauthori[sz]ed|authenticat|invalid token|expired token|credentials", re.I),
     "請檢查金鑰或存取權杖是否正確、是否過期"),
    (re.compile(r"insufficient.{0,20}(balance|credit|fund|quota)|balance|quota|\bcredits?\b|billing|payment required", re.I),
     "帳戶額度或點數不足，請到供應商網站儲值或確認方案"),
    (re.compile(r"rate.?limit|too many requests|concurren", re.I),
     "請求太頻繁或同時執行的任務太多，請稍等幾分鐘再試"),
    (re.compile(r"model.{0,40}(not found|not exist|unsupported|not supported)|(unknown|invalid) model|no such model", re.I),
     "模型名稱不存在或這組金鑰不能使用，請從模型清單重新挑選"),
    (re.compile(r"(duration|aspect.?ratio|resolution|seconds).{0,60}(invalid|unsupported|not supported|must|allowed"
                r"|out of range)|(invalid|unsupported) (duration|aspect.?ratio|resolution)", re.I),
     "秒數、比例或解析度不在模型支援的範圍內，請改用模型支援的設定"),
    (re.compile(r"too large|file size", re.I), "檔案太大，請縮小圖片或影片後再試"),
]
_STATUS_ADVICE = {
    400: "請檢查模型名稱與參數設定",
    401: "請檢查金鑰或存取權杖是否正確、是否過期",
    402: "帳戶額度或點數不足，請到供應商網站儲值或確認方案",
    403: "這組金鑰沒有使用這個模型或功能的權限，請到供應商後台確認",
    404: "請檢查網址或模型名稱是否正確",
    408: "處理逾時，請稍後再試",
    413: "檔案太大，請縮小圖片或影片後再試",
    422: "秒數、比例、解析度或其他參數不被接受，請改用模型支援的設定",
    429: "請稍等幾分鐘再試，或到供應商網站確認額度",
}
_SERVER_ADVICE = "供應商服務暫時異常，請稍後再試"


def _reason_advice(text: str) -> str:
    return next((hint for pattern, hint in _REASON_ADVICE if pattern.search(text)), "")


def _with_hint(text: str, hint: str) -> str:
    return f"{text.rstrip('。.')}。{hint}" if hint else text


def advise(message: str) -> str:
    """Add what to do about a failure reason a vendor reported in English, unless the message already says it."""
    hint = _reason_advice(message)
    return message if not hint or hint in message else _with_hint(message, hint)


def _read_limited(resp: Any, max_bytes: int) -> bytes:
    chunks = []
    total = 0
    while True:
        chunk = resp.read(_CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise AppError(f"回應內容超過上限（{max_bytes // (1024 * 1024)} MB）", 502)
        chunks.append(chunk)
    return b"".join(chunks)


def _network_error(exc: BaseException, url: str) -> HttpError:
    reason = getattr(exc, "reason", None) or exc
    if isinstance(reason, (socket.timeout, TimeoutError)):
        text = "連線逾時"
    elif isinstance(reason, ssl.SSLError):
        text = f"SSL 連線錯誤：{reason}"
    elif isinstance(reason, ConnectionRefusedError):
        text = "連線被拒（服務沒有啟動或連接埠錯誤）"
    elif isinstance(reason, socket.gaierror):
        text = "找不到主機（網址錯誤或網路未連線）"
    else:
        text = f"連線失敗：{reason}"
    host = urllib.parse.urlsplit(url).netloc
    return HttpError(f"{text}（{host}）", 0, b"", url)


def request(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    json_body: Any = None,
    data: bytes | None = None,
    timeout: float = 60,
    max_bytes: int = 512 * 1024 * 1024,
) -> Response:
    hdrs = {"User-Agent": USER_AGENT, **(headers or {})}
    if json_body is not None:
        data = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, method=method.upper(), headers=hdrs)
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            body = _read_limited(resp, max_bytes)
            return Response(resp.status, {k.lower(): v for k, v in resp.headers.items()}, body)
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read(256 * 1024) if exc.fp else b""
        except OSError:
            body = b""
        raise HttpError(describe_status(exc.code, body), exc.code, body, url) from None
    except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, ssl.SSLError, OSError) as exc:
        raise _network_error(exc, url) from None


def get_json(url: str, *, headers: dict[str, str] | None = None, timeout: float = 60) -> Any:
    return request("GET", url, headers=headers, timeout=timeout).json()


def post_json(url: str, body: Any, *, headers: dict[str, str] | None = None, timeout: float = 120) -> Any:
    return request("POST", url, headers=headers, json_body=body, timeout=timeout).json()


def download(
    url: str,
    dst: Path,
    *,
    headers: dict[str, str] | None = None,
    timeout: float = 120,
    max_bytes: int = 4 * 1024 * 1024 * 1024,
    cancelled: Callable[[], bool] | None = None,
) -> str:
    """Stream a URL to `dst`; returns the response Content-Type."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    tmp = dst.with_name(dst.name + ".part")
    try:
        with _OPENER.open(req, timeout=timeout) as resp, open(tmp, "wb") as fh:
            total = 0
            while True:
                if cancelled and cancelled():
                    raise AppError("已取消", 499)
                chunk = resp.read(_CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise AppError("下載檔案超過大小上限", 502)
                fh.write(chunk)
            ctype = resp.headers.get("Content-Type", "")
        if total == 0:
            raise AppError("下載到的檔案是空的", 502)
        tmp.replace(dst)
        return ctype
    except urllib.error.HTTPError as exc:
        raise HttpError("下載失敗：" + describe_status(exc.code), exc.code, b"", url) from None
    except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, ssl.SSLError) as exc:
        raise _network_error(exc, url) from None
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def multipart(fields: dict[str, Any], files: list[tuple[str, str, bytes, str]] | None = None) -> tuple[bytes, str]:
    """Encode multipart/form-data. files: (field, filename, data, mime)."""
    boundary = "----vc" + uuid.uuid4().hex
    out = bytearray()
    for name, value in fields.items():
        if value is None:
            continue
        out += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n".encode("utf-8")
        out += str(value).encode("utf-8") + b"\r\n"
    for name, filename, data, mime in files or []:
        out += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"; filename=\"{filename}\"\r\n"
                f"Content-Type: {mime}\r\n\r\n").encode("utf-8")
        out += data + b"\r\n"
    out += f"--{boundary}--\r\n".encode("utf-8")
    return bytes(out), f"multipart/form-data; boundary={boundary}"
