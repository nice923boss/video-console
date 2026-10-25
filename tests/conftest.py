"""Shared fixtures: the app package on sys.path, temp stores, and a local mock HTTP server."""
from __future__ import annotations

import json
import sys
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from vc.events import EventBus  # noqa: E402
from vc.secrets import SecretStore  # noqa: E402
from vc.settings import SettingsStore  # noqa: E402


@dataclass(frozen=True)
class Call:
    method: str
    path: str
    headers: dict[str, str]
    body: bytes

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8"))


Route = Callable[[Call], "tuple[int, Any]"]


class MockServer:
    """Records every request and answers from `routes[(method, path)]` (404 otherwise). An answer is
    (status, payload) or (status, payload, headers); the headers override the default ones."""

    def __init__(self) -> None:
        self.calls: list[Call] = []
        self.routes: dict[tuple[str, str], Route] = {}
        self.got_call = threading.Event()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.url = f"http://127.0.0.1:{self.httpd.server_port}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def route(self, method: str, path: str, answer: Route | tuple[int, Any]) -> None:
        self.routes[(method, path)] = answer if callable(answer) else (lambda _call, a=answer: a)

    def wait_for_calls(self, count: int, timeout: float = 5.0) -> list[Call]:
        deadline = threading.Event()
        timer = threading.Timer(timeout, deadline.set)
        timer.start()
        try:
            while len(self.calls) < count and not deadline.is_set():
                self.got_call.wait(0.05)
                self.got_call.clear()
        finally:
            timer.cancel()
        return list(self.calls)

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        server = self

        class Handler(BaseHTTPRequestHandler):
            def _handle(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                call = Call(self.command, self.path, {k.lower(): v for k, v in self.headers.items()},
                            self.rfile.read(length) if length else b"")
                # Recorded before answering, so a caller that got its response always finds its call here.
                server.calls.append(call)
                server.got_call.set()
                route = server.routes.get((self.command, self.path))
                status, payload, *rest = route(call) if route else (404, {"error": "not found"})
                data = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode()
                headers = {"Content-Type": "application/json; charset=utf-8", "Content-Length": str(len(data)),
                           **(rest[0] if rest else {})}
                self.send_response(status)
                for name, value in headers.items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = _handle

            def log_message(self, *args: Any) -> None:
                pass

        return Handler


@pytest.fixture()
def mock_http() -> MockServer:
    server = MockServer()
    yield server
    server.close()


@pytest.fixture()
def bus() -> EventBus:
    return EventBus()


@pytest.fixture()
def settings(tmp_path: Path, bus: EventBus) -> SettingsStore:
    return SettingsStore(tmp_path / "settings.json", bus)


@pytest.fixture()
def secrets(tmp_path: Path) -> SecretStore:
    return SecretStore(tmp_path / "secrets.json")


@pytest.fixture()
def events(bus: EventBus) -> Callable[[str], list[Any]]:
    """events(type) returns the data of every event of that type published since the fixture was created."""
    sub = bus.subscribe()
    seen: list[dict] = []

    def read(type_: str) -> list[Any]:
        while not sub.queue.empty():
            seen.append(json.loads(sub.queue.get_nowait()))
        return [e["data"] for e in seen if e["type"] == type_]

    return read
