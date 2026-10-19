"""API keys and tokens, encrypted at rest with Windows DPAPI (current-user scope).

Values never leave this module except through get(); the UI only ever sees masked strings.
"""
from __future__ import annotations

import base64
import ctypes
import logging
import re
import sys
import threading
from pathlib import Path

from .util import AppError, mask_secret, read_json, write_json_atomic

log = logging.getLogger("vc.secrets")
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,79}$")
_ENTROPY = b"video-console/secrets/v1"

if sys.platform == "win32":
    from ctypes import wintypes

    class _Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    _crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _BLOB_P = ctypes.POINTER(_Blob)
    _crypt32.CryptProtectData.argtypes = [
        _BLOB_P, wintypes.LPCWSTR, _BLOB_P, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, _BLOB_P,
    ]
    _crypt32.CryptProtectData.restype = wintypes.BOOL
    _crypt32.CryptUnprotectData.argtypes = [
        _BLOB_P, ctypes.c_void_p, _BLOB_P, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, _BLOB_P,
    ]
    _crypt32.CryptUnprotectData.restype = wintypes.BOOL
    _kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    _kernel32.LocalFree.restype = ctypes.c_void_p
    _UI_FORBIDDEN = 0x01

    def _blob(data: bytes) -> tuple[_Blob, ctypes.Array]:
        buf = ctypes.create_string_buffer(data, len(data))
        return _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf

    def _take(blob: _Blob) -> bytes:
        try:
            return ctypes.string_at(blob.pbData, blob.cbData)
        finally:
            _kernel32.LocalFree(ctypes.cast(blob.pbData, ctypes.c_void_p))

    def protect(data: bytes) -> bytes:
        src, _src_buf = _blob(data)
        ent, _ent_buf = _blob(_ENTROPY)
        out = _Blob()
        ok = _crypt32.CryptProtectData(
            ctypes.byref(src), "video-console", ctypes.byref(ent), None, None, _UI_FORBIDDEN, ctypes.byref(out)
        )
        if not ok:
            raise ctypes.WinError(ctypes.get_last_error())
        return _take(out)

    def unprotect(data: bytes) -> bytes:
        src, _src_buf = _blob(data)
        ent, _ent_buf = _blob(_ENTROPY)
        out = _Blob()
        ok = _crypt32.CryptUnprotectData(
            ctypes.byref(src), None, ctypes.byref(ent), None, None, _UI_FORBIDDEN, ctypes.byref(out)
        )
        if not ok:
            raise ctypes.WinError(ctypes.get_last_error())
        return _take(out)

    SCHEME = "dpapi"
else:  # pragma: no cover - the app targets Windows; this keeps tests importable elsewhere

    def protect(data: bytes) -> bytes:
        return data

    def unprotect(data: bytes) -> bytes:
        return data

    SCHEME = "plain"


class SecretStore:
    def __init__(self, path: Path):
        self._path = path
        self._lock = threading.Lock()
        self._values: dict[str, str] = {}
        self._unreadable: set[str] = set()
        self._load()

    def _load(self) -> None:
        raw = read_json(self._path, {}) or {}
        scheme = raw.get("scheme", SCHEME)
        for name, enc in (raw.get("items") or {}).items():
            try:
                blob = base64.b64decode(enc)
                plain = unprotect(blob) if scheme == "dpapi" else blob
                self._values[name] = plain.decode("utf-8")
            except Exception:  # noqa: BLE001 - any failure means the value is unusable
                self._unreadable.add(name)
                log.warning("Secret %s could not be decrypted (different Windows account?)", name)

    def _save(self) -> None:
        items = {
            name: base64.b64encode(protect(value.encode("utf-8"))).decode("ascii")
            for name, value in self._values.items()
        }
        write_json_atomic(self._path, {"scheme": SCHEME, "items": items})

    def get(self, name: str) -> str:
        with self._lock:
            return self._values.get(name, "")

    def set(self, name: str, value: str | None) -> None:
        if not _NAME_RE.match(name or ""):
            raise AppError("金鑰名稱格式不正確")
        value = (value or "").strip()
        with self._lock:
            if value:
                self._values[name] = value
            else:
                self._values.pop(name, None)
            self._unreadable.discard(name)
            self._save()

    def status(self) -> dict[str, dict]:
        """Masked view for the UI: {name: {set, masked, unreadable?}}."""
        with self._lock:
            out = {n: {"set": True, "masked": mask_secret(v)} for n, v in self._values.items()}
            for n in self._unreadable:
                out.setdefault(n, {"set": False, "masked": "", "unreadable": True})
            return out
