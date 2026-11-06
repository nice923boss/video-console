"""Re-render a p5 animation segment to transparent WebM with the text edits from p5src.py, and swap it in.

The page runs in headless Edge (or Chrome) driven over the DevTools protocol; only the standard library is
used, so the APP needs no Playwright. A small server on 127.0.0.1 serves the page's folder, handing out the
edited copy of the script that holds the segment; the user's files are never written. The page contract is the
p5-comfyui-animation skill's: ?id=<segment>&t=0, window.READY (a promise), window.renderAt(t) returning a PNG
data URL with alpha. Frames go to ffmpeg as PNG and come out VP9 with alpha, like the skill's render.py.
"""
from __future__ import annotations

import base64
import functools
import http.server
import json
import logging
import os
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, unquote

from . import p5src
from .jobs import JobControl
from .media import NO_WINDOW, Media
from .store import ProjectStore
from .takes import IMPORT_DIR, relink_ops, stage_file
from .util import AppError, new_id

log = logging.getLogger("vc.p5render")

KIND = "p5render"
LANE = "local:ffmpeg"
READY_TIMEOUT = 90.0
FRAME_TIMEOUT = 60.0
MAX_SECONDS = 600.0
NET_HINT = "p5 頁面從網路載入 p5.js（cdn.jsdelivr.net），請確認這台電腦可以上網"
ENCODE = ["-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p", "-b:v", "0", "-crf", "30", "-row-mt", "1",
          "-deadline", "good", "-cpu-used", "4", "-auto-alt-ref", "0"]
BROWSERS = (
    ("ProgramFiles(x86)", r"Microsoft\Edge\Application\msedge.exe"),
    ("ProgramFiles", r"Microsoft\Edge\Application\msedge.exe"),
    ("LOCALAPPDATA", r"Microsoft\Edge\Application\msedge.exe"),
    ("ProgramFiles", r"Google\Chrome\Application\chrome.exe"),
    ("ProgramFiles(x86)", r"Google\Chrome\Application\chrome.exe"),
    ("LOCALAPPDATA", r"Google\Chrome\Application\chrome.exe"),
)


def find_browser() -> str | None:
    for env, rel in BROWSERS:
        base = os.environ.get(env)
        if base and (Path(base) / rel).is_file():
            return str(Path(base) / rel)
    for name in ("msedge", "chrome", "chromium", "google-chrome"):
        found = shutil.which(name)
        if found:
            return found
    return None


