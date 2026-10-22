"""The HTTP layer against a real App on a free port: guards, editing + SSE, files, uploads, jobs, export, launcher."""
from __future__ import annotations

import http.client
import io
import json
import os
import re
import struct
import subprocess
import threading
import time
import zipfile
import zlib
from pathlib import Path
from typing import Any, Callable, Iterator

import pytest

import main as launcher
from vc.app import App
from vc.media import find_ffmpeg
from vc.paths import Paths
from vc.util import new_id

needs_ffmpeg = pytest.mark.skipif(find_ffmpeg() is None, reason="ffmpeg is not installed")
BOM = b"\xef\xbb\xbf"
FINISHED = ("done", "failed", "canceled", "interrupted")
SCRIPT = (
    "1. 清晨的漁港，薄霧籠罩，漁船靜靜停泊。鏡頭緩慢推進。5秒\n"
    "2. 穿黃色雨衣的小女孩跑過碼頭，她說：「爸爸回來了！」\n"
    "3. 燈塔在夕陽下發光，海鷗飛過，空拍緩慢前進，約8秒\n"
)


def png_bytes(w: int = 320, h: int = 180, rgb: tuple[int, int, int] = (200, 80, 40)) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))
    header = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


class Client:
    """Talks to the app the way its window does: right Host header, X-VC on every state-changing request."""

    def __init__(self, port: int):
        self.port = port

    def request(self, method: str, path: str, body: Any = None,
                headers: dict[str, str | None] | None = None) -> tuple[int, dict[str, str], bytes]:
        hdrs: dict[str, str] = {"Host": f"127.0.0.1:{self.port}"}
        if method not in ("GET", "HEAD"):
            hdrs["X-VC"] = "1"
        data = None
        if isinstance(body, bytes):
            data, hdrs["Content-Type"] = body, "application/octet-stream"
        elif body is not None:
            data, hdrs["Content-Type"] = json.dumps(body).encode("utf-8"), "application/json"
        for key, value in (headers or {}).items():
            if value is None:
                hdrs.pop(key, None)
            else:
                hdrs[key] = value
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            conn.request(method, path, body=data, headers=hdrs)
            resp = conn.getresponse()
            return resp.status, {k.lower(): v for k, v in resp.getheaders()}, resp.read()
        finally:
            conn.close()

    def json(self, method: str, path: str, body: Any = None, status: int = 200) -> Any:
        code, _, data = self.request(method, path, body)
        assert code == status, f"{method} {path} -> {code}: {data.decode('utf-8', 'replace')}"
        return json.loads(data.decode("utf-8")) if data else None

    def wait_job(self, jid: str, timeout: float = 120.0) -> dict:
        deadline = time.monotonic() + timeout
        job: dict = {}
        while time.monotonic() < deadline:
            job = self.json("GET", f"/api/jobs/{jid}")
            if job["status"] in FINISHED:
                return job
            time.sleep(0.1)
        raise AssertionError(f"job {jid} is still {job.get('status')}: {job.get('message')}")


@pytest.fixture()
def app(tmp_path: Path) -> Iterator[App]:
    data = tmp_path / "data"
    data.mkdir()
    # one disabled ComfyUI server, so no test ever reaches a real machine
    servers = [{"id": "local", "name": "x", "url": "http://127.0.0.1:9", "enabled": False}]
    (data / "settings.json").write_text(json.dumps({"comfy": {"servers": servers}}), encoding="utf-8")
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


def sse_reader(port: int) -> tuple[http.client.HTTPConnection, Callable[[], dict]]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("GET", "/api/events", headers={"Host": f"127.0.0.1:{port}"})
    resp = conn.getresponse()
    assert resp.status == 200
    assert resp.getheader("Content-Type", "").startswith("text/event-stream")

    def next_event() -> dict:
        while True:
            line = resp.fp.readline()
            if not line:
                raise EOFError("event stream closed")
            if line.startswith(b"data: "):
                return json.loads(line[6:].decode("utf-8"))

    return conn, next_event


# ---------------------------------------------------------------- basics and guards
def test_ping_and_bootstrap(app: App, client: Client) -> None:
    assert client.json("GET", "/api/ping")["instance"] == app.instance
    boot = client.json("GET", "/api/bootstrap")
    for key in ("settings", "secrets", "projects", "jobs", "queue", "comfy", "workflows", "providers",
                "presets", "system"):
        assert key in boot, key
    assert [s["id"] for s in boot["settings"]["comfy"]["servers"]] == ["local"]


def test_requests_from_other_sites_are_refused(client: Client) -> None:
    assert client.request("GET", "/api/ping", headers={"Host": "evil.example"})[0] == 403
    assert client.request("GET", "/api/ping", headers={"Host": f"localhost:{client.port}"})[0] == 200
    assert client.request("GET", "/api/ping", headers={"Sec-Fetch-Site": "cross-site"})[0] == 403
    assert client.request("POST", "/api/projects", {}, headers={"X-VC": None})[0] == 403
    status, _, body = client.request("POST", "/api/projects", {}, headers={"Origin": "http://evil.example"})
    assert status == 403
    assert json.loads(body)["error"]


