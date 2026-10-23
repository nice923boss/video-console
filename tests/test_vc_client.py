"""tools/vc_client.py against a stand-in server: a copy running code from before an update is replaced or reported."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from conftest import MockServer

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import vc_client  # noqa: E402
from vc import BUILD  # noqa: E402


def test_the_source_build_is_the_one_the_server_reports() -> None:
    assert vc_client._source_build(ROOT) == BUILD
    assert vc_client._source_build(ROOT / "tests") is None  # a packaged install has no source


@pytest.fixture()
def root(tmp_path: Path, mock_http: MockServer, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "main.py").write_text("", encoding="utf-8")
    (tmp_path / "data" / "run").mkdir(parents=True)
    info = {"port": mock_http.httpd.server_port}
    (tmp_path / "data" / "run" / "server.json").write_text(json.dumps(info), encoding="utf-8")
    monkeypatch.setattr(vc_client, "_source_build", lambda _root: "new")
    return tmp_path


def serve_build(mock_http: MockServer, build: str, running: int = 0, tray: bool = False) -> None:
    pong = {"ok": True, "version": "1.6.0", "build": build, "running": running, "tray": tray}
    mock_http.route("GET", "/api/ping", (200, pong))
    mock_http.route("POST", "/api/tray", (200, {"tray": True}))


def test_a_copy_running_the_same_build_is_used_as_is(root: Path, mock_http: MockServer,
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    serve_build(mock_http, "new")
    monkeypatch.setattr(vc_client.VC, "_start", lambda self, build=None: pytest.fail("must not relaunch"))
    assert vc_client.VC(root, keepalive=False).port == mock_http.httpd.server_port


def test_an_older_copy_is_replaced_and_keeps_its_tray_icon(root: Path, mock_http: MockServer,
                                                           monkeypatch: pytest.MonkeyPatch) -> None:
    serve_build(mock_http, "old", tray=True)
    launched = []

    def relaunch(self: vc_client.VC, build: str | None = None) -> int:
        launched.append(build)
        serve_build(mock_http, "new")  # the launcher stopped the old copy and serves the new code
        return mock_http.httpd.server_port

    monkeypatch.setattr(vc_client.VC, "_start", relaunch)
    vc_client.VC(root, keepalive=False)
    assert launched == ["new"]
    assert ("POST", "/api/tray") in [(c.method, c.path) for c in mock_http.calls]


def test_an_older_copy_running_jobs_is_kept_with_a_warning(root: Path, mock_http: MockServer,
                                                           monkeypatch: pytest.MonkeyPatch,
                                                           capsys: pytest.CaptureFixture[str]) -> None:
    serve_build(mock_http, "old", running=2, tray=True)
    monkeypatch.setattr(vc_client.VC, "_start", lambda self, build=None: mock_http.httpd.server_port)
    vc_client.VC(root, keepalive=False)
    assert "還有 2 個工作在執行" in capsys.readouterr().err
    assert ("POST", "/api/tray") not in [(c.method, c.path) for c in mock_http.calls]


def test_without_start_an_older_copy_is_only_reported(root: Path, mock_http: MockServer,
                                                      monkeypatch: pytest.MonkeyPatch,
                                                      capsys: pytest.CaptureFixture[str]) -> None:
    serve_build(mock_http, "old")
    monkeypatch.setattr(vc_client.VC, "_start", lambda self, build=None: pytest.fail("must not relaunch"))
    vc_client.VC(root, start=False, keepalive=False)
    assert "start=False" in capsys.readouterr().err
