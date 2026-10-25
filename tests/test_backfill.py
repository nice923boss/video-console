"""Old imports get their missing waveform and alpha marker in the background when the project is opened."""
from __future__ import annotations

import json
import time
from pathlib import Path

from test_server import App, Client, app, client, project  # noqa: F401  (server fixtures)
from vc.backfill import missing
from vc.util import AppError


class FakeMedia:
    available = True

    def __init__(self) -> None:
        self.probed: list[str] = []

    def probe(self, path: Path) -> dict:
        self.probed.append(path.name)
        return {"alpha": path.stem.endswith("clear"), "vcodec": "vp9"}

    def waveform(self, path: Path) -> tuple[list[int], float, float]:
        return [0, 100, 50], 2.0, 0.5

    def thumbnail(self, src: Path, dst: Path, width: int = 384, at: float | None = None) -> None:
        if src.stem == "broken":
            raise AppError("unreadable", 500)
        dst.write_bytes(b"jpg")

    def strip(self, src: Path, dst: Path, duration: float, count: int = 8) -> None:
        dst.write_bytes(b"jpg")


def _wait_for(app: App, pid: str, check, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        project = app.store.get(pid)
        if check(project):
            return project
        time.sleep(0.05)
    raise AssertionError("backfill did not finish")


def test_open_fills_waveform_and_alpha_once(app: App, client: Client, project: dict, monkeypatch) -> None:
    fake = FakeMedia()
    monkeypatch.setattr(app.backfill, "_media", lambda: fake)
    pid = project["id"]
    pdir = app.store.project_dir(pid)
    items = {"m_talk01": ("talk.mp4", True), "m_clear01": ("clear.webm", False), "m_solid01": ("solid.webm", False)}
    for mid, (name, sound) in items.items():
        (pdir / "media" / name).parent.mkdir(parents=True, exist_ok=True)
        (pdir / "media" / name).write_bytes(b"x")
        media = {"id": mid, "name": name, "kind": "video", "file": f"media/{name}", "hasAudio": sound}
        client.json("POST", f"/api/projects/{pid}/ops", {"ops": [{"op": "media.add", "media": media}]})
    assert sorted(missing(app.store.get(pid))) == ["m_clear01", "m_solid01", "m_talk01"]

    client.json("POST", f"/api/projects/{pid}/open")
    done = _wait_for(app, pid, lambda p: p["media"]["m_talk01"].get("wave") and p["media"]["m_clear01"].get("alphaCodec"))
    assert done["media"]["m_talk01"]["wave"] == "media/thumbs/m_talk01_wave.json"
    assert json.loads((pdir / "media/thumbs/m_talk01_wave.json").read_text("utf-8"))["peaks"] == [0, 100, 50]
    assert done["media"]["m_clear01"]["alphaCodec"] == "vp9" and "alphaCodec" not in done["media"]["m_solid01"]
    assert client.json("GET", f"/api/projects/{pid}")["undo"]["undo"] == 3  # the three imports, not the backfill  # not an edit

    time.sleep(0.2)
    client.json("POST", f"/api/projects/{pid}/open")  # the solid WebM is not probed again
    time.sleep(0.3)
    assert sorted(fake.probed) == ["clear.webm", "solid.webm"]


def test_derive_op_only_takes_derived_facts(client: Client, project: dict) -> None:
    pid = project["id"]
    media = {"id": "m_aaaaaa", "name": "a", "kind": "video", "file": "media/a.mp4"}
    client.json("POST", f"/api/projects/{pid}/ops", {"ops": [{"op": "media.add", "media": media}]})
    for bad in ({"wave": "../x.json"}, {"thumb": "C:/x.jpg"}, {"alphaCodec": "h264"}, {"name": "x"}):
        res = client.request("POST", f"/api/projects/{pid}/ops", {"ops": [{"op": "item.derive", "id": "m_aaaaaa", "patch": bad}]})
        assert res[0] == 400


def test_open_makes_missing_thumbnails_once(app: App, client: Client, project: dict, monkeypatch) -> None:
    fake = FakeMedia()
    monkeypatch.setattr(app.backfill, "_media", lambda: fake)
    pid = project["id"]
    pdir = app.store.project_dir(pid)
    items = [  # imported while ffmpeg was missing: images shown from the full file, videos without pictures
        {"id": "m_pic001", "kind": "image", "file": "media/pic.png", "thumb": "media/pic.png"},
        {"id": "m_vid001", "kind": "video", "file": "media/vid.mp4", "duration": 3.0},
        {"id": "m_broken", "kind": "image", "file": "media/broken.png", "thumb": "media/broken.png"},
        {"id": "m_done01", "kind": "image", "file": "media/done.png", "thumb": "media/thumbs/m_done01.jpg"},
    ]
    for item in items:
        (pdir / item["file"]).parent.mkdir(parents=True, exist_ok=True)
        (pdir / item["file"]).write_bytes(b"x")
        client.json("POST", f"/api/projects/{pid}/ops", {"ops": [{"op": "media.add", "media": {**item, "name": item["id"]}}]})
    assert sorted(missing(app.store.get(pid))) == ["m_broken", "m_pic001", "m_vid001"]

    client.json("POST", f"/api/projects/{pid}/open")
    done = _wait_for(app, pid, lambda p: p["media"]["m_pic001"]["thumb"] != "media/pic.png" and p["media"]["m_vid001"].get("strip"))
    assert done["media"]["m_pic001"]["thumb"] == "media/thumbs/m_pic001.jpg"
    assert (done["media"]["m_vid001"]["thumb"], done["media"]["m_vid001"]["strip"]) == (
        "media/thumbs/m_vid001.jpg", "media/thumbs/m_vid001_strip.jpg")
    assert (pdir / "media/thumbs/m_vid001_strip.jpg").is_file()
    # A file ffmpeg cannot read keeps the full-file fallback and is left alone until the app restarts.
    assert done["media"]["m_broken"]["thumb"] == "media/broken.png"
    assert missing(done) == ["m_broken"]
