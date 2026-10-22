"""System tray icon (Windows only, plain ctypes): keeps Video Console reachable after its window is closed."""
from __future__ import annotations

import ctypes
import logging
import sys
import threading
from ctypes import wintypes
from pathlib import Path
from typing import Callable

log = logging.getLogger("vc.tray")

WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_TRAY = 0x0400 + 1  # WM_USER + 1: the icon's mouse events
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
NIM_ADD, NIM_DELETE = 0, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP = 0x1, 0x2, 0x4
MF_STRING, MF_SEPARATOR = 0x0, 0x800
TPM_RIGHTBUTTON, TPM_RETURNCMD = 0x2, 0x100
IMAGE_ICON, LR_LOADFROMFILE = 1, 0x10
SM_CXSMICON, SM_CYSMICON = 49, 50
IDI_APPLICATION = 32512
CMD_OPEN, CMD_QUIT = 1, 2
ERROR_CLASS_ALREADY_EXISTS = 1410  # start() again after the tray thread died

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND), ("uID", wintypes.UINT),
                ("uFlags", wintypes.UINT), ("uCallbackMessage", wintypes.UINT), ("hIcon", wintypes.HICON),
                ("szTip", wintypes.WCHAR * 128), ("dwState", wintypes.DWORD), ("dwStateMask", wintypes.DWORD),
                ("szInfo", wintypes.WCHAR * 256), ("uVersion", wintypes.UINT), ("szInfoTitle", wintypes.WCHAR * 64),
                ("dwInfoFlags", wintypes.DWORD), ("guidItem", ctypes.c_byte * 16), ("hBalloonIcon", wintypes.HICON)]


def _api() -> tuple[ctypes.WinDLL, ctypes.WinDLL, ctypes.WinDLL]:
    """Private DLL handles, so these argtypes never clash with other ctypes users of user32."""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.DefWindowProcW.restype = LRESULT
    user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                       ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND,
                                       wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
    user32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT, ctypes.c_int, ctypes.c_int,
                                  wintypes.UINT]
    user32.LoadImageW.restype = wintypes.HANDLE
    user32.LoadIconW.argtypes = [wintypes.HINSTANCE, wintypes.LPVOID]
    user32.LoadIconW.restype = wintypes.HICON
    user32.CreatePopupMenu.restype = wintypes.HMENU
    user32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR]
    user32.SetMenuDefaultItem.argtypes = [wintypes.HMENU, wintypes.UINT, wintypes.UINT]
    user32.TrackPopupMenu.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                      wintypes.HWND, wintypes.LPVOID]
    user32.DestroyMenu.argtypes = [wintypes.HMENU]
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.DestroyWindow.argtypes = [wintypes.HWND]
    user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
    user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.DispatchMessageW.restype = LRESULT
    shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
    shell32.ExtractIconExW.argtypes = [wintypes.LPCWSTR, ctypes.c_int, ctypes.POINTER(wintypes.HICON),
                                       ctypes.POINTER(wintypes.HICON), wintypes.UINT]
    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel32.GetModuleHandleW.restype = wintypes.HMODULE
    return user32, shell32, kernel32


