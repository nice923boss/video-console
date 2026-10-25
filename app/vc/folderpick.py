"""The system's own "select folder" dialog (Windows only, plain ctypes COM).

A browser page cannot learn local paths, so the server opens the dialog and hands back the folder picked.
Like File Explorer (see explorer.py), the dialog belongs to a windowless process and would open behind the
browser, so it is brought to the front once it appears."""
from __future__ import annotations

import ctypes
import logging
import os
import threading
import time
from ctypes import wintypes
from pathlib import Path

from . import explorer
from .util import AppError

log = logging.getLogger("vc.folderpick")

CLSID_FILE_OPEN_DIALOG = "{DC1C5A9C-E88A-4DDE-A5A1-60F82A20AEF7}"
IID_FILE_OPEN_DIALOG = "{D57C7288-D4AD-4768-BE02-9D969532D960}"
IID_SHELL_ITEM = "{43826D1E-E718-42EE-BC55-A1E261C37BFE}"
CLSCTX_INPROC_SERVER = 1
COINIT_APARTMENTTHREADED = 2
FOS_PICKFOLDERS = 0x20
FOS_FORCEFILESYSTEM = 0x40
SIGDN_FILESYSPATH = 0x80058000 - 0x100000000  # as a signed int, the way ctypes passes an enum
CANCELED = 0x800704C7 - 0x100000000  # HRESULT_FROM_WIN32(ERROR_CANCELLED)
DIALOG_CLASS = "#32770"
# Vtable slots: IUnknown 0-2, IModalWindow::Show 3, then IFileDialog in declaration order; IShellItem 3-7.
RELEASE, SHOW, SET_OPTIONS, GET_OPTIONS, SET_FOLDER, SET_TITLE, GET_RESULT = 2, 3, 9, 10, 12, 17, 20
GET_DISPLAY_NAME = 5

_open = threading.Lock()  # one dialog at a time


class GUID(ctypes.Structure):
    _fields_ = [("data1", wintypes.DWORD), ("data2", wintypes.WORD), ("data3", wintypes.WORD),
                ("data4", ctypes.c_ubyte * 8)]


def _guid(ole32: ctypes.WinDLL, text: str) -> GUID:
    guid = GUID()
    ole32.CLSIDFromString(text, ctypes.byref(guid))
    return guid


def _call(obj: ctypes.c_void_p, slot: int, *args: tuple[type, object]) -> int:
    """Call a COM method by vtable slot; returns the HRESULT (or the count, for Release)."""
    vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    proto = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *(kind for kind, _ in args))
    return proto(vtable[slot])(obj, *(value for _, value in args))


def _check(hr: int, what: str) -> None:
    if hr < 0:
        raise OSError(f"{what} failed: HRESULT 0x{hr & 0xFFFFFFFF:08X}")


def pick(start: Path | None = None, title: str = "選擇資料夾") -> str | None:
    """Show the dialog, starting in start (or its nearest existing parent), and wait for the user. Returns
    the folder picked, or None when canceled."""
    if not _open.acquire(blocking=False):
        raise AppError("已經開著一個選擇資料夾的視窗", 409)
    try:
        return _pick(start, title)
    finally:
        _open.release()


def _pick(start: Path | None, title: str) -> str | None:
    ole32 = ctypes.WinDLL("ole32")
    shell32 = ctypes.WinDLL("shell32")
    ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    ole32.CoCreateInstance.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p,
                                       ctypes.POINTER(ctypes.c_void_p)]
    ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    shell32.SHCreateItemFromParsingName.argtypes = [wintypes.LPCWSTR, ctypes.c_void_p, ctypes.c_void_p,
                                                    ctypes.POINTER(ctypes.c_void_p)]
    initialized = ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED) >= 0
    dialog = ctypes.c_void_p()
    try:
        _check(ole32.CoCreateInstance(ctypes.byref(_guid(ole32, CLSID_FILE_OPEN_DIALOG)), None,
                                      CLSCTX_INPROC_SERVER, ctypes.byref(_guid(ole32, IID_FILE_OPEN_DIALOG)),
                                      ctypes.byref(dialog)), "CoCreateInstance")
        options = wintypes.DWORD()
        _call(dialog, GET_OPTIONS, (ctypes.POINTER(wintypes.DWORD), ctypes.byref(options)))
        _call(dialog, SET_OPTIONS, (wintypes.DWORD, options.value | FOS_PICKFOLDERS | FOS_FORCEFILESYSTEM))
        _call(dialog, SET_TITLE, (wintypes.LPCWSTR, title))
        folder = _existing(start)
        if folder is not None:
            item = ctypes.c_void_p()
            if shell32.SHCreateItemFromParsingName(str(folder), None, ctypes.byref(_guid(ole32, IID_SHELL_ITEM)),
                                                   ctypes.byref(item)) >= 0:
                _call(dialog, SET_FOLDER, (ctypes.c_void_p, item))
                _call(item, RELEASE)
        threading.Thread(target=_bring_forward, name="folderpick-front", daemon=True).start()
        hr = _call(dialog, SHOW, (wintypes.HWND, None))
        if hr == CANCELED:
            return None
        _check(hr, "IFileDialog::Show")
        item = ctypes.c_void_p()
        _check(_call(dialog, GET_RESULT, (ctypes.POINTER(ctypes.c_void_p), ctypes.byref(item))), "GetResult")
        name = ctypes.c_void_p()
        try:
            _check(_call(item, GET_DISPLAY_NAME, (ctypes.c_int, SIGDN_FILESYSPATH),
                         (ctypes.POINTER(ctypes.c_void_p), ctypes.byref(name))), "GetDisplayName")
            return ctypes.wstring_at(name.value)
        finally:
            ole32.CoTaskMemFree(name)
            _call(item, RELEASE)
    finally:
        if dialog:
            _call(dialog, RELEASE)
        if initialized:
            ole32.CoUninitialize()


def _existing(path: Path | None) -> Path | None:
    while path is not None and not path.is_dir():
        path = path.parent if path.parent != path else None
    return path


def _bring_forward() -> None:
    user32, kernel32 = explorer._api()
    me = os.getpid()
    pid = wintypes.DWORD()
    buf = ctypes.create_unicode_buffer(64)
    deadline = time.monotonic() + explorer.WAIT
    while time.monotonic() < deadline:
        found: list[int] = []

        def collect(hwnd: int, _: int) -> bool:
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value == me and user32.IsWindowVisible(hwnd):
                user32.GetClassNameW(hwnd, buf, 64)
                if buf.value == DIALOG_CLASS:
                    found.append(hwnd)
            return True

        user32.EnumWindows(explorer.ENUMPROC(collect), 0)
        if found:
            if not explorer._activate(user32, kernel32, found[0]):
                log.warning("Could not bring the folder dialog to the front")
            return
        time.sleep(0.1)
    log.warning("The folder dialog did not appear within %.0f s", explorer.WAIT)
