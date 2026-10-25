"""AU-09: files picked in the system's open-file dialog come in like uploads, and the page learns the folder they
came from, to remember an import volume per folder. Only files the user picked can be imported, as copies."""
from __future__ import annotations

from pathlib import Path

import pytest

from test_server import App, Client, app, client, needs_ffmpeg, project  # noqa: F401  (server fixtures)
from test_voice import wav_bytes
from vc.api import files as files_api


def dialog_returns(monkeypatch: pytest.MonkeyPatch, paths: list[Path]) -> list[tuple[str, str]]:
    """Stand in for the user picking paths; returns the (title, first filter) of each dialog shown."""
    shown: list[tuple[str, str]] = []

    def fake(start: Path | None, title: str, types: list[tuple[str, str]]) -> list[str]:
        shown.append((title, types[0][1]))
        return [str(p) for p in paths]

    monkeypatch.setattr(files_api.folderpick, "pick_files", fake)
    return shown


def pick(client: Client, pid: str, audio: bool = False) -> dict:
    return client.json("POST", f"/api/projects/{pid}/pick-files", {"audio": audio})


def test_the_pick_names_the_folder_and_skips_other_files(client: Client, project: dict, tmp_path: Path,  # noqa: F811
                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    folder = tmp_path / "VoAI 下載"
    folder.mkdir()
    wav, clip, note = folder / "第一句.wav", folder / "片段.mp4", folder / "說明.txt"
    for f in (wav, clip, note):
        f.write_bytes(b"x")
    shown = dialog_returns(monkeypatch, [wav, clip, note])
    res = pick(client, project["id"], audio=True)
    assert res["folder"] == str(folder) and res["files"] == [{"name": "第一句.wav"}] and res["skipped"] == 2
    assert shown[0][0] == "加入音訊" and "*.wav" in shown[0][1] and "*.mp4" not in shown[0][1]
    assert [f["name"] for f in pick(client, project["id"])["files"]] == ["第一句.wav", "片段.mp4"]
    assert shown[1][0] == "匯入檔案" and "*.mp4" in shown[1][1]
    dialog_returns(monkeypatch, [])  # canceled
    assert pick(client, project["id"]) == {"files": [], "skipped": 0}


def test_only_a_picked_file_can_be_imported(client: Client, project: dict, tmp_path: Path,  # noqa: F811
                                            monkeypatch: pytest.MonkeyPatch) -> None:
    pid = project["id"]
    wav = tmp_path / "a.wav"
    wav.write_bytes(b"x")
    dialog_returns(monkeypatch, [wav])
    token = pick(client, pid)["token"]
    for query in ("token=k_nothing&index=0", f"token={token}&index=1", f"token={token}&index=-1", f"token={token}"):
        status, _, body = client.request("POST", f"/api/projects/{pid}/import-picked?{query}")
        assert status == 410 and "請重新選擇" in body.decode("utf-8"), query
    wav.unlink()
    status, _, body = client.request("POST", f"/api/projects/{pid}/import-picked?token={token}&index=0")
    assert status == 404 and "a.wav" in body.decode("utf-8")


@needs_ffmpeg
def test_a_picked_sound_comes_in_as_a_copy_with_its_volume(app: App, client: Client, project: dict,  # noqa: F811
                                                           tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pid = project["id"]
    wav = tmp_path / "第一句.wav"
    wav.write_bytes(wav_bytes(0.5))
    dialog_returns(monkeypatch, [wav])
    token = pick(client, pid, audio=True)["token"]
    job = client.json("POST", f"/api/projects/{pid}/import-picked?token={token}&index=0&gain=1.9953&track=1&start=2")
    done = client.wait_job(job["job"]["id"])
    assert done["status"] == "done", done["error"]
    saved = client.json("GET", f"/api/projects/{pid}")["project"]
    (media,) = saved["media"].values()
    assert media["name"] == "第一句.wav" and media["gain"] == 1.9953
    assert [(a["start"], a["volume"]) for a in saved["audio"]] == [(2.0, 1.9953)]
    assert wav.is_file()  # the original stays where it was
    assert not list((app.store.project_dir(pid) / "media" / "cache").glob("upload-*"))
