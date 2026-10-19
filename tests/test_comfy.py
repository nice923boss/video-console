"""ComfyUI against a local fake server: health, a text-to-image job end to end, rejections, OOM, cancel, feed."""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import socket
import struct
import threading
import time
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Iterator

import pytest

from test_server import Client, png_bytes
from vc.app import App
from vc.comfy.client import OOM_MESSAGE, describe_prompt_error, is_oom
from vc.comfy.manager import execution_error, history_error, summarize_stats
from vc.comfy.workflows import validate_template
from vc.notext import GUARD, with_guard
from vc.settings import DEFAULTS

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
UNET = "zImageTurboQuantized_fp8E4m3fn.safetensors"
OUTPUT_PNG = png_bytes(64, 48, (30, 120, 200))
PREVIEW_PNG = png_bytes(16, 9, (250, 250, 0))
SYSTEM_STATS = {
    "system": {"os": "nt", "comfyui_version": "0.3.60", "python_version": "3.12.7 (tags/v3.12.7, Oct  1 2024)",
               "pytorch_version": "2.7.1+cu128", "ram_total": 34_000_000_000, "ram_free": 20_000_000_000},
    "devices": [{"name": "cuda:0 NVIDIA GeForce RTX 4050 Laptop GPU : cudaMallocAsync", "type": "cuda",
                 "index": 0, "vram_total": 6_442_450_944, "vram_free": 5_000_000_000}],
}
NODE_ERRORS = {
    "error": {"type": "prompt_outputs_failed_validation", "message": "Prompt outputs failed validation",
              "details": "", "extra_info": {}},
    "node_errors": {"1": {"class_type": "UNETLoader", "dependent_outputs": ["9"], "errors": [
        {"type": "value_not_in_list", "message": "Value not in list",
         "details": f"unet_name: '{UNET}' not in []", "extra_info": {}}]}},
}
OOM_ERROR = {"node_id": "7", "node_type": "KSampler", "exception_type": "torch.OutOfMemoryError",
             "exception_message": "Allocation on device 0 would exceed allowed memory. (out of memory)\n"
                                  "Currently allocated: 5.10 GiB", "traceback": []}


def _node(**combos: list[str]) -> dict:
    return {"input": {"required": {name: [values, {}] for name, values in combos.items()}}}


# just enough node definitions for the bundled zimage-turbo-t2i and flux3-klein templates to pass check_availability
KLEIN_UNET = "flux3-image-klein-int8-convrot.safetensors"
OBJECT_INFO = {
    "UNETLoader": _node(unet_name=[UNET, KLEIN_UNET], weight_dtype=["default", "fp8_e4m3fn"]),
    "CLIPLoader": _node(clip_name=["qwen_3_4b.safetensors", "qwen3vl_4b_int8_convrot.safetensors"],
                        type=["stable_diffusion", "lumina2", "flux3"], device=["default", "cpu"]),
    "VAELoader": _node(vae_name=["ae.safetensors", "flux2-vae.safetensors"]),
    **{cls: _node() for cls in ("CLIPTextEncode", "EmptySD3LatentImage", "KSampler", "VAEDecode", "SaveImage",
                                "ModelSamplingFlux", "EmptyFlux2LatentImage", "ReferenceMultiLatent", "LoadImage",
                                "PrimitiveStringMultiline", "CreateBoundingBoxes", "Flux3LayoutPrompt")},
}
# The tests in this file were written against Z-Image (its node ids), so its route stays pinned here;
# FLUX3-Klein, the default local route, is covered in test_klein.py.
ZIMAGE_ROUTES = copy.deepcopy(DEFAULTS["routes"])
ZIMAGE_ROUTES[0]["image"].update(workflow="zimage-turbo-t2i", workflowI2I="zimage-turbo-i2i")


