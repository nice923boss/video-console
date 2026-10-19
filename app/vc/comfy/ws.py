"""Minimal RFC 6455 WebSocket client (client side only), enough for ComfyUI's /ws feed."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import ssl
import struct
import urllib.parse
from typing import Any

_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
OP_CONT, OP_TEXT, OP_BINARY, OP_CLOSE, OP_PING, OP_PONG = 0, 1, 2, 8, 9, 10
MAX_MESSAGE = 64 * 1024 * 1024


class WSClosed(Exception):
    pass


class WebSocket:
    def __init__(self, url: str, headers: dict[str, str] | None = None, timeout: float = 10):
        parts = urllib.parse.urlsplit(url)
        if parts.scheme not in ("ws", "wss"):
            raise ValueError("WebSocket URL must start with ws:// or wss://")
        secure = parts.scheme == "wss"
        host = parts.hostname or "127.0.0.1"
        port = parts.port or (443 if secure else 80)
        path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        raw = socket.create_connection((host, port), timeout=timeout)
        if secure:
            raw = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
        self.sock = raw
        self._buf = bytearray()
        self._frags: list[bytes] = []
        self._frag_op = 0
        self.closed = False
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        host_header = parts.netloc.split("@")[-1]
        lines = [
            f"GET {path} HTTP/1.1",
            f"Host: {host_header}",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Key: {key}",
            "Sec-WebSocket-Version: 13",
            "User-Agent: VideoConsole/1.0",
        ]
        lines += [f"{k}: {v}" for k, v in (headers or {}).items()]
        self.sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))
        head = self._read_http_head()
        status_line, _, rest = head.partition("\r\n")
        if " 101 " not in status_line + " ":
            self.close()
            raise ConnectionError(f"WebSocket handshake refused: {status_line.strip()}")
        accept = ""
        for line in rest.split("\r\n"):
            name, _, value = line.partition(":")
            if name.strip().lower() == "sec-websocket-accept":
                accept = value.strip()
        expected = base64.b64encode(hashlib.sha1((key + _GUID).encode("ascii")).digest()).decode("ascii")
        if accept != expected:
            self.close()
            raise ConnectionError("WebSocket handshake failed: bad Sec-WebSocket-Accept")

    def _read_http_head(self) -> str:
        while b"\r\n\r\n" not in self._buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("WebSocket handshake: connection closed")
            self._buf += chunk
            if len(self._buf) > 65536:
                raise ConnectionError("WebSocket handshake: header too large")
        idx = self._buf.index(b"\r\n\r\n")
        head = bytes(self._buf[:idx]).decode("latin-1")
        del self._buf[: idx + 4]
        return head

    # ---------------------------------------------------------------- sending
    def _send_frame(self, opcode: int, payload: bytes) -> None:
        header = bytearray([0x80 | opcode])
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack("!H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack("!Q", n)
        mask = os.urandom(4)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    def send_text(self, text: str) -> None:
        self._send_frame(OP_TEXT, text.encode("utf-8"))

    def ping(self) -> None:
        self._send_frame(OP_PING, b"vc")

    # ---------------------------------------------------------------- receiving
    def _parse_frame(self) -> tuple[bool, int, bytes] | None:
        buf = self._buf
        if len(buf) < 2:
            return None
        fin = bool(buf[0] & 0x80)
        opcode = buf[0] & 0x0F
        masked = bool(buf[1] & 0x80)
        n = buf[1] & 0x7F
        pos = 2
        if n == 126:
            if len(buf) < 4:
                return None
            n = struct.unpack("!H", buf[2:4])[0]
            pos = 4
        elif n == 127:
            if len(buf) < 10:
                return None
            n = struct.unpack("!Q", buf[2:10])[0]
            pos = 10
        if n > MAX_MESSAGE:
            raise WSClosed("frame too large")
        mask = b""
        if masked:
            if len(buf) < pos + 4:
                return None
            mask = bytes(buf[pos:pos + 4])
            pos += 4
        if len(buf) < pos + n:
            return None
        payload = bytes(buf[pos:pos + n])
        del buf[: pos + n]
        if masked:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        return fin, opcode, payload

    def recv(self, timeout: float = 1.0) -> tuple[int, bytes] | None:
        """Return (opcode, payload) for the next text/binary message, or None on timeout."""
        if self.closed:
            raise WSClosed("closed")
        self.sock.settimeout(timeout)
        while True:
            frame = self._parse_frame()
            if frame is None:
                try:
                    chunk = self.sock.recv(65536)
                except (socket.timeout, TimeoutError):
                    return None
                except OSError as exc:
                    self.closed = True
                    raise WSClosed(str(exc)) from None
                if not chunk:
                    self.closed = True
                    raise WSClosed("connection closed by server")
                self._buf += chunk
                continue
            fin, opcode, payload = frame
            if opcode == OP_PING:
                self._send_frame(OP_PONG, payload)
                continue
            if opcode == OP_PONG:
                continue
            if opcode == OP_CLOSE:
                self.close()
                raise WSClosed("closed by server")
            if opcode in (OP_TEXT, OP_BINARY):
                if fin:
                    return opcode, payload
                self._frag_op, self._frags = opcode, [payload]
                continue
            if opcode == OP_CONT:
                self._frags.append(payload)
                if sum(len(f) for f in self._frags) > MAX_MESSAGE:
                    raise WSClosed("message too large")
                if fin:
                    data, self._frags = b"".join(self._frags), []
                    return self._frag_op, data

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            self._send_frame(OP_CLOSE, struct.pack("!H", 1000))
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


def parse_message(opcode: int, payload: bytes) -> dict[str, Any] | None:
    """Decode a ComfyUI feed message into {"type", "data"} or {"type": "preview", "image", "mime"}."""
    if opcode == OP_TEXT:
        try:
            msg = json.loads(payload.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None
        return msg if isinstance(msg, dict) and "type" in msg else None
    if len(payload) < 8:
        return None
    event = struct.unpack(">I", payload[:4])[0]
    if event == 1:  # PREVIEW_IMAGE: [type][format][image]
        fmt = struct.unpack(">I", payload[4:8])[0]
        return {"type": "preview", "mime": "image/png" if fmt == 2 else "image/jpeg", "image": payload[8:],
                "data": {}}
    if event == 4:  # PREVIEW_IMAGE_WITH_METADATA: [type][meta len][meta json][image]
        meta_len = struct.unpack(">I", payload[4:8])[0]
        try:
            meta = json.loads(payload[8:8 + meta_len].decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            meta = {}
        return {"type": "preview", "mime": meta.get("image_type", "image/jpeg"), "image": payload[8 + meta_len:],
                "data": meta}
    return None