def test_unknown_routes_and_static_files(client: Client) -> None:
    assert client.request("GET", "/api/nope")[0] == 404
    assert client.request("PUT", "/api/ping", {})[0] == 405
    status, headers, body = client.request("GET", "/vendor/preact.module.js")
    assert status == 200 and body
    assert headers["content-type"] == "text/javascript; charset=utf-8"
    assert client.request("GET", "/nope.js")[0] == 404


def test_index_page_is_locked_down_and_its_assets_load(client: Client) -> None:
    status, headers, body = client.request("GET", "/")
    assert status == 200
    assert headers["content-type"].startswith("text/html")
    csp = headers["content-security-policy"]
    assert "script-src 'self'" in csp and "object-src 'none'" in csp and "frame-ancestors 'none'" in csp
    assert headers["x-frame-options"] == "DENY"
    assert headers["x-content-type-options"] == "nosniff"
    page = body.decode("utf-8")
    assets = re.findall(r'(?:href|src)="([^"]+)"', page)
    assert any(a.endswith(".css") for a in assets) and "js/main.js" in assets
    for asset in assets:
        code, asset_headers, data = client.request("GET", f"/{asset}")
        assert code == 200 and data, asset
        assert "content-security-policy" not in asset_headers or asset.endswith(".html"), asset
        if asset.endswith(".css"):
            assert asset_headers["content-type"].startswith("text/css"), asset


# ---------------------------------------------------------------- editing
def test_edits_reach_the_event_stream_and_undo(client: Client, project: dict) -> None:
    pid, sid = project["id"], project["shots"][0]["id"]
    conn, next_event = sse_reader(client.port)
    try:
        assert next_event()["type"] == "hello"
        op = {"op": "shot.update", "id": sid, "patch": {"name": "日出"}}
        result = client.json("POST", f"/api/projects/{pid}/ops", {"ops": [op], "batch": "b1", "label": "改名"})
        assert result["changed"] is True
        while True:
            event = next_event()
            if event["type"] == "project" and event["data"]["project"]["id"] == pid:
                break
        data = event["data"]
        assert data["batch"] == "b1" and data["origin"] == "user"
        assert data["project"]["rev"] == result["rev"]
        assert data["project"]["shots"][0]["name"] == "日出"
    finally:
        conn.close()

    client.json("POST", f"/api/projects/{pid}/undo")
    assert client.json("GET", f"/api/projects/{pid}")["project"]["shots"][0]["name"] == "鏡頭 1"
    client.json("POST", f"/api/projects/{pid}/redo")
    got = client.json("GET", f"/api/projects/{pid}")
    assert got["project"]["shots"][0]["name"] == "日出"
    assert got["undo"]["undo"] == 1 and got["undo"]["redo"] == 0
    client.json("POST", f"/api/projects/{pid}/redo", status=409)


def test_script_parse_then_apply(client: Client, project: dict) -> None:
    pid = project["id"]
    parsed = client.json("POST", "/api/script/parse", {"text": SCRIPT})
    assert len(parsed["shots"]) == 3
    applied = client.json("POST", f"/api/projects/{pid}/script/apply", {"shots": parsed["shots"]})
    assert applied["added"] == 3 and applied["captions"] == 1
    saved = client.json("GET", f"/api/projects/{pid}")["project"]
    # the untouched 鏡頭 1 a new project starts with gives way to the script
    assert [s["id"] for s in saved["shots"]] == applied["shotIds"]
    assert [c["text"] for c in saved["captions"]] == ["爸爸回來了！"]
    client.json("POST", f"/api/projects/{pid}/script/apply", {"shots": []}, status=400)
    client.json("POST", f"/api/projects/{pid}/undo")
    assert [s["name"] for s in client.json("GET", f"/api/projects/{pid}")["project"]["shots"]] == ["鏡頭 1"]


def test_script_apply_keeps_a_shot_the_user_wrote(client: Client, project: dict) -> None:
    pid, sid = project["id"], project["shots"][0]["id"]
    client.json("POST", f"/api/projects/{pid}/ops", {"ops": [{"op": "shot.update", "id": sid, "patch": {"prompt": "日出"}}]})
    parsed = client.json("POST", "/api/script/parse", {"text": SCRIPT})
    applied = client.json("POST", f"/api/projects/{pid}/script/apply", {"shots": parsed["shots"]})
    saved = client.json("GET", f"/api/projects/{pid}")["project"]
    assert [s["id"] for s in saved["shots"]] == [sid, *applied["shotIds"]]


