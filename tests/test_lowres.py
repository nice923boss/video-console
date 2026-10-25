"""Smaller preview copies (PV-06): the preview asks for 1/2 or 1/4 copies of the video files it plays; they are
made in the background, recorded on the item and counted as used files."""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from test_server import App, Client, app, client, project  # noqa: F401  (server fixtures)
from test_timeline_keys import FFMPEG, VID, make_project, with_video
from vc.lowres import transcode_args, wanted
from vc.media import Media


class FakeMedia:
    def __init__(self) -> None:
        self.made: list[str] = []

    def require(self) -> str:
        return "ffmpeg"

    def run(self, args: list[str], timeout: float = 600) -> None:
        Path(args[-1]).write_bytes(b"mp4")
        self.made.append(Path(args[-1]).name)


def test_wanted_lists_the_played_videos_without_a_copy() -> None:
    p = with_video(make_project())
    p["media"] = {
        "m_pip000001": {"id": "m_pip000001", "kind": "video", "file": "media/import/a.mp4"},
        "m_clear0001": {"id": "m_clear0001", "kind": "video", "file": "media/import/b.webm", "alphaCodec": "vp9"},
        "m_still0001": {"id": "m_still0001", "kind": "image", "file": "media/import/c.png"},
    }
    p["overlays"] = [{"id": f"o_{m}", "mediaId": m} for m in p["media"]]
    assert wanted(p, 2) == [VID, "m_pip000001"]  # see-through and still files keep the original
    p["takes"][VID]["preview2"] = "media/thumbs/x_p2.mp4"
    assert wanted(p, 2) == ["m_pip000001"] and wanted(p, 4) == [VID, "m_pip000001"]


def test_request_makes_each_copy_once_and_records_it(app: App, client: Client, project: dict, monkeypatch) -> None:
    fake = FakeMedia()
    monkeypatch.setattr(app.lowres, "_media", lambda: fake)
    pid = project["id"]
    pdir = app.store.project_dir(pid)
    (pdir / "media" / "import").mkdir(parents=True, exist_ok=True)
    (pdir / "media" / "import" / "m_clip00001.mp4").write_bytes(b"x")
    media = {"id": "m_clip00001", "name": "clip.mp4", "kind": "video", "file": "media/import/m_clip00001.mp4"}
    layer = {"id": "l_top00000", "name": "", "hidden": False, "muted": False}
    overlay = {"id": "o_clip00001", "mediaId": "m_clip00001", "layer": layer["id"], "start": 0, "in": 0, "out": 2}
    client.json("POST", f"/api/projects/{pid}/ops", {"ops": [{"op": "media.add", "media": media},
                                                           {"op": "layer.add", "layer": layer},
                                                           {"op": "overlay.add", "item": overlay}]})

    assert client.json("POST", f"/api/projects/{pid}/preview-files", {"scale": 2})["pending"] == 1
    deadline = time.monotonic() + 5
    while not app.store.get(pid)["media"]["m_clip00001"].get("preview2"):
        assert time.monotonic() < deadline, "the copy was not recorded"
        time.sleep(0.05)
    assert app.store.get(pid)["media"]["m_clip00001"]["preview2"] == "media/thumbs/m_clip00001_p2.mp4"
    assert (pdir / "media" / "thumbs" / "m_clip00001_p2.mp4").is_file()
    assert not list((pdir / "media" / "thumbs").glob("*.part.mp4"))
    assert client.json("POST", f"/api/projects/{pid}/preview-files", {"scale": 2})["pending"] == 0
    assert fake.made == ["m_clip00001_p2.part.mp4"]
    assert "media/thumbs/m_clip00001_p2.mp4" not in app.store.usage(pid)["orphans"]

    assert "1/2" in client.json("POST", f"/api/projects/{pid}/preview-files", {"scale": 3}, status=400)["error"]


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg not installed")
def test_the_copy_is_half_the_size_and_keeps_the_sound(tmp_path: Path) -> None:
    media = Media(FFMPEG)
    src = tmp_path / "src.mp4"
    media.run(["-f", "lavfi", "-i", "testsrc=s=640x360:r=24:d=1", "-f", "lavfi", "-i", "sine=d=1",
               "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(src)])
    out = tmp_path / "out.mp4"
    media.run(transcode_args(src, out, 4))
    info = media.probe(out)
    assert (info["width"], info["height"]) == (160, 90) and info["hasAudio"]
