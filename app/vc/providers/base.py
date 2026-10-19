"""Shared plumbing for cloud image/video providers.

A provider is a stateless object that describes one vendor API: the settings fields and secrets
it needs (rendered by the settings page) plus `run` / `resume` / `test`. Everything that belongs
to a single job (config values, secrets, cancellation, progress, output folder) arrives in a
`Context`, so providers never touch settings, the secret store or the job queue directly.
"""
from __future__ import annotations

import base64
import binascii
import json
import logging
import math
import mimetypes
import re
import time
import urllib.parse
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Iterable

from .. import net
from ..util import AppError, fmt_seconds, guess_ext

log = logging.getLogger("vc.providers")

ProgressFn = Callable[[float, str], None]
CancelFn = Callable[[], bool]
RemoteFn = Callable[[dict], None]

TRANSIENT_CODES = {0, 408, 425, 429, 500, 502, 503, 504}
MAX_TRANSIENT = 6
_URL_RE = re.compile(r"^https?://[^\s/]+")
_MODEL_RE = re.compile(r"^[A-Za-z0-9._:/-]{1,200}$")


# ------------------------------------------------------------------ settings fields
def _options(options: Iterable[Any]) -> list[dict]:
    out = []
    for opt in options:
        if isinstance(opt, (tuple, list)):
            out.append({"value": opt[0], "label": opt[1]})
        else:
            out.append({"value": opt, "label": str(opt)})
    return out


def text(key: str, label: str, default: str = "", **extra: Any) -> dict:
    return {"key": key, "label": label, "type": "text", "default": default, **extra}


def url_field(key: str, label: str, default: str = "", **extra: Any) -> dict:
    return {"key": key, "label": label, "type": "url", "default": default, **extra}


def json_field(key: str, label: str, default: str = "{}", **extra: Any) -> dict:
    return {"key": key, "label": label, "type": "json", "default": default, **extra}


def select(key: str, label: str, options: Iterable[Any], default: Any, **extra: Any) -> dict:
    return {"key": key, "label": label, "type": "select", "options": _options(options), "default": default, **extra}


def combo(key: str, label: str, options: Iterable[Any], default: str, **extra: Any) -> dict:
    """Free text with suggestions (model names change faster than this app)."""
    return {"key": key, "label": label, "type": "combo", "options": _options(options), "default": default, **extra}


def number(key: str, label: str, default: float, lo: float, hi: float, step: float = 1, **extra: Any) -> dict:
    return {"key": key, "label": label, "type": "number", "default": default, "min": lo, "max": hi, "step": step,
            **extra}


def toggle(key: str, label: str, default: bool = False, **extra: Any) -> dict:
    return {"key": key, "label": label, "type": "bool", "default": default, **extra}


def secret(key: str, label: str, help: str = "") -> dict:  # noqa: A002 - mirrors field dict keys
    return {"key": key, "label": label, "help": help}


def _coerce(spec: dict, value: Any, strict: bool) -> Any:
    kind, default, label = spec["type"], spec["default"], spec["label"]

    def bad(message: str) -> Any:
        if strict:
            raise AppError(message)
        return default

    if value is None:
        return default
    if kind == "bool":
        return bool(value)
    if kind == "number":
        try:
            num = float(value)
        except (TypeError, ValueError):
            return bad(f"{label} 必須是數字")
        if not math.isfinite(num):
            return bad(f"{label} 必須是數字")
        num = min(max(num, spec["min"]), spec["max"])
        return int(round(num)) if isinstance(default, int) and not isinstance(default, bool) else num
    if kind == "select":
        allowed = [o["value"] for o in spec["options"]]
        return value if value in allowed else bad(f"{label} 的選項不正確")
    raw = str(value)[:20000]
    raw = raw if kind == "json" else raw.strip()
    if kind == "url" and raw and not _URL_RE.match(raw):
        return bad(f"{label} 必須是 http:// 或 https:// 開頭的網址")
    if kind == "url":
        raw = raw.rstrip("/")
    if kind == "json" and raw.strip():
        try:
            json.loads(raw)
        except ValueError as exc:
            return bad(f"{label} 不是有效的 JSON：{exc}")
    return raw