def test_word_script_is_read_as_text(client: Client) -> None:
    cell = '<w:tc><w:p><w:r><w:t>{}</w:t></w:r></w:p></w:tc>'
    rows = "".join("<w:tr>" + cell.format(a) + cell.format(b) + "</w:tr>"
                   for a, b in (("影部", "聲部"), ("清晨漁港", "旁白：「早安」")))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("word/document.xml", '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml'
                    f'/2006/main"><w:body><w:tbl>{rows}</w:tbl></w:body></w:document>')
    result = client.json("POST", "/api/script/docx", buf.getvalue())
    assert result == {"text": "影部\t聲部\n清晨漁港\t旁白：「早安」\n", "notes": ["讀入 1 個分鏡表，共 1 列"]}
    assert client.request("POST", "/api/script/docx", b"not a docx")[0] == 400


# ---------------------------------------------------------------- files
def test_media_files_support_ranges_and_refuse_traversal(app: App, client: Client, project: dict) -> None:
    pid = project["id"]
    (app.store.project_dir(pid) / "media" / "sample.bin").write_bytes(bytes(range(10)))
    url = f"/api/projects/{pid}/file?path=media/sample.bin"
    status, headers, body = client.request("GET", url, headers={"Range": "bytes=2-5"})
    assert status == 206 and body == bytes([2, 3, 4, 5])
    assert headers["content-range"] == "bytes 2-5/10"
    assert client.request("GET", url, headers={"Range": "bytes=50-"})[0] == 416
    assert client.request("GET", url)[2] == bytes(range(10))
    for bad in ("media/../project.json", "project.json", "media/..%2Fproject.json"):
        assert client.request("GET", f"/api/projects/{pid}/file?path={bad}")[0] in (403, 404), bad


def test_export_listing_skips_partial_files(app: App, client: Client, project: dict) -> None:
    pid = project["id"]
    exports = app.store.project_dir(pid) / "exports"
    exports.mkdir(exist_ok=True)
    (exports / "a.mp4").write_bytes(b"x" * 10)
    (exports / ".b_package.zip.part").write_bytes(b"x")
    assert [e["name"] for e in client.json("GET", f"/api/projects/{pid}/exports")] == ["a.mp4"]
    assert client.request("DELETE", f"/api/projects/{pid}/exports/..")[0] == 403
    client.json("DELETE", f"/api/projects/{pid}/exports/a.mp4")
    assert client.json("GET", f"/api/projects/{pid}/exports") == []
    assert client.request("DELETE", f"/api/projects/{pid}/exports/a.mp4")[0] == 404


def test_captions_shot_list_and_storyboard(client: Client, project: dict) -> None:
    pid = project["id"]
    assert client.request("GET", f"/api/projects/{pid}/captions.srt")[0] == 409
    # The export dialog hides its SRT buttons by the same rule: blank captions alone give nothing to download.
    blank = {"id": new_id("c"), "start": 3.0, "end": 4.0, "text": "  ", "position": "bottom"}
    client.json("POST", f"/api/projects/{pid}/ops", {"ops": [{"op": "caption.add", "item": blank}]})
    assert client.request("GET", f"/api/projects/{pid}/captions.srt")[0] == 409
    cap = {"id": new_id("c"), "start": 0.0, "end": 2.0, "text": "你好，世界", "position": "bottom"}
    client.json("POST", f"/api/projects/{pid}/ops", {"ops": [{"op": "caption.add", "item": cap}]})

    status, headers, body = client.request("GET", f"/api/projects/{pid}/captions.srt")
    assert status == 200 and body.startswith(BOM) and "你好，世界" in body.decode("utf-8")
    assert "attachment" in headers["content-disposition"]
    status, headers, body = client.request("GET", f"/api/projects/{pid}/shots.csv")
    assert status == 200 and body.startswith(BOM) and not body.startswith(BOM * 2)
    assert headers["content-disposition"].endswith('.csv"') or ".csv" in headers["content-disposition"]

    status, headers, body = client.request("GET", f"/api/projects/{pid}/storyboard")
    assert status == 200 and headers["content-type"].startswith("text/html")
    assert "default-src 'self'" in headers["content-security-policy"]
    assert "港口短片" in body.decode("utf-8")


def test_upload_refuses_unknown_file_types(client: Client, project: dict) -> None:
    assert client.request("POST", f"/api/projects/{project['id']}/upload?name=evil.exe", b"MZ")[0] == 415


