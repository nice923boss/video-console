"""Local HTTP server: routing, request checks, static files, media streaming with Range, and Server-Sent Events.

The server listens on 127.0.0.1 only. On top of that:
* the Host header must name this server, so pages on other sites cannot reach it through DNS rebinding;
* state-changing requests must carry `X-VC: 1`. Browsers send custom headers cross-origin only after a CORS
  preflight, and this server never approves one;
* `Sec-Fetch-Site`, when the browser sends it, must be same-origin or none (a typed address or the app window).
"""
from __future__ import annotations

import json
import logging
import mimetypes
import queue
import re
import socket
import socketserver
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Iterable

from .events import EventBus
from .paths import WEB_DIR
from .util import AppError, is_within

log = logging.getLogger("vc.server")

CHUNK = 256 * 1024
MAX_JSON = 32 * 1024 * 1024
DRAIN_LIMIT = 8 * 1024 * 1024  # unread request bodies up to this size are read before closing (avoids a TCP reset)
SSE_PING_SEC = 15.0
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
CLIENT_GONE = (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, socket.timeout)

CSP = ("default-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; style-src 'self' 'unsafe-inline'; "
       "script-src 'self'; connect-src 'self'; font-src 'self' data:; object-src 'none'; base-uri 'none'; "
       "frame-ancestors 'none'; form-action 'self'")
COMMON_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Referrer-Policy": "no-referrer",
}
HTML_HEADERS = {"Content-Security-Policy": CSP, "X-Frame-Options": "DENY"}

# The Windows registry often maps .js to text/plain, which browsers refuse for module scripts.
MIME = {
    ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png",
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp",
    ".ico": "image/x-icon", ".mp4": "video/mp4", ".m4v": "video/mp4", ".webm": "video/webm",
    ".mov": "video/quicktime", ".mkv": "video/x-matroska", ".avi": "video/x-msvideo", ".mp3": "audio/mpeg",
    ".wav": "audio/wav", ".m4a": "audio/mp4", ".aac": "audio/aac", ".flac": "audio/flac", ".ogg": "audio/ogg",
    ".opus": "audio/ogg", ".srt": "application/x-subrip; charset=utf-8", ".ass": "text/plain; charset=utf-8",
    ".txt": "text/plain; charset=utf-8", ".csv": "text/csv; charset=utf-8", ".zip": "application/zip",
    ".woff2": "font/woff2",
}


def content_type(path: Path) -> str:
    return MIME.get(path.suffix.lower()) or mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def content_disposition(name: str) -> str:
    """Attachment header with an ASCII fallback and the real (often Chinese) name in RFC 5987 form."""
    stem, dot, ext = name.rpartition(".")
    fallback = re.sub(r"[^A-Za-z0-9._-]+", "_", stem if dot else name).strip("_") or "download"
    if dot:
        fallback += "." + re.sub(r"[^A-Za-z0-9]+", "", ext)[:8]
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{urllib.parse.quote(name, safe='')}"


def parse_range(header: str, size: int) -> tuple[int, int] | None:
    """(start, end) of a single byte range; None to send the whole file; AppError 416 when unsatisfiable."""
    m = re.fullmatch(r"\s*bytes\s*=\s*(\d*)\s*-\s*(\d*)\s*", header or "")
    if not m or (not m.group(1) and not m.group(2)):
        return None  # multiple ranges or junk: ignoring Range is allowed
    if not m.group(1):
        suffix = int(m.group(2))
        if suffix == 0 or size == 0:
            raise AppError("要求的範圍不存在", 416)
        return max(0, size - suffix), size - 1
    start = int(m.group(1))
    end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
    if start >= size or start > end:
        raise AppError("要求的範圍不存在", 416)
    return start, end


# ------------------------------------------------------------------ handler results
@dataclass
class Response:
    body: bytes | str = b""
    content_type: str = "application/json; charset=utf-8"
    status: int = 200
    headers: dict[str, str] = field(default_factory=dict)
    download_name: str | None = None


@dataclass
class FileResponse:
    path: Path
    download_name: str | None = None
    cache: str = "private, max-age=3600"
    content_type: str | None = None


