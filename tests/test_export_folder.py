"""The shared export folder: settings, file naming, each project's index of what it wrote there, and the API."""
from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Iterator
from urllib.parse import quote

import pytest

from test_server import Client, needs_ffmpeg, png_bytes
from vc.api import system as system_api
from vc.app import App
from vc.exportdir import export_root, file_ref, find_export, forget_export, project_exports, record_export, unique_path
from vc.settings import SettingsStore
from vc.util import AppError


@pytest.fixture()
def out_dir(tmp_path: Path) -> Path:
    return tmp_path / "成品 輸出"


@pytest.fixture()
def app(tmp_path: Path, out_dir: Path) -> Iterator[App]:
    data = tmp_path / "data"
    data.mkdir()
    servers = [{"id": "local", "name": "x", "url": "http://127.0.0.1:9", "enabled": False}]
    cfg = {"comfy": {"servers": servers}, "export": {"folder": str(out_dir)}}
    (data / "settings.json").write_text(json.dumps(cfg), encoding="utf-8")
    instance = App(data)
    instance.start(port=0, watchdog=False)
    yield instance
    instance.stop()


@pytest.fixture()
def client(app: App) -> Client:
    return Client(app.server.port)


@pytest.fixture()
def project(client: Client) -> dict:
    return client.json("POST", "/api/projects", {"name": "港口短片"})["project"]


def _run(client: Client, kind: str, pid: str, payload: dict | None = None) -> dict:
    job = client.json("POST", "/api/jobs", {"kind": kind, "pid": pid, "payload": payload or {}})["job"]
    return client.wait_job(job["id"])


def test_folder_setting_accepts_only_full_paths(settings: SettingsStore, tmp_path: Path) -> None:
    assert settings.section("export")["folder"] == ""
    for bad in ("影片輸出", "exports\\videos", f"{tmp_path}\nx"):
        with pytest.raises(AppError):
            settings.update({"export": {"folder": bad}})
    assert settings.update({"export": {"folder": f' "{tmp_path}" '}})["export"]["folder"] == str(tmp_path)
    # an unplugged drive is not an error: the folder only has to exist when something is exported
    missing = str(tmp_path / "not-there")
    assert settings.update({"export": {"folder": missing}})["export"]["folder"] == missing
    assert SettingsStore(tmp_path / "settings.json").section("export")["folder"] == missing
    assert settings.update({"export": {"folder": ""}})["export"]["folder"] == ""


def test_root_names_and_refs(tmp_path: Path) -> None:
    assert export_root("", tmp_path) == tmp_path / "exports"
    assert export_root(str(tmp_path / "out"), tmp_path) == tmp_path / "out"
    (tmp_path / "a.mp4").write_bytes(b"x")
    (tmp_path / ".a-2.mp4.part").write_bytes(b"x")  # another export is still writing a-2
    assert unique_path(tmp_path, "a.mp4") == tmp_path / "a-3.mp4"
    assert unique_path(tmp_path, "b.zip") == tmp_path / "b.zip"
    pdir = tmp_path / "p"
    assert file_ref(pdir / "exports" / "a.mp4", pdir) == "exports/a.mp4"
    assert file_ref(tmp_path / "a.mp4", pdir) == str(tmp_path / "a.mp4")


def test_project_lists_its_own_and_shared_files(tmp_path: Path) -> None:
    pdir, shared = tmp_path / "p", tmp_path / "shared"
    (pdir / "exports").mkdir(parents=True)
    shared.mkdir()
    for name in ("old.mp4", ".hidden", "x.mp4.part"):
        (pdir / "exports" / name).write_bytes(b"x")
    new, gone, other = shared / "new.mp4", shared / "gone.zip", shared / "other.mp4"
    for f in (new, gone, other):
        f.write_bytes(b"x")
    for f in (new, gone, new):
        record_export(pdir, f)
    gone.unlink()
    assert sorted(f.name for f in project_exports(pdir)) == ["new.mp4", "old.mp4"]  # other belongs to no one here
    assert find_export(pdir, "new.mp4") == new
    assert find_export(pdir, str(new)) == new
    assert find_export(pdir, "exports/old.mp4") == pdir / "exports" / "old.mp4"
    assert find_export(pdir, str(other)) is None
    gone.write_bytes(b"x")  # the drive came back
    assert gone in project_exports(pdir)
    forget_export(pdir, gone)
    assert gone not in project_exports(pdir)