@needs_ffmpeg
def test_uploaded_picture_becomes_the_shot_image_and_exports(app: App, client: Client, project: dict) -> None:
    pid, sid = project["id"], project["shots"][0]["id"]
    client.json("POST", f"/api/projects/{pid}/ops",
                {"ops": [{"op": "shot.update", "id": sid, "patch": {"duration": 1.0}}]})
    job = client.json("POST", f"/api/projects/{pid}/upload?name=frame.png&target=shot&shot={sid}",
                      png_bytes())["job"]
    done = client.wait_job(job["id"])
    assert done["status"] == "done", done["error"]
    saved = client.json("GET", f"/api/projects/{pid}")["project"]
    tid = saved["shots"][0]["activeImage"]
    assert tid in saved["takes"]
    cache = app.store.project_dir(pid) / "media" / "cache"
    assert not [f for f in cache.iterdir() if f.name.startswith("upload-")]

    job = client.json("POST", "/api/jobs", {"kind": "export", "pid": pid, "payload": {}})["job"]
    done = client.wait_job(job["id"])
    assert done["status"] == "done", done["error"]
    [entry] = client.json("GET", f"/api/projects/{pid}/exports")
    video = app.store.project_dir(pid) / entry["path"]
    assert entry["kind"] == "video" and video.stat().st_size > 1000
    assert video.read_bytes()[4:8] == b"ftyp"
    assert not [f for f in cache.iterdir() if f.name.startswith(("export-", "job-"))]


def test_picture_in_picture_tracks_edit_and_undo(app: App, client: Client, project: dict) -> None:
    pid = project["id"]
    root = app.store.project_dir(pid)
    (root / "media/import/m_pic00000a.png").write_bytes(png_bytes())
    media = {"id": "m_pic00000a", "kind": "image", "name": "logo.png", "file": "media/import/m_pic00000a.png",
             "width": 320, "height": 180}
    item = {"id": "o_pip00000a", "mediaId": "m_pic00000a", "layer": "l_top00000a", "start": 0.5, "length": 1.0,
            "x": 0.6, "y": 0.6, "w": 0.3, "opacity": 1.0, "volume": 1.0}

    def send(*ops: dict, status: int = 200) -> Any:
        return client.json("POST", f"/api/projects/{pid}/ops", {"ops": list(ops)}, status=status)

    def saved() -> dict:
        return client.json("GET", f"/api/projects/{pid}")["project"]

    send({"op": "media.add", "media": media})
    send({"op": "overlay.add", "item": item}, status=400)  # its track does not exist yet
    send({"op": "layer.add", "layer": {"id": "l_low00000a"}}, {"op": "layer.add", "layer": {"id": "l_top00000a"}},
         {"op": "overlay.add", "item": item})
    send({"op": "overlay.update", "id": "o_pip00000a", "patch": {"layer": "l_nope0000a"}}, status=400)
    send({"op": "overlay.update", "id": "o_pip00000a", "patch": {"x": 5, "layer": "l_low00000a"}},
         {"op": "layer.update", "id": "l_top00000a", "patch": {"hidden": True, "muted": True}},
         {"op": "project.update", "patch": {"tracks": {"videoHidden": True, "captionsHidden": True}}})
    p = saved()
    assert [x["id"] for x in p["layers"]] == ["l_low00000a", "l_top00000a"]  # bottom to top
    assert p["layers"][1] == {"id": "l_top00000a", "name": "", "hidden": True, "muted": True}
    assert p["overlays"][0]["x"] == 1.0 and p["overlays"][0]["layer"] == "l_low00000a"
    assert p["tracks"]["videoHidden"] is True and p["tracks"]["captionsHidden"] is True

    send({"op": "layer.delete", "id": "l_low00000a"})
    assert [x["id"] for x in saved()["layers"]] == ["l_top00000a"] and saved()["overlays"] == []
    client.json("POST", f"/api/projects/{pid}/undo")
    p = saved()
    assert [x["id"] for x in p["layers"]] == ["l_low00000a", "l_top00000a"]
    assert [o["id"] for o in p["overlays"]] == ["o_pip00000a"]

    send({"op": "media.delete", "id": "m_pic00000a"})
    assert saved()["overlays"] == []
    client.json("POST", f"/api/projects/{pid}/undo")
    assert saved()["overlays"][0]["mediaId"] == "m_pic00000a"


