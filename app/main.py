"""Video Console launcher: one server per data folder, shown in a Chrome (or Edge) app window.
A window launch also puts an icon in the system tray, so closing the window leaves it running there.

    pythonw app\\main.py             start (or bring up a window onto the running copy)
    python app\\main.py --stop       stop the running copy
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path
from typing import IO, Any

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from vc import APP_NAME, BUILD, DEFAULT_PORT, VERSION  # noqa: E402
from vc.logsetup import setup_logging  # noqa: E402
from vc.paths import Paths  # noqa: E402
from vc.tray import Tray  # noqa: E402

log = logging.getLogger("vc.main")

PORTS = (*range(DEFAULT_PORT, DEFAULT_PORT + 10), 0)  # 0 lets Windows pick any free port
BROWSERS = (("chrome.exe", "Google/Chrome/Application"), ("msedge.exe", "Microsoft/Edge/Application"))
# a system proxy must never see requests to our own loopback server
LOCAL = urllib.request.build_opener(urllib.request.ProxyHandler({}))
PROCESS = Path(sys.executable).name  # pythonw.exe from source, "Video Console.exe" when packaged
# the tray icon: the packaged exe carries its own; from source, use the one the build embeds
ICON = Path(sys.executable) if getattr(sys, "frozen", False) else ROOT.parent / "packaging" / "icon.ico"


# ---------------------------------------------------------------- the running copy
def read_server_info(paths: Paths) -> dict | None:
    try:
        info = json.loads(paths.pid_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return info if isinstance(info, dict) and isinstance(info.get("port"), int) else None


def ping(info: dict | None, timeout: float = 2.0) -> dict | None:
    """The ping answer of the server described by server.json, when it answers as that same instance."""
    if not info:
        return None
    try:
        with LOCAL.open(f"http://127.0.0.1:{info['port']}/api/ping", timeout=timeout) as resp:
            pong = json.loads(resp.read().decode("utf-8"))
    except (OSError, ValueError):
        return None
    return pong if isinstance(pong, dict) and pong.get("instance") == info.get("instance") else None


def alive(info: dict | None, timeout: float = 2.0) -> bool:
    """True when the server described by server.json answers as that same instance."""
    return ping(info, timeout) is not None


def stop_running(paths: Paths) -> int:
    info = read_server_info(paths)
    if not alive(info):
        print("Video Console 沒有在執行。")
        return 0
    req = urllib.request.Request(f"http://127.0.0.1:{info['port']}/api/shutdown", data=b"{}", method="POST",
                                 headers={"X-VC": "1", "Content-Type": "application/json"})
    try:
        LOCAL.open(req, timeout=5).close()
    except OSError as exc:
        print(f"無法通知 Video Console 結束：{exc}")
        return 1
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if not alive(info, 0.5):
            print("Video Console 已結束。")
            return 0
        time.sleep(0.3)
    print(f"Video Console 沒有在 15 秒內結束，請從工作管理員結束 {PROCESS}。")
    return 1


def request_tray(info: dict) -> None:
    """Ask a copy started without a window (by Claude or a tool) to show its tray icon."""
    req = urllib.request.Request(f"http://127.0.0.1:{info['port']}/api/tray", data=b"{}", method="POST",
                                 headers={"X-VC": "1", "Content-Type": "application/json"})
    try:
        LOCAL.open(req, timeout=5).close()
    except OSError as exc:
        log.warning("Could not ask the running copy for its tray icon: %s", exc)


def acquire_lock(path: Path) -> IO[bytes] | None:
    """Exclusive lock held for the life of this process; None when another copy already holds it."""
    f = open(path, "a+b")  # noqa: SIM115 - kept open on purpose: closing it releases the lock
    try:
        if sys.platform == "win32":
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    return f


# ---------------------------------------------------------------- the window
def registered_exe(exe: str) -> Path | None:
    if sys.platform != "win32":
        return None
    import winreg
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            with winreg.OpenKey(hive, rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe}") as key:
                path = Path(str(winreg.QueryValue(key, None)).strip('"'))
        except OSError:
            continue
        if path.is_file():
            return path
    return None


def find_browser() -> Path | None:
    """Chrome first, then Edge (always present on Windows 11)."""
    for exe, folder in BROWSERS:
        for env in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA"):
            base = os.environ.get(env)
            if base and (Path(base) / folder / exe).is_file():
                return Path(base) / folder / exe
        found = registered_exe(exe)
        if found:
            return found
    return None


def open_window(url: str, paths: Paths) -> None:
    exe = find_browser()
    if exe is None:
        log.info("Neither Chrome nor Edge found; opening the default browser")
        webbrowser.open(url)
        return
    # a profile of its own keeps the window out of the user's normal browser session;
    # one per browser, since Chrome and Edge cannot share a profile folder
    profile = paths.run / f"browser-profile-{exe.stem}"
    subprocess.Popen([str(exe), f"--app={url}", f"--user-data-dir={profile}", "--no-first-run",
                      "--no-default-browser-check", "--start-maximized"])
    log.info("Opened a window with %s", exe.name)


# ---------------------------------------------------------------- launching
def fail(message: str, quiet: bool) -> int:
    """Report a launch failure. pythonw has no console, so a window launch also shows a message box."""
    log.error("Launch failed: %s", message)
    print(message, file=sys.stderr)
    if not quiet and sys.platform == "win32":
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, message, APP_NAME, 0x10)  # MB_ICONERROR
    return 1


def should_replace(pong: dict, no_window: bool) -> bool:
    """A copy left running in the tray from before an update serves old code to the new pages, so restart it.
    When that would interrupt running jobs, a window launch asks first and any other launch keeps it."""
    if pong.get("build") == BUILD:
        return False
    running = pong.get("running") or 0  # older copies do not report it
    if not running:
        return True
    if no_window or sys.platform != "win32":
        return False
    import ctypes
    message = (f"正在執行的 Video Console 是更新前的程式（{pong.get('version') or '舊版'}），重新啟動後才會用到 {VERSION}。\n"
               f"還有 {running} 個工作在執行，重新啟動會中斷它們。\n\n"
               "要現在重新啟動嗎？選「否」會先開啟目前執行中的程式。")
    return ctypes.windll.user32.MessageBoxW(None, message, APP_NAME, 0x34) == 6  # MB_YESNO | MB_ICONWARNING, IDYES


def claim_instance(paths: Paths, no_window: bool, wait: float = 10.0) -> IO[bytes] | int:
    """The instance lock when this launch should serve; otherwise the exit code after opening a window
    onto the copy that holds it. A copy that is shutting down keeps the lock for a moment after its
    server stops answering, so keep trying for a while before calling it stuck."""
    deadline = time.monotonic() + wait
    replaced = False
    while True:
        lock = acquire_lock(paths.run / "instance.lock")
        if lock is not None:
            return lock
        info = read_server_info(paths)
        pong = ping(info)
        if pong is not None and not replaced and should_replace(pong, no_window):
            log.info("Restarting the running copy (%s) to run %s", pong.get("build") or pong.get("version"), BUILD)
            replaced = True
            stop_running(paths)
            deadline = time.monotonic() + wait
            continue
        if pong is not None:
            if not no_window:
                request_tray(info)
                open_window(f"http://127.0.0.1:{info['port']}/", paths)
            log.info("Already running on port %s", info["port"])
            return 0
        if time.monotonic() >= deadline:
            return fail(f"另一個 Video Console 正在執行但沒有回應。請在工作管理員結束 {PROCESS} 後再試一次。",
                        no_window)
        time.sleep(0.3)


def confirm_quit(app: Any) -> bool:
    """Quitting from the tray interrupts running jobs, so ask first when there are any."""
    counts = app.jobs.state()["counts"]
    busy = (counts.get("running") or 0) + (counts.get("queued") or 0)
    if not busy or sys.platform != "win32":
        return True
    import ctypes
    message = (f"還有 {busy} 個工作在執行或排隊。結束後執行中的工作會中斷，排隊中的工作下次啟動後繼續。\n\n"
               "確定要結束 Video Console？")
    return ctypes.windll.user32.MessageBoxW(None, message, APP_NAME, 0x34) == 6  # MB_YESNO | MB_ICONWARNING, IDYES


def serve(paths: Paths, port: int | None, no_window: bool) -> int:
    from vc.app import App

    app = App(paths.data)
    bound = 0
    for candidate in (port,) if port is not None else PORTS:
        try:
            bound = app.start(candidate)
            break
        except OSError as exc:
            log.info("Port %s unavailable: %s", candidate, exc)
    if not bound:
        app.stop()
        return fail("本機沒有可用的連接埠，Video Console 無法啟動。", no_window)
    url = f"http://127.0.0.1:{bound}/"
    info: dict[str, Any] = {"port": bound, "pid": os.getpid(), "instance": app.instance, "url": url,
                            "started": app.started}
    paths.pid_file.write_text(json.dumps(info), encoding="utf-8")
    app.tray = Tray(APP_NAME, ICON, on_open=lambda: open_window(url, paths), on_quit=app.stop,
                    can_quit=lambda: confirm_quit(app))
    try:
        if not no_window:
            app.tray.start()
            open_window(url, paths)
        app.wait()
    except KeyboardInterrupt:
        app.stop()
    finally:
        app.tray.stop()
        paths.pid_file.unlink(missing_ok=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="main.py", description=f"{APP_NAME} {VERSION}")
    parser.add_argument("--no-window", action="store_true", help="只啟動伺服器，不開視窗")
    parser.add_argument("--stop", action="store_true", help="結束正在執行的 Video Console")
    parser.add_argument("--port", type=int, help=f"指定連接埠（預設從 {DEFAULT_PORT} 開始找可用的）")
    parser.add_argument("--data", help="資料資料夾（預設為程式旁的 data）")
    parser.add_argument("--verbose", action="store_true", help="記錄檔寫入除錯細節")
    args = parser.parse_args(argv)
    paths = Paths(args.data)
    try:
        paths.ensure()
    except OSError as exc:  # e.g. the folder was put under Program Files, which needs administrator rights
        return fail(f"無法建立資料資料夾 {paths.data}：{exc}\n請把 Video Console 資料夾移到可以寫入的位置"
                    "（例如「文件」或「桌面」）再執行。", args.no_window or args.stop)
    if args.stop:
        return stop_running(paths)
    setup_logging(paths.logs, args.verbose)
    claimed = claim_instance(paths, args.no_window)
    if isinstance(claimed, int):
        return claimed
    try:
        return serve(paths, args.port, args.no_window)
    except Exception as exc:
        log.exception("Video Console stopped with an error")
        return fail(f"Video Console 發生錯誤而停止：{exc}\n詳細內容在 {paths.logs / 'app.log'}", args.no_window)
    finally:
        claimed.close()


if __name__ == "__main__":
    sys.exit(main())