# ------------------------------------------------------------------ request / context
@dataclass(frozen=True)
class Request:
    kind: str  # "image" | "video"
    prompt: str
    negative: str = ""
    width: int = 768
    height: int = 448
    seed: int = 0
    image: bytes | None = None  # reference image (i2i) or first frame (i2v)
    image_mime: str = "image/png"
    denoise: float = 0.55  # i2i only: 0 keeps the reference, 1 ignores it
    duration: float = 5.0  # video only, seconds
    fps: int = 24

    @property
    def mode(self) -> str:
        if self.kind == "image":
            return "i2i" if self.image else "t2i"
        return "i2v" if self.image else "t2v"

    def image_b64(self) -> str:
        return base64.b64encode(self.image).decode("ascii") if self.image else ""

    def image_data_uri(self) -> str:
        return f"data:{self.image_mime};base64,{self.image_b64()}" if self.image else ""


class Context:
    """Per-job hooks handed to a provider."""

    def __init__(
        self,
        provider_id: str,
        config: dict,
        secret_getter: Callable[[str], str],
        *,
        dst_dir: Path | None = None,
        on_progress: ProgressFn | None = None,
        cancelled: CancelFn | None = None,
        on_remote: RemoteFn | None = None,
        timeout: float = 1800,
        models: dict | None = None,
    ):
        self.provider_id = provider_id
        self.config = config
        self.models = models  # the provider's detected model list (catalog.py), when it has one
        self._secret = secret_getter
        self.dst_dir = dst_dir
        self._on_progress = on_progress
        self._cancelled = cancelled
        self._on_remote = on_remote
        self.timeout = timeout
        self.files: list[Path] = []
        self.notes: list[str] = []  # user-visible remarks, e.g. "reference image skipped"

    # secrets ---------------------------------------------------------
    def secret(self, key: str) -> str:
        return (self._secret(key) or "").strip()

    def require_secret(self, key: str, label: str) -> str:
        value = self.secret(key)
        if not value:
            raise AppError(f"尚未填寫{label}，請到「引擎 › 雲端 API」補上", 400)
        return value

    # progress / cancel -----------------------------------------------
    def progress(self, frac: float, message: str) -> None:
        if self._on_progress:
            self._on_progress(max(0.0, min(1.0, frac)), message)

    def is_cancelled(self) -> bool:
        return bool(self._cancelled and self._cancelled())

    def check(self) -> None:
        if self.is_cancelled():
            raise AppError("已取消", 499)

    def sleep(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while True:
            self.check()
            left = deadline - time.monotonic()
            if left <= 0:
                return
            time.sleep(min(0.25, left))

    def remote(self, data: dict) -> None:
        """Record the vendor task handle so the job can resume after an app restart."""
        if self._on_remote:
            self._on_remote(data)

    # outputs -----------------------------------------------------------
    def _target(self, ext: str) -> Path:
        if self.dst_dir is None:
            raise AppError("內部錯誤：沒有指定輸出資料夾", 500)
        self.dst_dir.mkdir(parents=True, exist_ok=True)
        return self.dst_dir / f"{len(self.files):02d}_{self.provider_id}{ext}"

    def save_bytes(self, data: bytes, ext: str = "") -> Path:
        if not data:
            raise AppError("供應商回傳的檔案是空的", 502)
        path = self._target(guess_ext(data, ext or ".bin"))
        path.write_bytes(data)
        self.files.append(path)
        return path

    def save_url(self, url: str, headers: dict[str, str] | None = None) -> Path:
        if url.startswith("data:"):
            return self.save_bytes(decode_data_uri(url))
        if not _URL_RE.match(url):
            raise AppError("供應商回傳的檔案網址格式不正確", 502)
        tmp = self._target(".download")
        ctype = net.download(url, tmp, headers=headers, timeout=180, cancelled=self.is_cancelled)
        with open(tmp, "rb") as fh:
            head = fh.read(64)
        final = tmp.with_suffix(guess_ext(head, _ext_from(ctype, url)))
        tmp.replace(final)
        self.files.append(final)
        return final


def _ext_from(content_type: str, url: str) -> str:
    ext = mimetypes.guess_extension((content_type or "").split(";")[0].strip()) or ""
    if ext in (".jpe", ".jpeg"):
        ext = ".jpg"
    if not ext:
        ext = Path(urllib.parse.urlparse(url).path).suffix.lower()
    return ext if re.match(r"^\.[a-z0-9]{2,5}$", ext) else ".bin"


# ------------------------------------------------------------------ helpers
def b64decode_loose(value: str) -> bytes:
    value = value.split(",", 1)[1] if value.startswith("data:") else value
    value = re.sub(r"\s+", "", value)
    try:
        return base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_" if "-" in value or "_" in value
                                else None)
    except (binascii.Error, ValueError):
        raise AppError("供應商回傳的 base64 內容無法解碼", 502) from None


def decode_data_uri(uri: str) -> bytes:
    if ";base64," not in uri[:120]:
        return urllib.parse.unquote_to_bytes(uri.split(",", 1)[-1])
    return b64decode_loose(uri)