class FakeComfy:
    """A stand-in ComfyUI on 127.0.0.1: the REST endpoints the app uses plus a /ws feed (unmasked frames).

    mode "ok" finishes every prompt with one PNG, "reject" answers POST /prompt with 400 node_errors,
    "oom" ends it with a CUDA out-of-memory execution_error, "hang" keeps it running until interrupted.
    """

    def __init__(self) -> None:
        self.mode = "ok"
        self.ws_enabled = True
        self.object_info = copy.deepcopy(OBJECT_INFO)
        self.calls: list[tuple[str, str, Any]] = []
        self.prompts: list[dict] = []
        self.ws_paths: list[str] = []
        self.history: dict[str, dict] = {}
        self.running: list[str] = []
        self._socks: list[socket.socket] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.url = f"http://127.0.0.1:{self.httpd.server_port}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self) -> None:
        self._stop.set()
        self.httpd.shutdown()
        self.httpd.server_close()

    # ---------------------------------------------------------------- feed
    def push(self, msg: dict) -> None:
        self._broadcast(0x1, json.dumps(msg).encode("utf-8"))

    def push_preview(self, image: bytes) -> None:
        self._broadcast(0x2, struct.pack(">II", 1, 2) + image)  # PREVIEW_IMAGE, format 2 = PNG

    def _broadcast(self, opcode: int, payload: bytes) -> None:
        n = len(payload)
        size = bytes([n]) if n < 126 else bytes([126]) + struct.pack("!H", n) if n < 65536 \
            else bytes([127]) + struct.pack("!Q", n)
        with self._lock:
            for sock in list(self._socks):
                try:
                    sock.sendall(bytes([0x80 | opcode]) + size + payload)
                except OSError:
                    self._socks.remove(sock)

    # ---------------------------------------------------------------- prompts
    def queue_prompt(self, body: dict) -> tuple[int, Any]:
        if self.mode == "reject":
            return 400, NODE_ERRORS
        pid = str(body.get("prompt_id") or uuid.uuid4())
        self.prompts.append(body)
        if self.mode == "hang":
            self.running.append(pid)
        elif self.mode == "oom":
            self.history[pid] = {"outputs": {}, "status": {"status_str": "error", "completed": False,
                                                           "messages": [["execution_error", OOM_ERROR]]}}
            self.push({"type": "execution_start", "data": {"prompt_id": pid}})
            self.push({"type": "execution_error", "data": {**OOM_ERROR, "prompt_id": pid}})
        else:
            image = {"filename": "vc_00001_.png", "subfolder": "vc", "type": "output"}
            self.history[pid] = {"outputs": {"9": {"images": [image]}},
                                 "status": {"status_str": "success", "completed": True, "messages": []}}
            self.push({"type": "execution_start", "data": {"prompt_id": pid}})
            self.push({"type": "executing", "data": {"node": "7", "prompt_id": pid}})
            self.push({"type": "progress", "data": {"value": 5, "max": 10, "node": "7", "prompt_id": pid}})
            self.push_preview(PREVIEW_PNG)
            self.push({"type": "executing", "data": {"node": None, "prompt_id": pid}})
        return 200, {"prompt_id": pid, "number": len(self.prompts), "node_errors": {}}

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def _send(self, status: int, payload: Any, ctype: str = "application/json") -> None:
                data = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:
                path = urllib.parse.urlsplit(self.path).path
                fake.calls.append(("GET", self.path, None))
                if path == "/ws":
                    return self._websocket()
                if path == "/system_stats":
                    return self._send(200, SYSTEM_STATS)
                if path == "/object_info":
                    return self._send(200, fake.object_info)
                if path == "/queue":
                    running = [[0, pid, {}, {}, ["9"]] for pid in fake.running]
                    return self._send(200, {"queue_running": running, "queue_pending": []})
                if path.startswith("/history/"):
                    pid = urllib.parse.unquote(path[len("/history/"):])
                    entry = fake.history.get(pid)
                    return self._send(200, {pid: entry} if entry else {})
                if path == "/view":
                    return self._send(200, OUTPUT_PNG, "image/png")
                self._send(404, {"error": "not found"})

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                path = urllib.parse.urlsplit(self.path).path
                is_json = self.headers.get("Content-Type", "").startswith("application/json")
                body = json.loads(raw) if raw and is_json else raw
                fake.calls.append(("POST", path, body))
                if path == "/prompt":
                    return self._send(*fake.queue_prompt(body))
                if path == "/upload/image":
                    return self._send(200, {"name": "vc_ref.png", "subfolder": "vc", "type": "input"})
                if path == "/interrupt":
                    fake.running.clear()
                if path in ("/queue", "/interrupt", "/free"):
                    return self._send(200, {})
                self._send(404, {"error": "not found"})

            def _websocket(self) -> None:
                key = self.headers.get("Sec-WebSocket-Key", "")
                if not fake.ws_enabled or self.headers.get("Upgrade", "").lower() != "websocket" or not key:
                    return self._send(404, {"error": "no feed"})
                accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode("ascii")).digest()).decode("ascii")
                self.close_connection = True
                sock = self.connection
                with fake._lock:  # registered before anyone can push, so no frame is lost or sent early
                    sock.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                                  f"Connection: Upgrade\r\nSec-WebSocket-Accept: {accept}\r\n\r\n").encode("ascii"))
                    fake._socks.append(sock)
                    fake.ws_paths.append(self.path)
                sock.settimeout(0.5)
                try:
                    while not fake._stop.is_set():  # client frames (pings, close) are read and dropped
                        try:
                            data = sock.recv(4096)
                        except socket.timeout:
                            continue
                        if not data or data[0] & 0x0F == 0x8:
                            break
                except OSError:
                    pass
                finally:
                    with fake._lock:
                        if sock in fake._socks:
                            fake._socks.remove(sock)

            def log_message(self, *args: Any) -> None:
                pass

        return Handler