# ---------------------------------------------------------------- DevTools protocol over a websocket
class Cdp:
    """One page target. Page exceptions and console errors are kept in .errors for the failure message."""

    def __init__(self, url: str, timeout: float) -> None:
        if not url.startswith("ws://"):
            raise AppError("瀏覽器除錯連線網址不正確", 500)
        hostport, path = url[5:].split("/", 1)
        host, port = hostport.rsplit(":", 1)
        self.sock = socket.create_connection((host, int(port)), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((f"GET /{path} HTTP/1.1\r\nHost: {hostport}\r\nUpgrade: websocket\r\n"
                           f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise AppError("瀏覽器除錯連線中斷", 500)
            buf += chunk
        head, self._rest = buf.split(b"\r\n\r\n", 1)
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            raise AppError("瀏覽器拒絕除錯連線", 500)
        self._next = 0
        self.errors: list[str] = []

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass

    def _read(self, n: int) -> bytes:
        out = bytearray(self._rest[:n])
        self._rest = self._rest[n:]
        while len(out) < n:
            chunk = self.sock.recv(max(65536, n - len(out)))
            if not chunk:
                raise AppError("瀏覽器除錯連線中斷（瀏覽器可能已關閉）", 500)
            out += chunk
        if len(out) > n:
            self._rest = bytes(out[n:]) + self._rest
            del out[n:]
        return bytes(out)

    def _send(self, text: str) -> None:
        data = text.encode()
        n = len(data)
        if n < 126:
            head = bytes([0x81, 0x80 | n])
        elif n < 65536:
            head = bytes([0x81, 0x80 | 126]) + struct.pack(">H", n)
        else:
            head = bytes([0x81, 0x80 | 127]) + struct.pack(">Q", n)
        mask = os.urandom(4)
        self.sock.sendall(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def _recv(self) -> str:
        parts = []
        while True:
            b0, b1 = self._read(2)
            op, n = b0 & 0x0F, b1 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read(8))[0]
            payload = self._read(n)
            if op == 8:
                raise AppError("瀏覽器關閉了除錯連線", 500)
            if op in (9, 10):
                continue
            parts.append(payload)
            if b0 & 0x80:
                return b"".join(parts).decode("utf-8", "replace")

    def _note(self, msg: dict) -> None:
        method, params = msg.get("method"), msg.get("params") or {}
        if method == "Runtime.exceptionThrown":
            d = params.get("exceptionDetails") or {}
            self.errors.append(str((d.get("exception") or {}).get("description") or d.get("text") or "頁面錯誤"))
        elif method == "Runtime.consoleAPICalled" and params.get("type") == "error":
            self.errors.append(" ".join(str(a.get("value", a.get("description", ""))) for a in params.get("args", [])))
        elif method == "Log.entryAdded" and (params.get("entry") or {}).get("level") == "error":
            e = params["entry"]
            self.errors.append(f"{e.get('text', '')} {e.get('url', '')}".strip())

    def call(self, method: str, timeout: float | None = None, **params: Any) -> dict:
        self._next += 1
        mid = self._next
        self.sock.settimeout(timeout)
        self._send(json.dumps({"id": mid, "method": method, "params": params}))
        try:
            while True:
                msg = json.loads(self._recv())
                if msg.get("id") == mid:
                    if "error" in msg:
                        raise AppError(f"瀏覽器指令失敗：{msg['error'].get('message', msg['error'])}", 500)
                    return msg.get("result") or {}
                self._note(msg)
        except socket.timeout:
            raise TimeoutError(method) from None

    def eval(self, expr: str, *, wait: bool = False, timeout: float = FRAME_TIMEOUT) -> Any:
        r = self.call("Runtime.evaluate", timeout=timeout, expression=expr, returnByValue=True, awaitPromise=wait)
        if "exceptionDetails" in r:
            d = r["exceptionDetails"]
            raise AppError(f"p5 頁面執行錯誤：{(d.get('exception') or {}).get('description') or d.get('text')}", 500)
        return (r.get("result") or {}).get("value")


# ---------------------------------------------------------------- page server
def serve(root: Path, subs: dict[str, str]) -> http.server.ThreadingHTTPServer:
    """Serve root on 127.0.0.1; the files in subs (relative posix path -> text) are served from memory."""
    bodies = {rel: text.encode("utf-8") for rel, text in subs.items()}

    class Handler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:
            pass

        def end_headers(self) -> None:
            self.send_header("Cache-Control", "no-store")
            super().end_headers()

        def do_GET(self) -> None:  # noqa: N802 (http.server API)
            rel = unquote(self.path.split("?", 1)[0].split("#", 1)[0]).lstrip("/")
            body = bodies.get(rel)
            if body is None:
                super().do_GET()
                return
            kind = "text/html" if rel.lower().endswith(p5src.PAGE_SUFFIXES) else "text/javascript"
            self.send_response(200)
            self.send_header("Content-Type", f"{kind}; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Handler, directory=str(root)))
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True, name="p5-serve").start()
    return srv


def _close_browser(port: int, path: str) -> None:
    """Ask the browser to quit over its own DevTools target. Edge 157's msedge.exe hands the browser to another
    process and exits at once, so killing the process we started does not reach the browser."""
    try:
        cdp = Cdp(f"ws://127.0.0.1:{port}{path}", 5)
    except (AppError, OSError) as e:
        log.warning("could not reach the browser on port %s to close it: %s", port, e)
        return
    try:
        cdp.call("Browser.close", timeout=5)
    except (AppError, TimeoutError, OSError):
        pass  # the browser may drop the connection before it answers
    finally:
        cdp.close()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:  # its profile files stay locked until it is gone
        try:
            socket.create_connection(("127.0.0.1", port), timeout=1).close()
        except OSError:
            return
        time.sleep(0.1)
    log.warning("browser on port %s did not quit", port)


def _kill(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    if sys.platform == "win32":  # the browser's helper processes too
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, creationflags=NO_WINDOW)
    proc.kill()
    try:
        proc.wait(10)
    except subprocess.TimeoutExpired:
        log.warning("browser %s did not exit", proc.pid)


def _failure(prefix: str, errors: list[str]) -> AppError:
    seen = list(dict.fromkeys(e for e in errors if e))[:3]
    detail = "；".join(e[:300] for e in seen)
    net = any(k in detail.lower() for k in ("p5 is not defined", "net::", "failed to load", "jsdelivr"))
    msg = f"{prefix}：{detail}" if detail else prefix
    return AppError(f"{msg}。{NET_HINT}" if net or not seen else msg, 500)


def render(page: Path, segment: str, subs: dict[str, str], out: Path, work: Path, *, ffmpeg: str,
           fps: float, fallback_seconds: float, progress: Callable[[float, str], None],
           cancelled: Callable[[], bool]) -> dict:
    """Draw every frame of the segment and encode them to out (VP9 WebM with alpha)."""
    browser = find_browser()
    if not browser:
        raise AppError("找不到 Microsoft Edge 或 Google Chrome：重算 p5 動畫要用瀏覽器畫圖", 503)
    srv = serve(page.parent, subs)
    profile = work / "browser"
    profile.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen([browser, "--headless=new", "--remote-debugging-port=0", f"--user-data-dir={profile}",
                             "--enable-unsafe-swiftshader", "--no-first-run", "--no-default-browser-check",
                             "--disable-extensions", "--mute-audio", "about:blank"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)
    cdp: Cdp | None = None
    enc: subprocess.Popen | None = None
    endpoint: tuple[int, str] | None = None  # the browser's own DevTools target, to close it with
    try:
        port_file = profile / "DevToolsActivePort"
        deadline = time.monotonic() + 30
        while not (port_file.is_file() and port_file.read_text(encoding="ascii", errors="replace").strip()):
            code = proc.poll()
            if code not in (None, 0):  # 0: msedge.exe handed the browser to another process (see _close_browser)
                raise AppError(f"瀏覽器啟動失敗（結束代碼 {code}）", 500)
            if time.monotonic() > deadline:
                raise AppError("等待瀏覽器啟動逾時", 500)
            time.sleep(0.05)
        lines = port_file.read_text(encoding="ascii").split()
        port = int(lines[0])
        if len(lines) > 1:
            endpoint = (port, lines[1])
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=10) as resp:
            targets = json.load(resp)
        target = next((t for t in targets if t.get("type") == "page" and t.get("webSocketDebuggerUrl")), None)
        if target is None:
            raise AppError("瀏覽器沒有可用的分頁", 500)
        cdp = Cdp(target["webSocketDebuggerUrl"], FRAME_TIMEOUT)
        for method in ("Runtime.enable", "Page.enable", "Log.enable"):
            cdp.call(method, timeout=FRAME_TIMEOUT)
        progress(0.03, "載入 p5 頁面")
        url = f"http://127.0.0.1:{srv.server_address[1]}/{quote(page.name)}?id={quote(segment)}&t=0"
        cdp.call("Page.navigate", timeout=FRAME_TIMEOUT, url=url)
        deadline = time.monotonic() + READY_TIMEOUT
        while not cdp.eval("window.READY !== undefined && typeof window.renderAt === 'function'"):
            if cancelled():
                raise AppError("已取消", 499)
            if time.monotonic() > deadline:
                raise _failure("p5 頁面一直沒有準備好（沒有 window.READY 或 window.renderAt）", cdp.errors)
            time.sleep(0.1)
        try:
            cdp.eval("Promise.resolve(window.READY).then(() => true)", wait=True, timeout=READY_TIMEOUT)
        except TimeoutError:
            raise _failure("等待 p5 頁面載入字型與素材逾時", cdp.errors) from None
        seconds = float(cdp.eval("(typeof SEG !== 'undefined' && SEG && Number(SEG.dur)) || 0") or 0)
        seconds = seconds if 0 < seconds <= MAX_SECONDS else fallback_seconds
        if not 0 < seconds <= MAX_SECONDS:
            raise AppError("讀不到這個片段的長度（SEG.dur）", 500)
        frames = max(1, round(seconds * fps))
        log_file = work / "ffmpeg.log"
        with log_file.open("wb") as err:
            enc = subprocess.Popen([ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-y", "-f", "image2pipe",
                                    "-framerate", f"{fps:g}", "-c:v", "png", "-i", "-", *ENCODE, str(out)],
                                   stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=err, creationflags=NO_WINDOW)
        assert enc.stdin is not None
        for i in range(frames):
            if cancelled():
                raise AppError("已取消", 499)
            try:
                data = cdp.eval(f"window.renderAt({i / fps!r})")
            except TimeoutError:
                raise _failure(f"第 {i + 1} 格畫圖逾時", cdp.errors) from None
            if not isinstance(data, str) or not data.startswith("data:image/png"):
                raise _failure(f"第 {i + 1} 格沒有畫出 PNG（window.renderAt 要回傳 PNG data URL）", cdp.errors)
            try:
                enc.stdin.write(base64.b64decode(data.split(",", 1)[1]))
            except OSError:
                break  # ffmpeg stopped; its log says why
            if i % 10 == 0:
                progress(0.05 + 0.85 * (i + 1) / frames, f"畫第 {i + 1} / {frames} 格")
        try:
            enc.stdin.close()
        except OSError:
            pass
        code = enc.wait(600)
        if code != 0 or not out.is_file():
            tail = log_file.read_text(encoding="utf-8", errors="replace").strip().splitlines()[-3:]
            raise AppError(f"ffmpeg 編碼透明 WebM 失敗：{' '.join(tail) or f'結束代碼 {code}'}", 500)
        return {"frames": frames, "seconds": seconds}
    finally:
        if enc is not None and enc.poll() is None:
            enc.kill()
        if cdp is not None:
            cdp.close()
        if endpoint is not None:
            _close_browser(*endpoint)
        _kill(proc)
        srv.shutdown()
        srv.server_close()


# ---------------------------------------------------------------- the job
def _link(item: dict) -> dict:
    link = item.get("p5")
    if not isinstance(link, dict) or not link.get("page"):
        raise AppError(f"「{item.get('name') or '素材'}」還沒有連結 p5 原始碼", 409)
    return link


class P5Jobs:
    """kind "p5render": payload {media, texts: [{at, text}]} (positions from p5src.read_texts)."""

    def __init__(self, store: ProjectStore, media: Callable[[], Media]) -> None:
        self._store = store
        self._media = media

    def prepare(self, project: dict, payload: dict) -> dict:
        item = project["media"].get(str(payload.get("media") or ""))
        if item is None:
            raise AppError("找不到要重算的素材", 404)
        if item.get("kind") != "video":
            raise AppError("只有影片素材能重算 p5 動畫")
        link = _link(item)
        edits = p5src.plan_edits(link["page"], link["segment"], payload.get("texts") or [])
        if not find_browser():
            raise AppError("找不到 Microsoft Edge 或 Google Chrome：重算 p5 動畫要用瀏覽器畫圖", 503)
        return {"title": f"重算 p5：{item.get('name') or link['segment']}", "lane": LANE, "shotId": None,
                "payload": {"media": item["id"], "page": link["page"], "segment": link["segment"], "edits": edits}}

    def execute(self, ctl: JobControl) -> dict:
        work = self._store.project_dir(ctl.pid) / "media" / "cache" / f"job-{ctl.id}"
        work.mkdir(parents=True, exist_ok=True)
        try:
            return self._run(ctl, work)
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def _run(self, ctl: JobControl, work: Path) -> dict:
        p = ctl.payload
        project = self._store.get(ctl.pid)
        item = project["media"].get(p["media"])
        if item is None:
            raise AppError("素材已被刪除，沒有重算", 410)
        page = p5src.check_page(p["page"])
        subs = p5src.substitutions(page, p["segment"], p["edits"])
        media = self._media()
        exe = media.require()
        if not media.has_encoder("libvpx-vp9"):
            raise AppError("這個 ffmpeg 沒有 libvpx-vp9 編碼器，做不出透明 WebM：請到「設定 › 依賴元件」安裝完整版 ffmpeg", 503)
        fps = float(item.get("fps") or 0) or 30.0
        out = work / "p5.webm"
        info = render(page, p["segment"], subs, out, work, ffmpeg=exe, fps=fps,
                      fallback_seconds=float(item.get("duration") or 0), progress=ctl.progress,
                      cancelled=ctl.cancelled)
        ctl.progress(0.92, "放進素材庫")
        pdir = self._store.project_dir(ctl.pid)
        staged = stage_file(media, pdir, out, IMPORT_DIR, new_id("m"), cancelled=ctl.cancelled)
        if not staged.get("alphaCodec"):
            (pdir / staged["file"]).unlink(missing_ok=True)
            raise AppError("算出來的影片沒有透明通道，沒有換上", 500)
        with self._store.locked(ctl.pid):
            project = self._store.get(ctl.pid)
            item = project["media"].get(p["media"])
            if item is None:
                raise AppError("素材已被刪除，沒有換上重算的影片", 410)
            link = _link(item)
            if (link.get("page"), link.get("segment")) != (p["page"], p["segment"]):
                raise AppError("算圖期間 p5 原始碼連結改了，沒有換上，請重新重算", 409)
            changed = link.get("edits") != p["edits"]
            ops = [*relink_ops(project, item, staged),
                   {"op": "media.update", "id": item["id"], "patch": {"p5": {**link, "edits": p["edits"]}}}]
            # One undo step: the old file and the old texts come back together.
            self._store.apply(ctl.pid, ops, origin="job", label="修改 p5 文字" if changed else "重算 p5 動畫")
        return {"mediaIds": [item["id"]], "message": f"重算完成（{info['frames']} 格）"}