class Request:
    def __init__(self, handler: Handler, method: str, path: str, query: dict[str, str], params: dict[str, str]):
        self._handler = handler
        self.method = method
        self.path = path
        self.query = query
        self.params = params
        self.headers = handler.headers
        self._body: bytes | None = None

    def content_length(self) -> int | None:
        raw = self.headers.get("Content-Length")
        if raw is None:
            return None
        try:
            length = int(raw)
        except ValueError:
            raise AppError("Content-Length 格式不正確") from None
        if length < 0:
            raise AppError("Content-Length 格式不正確")
        return length

    def body(self, limit: int = MAX_JSON) -> bytes:
        if self._body is None:
            length = self.content_length() or 0
            if length > limit:
                raise AppError("送出的資料太大", 413)
            self._body = self._handler.read_body(length)
        return self._body

    def json(self) -> Any:
        raw = self.body()
        if not raw.strip():
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise AppError("送出的資料不是有效的 JSON") from None

    def json_object(self) -> dict:
        data = self.json()
        if not isinstance(data, dict):
            raise AppError("送出的資料格式不正確（需要 JSON 物件）")
        return data

    def save_body(self, dst: Path, limit: int) -> int:
        """Stream the raw request body into dst (through dst.part); returns the size."""
        length = self.content_length()
        if length is None:
            raise AppError("上傳需要提供檔案大小（Content-Length）", 411)
        if length > limit:
            raise AppError(f"檔案太大，單檔上限 {limit // 1024 ** 3} GB", 413)
        part = dst.with_name(dst.name + ".part")
        try:
            with open(part, "wb") as f:
                left = length
                while left:
                    chunk = self._handler.read_body(min(CHUNK, left))
                    if not chunk:
                        raise AppError("上傳中斷，請重新上傳")
                    f.write(chunk)
                    left -= len(chunk)
            part.replace(dst)
        except BaseException:
            part.unlink(missing_ok=True)
            raise
        return length


Route = Callable[[Request], Any]


class Router:
    def __init__(self) -> None:
        self._routes: list[tuple[str, re.Pattern[str], Route]] = []

    def add(self, method: str, pattern: str, fn: Route) -> None:
        regex = "^" + re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", pattern) + "$"
        self._routes.append((method.upper(), re.compile(regex), fn))

    def route(self, method: str, pattern: str) -> Callable[[Route], Route]:
        def register(fn: Route) -> Route:
            self.add(method, pattern, fn)
            return fn
        return register

    def match(self, method: str, path: str) -> tuple[Route | None, dict[str, str], bool]:
        """(handler, path params, path exists for another method)."""
        other = False
        for m, regex, fn in self._routes:
            found = regex.match(path)
            if not found:
                continue
            if m == method:
                return fn, {k: urllib.parse.unquote(v) for k, v in found.groupdict().items()}, True
            other = True
        return None, {}, other