@needs_ffmpeg
def test_upload_onto_a_track_becomes_picture_in_picture_and_exports(app: App, client: Client, project: dict) -> None:
    pid, sid = project["id"], project["shots"][0]["id"]
    client.json("POST", f"/api/projects/{pid}/ops", {"ops": [
        {"op": "shot.update", "id": sid, "patch": {"duration": 1.0}},
        {"op": "layer.add", "layer": {"id": "l_pip00000a"}},
        {"op": "caption.add", "item": {"id": "c_cap00000a", "start": 0.0, "end": 1.0, "text": "字幕"}},
    ]})
    for query, image in ((f"name=frame.png&target=shot&shot={sid}", png_bytes(rgb=(200, 80, 40))),
                         ("name=logo.png&layer=l_pip00000a&start=0.25", png_bytes(90, 160, rgb=(20, 200, 60)))):
        job = client.json("POST", f"/api/projects/{pid}/upload?{query}", image)["job"]
        done = client.wait_job(job["id"])
        assert done["status"] == "done", done["error"]
    saved = client.json("GET", f"/api/projects/{pid}")["project"]
    [pip] = saved["overlays"]
    assert pip["layer"] == "l_pip00000a" and pip["start"] == 0.25 and pip["length"] == 5.0
    assert saved["media"][pip["mediaId"]]["kind"] == "image"

    w, h = int(saved["settings"]["width"]), int(saved["settings"]["height"])
    # A portrait picture fills the frame height, centered, with the shot showing at the sides.
    assert pip["y"] == 0.0 and pip["w"] == pytest.approx(h * 90 / 160 / w, abs=1e-4)
    assert pip["x"] == pytest.approx((1 - pip["w"]) / 2, abs=1e-4)

    def export_frame(t: float) -> bytes:
        job = client.json("POST", "/api/jobs", {"kind": "export", "pid": pid,
                                                "payload": {"options": {"burnCaptions": True}}})["job"]
        done = client.wait_job(job["id"])
        assert done["status"] == "done", done["error"]
        video = app.store.project_dir(pid) / done["result"]["file"]
        proc = subprocess.run([find_ffmpeg(), "-v", "error", "-ss", f"{t}", "-i", str(video), "-frames:v", "1",
                               "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True, check=True)
        assert len(proc.stdout) == w * h * 3
        return proc.stdout

    def pixel(frame: bytes, fx: float, fy: float) -> tuple[int, ...]:
        i = (int(fy * h) * w + int(fx * w)) * 3
        return tuple(frame[i:i + 3])

    def near(got: tuple[int, ...], want: tuple[int, int, int]) -> bool:
        return all(abs(a - b) < 40 for a, b in zip(got, want))

    inside, outside = (0.5, 0.5), (0.15, 0.5)
    frame = export_frame(0.6)
    assert near(pixel(frame, *inside), (20, 200, 60)) and near(pixel(frame, *outside), (200, 80, 40))
    assert near(pixel(export_frame(0.1), *inside), (200, 80, 40))  # not started yet

    # Hiding the picture track leaves the picture-in-picture over black; a hidden PiP track shows nothing.
    client.json("POST", f"/api/projects/{pid}/ops", {"ops": [
        {"op": "project.update", "patch": {"tracks": {"videoHidden": True, "captionsHidden": True}}}]})
    frame = export_frame(0.6)
    assert near(pixel(frame, *inside), (20, 200, 60)) and near(pixel(frame, *outside), (0, 0, 0))
    client.json("POST", f"/api/projects/{pid}/ops", {"ops": [
        {"op": "layer.update", "id": "l_pip00000a", "patch": {"hidden": True}}]})
    assert near(pixel(export_frame(0.6), *inside), (0, 0, 0))


@needs_ffmpeg
def test_adjustment_layer_grades_only_its_span_in_export(app: App, client: Client, project: dict) -> None:
    pid, sid = project["id"], project["shots"][0]["id"]
    client.json("POST", f"/api/projects/{pid}/ops", {"ops": [
        {"op": "shot.update", "id": sid, "patch": {"duration": 2.0}},
        {"op": "adjust.add", "item": {"id": "adj_warm00000", "start": 1.0, "length": 1.0, "hidden": False,
                                      "grade": {"temp": 10000}}},
    ]})
    job = client.json("POST", f"/api/projects/{pid}/upload?name=gray.png&target=shot&shot={sid}",
                      png_bytes(rgb=(128, 128, 128)))["job"]
    assert client.wait_job(job["id"])["status"] == "done"
    saved = client.json("GET", f"/api/projects/{pid}")["project"]
    w, h = int(saved["settings"]["width"]), int(saved["settings"]["height"])

    def center(t: float) -> tuple[int, ...]:
        job = client.json("POST", "/api/jobs", {"kind": "export", "pid": pid, "payload": {}})["job"]
        done = client.wait_job(job["id"])
        assert done["status"] == "done", done["error"]
        video = app.store.project_dir(pid) / done["result"]["file"]
        proc = subprocess.run([find_ffmpeg(), "-v", "error", "-ss", f"{t}", "-i", str(video), "-frames:v", "1",
                               "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True, check=True)
        i = (h // 2 * w + w // 2) * 3
        return tuple(proc.stdout[i:i + 3])

    before, during = center(0.5), center(1.5)
    assert max(before) - min(before) < 12  # still gray before the layer starts
    assert during[0] - during[2] > 60  # warm while it runs
    cache = app.store.project_dir(pid) / "media" / "cache"
    assert not [f for f in cache.iterdir() if f.suffix == ".cube"]

    client.json("POST", f"/api/projects/{pid}/ops", {"ops": [
        {"op": "project.update", "patch": {"tracks": {"adjustHidden": True}}}]})
    graded_off = center(1.5)
    assert max(graded_off) - min(graded_off) < 12


@needs_ffmpeg
def test_video_picture_in_picture_keeps_its_sound(app: App, client: Client, project: dict, tmp_path: Path) -> None:
    pid, sid = project["id"], project["shots"][0]["id"]
    clip = tmp_path / "pip.mp4"
    subprocess.run([find_ffmpeg(), "-v", "error", "-f", "lavfi", "-i", "testsrc=s=320x180:r=25:d=2",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-shortest", "-pix_fmt", "yuv420p",
                    str(clip)], check=True)
    client.json("POST", f"/api/projects/{pid}/ops", {"ops": [
        {"op": "shot.update", "id": sid, "patch": {"duration": 2.0}},
        {"op": "layer.add", "layer": {"id": "l_pip00000a"}},
        {"op": "project.update", "patch": {"tracks": {"videoMuted": True}}}]})
    job = client.json("POST", f"/api/projects/{pid}/upload?name=pip.mp4&layer=l_pip00000a&start=0.5",
                      clip.read_bytes())["job"]
    done = client.wait_job(job["id"])
    assert done["status"] == "done", done["error"]
    [pip] = client.json("GET", f"/api/projects/{pid}")["project"]["overlays"]
    client.json("POST", f"/api/projects/{pid}/ops", {"ops": [
        {"op": "overlay.update", "id": pip["id"], "patch": {"in": 0.5, "length": 1.0, "opacity": 0.5}}]})

    def loudness() -> float:
        job = client.json("POST", "/api/jobs", {"kind": "export", "pid": pid, "payload": {}})["job"]
        done = client.wait_job(job["id"])
        assert done["status"] == "done", done["error"]
        video = app.store.project_dir(pid) / done["result"]["file"]
        proc = subprocess.run([find_ffmpeg(), "-v", "error", "-i", str(video), "-f", "s16le", "-ac", "1", "-"],
                              capture_output=True, check=True)
        samples = struct.unpack(f"<{len(proc.stdout) // 2}h", proc.stdout[: len(proc.stdout) // 2 * 2])
        return max(abs(s) for s in samples)

    assert loudness() > 1500  # the tone of the picture-in-picture clip
    client.json("POST", f"/api/projects/{pid}/ops", {"ops": [
        {"op": "layer.update", "id": "l_pip00000a", "patch": {"muted": True}}]})
    assert loudness() < 50


# ---------------------------------------------------------------- jobs
def test_queue_controls_while_paused(app: App, client: Client, project: dict) -> None:
    assert client.json("POST", "/api/jobs/pause", {"paused": True})["paused"] is True
    job = client.json("POST", "/api/jobs", {"kind": "export", "pid": project["id"], "payload": {}})["job"]
    assert job["status"] == "queued"
    assert app._busy() is False  # a paused queue does not keep a windowless server alive
    client.json("POST", f"/api/jobs/{job['id']}/top")
    assert client.json("POST", f"/api/jobs/{job['id']}/cancel")["status"] == "canceled"
    client.json("POST", f"/api/jobs/{job['id']}/top", status=409)
    client.json("DELETE", f"/api/jobs/{job['id']}")
    assert client.request("GET", f"/api/jobs/{job['id']}")[0] == 404
    assert client.json("POST", "/api/jobs/pause", {"paused": False})["paused"] is False


class FakeTray:
    def __init__(self) -> None:
        self.running = False

    def start(self) -> None:
        self.running = True


def test_tray_keeps_a_windowless_server_alive(app: App, client: Client) -> None:
    assert client.json("POST", "/api/tray", {}) == {"tray": False}  # started without a tray: nothing to show
    app.tray = FakeTray()
    assert app._busy() is False
    assert client.json("POST", "/api/tray", {}) == {"tray": True}  # a window opened onto a --no-window copy
    assert app._busy() is True
    app.tray = None
    assert app._busy() is False


def test_job_batch_reports_each_bad_item(client: Client, project: dict) -> None:
    items = [{"kind": "nope", "pid": project["id"]}, "junk", {"kind": "export", "pid": "p_missing000"}]
    result = client.json("POST", "/api/jobs/batch", {"jobs": items})
    assert result["jobs"] == []
    assert [e["index"] for e in result["errors"]] == [0, 1, 2]
    client.json("POST", "/api/jobs/batch", {"jobs": []}, status=400)


# ---------------------------------------------------------------- projects, secrets, templates
def test_deleted_project_goes_to_trash_and_comes_back(client: Client, project: dict) -> None:
    pid = project["id"]
    client.json("DELETE", f"/api/projects/{pid}")
    assert client.request("GET", f"/api/projects/{pid}")[0] == 404
    [item] = client.json("GET", "/api/trash")
    assert item["id"] == pid
    assert client.json("POST", f"/api/trash/{item['folder']}/restore") == {"id": pid}
    assert client.json("GET", f"/api/projects/{pid}")["project"]["name"] == "港口短片"


def _seed_media(app: App, client: Client, project: dict) -> None:
    """Give the first shot an active picture and video, one unused picture take, and an audio clip,
    with files written straight into the project folder (no ffmpeg needed)."""
    pid, sid = project["id"], project["shots"][0]["id"]
    root = app.store.project_dir(pid)
    files = {"media/takes/t_pic00000a.png": png_bytes(), "media/thumbs/t_pic00000a.jpg": b"thumb",
             "media/takes/t_old00000a.png": png_bytes(rgb=(1, 2, 3)),
             "media/takes/t_vid00000a.mp4": b"\x00\x00\x00\x18ftypmp42video",
             "media/thumbs/t_vid00000a.jpg": b"vthumb", "media/thumbs/t_vid00000a_strip.jpg": b"strip",
             "media/import/m_snd00000a.wav": b"RIFFwave", "media/thumbs/m_snd00000a_wave.json": b"{}"}
    for rel, data in files.items():
        (root / rel).write_bytes(data)
    takes = [
        {"id": "t_pic00000a", "kind": "image", "file": "media/takes/t_pic00000a.png",
         "thumb": "media/thumbs/t_pic00000a.jpg", "shotId": sid},
        {"id": "t_old00000a", "kind": "image", "file": "media/takes/t_old00000a.png",
         "thumb": "media/takes/t_old00000a.png", "shotId": sid},
        {"id": "t_vid00000a", "kind": "video", "file": "media/takes/t_vid00000a.mp4", "duration": 2.0,
         "thumb": "media/thumbs/t_vid00000a.jpg", "strip": "media/thumbs/t_vid00000a_strip.jpg", "shotId": sid},
    ]
    media = {"id": "m_snd00000a", "kind": "audio", "name": "海浪.wav", "file": "media/import/m_snd00000a.wav",
             "wave": "media/thumbs/m_snd00000a_wave.json", "duration": 3.0}
    ops = [*({"op": "take.add", "take": t} for t in takes), {"op": "media.add", "media": media},
           {"op": "audio.add", "item": {"id": "a_clip00000a", "mediaId": "m_snd00000a", "start": 0}},
           {"op": "shot.update", "id": sid, "patch": {"activeImage": "t_pic00000a", "activeVideo": "t_vid00000a"}}]
    client.json("POST", f"/api/projects/{pid}/ops", {"ops": ops})


def _package(app: App, client: Client, pid: str, **opts: bool) -> bytes:
    job = client.json("POST", "/api/jobs", {"kind": "package", "pid": pid, "payload": opts})["job"]
    done = client.wait_job(job["id"])
    assert done["status"] == "done", done["error"]
    return (app.store.project_dir(pid) / done["result"]["file"]).read_bytes()


def test_full_backup_imports_back_as_a_new_project(app: App, client: Client, project: dict) -> None:
    _seed_media(app, client, project)
    data = _package(app, client, project["id"], fullBackup=True, includeExport=False)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        assert json.loads(zf.read("manifest.json"))["fullBackup"] is True
        assert "media/takes/t_old00000a.png" in zf.namelist()
        assert "media/takes/t_pic00000a.png" in zf.read("storyboard.html").decode("utf-8")
    result = client.json("POST", "/api/projects/import?name=backup.zip", data)
    imported = result["project"]
    assert imported["id"] != project["id"] and imported["name"] == "港口短片"
    assert result["fullBackup"] is True and result["dropped"] == 0
    source = client.json("GET", f"/api/projects/{project['id']}")["project"]
    assert imported["takes"] == source["takes"] and imported["media"] == source["media"]
    assert imported["shots"][0]["activeVideo"] == "t_vid00000a"
    root = app.store.project_dir(imported["id"])
    for take in imported["takes"].values():
        for key in ("file", "thumb", "strip"):
            if take.get(key):
                assert (root / take[key]).read_bytes() == (app.store.project_dir(project["id"]) / take[key]).read_bytes()
    assert {p["id"] for p in client.json("GET", "/api/projects")} == {project["id"], imported["id"]}
    assert not list(app.paths.tmp.iterdir())


def test_hand_off_package_imports_only_what_it_carries(app: App, client: Client, project: dict) -> None:
    _seed_media(app, client, project)
    data = _package(app, client, project["id"], includeVideos=False, includeExport=False)
    result = client.json("POST", "/api/projects/import?name=hand-off.zip", data)
    imported = result["project"]
    assert result["fullBackup"] is False
    assert set(imported["takes"]) == {"t_pic00000a"}  # the unused picture and the left-out video are dropped
    assert imported["takes"]["t_pic00000a"]["thumb"] == "media/takes/t_pic00000a.png"
    shot = imported["shots"][0]
    assert shot["activeImage"] == "t_pic00000a" and shot["activeVideo"] is None
    assert set(imported["media"]) == {"m_snd00000a"} and imported["media"]["m_snd00000a"]["wave"] is None
    assert [c["mediaId"] for c in imported["audio"]] == ["m_snd00000a"]
    assert result["dropped"] == 2


def test_import_refuses_bad_archives(app: App, client: Client) -> None:
    assert client.request("POST", "/api/projects/import?name=x.exe", b"MZ")[0] == 415
    assert client.request("POST", "/api/projects/import?name=x.zip", b"not a zip")[0] == 400
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("readme.txt", "hi")
    assert client.request("POST", "/api/projects/import?name=x.zip", buf.getvalue())[0] == 400
    # a project.json pointing outside media/ must not write there
    evil = {"name": "evil", "settings": {}, "shots": [{"id": "s_a00000000a", "activeImage": "t_bad00000a"}],
            "takes": {"t_bad00000a": {"id": "t_bad00000a", "kind": "image", "file": "media/../../evil.png"}}}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("project.json", json.dumps(evil))
        zf.writestr("manifest.json", json.dumps({"format": "video-console-package", "fullBackup": True}))
        zf.writestr("media/../../evil.png", b"x")
    imported = client.json("POST", "/api/projects/import?name=x.zip", buf.getvalue())["project"]
    assert imported["takes"] == {} and imported["shots"][0]["activeImage"] is None
    assert not (app.paths.data / "evil.png").exists() and not (app.paths.projects / "evil.png").exists()
    assert not list(app.paths.tmp.iterdir())


def test_secrets_are_never_echoed(app: App, client: Client) -> None:
    value = "sk-test-0123456789abcdef"
    client.json("PUT", "/api/secrets", {"name": "bad name", "value": "x"}, status=400)
    client.json("PUT", "/api/secrets", {"name": "llm.api_key", "value": "a\nb"}, status=400)
    status, _, body = client.request("PUT", "/api/secrets", {"name": "llm.api_key", "value": value})
    assert status == 200
    for text in (body, client.request("GET", "/api/secrets")[2], client.request("GET", "/api/bootstrap")[2]):
        assert value.encode() not in text
    assert value not in app.paths.secrets_file.read_text(encoding="utf-8")
    assert app.secrets.get("llm.api_key") == value


def test_template_used_by_a_route_cannot_be_deleted(client: Client) -> None:
    client.json("DELETE", "/api/workflows/zimage-turbo-t2i", status=409)


# ---------------------------------------------------------------- housekeeping and launcher
def test_cache_sweep_removes_only_leftovers(app: App, project: dict) -> None:
    pdir = app.store.project_dir(project["id"])
    cache = pdir / "media" / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    old, fresh, job_dir = cache / "upload-u_old0000000.png", cache / "upload-u_new0000000.png", cache / "job-j_old"
    for f in (old, fresh):
        f.write_bytes(b"x")
    job_dir.mkdir()
    (job_dir / "frame.png").write_bytes(b"x")
    part = pdir / "exports" / ".film.mp4.part"
    part.parent.mkdir(exist_ok=True)
    part.write_bytes(b"x")
    two_days_ago = time.time() - 2 * 86400
    for f in (old, job_dir):
        os.utime(f, (two_days_ago, two_days_ago))
    app._sweep_cache()
    assert fresh.exists()
    assert not old.exists() and not job_dir.exists() and not part.exists()


def test_launcher_lock_is_exclusive(tmp_path: Path) -> None:
    first = launcher.acquire_lock(tmp_path / "instance.lock")
    assert first is not None
    assert launcher.acquire_lock(tmp_path / "instance.lock") is None
    first.close()
    again = launcher.acquire_lock(tmp_path / "instance.lock")
    assert again is not None
    again.close()


def test_launcher_finds_and_stops_the_running_server(app: App) -> None:
    info = {"port": app.server.port, "instance": app.instance}
    assert launcher.alive(info)
    assert not launcher.alive({**info, "instance": "i_other"})
    app.paths.pid_file.write_text(json.dumps(info), encoding="utf-8")
    assert launcher.read_server_info(app.paths) == info
    assert launcher.stop_running(app.paths) == 0
    assert app._stopped.wait(10)  # the server stops answering first, then the rest winds down
    assert not launcher.alive(info, 0.5)


def test_launch_defers_to_the_copy_that_answers(app: App) -> None:
    held = launcher.acquire_lock(app.paths.run / "instance.lock")
    assert held is not None
    try:
        info = {"port": app.server.port, "instance": app.instance}
        app.paths.pid_file.write_text(json.dumps(info), encoding="utf-8")
        assert launcher.claim_instance(app.paths, True) == 0
    finally:
        held.close()


def test_launch_waits_out_a_copy_that_is_shutting_down(tmp_path: Path) -> None:
    paths = Paths(tmp_path / "data")
    paths.ensure()
    held = launcher.acquire_lock(paths.run / "instance.lock")
    assert held is not None
    threading.Timer(0.8, held.close).start()  # the old copy exits a moment later
    claimed = launcher.claim_instance(paths, True)
    assert not isinstance(claimed, int)
    claimed.close()

    stuck = launcher.acquire_lock(paths.run / "instance.lock")
    assert stuck is not None
    try:
        assert launcher.claim_instance(paths, True, wait=0.5) == 1
    finally:
        stuck.close()
