"""One-click dependency installs: pinned download, hash check, swap into data/tools, and the settings API."""
from __future__ import annotations

import dataclasses
import hashlib
import json
import sys
import time
import zipfile
from pathlib import Path
from typing import Iterator

import pytest

from test_server import Client
from vc import deps
from vc.app import App
from vc.media import find_ffmpeg
from vc.util import AppError

WHEEL_MEMBER = "imageio_ffmpeg/binaries/ffmpeg-win-x86_64-v7.1.exe"


def fake_package(monkeypatch: pytest.MonkeyPatch, pkg_id: str, archive: Path, sha256: str | None = None) -> None:
    data = archive.read_bytes()
    pkg = dataclasses.replace(deps.PACKAGES[pkg_id], url=archive.as_uri(), size=len(data),
                              sha256=sha256 or hashlib.sha256(data).hexdigest())
    monkeypatch.setitem(deps.PACKAGES, pkg_id, pkg)


def make_zip(path: Path, members: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name, body in members.items():
            zf.writestr(name, body)
    return path


def wait_task(installer: deps.DepInstaller, pkg_id: str, timeout: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        task = installer.tasks().get(pkg_id, {})
        if task.get("state") in ("done", "error"):
            return task
        time.sleep(0.02)
    raise AssertionError(f"{pkg_id} install still {installer.tasks().get(pkg_id)}")


@pytest.fixture()
def calls() -> list[tuple[str, bool]]:
    return []


@pytest.fixture()
def installer(tmp_path: Path, calls: list) -> deps.DepInstaller:
    return deps.DepInstaller(tmp_path / "tools", lambda pkg_id, ok: calls.append((pkg_id, ok)))


def test_installs_ffmpeg_from_the_wheel(tmp_path, monkeypatch, installer, calls):
    wheel = make_zip(tmp_path / "ffmpeg.whl", {WHEEL_MEMBER: b"MZ fake ffmpeg", "imageio_ffmpeg/__init__.py": b""})
    fake_package(monkeypatch, "ffmpeg", wheel)
    installer.start("ffmpeg")
    task = wait_task(installer, "ffmpeg")
    assert task["state"] == "done", task
    tools = tmp_path / "tools"
    assert (tools / "ffmpeg" / "ffmpeg.exe").read_bytes() == b"MZ fake ffmpeg"
    assert sorted(p.name for p in (tools / "ffmpeg").iterdir()) == ["ffmpeg.exe"]
    assert not list((tools / ".download").iterdir())
    assert not (tools / ".ffmpeg.new").exists()
    assert calls[0] == ("ffmpeg", False) and calls[-1] == ("ffmpeg", True)


def test_rejects_a_download_whose_hash_does_not_match(tmp_path, monkeypatch, installer, calls):
    wheel = make_zip(tmp_path / "ffmpeg.whl", {WHEEL_MEMBER: b"tampered"})
    fake_package(monkeypatch, "ffmpeg", wheel, sha256="0" * 64)
    installer.start("ffmpeg")
    task = wait_task(installer, "ffmpeg")
    assert task["state"] == "error" and "SHA-256" in task["error"]
    tools = tmp_path / "tools"
    assert not (tools / "ffmpeg").exists()
    assert not list((tools / ".download").iterdir())
    assert calls[-1] == ("ffmpeg", False)


def test_a_missing_program_in_the_archive_is_an_error(tmp_path, monkeypatch, installer):
    fake_package(monkeypatch, "video2x", make_zip(tmp_path / "v2x.zip", {"readme.txt": b"no exe here"}))
    installer.start("video2x")
    task = wait_task(installer, "video2x")
    assert task["state"] == "error" and "找不到程式檔" in task["error"]


def test_reinstalling_replaces_the_old_folder(tmp_path, monkeypatch, installer):
    old = tmp_path / "tools" / "video2x"
    old.mkdir(parents=True)
    (old / "stale.dll").write_bytes(b"old")
    archive = make_zip(tmp_path / "v2x.zip", {"video2x.exe": b"MZ new", "models/realesrgan/x.bin": b"m"})
    fake_package(monkeypatch, "video2x", archive)
    installer.start("video2x")
    assert wait_task(installer, "video2x")["state"] == "done"
    assert (old / "video2x.exe").read_bytes() == b"MZ new"
    assert (old / "models" / "realesrgan" / "x.bin").is_file()
    assert not (old / "stale.dll").exists()


def test_unknown_package_and_double_start_are_refused(installer):
    with pytest.raises(AppError):
        installer.start("photoshop")
    installer._tasks["ffmpeg"] = {"state": "downloading", "received": 0, "total": 1, "error": ""}
    with pytest.raises(AppError) as err:
        installer.start("ffmpeg")
    assert err.value.status == 409


def test_find_ffmpeg_uses_the_installed_copy(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "")
    monkeypatch.setitem(sys.modules, "imageio_ffmpeg", None)  # import now fails
    tools = tmp_path / "tools"
    exe = tools / "ffmpeg" / "ffmpeg.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"MZ")
    assert find_ffmpeg("", tools) == str(exe)


@pytest.fixture()
def client(tmp_path: Path) -> Iterator[Client]:
    data = tmp_path / "data"
    data.mkdir()
    servers = [{"id": "local", "name": "x", "url": "http://127.0.0.1:9", "enabled": False}]
    (data / "settings.json").write_text(json.dumps({"comfy": {"servers": servers}}), encoding="utf-8")
    app = App(data)
    app.start(port=0, watchdog=False)
    yield Client(app.server.port)
    app.stop()


def test_status_lists_the_three_dependencies(client):
    items = client.json("GET", "/api/system/deps")["items"]
    assert [i["id"] for i in items] == ["ffmpeg", "video2x", "comfyui"]
    ffmpeg, video2x, comfy = items
    assert ffmpeg["required"] and ffmpeg["installable"] and ffmpeg["sizeMB"] > 0
    assert not video2x["required"] and video2x["installable"]
    assert not comfy["installable"] and not comfy["ready"]  # the only server is disabled


def test_install_endpoint_rejects_unknown_ids(client):
    code, _, body = client.request("POST", "/api/system/deps/install", {"id": "photoshop"})
    assert code == 400, body