def nearest_ratio(width: int, height: int, choices: Iterable[str]) -> str:
    """Pick the vendor aspect ratio closest to width/height ("16:9" or "1280:720" style)."""
    target = math.log(max(width, 1) / max(height, 1))
    best, best_d = "", float("inf")
    for choice in choices:
        a, b = (float(x) for x in re.split(r"[:x]", choice))
        d = abs(math.log(a / b) - target)
        if d < best_d:
            best, best_d = choice, d
    return best


def scaled_size(width: int, height: int, megapixels: float = 1.0, multiple: int = 16) -> tuple[int, int]:
    """Same aspect ratio, about `megapixels` large, both sides a multiple of `multiple`."""
    scale = math.sqrt(megapixels * 1_000_000 / max(width * height, 1))
    w = max(multiple, int(round(width * scale / multiple)) * multiple)
    h = max(multiple, int(round(height * scale / multiple)) * multiple)
    return w, h


def nearest_value(value: float, allowed: Iterable[float]) -> float:
    options = list(allowed)
    return min(options, key=lambda v: (abs(v - value), v)) if options else value


def duration_seconds(value: Any) -> float | None:
    """Seconds of a video length as vendors spell it: 5, "5" or "5s"."""
    found = None if isinstance(value, bool) else re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*s?\s*", str(value))
    return float(found.group(1)) if found else None


def pick_duration(seconds: float, allowed: list) -> Any:
    """The allowed length closest to `seconds`, spelled as the vendor wants it; a tie goes to the shorter one."""
    return min(allowed, key=lambda v: (abs(duration_seconds(v) - seconds), duration_seconds(v)))


def parse_numbers(text_value: str) -> list[float]:
    out = []
    for part in re.split(r"[,\s，、]+", text_value or ""):
        try:
            out.append(float(part))
        except ValueError:
            continue
    return out


def with_avoid(prompt: str, negative: str) -> str:
    """For APIs without a negative prompt field, fold it into the prompt as an instruction."""
    negative = (negative or "").strip()
    return f"{prompt}\n\nAvoid: {negative}" if negative else prompt


def clean_model(name: str) -> str:
    name = (name or "").strip().strip("/")
    if not _MODEL_RE.match(name) or ".." in name:
        raise AppError(f"模型名稱格式不正確：{name or '（空白）'}")
    return name


def same_host(a: str, b: str) -> bool:
    return urllib.parse.urlparse(a).hostname == urllib.parse.urlparse(b).hostname


def require_host(url: str, *suffixes: str) -> str:
    """Refuse to send credentials to a host that is not the vendor's (defence for stored task URLs)."""
    host = urllib.parse.urlparse(url).hostname or ""
    if not url.startswith("https://") or not any(host == s or host.endswith("." + s) for s in suffixes):
        raise AppError(f"任務網址不在預期的服務網域：{host or url[:60]}", 502)
    return url


def first_url(data: Any, depth: int = 0) -> str | None:
    """Find the first media URL in a vendor result (images[0].url, video.url, output[0] ...)."""
    if depth > 6:
        return None
    if isinstance(data, str):
        return data if data.startswith(("http://", "https://", "data:")) else None
    if isinstance(data, list):
        for item in data:
            found = first_url(item, depth + 1)
            if found:
                return found
        return None
    if isinstance(data, dict):
        for key in ("url", "uri", "video", "videos", "images", "image", "output", "outputs", "file", "data"):
            if key in data:
                found = first_url(data[key], depth + 1)
                if found:
                    return found
    return None


def merge_extra(payload: dict, extra_json: str) -> dict:
    if not (extra_json or "").strip():
        return payload
    try:
        extra = json.loads(extra_json)
    except ValueError as exc:
        raise AppError(f"額外參數不是有效的 JSON：{exc}") from None
    if not isinstance(extra, dict):
        raise AppError("額外參數必須是 JSON 物件（大括號包住）")
    return {**payload, **extra}


# ------------------------------------------------------------------ polling
@dataclass(frozen=True)
class Poll:
    done: bool = False
    result: Any = None
    message: str = ""
    progress: float | None = None  # 0..1 when the vendor reports it