# ------------------------------------------------------------------ HTTP plumbing
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    server_version = "VideoConsole"
    sys_version = ""
    timeout = 30  # a stalled reader (e.g. a paused <video>) releases its connection after this
    server: HttpServer  # type: ignore[assignment]

    def setup(self) -> None:
        super().setup()
        self._sent = False
        self._left = 0

    def do_GET(self) -> None:  # noqa: N802 - http.server naming
        self._handle("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._handle("POST")

    def do_PUT(self) -> None:  # noqa: N802
        self._handle("PUT")

    def do_PATCH(self) -> None:  # noqa: N802
        self._handle("PATCH")

    def do_DELETE(self) -> None:  # noqa: N802
        self._handle("DELETE")

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - signature from the base class
        log.debug("%s %s", self.address_string(), format % args)

    def read_body(self, size: int) -> bytes:
        size = min(size, self._left)
        data = self.rfile.read(size) if size else b""
        self._left -= len(data)
        return data

    # ---------------------------------------------------------------- dispatch
    def _handle(self, method: str) -> None:
        split = urllib.parse.urlsplit(self.path)
        path = split.path
        try:
            self._left = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._left = 0
        try:
            self._guard(method)
            if method == "GET" and path == "/api/events":
                self.server.stream_events(self)
                return
            fn, params, other = self.server.router.match(method, path)
            if fn is None:
                if path.startswith("/api/"):
                    raise AppError("這個操作不支援此方法" if other else "找不到這個 API", 405 if other else 404)
                if method != "GET":
                    raise AppError("這個操作不支援此方法", 405)
                self._static(path)
                return
            query = dict(urllib.parse.parse_qsl(split.query, keep_blank_values=True))
            self._respond(fn(Request(self, method, path, query, params)))
        except AppError as exc:
            self._error(exc.status, exc.message, exc.detail)
        except CLIENT_GONE:
            pass
        except Exception:  # noqa: BLE001 - one broken request must not take the server down
            log.exception("Unhandled error on %s %s", method, path)
            self._error(500, "伺服器發生錯誤，詳細內容已寫入記錄檔")
        finally:
            if 0 < self._left <= DRAIN_LIMIT:
                try:
                    self.read_body(self._left)
                except OSError:
                    pass

    def _guard(self, method: str) -> None:
        if (self.headers.get("Host") or "").lower() not in self.server.allowed_hosts:
            raise AppError("不允許的主機名稱", 403)
        site = self.headers.get("Sec-Fetch-Site")
        if site and site not in ("same-origin", "none"):
            raise AppError("不允許跨站請求", 403)
        if method in UNSAFE_METHODS:
            if self.headers.get("X-VC") != "1":
                raise AppError("缺少必要的請求標頭", 403)
            origin = self.headers.get("Origin")
            if origin and origin.lower() not in self.server.allowed_origins:
                raise AppError("不允許跨站請求", 403)

    # ---------------------------------------------------------------- responses
    def _begin(self, status: int, ctype: str | None, length: int | None, headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        if ctype:
            self.send_header("Content-Type", ctype)
        if length is not None:
            self.send_header("Content-Length", str(length))
        for key, value in {**COMMON_HEADERS, **(headers or {})}.items():
            self.send_header(key, value)
        self.end_headers()
        self._sent = True

    def _send(self, status: int, body: bytes, ctype: str, headers: dict[str, str] | None = None) -> None:
        self._begin(status, ctype, len(body), headers)
        self.wfile.write(body)

    def _json(self, status: int, data: Any) -> None:
        body = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8", {"Cache-Control": "no-store"})

    def _error(self, status: int, message: str, detail: Any = None) -> None:
        if self._sent:
            return  # headers are out already; the client sees a cut-off response
        payload: dict[str, Any] = {"error": message, "status": status}
        if detail is not None:
            payload["detail"] = detail
        try:
            self._json(status, payload)
        except CLIENT_GONE:
            pass

    def _respond(self, result: Any) -> None:
        if isinstance(result, FileResponse):
            self._file(result.path, ctype=result.content_type, download=result.download_name, cache=result.cache)
        elif isinstance(result, Response):
            body = result.body.encode("utf-8") if isinstance(result.body, str) else result.body
            headers = {"Cache-Control": "no-store", **result.headers}
            if result.content_type.startswith("text/html"):
                headers.update(HTML_HEADERS)
            if result.download_name:
                headers["Content-Disposition"] = content_disposition(result.download_name)
            self._send(result.status, body, result.content_type, headers)
        else:
            self._json(200, {"ok": True} if result is None else result)

    def _static(self, path: str) -> None:
        root = self.server.web_dir
        rel = urllib.parse.unquote(path).lstrip("/") or "index.html"
        target = root / rel
        if not is_within(target, root) or not target.is_file():
            raise AppError("找不到頁面", 404)
        self._file(target, cache="no-cache")

    def _file(self, path: Path, *, ctype: str | None = None, download: str | None = None,
              cache: str = "private, max-age=3600") -> None:
        try:
            st = path.stat()
        except OSError:
            raise AppError("找不到檔案（可能已被刪除）", 404) from None
        if not path.is_file():
            raise AppError("找不到檔案（可能已被刪除）", 404)
        size = st.st_size
        etag = f'"{st.st_mtime_ns:x}-{size:x}"'
        ctype = ctype or content_type(path)
        headers = {"ETag": etag, "Cache-Control": cache, "Accept-Ranges": "bytes",
                   "Last-Modified": formatdate(st.st_mtime, usegmt=True)}
        if ctype.startswith("text/html"):
            headers.update(HTML_HEADERS)
        if download:
            headers["Content-Disposition"] = content_disposition(download)
        tags = [t.strip() for t in (self.headers.get("If-None-Match") or "").split(",")]
        if etag in tags or "*" in tags:
            self._begin(304, None, None, headers)
            return
        wanted = self.headers.get("Range")
        if wanted and (self.headers.get("If-Range") or etag).strip() != etag:
            wanted = None  # the file changed since the client cached its first part
        start, end, status = 0, size - 1, 200
        if wanted:
            try:
                picked = parse_range(wanted, size)
            except AppError:
                self._begin(416, None, 0, {"Content-Range": f"bytes */{size}"})
                return
            if picked:
                start, end = picked
                status = 206
                headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        length = max(0, end - start + 1)
        with open(path, "rb") as f:
            self._begin(status, ctype, length, headers)
            f.seek(start)
            left = length
            while left > 0:
                chunk = f.read(min(CHUNK, left))
                if not chunk:
                    break
                self.wfile.write(chunk)
                left -= len(chunk)


class HttpServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False  # on Windows SO_REUSEADDR would let a second server share the port
    request_queue_size = 128

    def __init__(self, host: str, port: int, router: Router, bus: EventBus, hello: Callable[[], dict],
                 web_dir: Path = WEB_DIR):
        super().__init__((host, port), Handler)
        self.router = router
        self.bus = bus
        self.hello = hello
        self.web_dir = web_dir
        self.port = int(self.server_address[1])
        hosts = {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}
        self.allowed_hosts = hosts
        self.allowed_origins = {f"http://{h}" for h in hosts}
        self.stopping = threading.Event()

    def server_bind(self) -> None:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        # HTTPServer.server_bind would also do a reverse DNS lookup, which can stall on some networks
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = str(self.server_address[0]), int(self.server_address[1])

    def shutdown(self) -> None:
        self.stopping.set()
        super().shutdown()

    def stream_events(self, handler: Handler) -> None:
        """Server-Sent Events: every bus message as `data: {"type", "data"}`; `resync` after an overflow."""
        sub = self.bus.subscribe()
        try:
            handler._begin(200, "text/event-stream; charset=utf-8", None, {"Cache-Control": "no-store"})
            write = handler.wfile.write
            write(b"retry: 2000\n\n")
            write(_sse({"type": "hello", "data": self.hello()}))
            last = time.monotonic()
            while not self.stopping.is_set():
                if sub.overflowed:
                    _drain(sub.queue)
                    sub.overflowed = False
                    write(_sse({"type": "resync", "data": None}))
                try:
                    msg = sub.queue.get(timeout=1.0)
                except queue.Empty:
                    if time.monotonic() - last >= SSE_PING_SEC:
                        write(b": ping\n\n")
                        last = time.monotonic()
                    continue
                batch = [msg, *_drain(sub.queue, 200)]
                write(b"".join(b"data: " + m.encode("utf-8") + b"\n\n" for m in batch))
                last = time.monotonic()
        except (OSError, ValueError):
            pass  # window closed or reloaded
        finally:
            self.bus.unsubscribe(sub)


def _sse(event: dict) -> bytes:
    return b"data: " + json.dumps(event, ensure_ascii=False).encode("utf-8") + b"\n\n"


def _drain(q: queue.Queue, limit: int | None = None) -> list[str]:
    out: list[str] = []
    while limit is None or len(out) < limit:
        try:
            out.append(q.get_nowait())
        except queue.Empty:
            break
    return out


def serve_in_thread(server: HttpServer) -> threading.Thread:
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.5}, name="http", daemon=True)
    thread.start()
    return thread


__all__: Iterable[str] = ("FileResponse", "HttpServer", "Request", "Response", "Router", "serve_in_thread")