def wait_until(check: Callable[[], bool], timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not check():
        assert time.monotonic() < deadline, "condition not reached in time"
        time.sleep(0.05)


@pytest.fixture()
def fake() -> Iterator[FakeComfy]:
    server = FakeComfy()
    yield server
    server.close()


@pytest.fixture()
def app(tmp_path: Path, fake: FakeComfy) -> Iterator[App]:
    data = tmp_path / "data"
    data.mkdir()
    servers = [{"id": "fake", "name": "Fake ComfyUI", "url": fake.url, "enabled": True}]
    settings = {"comfy": {"servers": servers}, "routes": ZIMAGE_ROUTES}
    (data / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    instance = App(data)
    # invalid settings would fall back to the defaults (the real machines): prove ours were taken
    assert [s["url"] for s in instance.comfy.servers()] == [fake.url]
    instance.start(port=0, watchdog=False)
    yield instance
    instance.stop()


@pytest.fixture()
def client(app: App) -> Client:
    return Client(app.server.port)


@pytest.fixture()
def project(client: Client) -> dict:
    return client.json("POST", "/api/projects", {"name": "漁港測試"})["project"]


def submit_image(client: Client, project: dict, prompt: str = "清晨漁港，薄霧，漁船", **extra: Any) -> dict:
    body = {"kind": "image", "pid": project["id"], "shotId": project["shots"][0]["id"],
            "payload": {"prompt": prompt, **extra}}
    return client.json("POST", "/api/jobs", body)["job"]


# ---------------------------------------------------------------- pure parsing
def test_summarize_stats_keeps_gpu_name_python_version_and_memory() -> None:
    info = summarize_stats(SYSTEM_STATS)
    assert info["device"] == {"name": "NVIDIA GeForce RTX 4050 Laptop GPU", "type": "cuda",
                              "vramTotal": 6_442_450_944, "vramFree": 5_000_000_000}
    assert (info["version"], info["python"], info["pytorch"]) == ("0.3.60", "3.12.7", "2.7.1+cu128")
    assert info["ram"] == {"total": 34_000_000_000, "free": 20_000_000_000}
    cpu_only = summarize_stats({"devices": [{"name": "cpu", "type": "cpu"}]})
    assert cpu_only["device"]["name"] == "cpu" and cpu_only["version"] == ""


def test_oom_and_execution_errors_are_classified() -> None:
    assert is_oom("CUDA out of memory. Tried to allocate 2.00 GiB")
    assert is_oom("torch.OutOfMemoryError") and is_oom("Allocation on device 0 would exceed allowed memory")
    assert not is_oom("Shape mismatch") and not is_oom("")
    assert execution_error(OOM_ERROR).message == OOM_MESSAGE
    other = execution_error({"node_id": "7", "node_type": "KSampler", "exception_message": "Shape mismatch\nat x"})
    assert other.message == "ComfyUI 執行失敗（節點 #7 KSampler）：Shape mismatch"
    assert history_error({"status": {"status_str": "error", "messages": []}}).message == "ComfyUI 回報執行失敗"
    assert history_error({"status": {"messages": [["execution_interrupted", {}]]}}).status == 409
    assert history_error({"status": {"status_str": "success", "messages": []}}) is None
    assert describe_prompt_error("not json") == "ComfyUI 拒絕了這個工作流程"


# ---------------------------------------------------------------- through the app
def test_health_check_reports_the_fake_server_and_its_models(client: Client, fake: FakeComfy) -> None:
    state = client.json("POST", "/api/comfy/fake/refresh")
    assert state["online"] is True and state["error"] == ""
    assert state["device"]["name"] == "NVIDIA GeForce RTX 4050 Laptop GPU"
    assert state["version"] == "0.3.60" and state["queue"] == {"running": 0, "pending": 0}
    [listed] = client.json("GET", "/api/comfy")
    assert listed["url"] == fake.url and listed["hasToken"] is False
    assert UNET in client.json("GET", "/api/comfy/fake/models")["diffusion_models"]
    assert client.json("GET", "/api/comfy/fake/templates")["zimage-turbo-t2i"]["ok"] is True


def test_text_to_image_job_ends_done_with_the_png_as_the_shot_picture(app: App, client: Client, project: dict,
                                                                      fake: FakeComfy) -> None:
    job = submit_image(client, project)
    done = client.wait_job(job["id"], timeout=30)
    assert done["status"] == "done", done["error"]
    assert done["message"] == "生圖完成" and done["remote"] is None
    assert done["previewRev"] >= 1  # the binary preview frame from /ws reached the job

    [queued] = fake.prompts
    assert queued["client_id"] == app.comfy.client_id
    assert "清晨漁港" in queued["prompt"]["4"]["inputs"]["text"]
    assert queued["prompt"]["9"]["inputs"]["filename_prefix"].startswith(f"vc/{job['id']}-")
    assert fake.ws_paths[0] == "/ws?" + urllib.parse.urlencode({"clientId": app.comfy.client_id})
    views = [path for method, path, _ in fake.calls if path.startswith("/view")]
    assert views == ["/view?filename=vc_00001_.png&subfolder=vc&type=output"]

    saved = client.json("GET", f"/api/projects/{project['id']}")["project"]
    tid = saved["shots"][0]["activeImage"]
    assert done["result"]["takeIds"] == [tid]
    take = saved["takes"][tid]
    assert take["kind"] == "image" and (take["width"], take["height"]) == (64, 48)
    assert take["source"]["backend"] == "comfy" and take["source"]["server"] == "fake"
    assert take["source"]["workflow"] == "zimage-turbo-t2i"
    stored = app.store.project_dir(project["id"]) / take["file"]
    assert stored.read_bytes() == OUTPUT_PNG


def test_rejected_prompt_fails_with_the_node_errors_explained(client: Client, project: dict,
                                                              fake: FakeComfy) -> None:
    fake.mode = "reject"
    done = client.wait_job(submit_image(client, project)["id"], timeout=30)
    assert done["status"] == "failed"
    assert done["error"] == ("工作流程驗證失敗：節點 #1（UNETLoader）選項不存在（多半是缺少模型檔，或自訂節點版本不同）："
                             f"unet_name: '{UNET}' not in []")


def test_missing_model_stops_the_job_before_anything_is_queued(client: Client, project: dict,
                                                               fake: FakeComfy) -> None:
    fake.object_info["UNETLoader"] = _node(unet_name=["other.safetensors"], weight_dtype=["default"])
    done = client.wait_job(submit_image(client, project)["id"], timeout=30)
    assert done["status"] == "failed"
    assert f"缺少 {UNET}" in done["error"]
    assert not [c for c in fake.calls if c[1] == "/prompt"]


@pytest.mark.parametrize("feed", [True, False], ids=["websocket", "polling"])
def test_out_of_memory_fails_with_the_oom_advice(client: Client, project: dict, fake: FakeComfy, feed: bool) -> None:
    # without the feed the error is found by polling /history, so both paths are covered
    fake.mode, fake.ws_enabled = "oom", feed
    done = client.wait_job(submit_image(client, project)["id"], timeout=30)
    assert done["status"] == "failed"
    assert done["error"] == OOM_MESSAGE
    assert done["remote"]["type"] == "comfy" and done["remote"]["server"] == "fake"
    assert bool(fake.ws_paths) is feed


def test_cancel_removes_the_prompt_and_interrupts_it(client: Client, project: dict, fake: FakeComfy) -> None:
    fake.mode = "hang"
    job = submit_image(client, project)
    wait_until(lambda: bool(fake.prompts))
    pid = fake.prompts[0]["prompt_id"]
    wait_until(lambda: (client.json("GET", f"/api/jobs/{job['id']}")["remote"] or {}).get("promptId") == pid)
    client.json("POST", f"/api/jobs/{job['id']}/cancel")
    done = client.wait_job(job["id"], timeout=30)
    assert done["status"] == "canceled" and done["remote"] is None
    assert ("POST", "/queue", {"delete": [pid]}) in fake.calls
    assert ("POST", "/interrupt", {"prompt_id": pid}) in fake.calls
    assert fake.running == []


def test_manager_run_reports_feed_progress_and_previews(app: App, fake: FakeComfy, tmp_path: Path) -> None:
    graph = copy.deepcopy(app.workflows.get("zimage-turbo-t2i")["graph"])
    messages: list[str] = []
    previews: list[tuple[str, bytes]] = []
    results = app.comfy.run("fake", graph, dst_dir=tmp_path / "out", outputs=["9"],
                            on_progress=lambda _frac, msg: messages.append(msg),
                            on_preview=lambda mime, data: previews.append((mime, data)))
    assert "開始執行" in messages and "取樣（5/10）" in messages
    assert previews == [("image/png", PREVIEW_PNG)]
    [result] = results
    assert result["kind"] == "image" and result["node"] == "9"
    assert Path(result["path"]).read_bytes() == OUTPUT_PNG


def test_engine_buttons_reach_comfy(app: App, client: Client, fake: FakeComfy) -> None:
    assert client.json("POST", "/api/comfy/fake/interrupt") == {"ok": True}
    assert client.json("POST", "/api/comfy/fake/free") == {"ok": True}
    assert ("POST", "/interrupt", {}) in fake.calls
    assert ("POST", "/free", {"unload_models": True, "free_memory": True}) in fake.calls
    assert app.comfy.upload_image("fake", OUTPUT_PNG) == "vc/vc_ref.png"
    [upload] = [body for method, path, body in fake.calls if path == "/upload/image"]
    assert OUTPUT_PNG in upload and b'name="subfolder"' in upload


def test_ltx_video_job_runs_at_480p_for_the_chosen_seconds(app: App, client: Client, project: dict) -> None:
    done = client.wait_job(submit_image(client, project)["id"], timeout=30)
    assert done["status"] == "done", done["error"]
    pid, sid = project["id"], project["shots"][0]["id"]
    request = {"workflow": "ltxv-2b-i2v"}

    def set_seconds(seconds: float) -> None:
        client.json("POST", f"/api/projects/{pid}/ops",
                    {"ops": [{"op": "shot.update", "id": sid, "patch": {"videoDuration": seconds}}]})

    job = app.runners.prepare("video", pid, sid, request)
    plan = job["payload"]
    assert (plan["width"], plan["height"]) == (864, 480)  # 1280x720 scaled to a 480 short side, snapped to 32
    assert "maxShortSide" not in plan
    assert plan["fps"] == 24 and plan["frames"] == 121  # the 5 s shot length, as 8n+1 frames
    assert plan["duration"] == round(121 / 24, 2)
    assert any("短邊上限是 480" in note for note in job["notes"])

    set_seconds(3)
    assert app.runners.prepare("video", pid, sid, request)["payload"]["frames"] == 73

    set_seconds(30)
    most = app.workflows.get("ltxv-2b-i2v")["maxFrames"]
    capped = app.runners.prepare("video", pid, sid, request)
    assert capped["payload"]["frames"] == most and (most - 1) % 8 == 0
    assert any("一次最多" in note for note in capped["notes"])


# ---------------------------------------------------------------- templates that read English only
CHAT = "/v1/chat/completions"


def english_only_copy(app: App) -> str:
    """A user copy of the image template flagged like LTX-Video, so the fake server can run it."""
    app.workflows.save({**app.workflows.get("zimage-turbo-t2i"), "id": "zimage-en", "promptLanguage": "en"})
    return "zimage-en"


def switch_on_llm(app: App, mock_http: Any, answer: tuple[int, Any]) -> None:
    mock_http.route("POST", CHAT, answer)
    app.settings.update({"llm": {"enabled": True, "baseUrl": mock_http.url + "/v1"}})


def test_chinese_prompt_is_translated_before_an_english_only_template(app: App, client: Client, project: dict,
                                                                      fake: FakeComfy, mock_http: Any) -> None:
    reply = {"choices": [{"message": {"role": "assistant", "content": "Misty fishing harbour at dawn, boats"}}]}
    switch_on_llm(app, mock_http, (200, reply))
    done = client.wait_job(submit_image(client, project, workflow=english_only_copy(app))["id"], timeout=30)
    assert done["status"] == "done", done["error"]

    [call] = mock_http.calls
    assert call.json()["messages"][1]["content"] == "清晨漁港，薄霧，漁船"
    [queued] = fake.prompts
    assert queued["prompt"]["4"]["inputs"]["text"] == with_guard("Misty fishing harbour at dawn, boats")
    assert done["payload"]["prompt"] == "Misty fishing harbour at dawn, boats"
    assert done["payload"]["promptOriginal"] == "清晨漁港，薄霧，漁船"
    assert "這個範本只讀得懂英文，提示詞已用文字模型翻成英文再送出" in done["notes"]

    saved = client.json("GET", f"/api/projects/{project['id']}")["project"]
    [take] = saved["takes"].values()
    assert (take["prompt"], take["promptOriginal"]) == ("Misty fishing harbour at dawn, boats", "清晨漁港，薄霧，漁船")


@pytest.mark.parametrize("answer, reason", [
    (None, "尚未開啟文字模型"),
    ((500, {"error": {"message": "upstream down"}}), "LLM 呼叫失敗"),
], ids=["switched-off", "server-error"])
def test_untranslated_prompt_is_sent_as_written_with_a_note(app: App, client: Client, project: dict, fake: FakeComfy,
                                                            mock_http: Any, answer: Any, reason: str) -> None:
    if answer:
        switch_on_llm(app, mock_http, answer)
    done = client.wait_job(submit_image(client, project, workflow=english_only_copy(app))["id"], timeout=30)
    assert done["status"] == "done", done["error"]

    [queued] = fake.prompts
    assert queued["prompt"]["4"]["inputs"]["text"] == with_guard("清晨漁港，薄霧，漁船")
    assert "promptOriginal" not in done["payload"]
    [note] = [n for n in done["notes"] if "沒有翻譯" in n]
    assert note.startswith("這個範本只讀得懂英文，提示詞含中文但沒有翻譯，照原文送出") and reason in note
    saved = client.json("GET", f"/api/projects/{project['id']}")["project"]
    assert all("promptOriginal" not in t for t in saved["takes"].values())


def test_english_prompt_or_a_bilingual_template_never_calls_the_llm(app: App, client: Client, project: dict,
                                                                    fake: FakeComfy, mock_http: Any) -> None:
    switch_on_llm(app, mock_http, (500, {}))
    english = client.wait_job(submit_image(client, project, "misty harbour", workflow=english_only_copy(app))["id"],
                              timeout=30)
    bilingual = client.wait_job(submit_image(client, project)["id"], timeout=30)  # Z-Image reads Chinese

    assert english["status"] == bilingual["status"] == "done"
    assert [p["prompt"]["4"]["inputs"]["text"] for p in fake.prompts] == [with_guard("misty harbour"),
                                                                   with_guard("清晨漁港，薄霧，漁船")]
    assert mock_http.calls == []
    assert not [n for job in (english, bilingual) for n in job["notes"] if "英文" in n]


def test_only_ltx_is_flagged_english_only_and_the_flag_is_normalized(app: App) -> None:
    flagged = {t["id"] for t in app.workflows.list() if t["promptLanguage"] == "en"}
    assert "ltxv-2b-i2v" in flagged and "zimage-turbo-t2i" not in flagged
    tpl = app.workflows.get("zimage-turbo-t2i")
    assert validate_template({**tpl, "promptLanguage": "zh"})["promptLanguage"] == ""
    assert validate_template({**tpl, "promptLanguage": "en"})["promptLanguage"] == "en"


def test_image_prompt_goes_out_as_written(app: App, project: dict) -> None:
    pid, sid = project["id"], project["shots"][0]["id"]
    app.store.apply(pid, [{"op": "project.update", "patch": {"style": {"prompt": "cinematic film still"}}},
                          {"op": "shot.update", "id": sid, "patch": {"prompt": "清晨漁港"}}])
    # empty box: the description plus the project style
    assert app.runners.prepare("image", pid, sid, {})["payload"]["prompt"] == "清晨漁港\ncinematic film still"
    # the shot's own picture prompt is sent unchanged, style not added again
    app.store.apply(pid, [{"op": "shot.update", "id": sid, "patch": {"imagePrompt": "  漁港黃昏，逆光  "}}])
    assert app.store.get(pid)["shots"][0]["imagePrompt"] == "  漁港黃昏，逆光  "
    assert app.runners.prepare("image", pid, sid, {})["payload"]["prompt"] == "漁港黃昏，逆光"
    # no description at all: the box alone is enough
    app.store.apply(pid, [{"op": "shot.update", "id": sid, "patch": {"prompt": ""}}])
    assert app.runners.prepare("image", pid, sid, {})["payload"]["prompt"] == "漁港黃昏，逆光"


# ---------------------------------------------------------------- no lettering in generated pictures
TITLE_CARD_SHOT = "【空拍運鏡】\n1. 高空俯瞰宜蘭平原的稻田與校舍。\n2. 晨光灑落校園操場，學生陸續走進校門。\n5. 標題字卡浮現"
CLEAN_SHOT = "【空拍運鏡】\n1. 高空俯瞰宜蘭平原的稻田與校舍。\n2. 晨光灑落校園操場，學生陸續走進校門。"
DROPPED_NOTE = "畫面不畫字卡與字幕（請在剪輯台加），已略過提示詞裡的：「標題字卡浮現」"


def test_title_card_direction_is_dropped_and_the_sent_prompt_forbids_text(app: App, client: Client, project: dict,
                                                                         fake: FakeComfy) -> None:
    pid, sid = project["id"], project["shots"][0]["id"]
    picture = TITLE_CARD_SHOT + "\ncinematic film still"
    app.store.apply(pid, [{"op": "shot.update", "id": sid, "patch": {"prompt": TITLE_CARD_SHOT, "imagePrompt": picture}}])
    done = client.wait_job(submit_image(client, project, prompt="")["id"], timeout=30)
    assert done["status"] == "done", done["error"]

    [queued] = fake.prompts
    sent = queued["prompt"]["4"]["inputs"]["text"]
    assert sent == f"{CLEAN_SHOT}\ncinematic film still\n{GUARD}"
    assert "字卡" not in sent and "標題" not in sent
    negative = queued["prompt"]["5"]["inputs"]["text"]
    assert all(term in negative for term in ("text", "subtitles", "captions", "title card", "watermark", "logo"))
    assert DROPPED_NOTE in done["notes"]
    # the job and its take keep the cleaned prompt without the clause, so a retry never doubles it
    assert done["payload"]["prompt"] == f"{CLEAN_SHOT}\ncinematic film still"
    saved = client.json("GET", f"/api/projects/{pid}")["project"]
    [take] = saved["takes"].values()
    assert take["prompt"] == f"{CLEAN_SHOT}\ncinematic film still"
    assert saved["shots"][0]["imagePrompt"] == picture  # the user's own text is left as written


def test_prompt_with_only_title_directions_is_refused(app: App, client: Client, project: dict,
                                                      fake: FakeComfy) -> None:
    pid, sid = project["id"], project["shots"][0]["id"]
    app.store.apply(pid, [{"op": "shot.update", "id": sid, "patch": {"imagePrompt": "【字卡】\n片名：海港"}}])
    reply = client.json("POST", "/api/jobs", {"kind": "image", "pid": pid, "shotId": sid, "payload": {}}, status=400)
    assert "提示詞只有字卡或字幕的指示" in json.dumps(reply, ensure_ascii=False)
    assert fake.prompts == []


def test_video_and_upscale_prompts_drop_title_directions_too(app: App, client: Client, project: dict,
                                                             fake: FakeComfy) -> None:
    pid, sid = project["id"], project["shots"][0]["id"]
    assert client.wait_job(submit_image(client, project)["id"], timeout=30)["status"] == "done"

    video = app.runners.prepare("video", pid, sid, {"workflow": "ltxv-2b-i2v", "prompt": TITLE_CARD_SHOT})
    assert video["payload"]["prompt"] == CLEAN_SHOT
    assert DROPPED_NOTE in video["notes"]

    # an older take may still carry the direction in its prompt
    project_now = copy.deepcopy(app.store.get(pid))
    tid = project_now["shots"][0]["activeImage"]
    project_now["takes"][tid]["prompt"] = TITLE_CARD_SHOT
    refine = app.runners._prepare_upscale(project_now, sid, {"workflow": "upscale-refine-zimage"})
    assert refine["payload"]["prompt"] == CLEAN_SHOT and refine["notes"] == [DROPPED_NOTE]
    plain = app.runners._prepare_upscale(project_now, sid, {"workflow": "upscale-ultrasharp"})
    assert plain["notes"] == []  # this template has no prompt, so there is nothing to explain