def poll(ctx: Context, fetch: Callable[[], Poll], *, interval: float = 4.0, expected: float = 90.0,
         label: str = "雲端生成中") -> Any:
    """Call `fetch` until it reports done. Tolerates a few transient network/5xx failures."""
    start = time.monotonic()
    failures = 0
    while True:
        ctx.check()
        try:
            state = fetch()
            failures = 0
        except net.HttpError as exc:
            if exc.code not in TRANSIENT_CODES:
                raise
            failures += 1
            if failures >= MAX_TRANSIENT:
                raise
            log.warning("%s poll failed (%s), retry %d", ctx.provider_id, exc.message, failures)
            state = Poll(message=f"連線不穩，重試中（{failures}/{MAX_TRANSIENT - 1}）")
        if state.done:
            return state.result
        elapsed = time.monotonic() - start
        if elapsed > ctx.timeout:
            raise AppError(f"雲端任務超過 {int(ctx.timeout // 60)} 分鐘仍未完成，已停止等待", 504)
        frac = state.progress if state.progress is not None else 1 - math.exp(-elapsed / max(expected, 1.0))
        ctx.progress(0.1 + 0.85 * frac, f"{state.message or label}（已等 {fmt_seconds(elapsed)}）")
        ctx.sleep(interval)


# ------------------------------------------------------------------ provider base
class Provider:
    id: str = ""
    name: str = ""
    kind: str = "image"  # image | video
    vendor: str = ""
    modes: tuple[str, ...] = ()  # t2i / i2i, or t2v / i2v
    docs: str = ""  # where to create an API key
    notes: str = ""
    resumable: bool = False  # can continue polling after an app restart
    online_test: bool = False  # test() really calls the vendor
    expected_seconds: float = 30.0
    key_owner: str = ""  # share another provider's secrets (same vendor)
    fields: tuple[dict, ...] = ()
    secrets: tuple[dict, ...] = (secret("api_key", "API 金鑰"),)
    model_keys: dict[str, str] = {}  # mode -> field holding its model, for providers whose models can be listed

    # config ----------------------------------------------------------------
    def defaults(self) -> dict:
        return {f["key"]: f["default"] for f in self.fields}

    def resolve_config(self, stored: dict | None) -> dict:
        stored = stored if isinstance(stored, dict) else {}
        return {f["key"]: _coerce(f, stored.get(f["key"]), strict=False) for f in self.fields}

    def validate_config(self, values: Any) -> dict:
        if not isinstance(values, dict):
            raise AppError("設定格式不正確")
        return {f["key"]: _coerce(f, values.get(f["key"]), strict=True) for f in self.fields}

    def secret_owner(self) -> str:
        return self.key_owner or self.id

    def describe(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "vendor": self.vendor,
            "modes": list(self.modes),
            "docs": self.docs,
            "notes": self.notes,
            "resumable": self.resumable,
            "onlineTest": self.online_test,
            "keyOwner": self.key_owner,
            "fields": [dict(f) for f in self.fields],
            "secrets": [dict(s) for s in self.secrets],
            "modelKeys": dict(self.model_keys),
        }

    # model list ----------------------------------------------------------------
    def detect_models(self, ctx: Context) -> dict[str, dict]:
        """The vendor's current models per field key (shape in catalog.py)."""
        raise AppError(f"{self.name} 沒有可查詢的模型清單", 400)

    def catalog_source(self, config: dict) -> str:
        """What a detected list depends on besides the key: a list fetched for another source is stale."""
        return str(config.get("baseUrl") or "")

    def durations(self, config: dict, mode: str, models: dict | None) -> list | None:
        """Video lengths the model for `mode` takes, spelled as the vendor wants them sent. [] when the model
        sets its own length; None when the shot's seconds go out as they are."""
        return None

    # work --------------------------------------------------------------------
    def run(self, ctx: Context, req: Request) -> list[Path]:
        raise NotImplementedError

    def resume(self, ctx: Context, remote: dict) -> list[Path]:
        raise AppError("這個供應商的任務無法在程式重新啟動後接續，請重新送出", 409)

    def cancel_remote(self, ctx: Context, remote: dict) -> None:
        """Best effort: ask the vendor to stop a submitted task (most vendors cannot)."""

    def test(self, ctx: Context) -> dict:
        for s in self.secrets:
            ctx.require_secret(s["key"], s["label"])
        return {"ok": True, "online": False, "message": "金鑰已填寫；這個供應商沒有免費的檢查端點，未實際連線驗證"}

    def check_mode(self, ctx: Context, req: Request) -> Request:
        """Drop the image when the vendor cannot use it; refuse when it requires one."""
        if req.mode in self.modes:
            return req
        if req.image is not None:
            ctx.notes.append(f"{self.name} 不支援參考圖，已略過")
            return replace(req, image=None)
        what = "鏡頭圖片" if self.kind == "video" else "參考圖"
        raise AppError(f"{self.name} 需要{what}才能生成，請先準備好{what}", 400)
