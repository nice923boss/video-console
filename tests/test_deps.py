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
from vc import deps, video2x
from vc.app import App
from vc.media import find_ffmpeg
from vc.util import AppError

WHEEL_MEMBER = "imageio_ffmpeg/binaries/ffmpeg-win-x86_64-v7.1.exe"


def fake_package(monkeypatch: pytest.MonkeyPatch, pkg_id: str, archive: Path, sha256: str | None = None,
                 urls: tuple[str, ...] = ()) -> None:
    data = archive.read_bytes()
    pkg = dataclasses.replace(deps.PACKAGES[pkg_id], urls=urls or (archive.as_uri(),), size=len(data),
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
    assert sorted(p.name for p in (tools / "ffmpeg").iterdir()) == [".vc-version", "ffmpeg.exe"]
    assert installer.installed_version("ffmpeg") == "7.1"
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


def range_route(body: bytes):
    """Serves `body` and honours "Range: bytes=N-" like a real download server."""
    def answer(call):
        spec = call.headers.get("range", "")
        if not spec:
            return 200, body, {"Content-Type": "application/zip"}
        start = int(spec.removeprefix("bytes=").rstrip("-"))
        return 206, body[start:], {"Content-Type": "application/zip",
                                   "Content-Range": f"bytes {start}-{len(body) - 1}/{len(body)}"}
    return answer


def test_a_broken_download_resumes_from_the_partial_file(tmp_path, monkeypatch, installer, mock_http):
    program = b"MZ" + bytes(range(256)) * 400
    wheel = make_zip(tmp_path / "ffmpeg.whl", {WHEEL_MEMBER: program})
    body = wheel.read_bytes()
    half = len(body) // 2
    # The official source drops the connection halfway (announces the full length, sends half); the mirror is down.
    mock_http.route("GET", "/a.whl", (200, body[:half], {"Content-Length": str(len(body))}))
    fake_package(monkeypatch, "ffmpeg", wheel, urls=(mock_http.url + "/a.whl", mock_http.url + "/b.whl"))
    installer.start("ffmpeg")
    task = wait_task(installer, "ffmpeg")
    assert task["state"] == "error" and "接著下載" in task["error"], task
    parts = list((tmp_path / "tools" / ".download").glob("ffmpeg-*.part"))
    assert len(parts) == 1 and parts[0].stat().st_size == half

    mock_http.route("GET", "/a.whl", range_route(body))
    installer.start("ffmpeg")
    task = wait_task(installer, "ffmpeg")
    assert task["state"] == "done", task
    assert mock_http.calls[-1].headers["range"] == f"bytes={half}-"
    assert (tmp_path / "tools" / "ffmpeg" / "ffmpeg.exe").read_bytes() == program
    assert not list((tmp_path / "tools" / ".download").iterdir())


def test_the_next_source_is_tried_when_one_fails(tmp_path, monkeypatch, installer, mock_http):
    archive = make_zip(tmp_path / "v2x.zip", {"video2x.exe": b"MZ v2x"})
    mock_http.route("GET", "/mirror.zip", range_route(archive.read_bytes()))
    fake_package(monkeypatch, "video2x", archive, urls=(mock_http.url + "/gone.zip", mock_http.url + "/mirror.zip"))
    installer.start("video2x")
    task = wait_task(installer, "video2x")
    assert task["state"] == "done", task
    assert task["source"] == "127.0.0.1"
    assert [c.path for c in mock_http.calls] == ["/gone.zip", "/mirror.zip"]


def test_a_source_ignoring_the_range_restarts_the_file(tmp_path, monkeypatch, installer, mock_http):
    archive = make_zip(tmp_path / "v2x.zip", {"video2x.exe": b"MZ whole"})
    fake_package(monkeypatch, "video2x", archive, urls=(mock_http.url + "/v.zip",))
    part = tmp_path / "tools" / ".download" / f"video2x-{deps.PACKAGES['video2x'].sha256[:16]}.part"
    part.parent.mkdir(parents=True)
    part.write_bytes(b"garbage")  # a 200 answer to a range request must not be appended to this
    mock_http.route("GET", "/v.zip", (200, archive.read_bytes(), {"Content-Type": "application/zip"}))
    installer.start("video2x")
    assert wait_task(installer, "video2x")["state"] == "done"
    assert mock_http.calls[0].headers["range"] == "bytes=7-"
    assert (tmp_path / "tools" / "video2x" / "video2x.exe").read_bytes() == b"MZ whole"


def test_a_failure_other_than_the_network_drops_the_partial_file(tmp_path, monkeypatch, installer, mock_http):
    archive = make_zip(tmp_path / "v2x.zip", {"video2x.exe": b"MZ"})
    mock_http.route("GET", "/v.zip", (200, archive.read_bytes() + b"extra", {"Content-Type": "application/zip"}))
    fake_package(monkeypatch, "video2x", archive, urls=(mock_http.url + "/v.zip",))
    installer.start("video2x")
    task = wait_task(installer, "video2x")
    assert task["state"] == "error" and "比預期大" in task["error"]
    assert not list((tmp_path / "tools" / ".download").iterdir())


def test_parse_manifest_keeps_only_complete_entries():
    good = {"version": "8.0", "name": "ffmpeg 8", "urls": ["https://a/x.whl", "http://plain/x.whl"], "size": 5,
            "sha256": "a" * 64, "member": "bin/ffmpeg.exe"}
    found = deps.parse_manifest({"packages": {
        "ffmpeg": good,
        "video2x": {**good, "sha256": "not a hash"},
        "photoshop": good,
    }})
    assert list(found) == ["ffmpeg"]
    assert found["ffmpeg"].urls == ("https://a/x.whl",)  # plain http is dropped
    assert found["ffmpeg"].exe == "ffmpeg/ffmpeg.exe"
    assert deps.parse_manifest({"packages": {"ffmpeg": {**good, "member": "../evil.exe"}}}) == {}
    assert deps.parse_manifest([1, 2]) == {}
    assert deps.parse_manifest(json.loads(deps.MANIFEST_FILE.read_text(encoding="utf-8"))) == deps.PACKAGES
    assert list(deps.PACKAGES) == ["ffmpeg", "video2x"]


def test_version_key_orders_numbers_numerically():
    assert deps.version_key("7.10") > deps.version_key("7.9") > deps.version_key("7.1")
    assert deps.version_key("6.4.0") < deps.version_key("6.10.0")


def test_check_uses_the_published_list_only_when_newer(monkeypatch, installer, mock_http):
    monkeypatch.setattr(deps, "MANIFEST_URL", mock_http.url + "/deps.json")
    newer = {"version": "8.0", "name": "ffmpeg 8.0", "urls": ["https://example.com/f.whl"], "size": 10,
             "sha256": "b" * 64, "member": "f.exe"}
    mock_http.route("GET", "/deps.json", (200, {"packages": {"ffmpeg": newer}}))
    assert installer.check() == ""
    assert installer.package("ffmpeg").version == "8.0"
    assert installer.package("video2x") == deps.PACKAGES["video2x"]  # missing from the published list
    mock_http.route("GET", "/deps.json", (200, {"packages": {"ffmpeg": {**newer, "version": "7.0"}}}))
    assert installer.check() == ""
    assert installer.package("ffmpeg") == deps.PACKAGES["ffmpeg"]
    mock_http.route("GET", "/deps.json", (200, {"packages": {}}))
    assert "格式不對" in installer.check()
    mock_http.route("GET", "/deps.json", (500, {"error": "x"}))
    assert "取不到" in installer.check()
    assert installer.package("ffmpeg") == deps.PACKAGES["ffmpeg"]  # a failed check keeps the last good list


def test_installed_version_of_an_install_without_a_marker(tmp_path, installer):
    assert installer.installed_version("video2x") is None
    exe = tmp_path / "tools" / "video2x" / "video2x.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"MZ")
    assert installer.installed_version("video2x") == "6.4.0"  # installed before the marker existed
    assert installer.is_managed("video2x", str(exe))
    assert not installer.is_managed("video2x", str(tmp_path / "elsewhere" / "video2x.exe"))


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


def test_status_offers_an_update_for_the_managed_copy_only(client, monkeypatch, mock_http):
    monkeypatch.setattr(deps, "MANIFEST_URL", mock_http.url + "/deps.json")
    entry = {"version": "6.5.0", "name": "Video2X 6.5.0", "urls": ["https://example.com/v.zip"], "size": 10,
             "sha256": "c" * 64, "member": ""}
    mock_http.route("GET", "/deps.json", (200, {"packages": {"video2x": entry}}))
    monkeypatch.setattr(video2x, "EXE", None)  # no system-wide install on this machine
    exe = video2x.PORTABLE_EXE  # data/tools/video2x/video2x.exe of this test's app
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"MZ")
    (exe.parent / ".vc-version").write_text("6.4.0", encoding="utf-8")
    res = client.json("POST", "/api/system/deps/check")
    assert res["remark"] == ""
    item = next(i for i in res["items"] if i["id"] == "video2x")
    assert item["update"] and item["installedVersion"] == "6.4.0" and item["latest"] == "6.5.0"
    assert item["package"] == "Video2X 6.5.0"
    ffmpeg = res["items"][0]
    assert not ffmpeg["update"] and ffmpeg["installedVersion"] is None  # this ffmpeg is not the data/tools copy


def test_install_endpoint_rejects_unknown_ids(client):
    code, _, body = client.request("POST", "/api/system/deps/install", {"id": "photoshop"})
    assert code == 400, body