class Tray:
    """A tray icon whose double-click or "開啟視窗" calls on_open and whose "結束" calls on_quit once
    can_quit agrees.

    start() may be called from any thread and more than once; the icon lives on a thread of its own."""

    def __init__(self, title: str, icon: Path, on_open: Callable[[], None], on_quit: Callable[[], None],
                 can_quit: Callable[[], bool] = lambda: True):
        self.title = title
        self.icon = icon
        self.on_open = on_open
        self.on_quit = on_quit
        self.can_quit = can_quit
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._hwnd: int | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if sys.platform != "win32":
            return
        with self._lock:
            if self.running:
                return
            self._ready.clear()
            self._thread = threading.Thread(target=self._run, name="tray", daemon=True)
            self._thread.start()
        self._ready.wait(5)

    def stop(self) -> None:
        """Remove the icon (a process that exits without this leaves a dead icon until hovered)."""
        thread, hwnd = self._thread, self._hwnd
        if thread is None or not thread.is_alive():
            return
        if hwnd:
            _api()[0].PostMessageW(hwnd, WM_CLOSE, 0, 0)
        thread.join(5)

    # ---------------------------------------------------------------- the tray thread
    def _run(self) -> None:
        try:
            self._loop()
        except Exception:
            log.exception("Tray icon failed")
        finally:
            self._hwnd = None
            self._ready.set()

    def _loop(self) -> None:
        user32, shell32, kernel32 = _api()
        hinst = kernel32.GetModuleHandleW(None)
        taskbar_created = user32.RegisterWindowMessageW("TaskbarCreated")  # Explorer restarted: add the icon again
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.uID = 1
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = WM_TRAY
        nid.hIcon = self._load_icon(user32, shell32)
        nid.szTip = self.title[:127]

        def wndproc(hwnd: int, msg: int, wparam: int, lparam: int) -> int:
            if msg == WM_TRAY:
                if lparam == WM_LBUTTONDBLCLK:
                    self._call(self.on_open)
                elif lparam == WM_RBUTTONUP:
                    self._menu(user32, hwnd)
                return 0
            if msg == taskbar_created:
                shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid))
                return 0
            if msg == WM_DESTROY:
                shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
                user32.PostQuitMessage(0)
                return 0
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        proc = WNDPROC(wndproc)  # kept referenced for the life of the window
        wc = WNDCLASSW(lpfnWndProc=proc, hInstance=hinst, lpszClassName=f"VideoConsoleTray{id(self)}")
        if not user32.RegisterClassW(ctypes.byref(wc)) and ctypes.get_last_error() != ERROR_CLASS_ALREADY_EXISTS:
            raise OSError(ctypes.get_last_error(), "RegisterClassW failed")
        hwnd = user32.CreateWindowExW(0, wc.lpszClassName, self.title, 0, 0, 0, 0, 0, None, None, hinst, None)
        if not hwnd:
            raise OSError(ctypes.get_last_error(), "CreateWindowExW failed")
        nid.hWnd = hwnd
        if not shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)):
            user32.DestroyWindow(hwnd)
            raise OSError(ctypes.get_last_error(), "Shell_NotifyIconW failed")
        self._hwnd = hwnd
        self._ready.set()
        log.info("Tray icon shown")
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        log.info("Tray icon removed")

    def _load_icon(self, user32: ctypes.WinDLL, shell32: ctypes.WinDLL) -> int:
        """The packaged exe's own icon, the .ico beside the source, or the stock application icon."""
        if self.icon.suffix.lower() == ".exe":
            small = wintypes.HICON()
            if shell32.ExtractIconExW(str(self.icon), 0, None, ctypes.byref(small), 1) and small.value:
                return small.value
        elif self.icon.is_file():
            cx, cy = user32.GetSystemMetrics(SM_CXSMICON), user32.GetSystemMetrics(SM_CYSMICON)
            handle = user32.LoadImageW(None, str(self.icon), IMAGE_ICON, cx, cy, LR_LOADFROMFILE)
            if handle:
                return handle
        log.warning("No icon in %s; using the stock one", self.icon)
        return user32.LoadIconW(None, ctypes.c_void_p(IDI_APPLICATION))

    def _menu(self, user32: ctypes.WinDLL, hwnd: int) -> None:
        menu = user32.CreatePopupMenu()
        user32.AppendMenuW(menu, MF_STRING, CMD_OPEN, "開啟視窗")
        user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(menu, MF_STRING, CMD_QUIT, f"結束 {self.title}")
        user32.SetMenuDefaultItem(menu, CMD_OPEN, 0)
        pt = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        user32.SetForegroundWindow(hwnd)  # without this the menu stays open after clicking elsewhere
        cmd = user32.TrackPopupMenu(menu, TPM_RIGHTBUTTON | TPM_RETURNCMD, pt.x, pt.y, 0, hwnd, None)
        user32.DestroyMenu(menu)
        if cmd == CMD_OPEN:
            self._call(self.on_open)
        elif cmd == CMD_QUIT and self.can_quit():
            user32.DestroyWindow(hwnd)  # the icon goes at once; shutting down can take a few seconds
            threading.Thread(target=self._call, args=(self.on_quit,), name="tray-quit").start()

    @staticmethod
    def _call(fn: Callable[[], None]) -> None:
        try:
            fn()
        except Exception:
            log.exception("Tray action failed")