def test_package_goes_to_the_shared_folder(app: App, client: Client, project: dict, out_dir: Path) -> None:
    pid = project["id"]
    done = _run(client, "package", pid, {"includeVideos": False, "includeExport": False})
    assert done["status"] == "done", done["error"]
    zip_path = Path(done["result"]["file"])
    assert zip_path.parent == out_dir and zip_path.name.endswith("_package.zip") and zip_path.is_file()
    assert not (app.store.project_dir(pid) / "exports" / zip_path.name).exists()
    [entry] = client.json("GET", f"/api/projects/{pid}/exports")
    assert entry["path"] == str(zip_path) and entry["kind"] == "package"
    code, _, data = client.request("GET", f"/api/projects/{pid}/file?path={quote(entry['path'])}")
    assert code == 200 and data == zip_path.read_bytes()
    # a full path is served only when this project wrote that file
    stranger = out_dir / "stranger.zip"
    stranger.write_bytes(b"x")
    assert client.request("GET", f"/api/projects/{pid}/file?path={quote(str(stranger))}")[0] == 404
    other = client.json("POST", "/api/projects", {"name": "另一個"})["project"]
    assert client.request("GET", f"/api/projects/{other['id']}/file?path={quote(entry['path'])}")[0] == 404
    assert client.request("DELETE", f"/api/projects/{other['id']}/exports/{quote(entry['name'])}")[0] == 404
    client.json("DELETE", f"/api/projects/{pid}/exports/{quote(entry['name'])}")
    assert not zip_path.exists() and stranger.exists()
    assert client.json("GET", f"/api/projects/{pid}/exports") == []


@needs_ffmpeg
def test_video_goes_to_the_shared_folder_and_into_the_package(app: App, client: Client, project: dict,
                                                              out_dir: Path) -> None:
    pid, sid = project["id"], project["shots"][0]["id"]
    client.json("POST", f"/api/projects/{pid}/ops",
                {"ops": [{"op": "shot.update", "id": sid, "patch": {"duration": 1.0}}]})
    assert client.wait_job(client.json("POST", f"/api/projects/{pid}/upload?name=f.png&target=shot&shot={sid}",
                                       png_bytes())["job"]["id"])["status"] == "done"
    done = _run(client, "export", pid)
    assert done["status"] == "done", done["error"]
    video = Path(done["result"]["file"])
    assert video.parent == out_dir and video.read_bytes()[4:8] == b"ftyp"
    assert not list(out_dir.glob(".*.part"))
    done = _run(client, "package", pid, {"includeVideos": False})
    assert done["status"] == "done", done["error"]
    with zipfile.ZipFile(done["result"]["file"]) as zf:
        assert video.name in zf.namelist()  # the latest video is found in the shared folder


def test_unusable_folder_fails_the_job_with_a_clear_message(app: App, client: Client, project: dict,
                                                            out_dir: Path) -> None:
    out_dir.write_bytes(b"a file where the folder should be")
    done = _run(client, "package", project["id"], {"includeVideos": False, "includeExport": False})
    assert done["status"] == "failed" and "無法使用匯出資料夾" in done["error"]


def test_reveal_opens_projects_and_export_folders(app: App, client: Client, out_dir: Path,
                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[Path] = []
    monkeypatch.setattr(system_api, "open_folder", opened.append)
    for what in ("projects", "exports"):
        client.json("POST", "/api/system/reveal", {"what": what})
    client.json("PUT", "/api/settings", {"export": {"folder": ""}})
    client.json("POST", "/api/system/reveal", {"what": "exports"})
    assert opened == [app.paths.projects, out_dir, app.paths.data / "exports"]
    assert out_dir.is_dir() and (app.paths.data / "exports").is_dir()


def test_deleting_a_project_can_take_its_shared_exports(app: App, client: Client, out_dir: Path) -> None:
    keep = client.json("POST", "/api/projects", {"name": "保留"})["project"]
    gone = client.json("POST", "/api/projects", {"name": "刪除"})["project"]
    out_dir.mkdir()
    files = {}
    for p in (keep, gone):
        pdir = app.store.project_dir(p["id"])
        files[p["id"]] = shared, own = out_dir / f"{p['name']}.mp4", pdir / "exports" / "舊.mp4"
        own.parent.mkdir(parents=True, exist_ok=True)
        for f in (shared, own):
            f.write_bytes(b"x")
        record_export(pdir, shared)

    assert client.json("DELETE", f"/api/projects/{keep['id']}") == {"removedExports": 0, "exportsInUse": 0}
    assert all(f.exists() for f in files[keep["id"]][:1])  # the shared file stays without ?exports=1
    assert client.json("DELETE", f"/api/projects/{gone['id']}?exports=1") == {"removedExports": 1, "exportsInUse": 0}
    assert not files[gone["id"]][0].exists()
    trashed = client.json("GET", "/api/trash")
    assert len(trashed) == 2  # the project's own exports/ went to the trash with it


def test_pick_folder_returns_the_choice(client: Client, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple] = []
    answers = iter(["D:\\影片輸出", None])
    monkeypatch.setattr(system_api.folderpick, "pick", lambda start, title: calls.append((start, title)) or next(answers))
    assert client.json("POST", "/api/system/pick-folder", {"start": "D:\\舊", "title": "選擇匯出資料夾"}) == \
        {"path": "D:\\影片輸出"}
    assert client.json("POST", "/api/system/pick-folder", {}) == {"path": None}  # canceled
    assert calls == [(Path("D:\\舊"), "選擇匯出資料夾"), (None, "選擇資料夾")]


def test_sweep_removes_only_our_partial_files_from_the_shared_folder(app: App, out_dir: Path) -> None:
    out_dir.mkdir()
    ours, theirs, kept = out_dir / ".film.mp4.part", out_dir / "notes.part", out_dir / "film.mp4"
    for f in (ours, theirs, kept):
        f.write_bytes(b"x")
    app._sweep_cache()
    assert not ours.exists() and theirs.exists() and kept.exists()
