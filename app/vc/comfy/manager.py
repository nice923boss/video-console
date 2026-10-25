"""ComfyUI server registry: health checks, cached node info, live progress feed and prompt execution."""
from __future__ import annotations

import logging
import queue
import re
import threading
import time
import urllib.parse
import uuid
from pathlib import Path
from typing import Any, Callable

from ..events import EventBus
from ..secrets import SecretStore
from ..settings import SettingsStore
from ..util import AppError, kind_of, now, safe_filename
from . import workflows as wf
from .client import OOM_MESSAGE, ComfyClient, is_oom
from .ws import WebSocket, WSClosed, parse_message

log = logging.getLogger("vc.comfy")

HEALTH_INTERVAL = 10.0
OBJECT_INFO_TTL = 600.0
OFFLINE_GRACE = 60.0

ProgressFn = Callable[[float, str], None]
PreviewFn = Callable[[str, bytes], None]
CancelFn = Callable[[], bool]

_NODE_LABELS = {
    "KSampler": "取樣",
    "KSamplerAdvanced": "取樣",
    "SamplerCustom": "取樣",
    "SamplerCustomAdvanced": "取樣",
    "VAEDecode": "解碼畫面",
    "VAEDecodeTiled": "解碼畫面",
    "VAEEncode": "編碼參考圖",
    "VAEEncodeTiled": "編碼參考圖",
    "CLIPTextEncode": "編碼提示詞",
    "UNETLoader": "載入模型",
    "CheckpointLoaderSimple": "載入模型",
    "CLIPLoader": "載入文字編碼器",
    "VAELoader": "載入 VAE",
    "LoraLoaderModelOnly": "載入 LoRA",
    "LoraLoader": "載入 LoRA",
    "UpscaleModelLoader": "載入放大模型",
    "ImageUpscaleWithModel": "放大畫面",
    "LoadImage": "讀取輸入圖",
    "SaveImage": "儲存輸出",
    "SaveVideo": "儲存影片",
    "CreateVideo": "合成影片",
    "VHS_VideoCombine": "合成影片",
    "ColorMatch": "色彩校正",
}
_MODEL_SOURCES = {
    "checkpoints": ("CheckpointLoaderSimple", "ckpt_name"),
    "diffusion_models": ("UNETLoader", "unet_name"),
    "text_encoders": ("CLIPLoader", "clip_name"),
    "vae": ("VAELoader", "vae_name"),
    "loras": ("LoraLoaderModelOnly", "lora_name"),
    "upscale_models": ("UpscaleModelLoader", "model_name"),
}


# ------------------------------------------------------------------ helpers
def _short_device(name: Any) -> str:
    text = str(name or "")
    text = re.sub(r"^cuda:\d+\s+", "", text)
    return text.split(" : ")[0].strip()


def summarize_stats(stats: dict) -> dict:
    """Reduce /system_stats to what the UI shows."""
    system = stats.get("system") or {}
    devices = [d for d in stats.get("devices") or [] if isinstance(d, dict)]
    gpu = next((d for d in devices if d.get("type") != "cpu"), devices[0] if devices else None)
    device = None
    if gpu:
        device = {
            "name": _short_device(gpu.get("name")),
            "type": str(gpu.get("type") or ""),
            "vramTotal": int(gpu.get("vram_total") or 0),
            "vramFree": int(gpu.get("vram_free") or 0),
        }
    return {
        "version": str(system.get("comfyui_version") or ""),
        "python": str(system.get("python_version") or "").split(" ")[0],
        "pytorch": str(system.get("pytorch_version") or ""),
        "ram": {"total": int(system.get("ram_total") or 0), "free": int(system.get("ram_free") or 0)},
        "device": device,
    }


def queue_state(q: dict, prompt_id: str) -> tuple[str, int]:
    """("running", 0), ("pending", tasks ahead) or ("missing", 0) for a prompt in a /queue response."""
    running = [item for item in q.get("queue_running") or [] if isinstance(item, list) and len(item) > 1]
    if any(item[1] == prompt_id for item in running):
        return "running", 0
    pending = sorted((item for item in q.get("queue_pending") or [] if isinstance(item, list) and len(item) > 1),
                     key=lambda item: item[0] if isinstance(item[0], (int, float)) else 0)
    for idx, item in enumerate(pending):
        if item[1] == prompt_id:
            return "pending", len(running) + idx
    return "missing", 0


def execution_error(data: dict) -> AppError:
    text = str(data.get("exception_message") or "").strip()
    if is_oom(text) or is_oom(str(data.get("exception_type") or "")):
        return AppError(OOM_MESSAGE, 502)
    first = text.splitlines()[0] if text else "未知錯誤"
    node = f"節點 #{data.get('node_id', '')} {data.get('node_type', '')}".strip()
    return AppError(f"ComfyUI 執行失敗（{node}）：{first[:300]}", 502)


def history_error(entry: dict) -> AppError | None:
    status = entry.get("status") or {}
    for item in status.get("messages") or []:
        if isinstance(item, list) and len(item) == 2:
            if item[0] == "execution_error":
                return execution_error(item[1] or {})
            if item[0] == "execution_interrupted":
                return AppError("這個任務在 ComfyUI 端被中斷了", 409)
    if status.get("status_str") == "error":
        return AppError("ComfyUI 回報執行失敗", 502)
    return None


def _node_label(graph: dict | None, nid: str) -> str:
    node = (graph or {}).get(nid) or {}
    cls = node.get("class_type", "")
    title = (node.get("_meta") or {}).get("title", "")
    return _NODE_LABELS.get(cls) or title or cls or f"節點 #{nid}"


def _node_sort(nid: str) -> tuple[int, str]:
    return (int(nid), "") if nid.isdigit() else (1 << 30, nid)


# ------------------------------------------------------------------ live feed
class _Hub:
    """One WebSocket per ComfyUI server; routes execution messages to the prompts waiting on them."""

    IDLE_CLOSE = 45.0

    def __init__(self, sid: str, url: str, token: str, client_id: str):
        self.sid = sid
        self.url = url
        self.token = token
        self.client_id = client_id
        self.current: str | None = None
        self._lock = threading.Lock()
        self._listeners: dict[str, queue.Queue] = {}
        self._thread: threading.Thread | None = None
        self._connected = threading.Event()
        self._closed = threading.Event()
        self._ws: WebSocket | None = None

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    def listen(self, prompt_id: str) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=2000)
        with self._lock:
            self._listeners[prompt_id] = q
        return q

    def rekey(self, old: str, new: str) -> None:
        with self._lock:
            q = self._listeners.pop(old, None)
            if q is not None:
                self._listeners[new] = q

    def unlisten(self, prompt_id: str) -> None:
        with self._lock:
            self._listeners.pop(prompt_id, None)

    def ensure(self, wait: float = 0.0) -> bool:
        with self._lock:
            if not self._closed.is_set() and (self._thread is None or not self._thread.is_alive()):
                self._thread = threading.Thread(target=self._run, name=f"comfy-feed-{self.sid}", daemon=True)
                self._thread.start()
        if wait:
            self._connected.wait(wait)
        return self.connected

    def close(self) -> None:
        self._closed.set()
        ws = self._ws
        if ws is not None:
            ws.close()

    def _ws_url(self) -> str:
        parts = urllib.parse.urlsplit(self.url)
        scheme = "wss" if parts.scheme == "https" else "ws"
        query = urllib.parse.urlencode({"clientId": self.client_id})
        return urllib.parse.urlunsplit((scheme, parts.netloc, parts.path.rstrip("/") + "/ws", query, ""))

    def _has_listeners(self) -> bool:
        with self._lock:
            return bool(self._listeners)

    def _run(self) -> None:
        backoff = 1.0
        idle_since = time.monotonic()
        while not self._closed.is_set():
            if self._has_listeners():
                idle_since = time.monotonic()
            elif time.monotonic() - idle_since > self.IDLE_CLOSE:
                break
            try:
                headers = {"Authorization": f"Bearer {self.token}"} if self.token else None
                ws = WebSocket(self._ws_url(), headers=headers, timeout=8)
            except (OSError, ConnectionError, ValueError) as exc:
                log.debug("ComfyUI feed %s connect failed: %s", self.sid, exc)
                self._closed.wait(backoff)
                backoff = min(backoff * 2, 15.0)
                continue
            self._ws = ws
            self._connected.set()
            backoff = 1.0
            log.info("ComfyUI feed %s connected", self.sid)
            last_ping = time.monotonic()
            try:
                while not self._closed.is_set():
                    item = ws.recv(timeout=1.0)
                    if item is not None:
                        msg = parse_message(*item)
                        if msg:
                            self._dispatch(msg)
                    t = time.monotonic()
                    if self._has_listeners():
                        idle_since = t
                    elif t - idle_since > self.IDLE_CLOSE:
                        break
                    if t - last_ping > 20:
                        ws.ping()
                        last_ping = t
            except (WSClosed, OSError) as exc:
                log.info("ComfyUI feed %s disconnected: %s", self.sid, exc)
            finally:
                self._connected.clear()
                self._ws = None
                ws.close()
            if not self._has_listeners() and time.monotonic() - idle_since > self.IDLE_CLOSE:
                break
        self._connected.clear()

    def _dispatch(self, msg: dict) -> None:
        mtype = msg.get("type")
        data = msg.get("data") if isinstance(msg.get("data"), dict) else {}
        if mtype == "preview":
            pid = self.current
        else:
            pid = data.get("prompt_id")
            if mtype == "execution_start":
                self.current = pid
            elif mtype == "executing":
                if data.get("node") is not None:
                    self.current = pid
                elif self.current == pid:
                    self.current = None
            elif mtype in ("execution_success", "execution_error", "execution_interrupted") and self.current == pid:
                self.current = None
        if not pid:
            return
        with self._lock:
            q = self._listeners.get(pid)
        if q is not None:
            try:
                q.put_nowait(msg)
            except queue.Full:
                pass


# ------------------------------------------------------------------ manager
class ComfyManager:
    def __init__(self, settings: SettingsStore, secrets: SecretStore, bus: EventBus | None, client_id: str):
        self.settings = settings
        self.secrets = secrets
        self.bus = bus
        self.client_id = client_id
        self._lock = threading.Lock()
        self._states: dict[str, dict] = {}
        self._info: dict[str, tuple[float, dict]] = {}
        self._info_locks: dict[str, threading.Lock] = {}
        self._hubs: dict[str, _Hub] = {}
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_payload: list | None = None

    # ---------------------------------------------------------------- config
    def servers(self) -> list[dict]:
        return list(self.settings.section("comfy")["servers"])

    def server(self, sid: str) -> dict:
        for s in self.servers():
            if s["id"] == sid:
                return s
        raise AppError(f"找不到 ComfyUI 伺服器設定：{sid}", 404)

    def token(self, sid: str) -> str:
        return self.secrets.get(f"comfy.{sid}.token")

    def client(self, sid: str, timeout: float = 30) -> ComfyClient:
        return ComfyClient(self.server(sid)["url"], self.token(sid), timeout)

    def _extra_data(self) -> dict | None:
        if self.settings.section("comfy").get("useComfyOrgKey"):
            key = self.secrets.get("comfyorg.api_key")
            if key:
                return {"api_key_comfy_org": key}
        return None

    # ---------------------------------------------------------------- health
    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._health_loop, name="comfy-health", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        with self._lock:
            hubs = list(self._hubs.values())
        for hub in hubs:
            hub.close()

    def poke(self) -> None:
        """Re-check servers now (after settings changed)."""
        self._wake.set()

    def _health_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.check_all()
            except Exception:  # noqa: BLE001 - the monitor must keep running
                log.exception("ComfyUI health check crashed")
            self._wake.wait(HEALTH_INTERVAL)
            self._wake.clear()

    def check_all(self) -> list[dict]:
        servers = self.servers()
        threads = [threading.Thread(target=self._check, args=(s,), daemon=True) for s in servers]
        for t in threads:
            t.start()
        for t in threads:
            t.join(12)
        with self._lock:
            ids = {s["id"] for s in servers}
            for sid in list(self._states):
                if sid not in ids:
                    self._states.pop(sid, None)
                    self._info.pop(sid, None)
        self._publish()
        return self.status()

    def _check(self, server: dict) -> dict:
        sid = server["id"]
        state = {
            "id": sid, "online": False, "checked": now(), "error": "", "latencyMs": None, "version": "",
            "python": "", "pytorch": "", "ram": None, "device": None, "queue": {"running": 0, "pending": 0},
        }
        if not server.get("enabled", True):
            state["error"] = "已停用"
        else:
            try:
                client = ComfyClient(server["url"], self.token(sid), timeout=6)
                started = time.monotonic()
                stats = client.system_stats(timeout=5)
                state["latencyMs"] = int((time.monotonic() - started) * 1000)
                q = client.queue()
                state.update(summarize_stats(stats))
                state["queue"] = {"running": len(q.get("queue_running") or []),
                                  "pending": len(q.get("queue_pending") or [])}
                state["online"] = True
            except AppError as exc:
                state["error"] = exc.message
            except Exception as exc:  # noqa: BLE001 - surface anything unexpected in the UI instead of crashing
                log.exception("Unexpected error checking ComfyUI %s", sid)
                state["error"] = f"檢查失敗：{exc}"
        state["url"] = server["url"]
        with self._lock:
            prev = self._states.get(sid)
            if prev and (prev.get("url") != state["url"] or (state["online"] and not prev.get("online"))):
                self._info.pop(sid, None)  # restarted or moved: node list may differ
            self._states[sid] = state
        return state

    def status(self) -> list[dict]:
        with self._lock:
            states = dict(self._states)
            hubs = dict(self._hubs)
        out = []
        for s in self.servers():
            st = states.get(s["id"]) or {"id": s["id"], "online": False, "checked": 0, "error": "尚未檢查",
                                         "latencyMs": None, "version": "", "device": None, "ram": None,
                                         "queue": {"running": 0, "pending": 0}}
            hub = hubs.get(s["id"])
            out.append({**st, "name": s["name"], "url": s["url"], "enabled": s.get("enabled", True),
                        "hasToken": bool(self.token(s["id"])), "feed": bool(hub and hub.connected)})
        return out

    def _publish(self) -> None:
        payload = self.status()
        comparable = [{k: v for k, v in s.items() if k not in ("checked", "latencyMs")} for s in payload]
        if comparable == self._last_payload:
            return
        self._last_payload = comparable
        if self.bus:
            self.bus.publish("comfy", payload)

    def refresh(self, sid: str) -> dict:
        with self._lock:
            self._info.pop(sid, None)
        state = self._check(self.server(sid))
        self._publish()
        return state

    def is_online(self, sid: str) -> bool:
        with self._lock:
            return bool((self._states.get(sid) or {}).get("online"))

    def test_connection(self, url: str, token: str = "") -> dict:
        url = (url or "").strip().rstrip("/")
        if not re.match(r"^https?://[^\s/]+", url):
            raise AppError("ComfyUI 位址格式不正確，例如 http://127.0.0.1:8000")
        client = ComfyClient(url, token, timeout=8)
        started = time.monotonic()
        stats = client.system_stats(timeout=8)
        return {"ok": True, "latencyMs": int((time.monotonic() - started) * 1000), **summarize_stats(stats)}

    # ---------------------------------------------------------------- node info
    def object_info(self, sid: str, force: bool = False) -> dict:
        with self._lock:
            lock = self._info_locks.setdefault(sid, threading.Lock())
        with lock:
            with self._lock:
                cached = self._info.get(sid)
            if cached and not force and time.monotonic() - cached[0] < OBJECT_INFO_TTL:
                return cached[1]
            info = self.client(sid).object_info()
            if not isinstance(info, dict) or not info:
                raise AppError("ComfyUI 沒有回傳節點清單", 502)
            with self._lock:
                self._info[sid] = (time.monotonic(), info)
            return info

    def check_template(self, sid: str, template: dict, force: bool = False) -> dict:
        return wf.check_availability(template, self.object_info(sid, force))

    def template_report(self, sid: str, templates: list[dict]) -> dict[str, dict]:
        info = self.object_info(sid)
        return {t["id"]: wf.check_availability(t, info) for t in templates}

    def models(self, sid: str) -> dict[str, list[str]]:
        info = self.object_info(sid)
        out: dict[str, list[str]] = {}
        for folder, (cls, name) in _MODEL_SOURCES.items():
            spec = info.get(cls)
            options: list[str] = []
            if spec:
                for input_name, typ, cfg in wf.input_specs(spec):
                    if input_name == name:
                        options = [str(o) for o in wf.combo_options(typ, cfg) or []]
            out[folder] = options
        return out

    def pick_server(self, preferred: str = "auto", template: dict | None = None) -> str:
        """Resolve "auto" to the first online server (settings order) that has everything the template needs."""
        if preferred and preferred != "auto":
            server = self.server(preferred)
            if not server.get("enabled", True):
                raise AppError(f"ComfyUI 伺服器「{server['name']}」已停用，請到「引擎 › ComfyUI」啟用", 409)
            return preferred
        servers = [s for s in self.servers() if s.get("enabled", True)]
        if not servers:
            raise AppError("沒有啟用中的 ComfyUI 伺服器，請到「引擎 › ComfyUI」新增或啟用", 409)
        online = [s for s in servers if self.is_online(s["id"])]
        if not online:
            for s in servers:
                self._check(s)
            self._publish()
            online = [s for s in servers if self.is_online(s["id"])]
        if not online:
            raise AppError("目前連不上任何 ComfyUI：請確認 ComfyUI 已啟動，或到「引擎」頁檢查伺服器位址", 503)
        if template is not None:
            for s in online:
                try:
                    if self.check_template(s["id"], template)["ok"]:
                        return s["id"]
                except AppError as exc:
                    log.info("Skipping ComfyUI %s for %s: %s", s["id"], template.get("id"), exc.message)
        return online[0]["id"]

    # ---------------------------------------------------------------- actions
    def free(self, sid: str) -> None:
        self.client(sid).free()

    def interrupt(self, sid: str) -> None:
        self.client(sid).interrupt()

    def upload_image(self, sid: str, data: bytes, ext: str = "") -> str:
        return self.client(sid).upload_image(data, ext)

    def _hub(self, sid: str) -> _Hub:
        server = self.server(sid)
        token = self.token(sid)
        with self._lock:
            hub = self._hubs.get(sid)
            if hub is None or hub.url != server["url"] or hub.token != token:
                if hub is not None:
                    hub.close()
                hub = _Hub(sid, server["url"], token, self.client_id)
                self._hubs[sid] = hub
            return hub

    def _cancel(self, client: ComfyClient, prompt_id: str) -> None:
        try:
            client.delete_queued([prompt_id])
            state, _ = queue_state(client.queue(), prompt_id)
            if state == "running":
                client.interrupt(prompt_id)
        except AppError as exc:
            log.warning("Could not cancel ComfyUI prompt %s: %s", prompt_id, exc.message)

    def run(self, sid: str, graph: dict, *, dst_dir: Path, outputs: list[str] | None = None,
            on_progress: ProgressFn | None = None, on_preview: PreviewFn | None = None,
            cancelled: CancelFn | None = None,
            on_submitted: Callable[[str, bool], None] | None = None) -> list[dict]:
        """Queue a graph, follow it to the end and download its outputs into dst_dir.

        on_submitted(prompt_id, sent) is called once before the graph goes out and once after ComfyUI took it,
        so a restart in between can ask ComfyUI whether it arrived instead of sending it twice (LG-03).
        """
        client = self.client(sid)
        hub = self._hub(sid)
        prompt_id = str(uuid.uuid4())
        events = hub.listen(prompt_id)
        try:
            hub.ensure(wait=3.0)
            if on_progress:
                on_progress(0.03, "送出到 ComfyUI")
            if on_submitted:
                on_submitted(prompt_id, False)
            real_id = client.submit(graph, hub.client_id, self._extra_data(), prompt_id=prompt_id)
            if real_id != prompt_id:
                hub.rekey(prompt_id, real_id)
                prompt_id = real_id
            if on_submitted:
                on_submitted(prompt_id, True)
            entry = self._wait(client, hub, prompt_id, events, graph, on_progress, on_preview, cancelled)
            if on_progress:
                on_progress(0.96, "取回輸出檔")
            return self._collect(client, entry, outputs, dst_dir, cancelled)
        finally:
            hub.unlisten(prompt_id)

    def received(self, sid: str, prompt_id: str) -> bool:
        """Whether ComfyUI has the prompt, finished or still queued."""
        client = self.client(sid)
        return bool(client.history(prompt_id)) or queue_state(client.queue(), prompt_id)[0] != "missing"

    def resume(self, sid: str, prompt_id: str, *, dst_dir: Path, graph: dict | None = None,
               outputs: list[str] | None = None, on_progress: ProgressFn | None = None,
               on_preview: PreviewFn | None = None, cancelled: CancelFn | None = None) -> list[dict]:
        """Pick a prompt back up after the app restarted."""
        client = self.client(sid)
        entry = client.history(prompt_id)
        if entry:
            err = history_error(entry)
            if err:
                raise err
            return self._collect(client, entry, outputs, dst_dir, cancelled)
        state, _ = queue_state(client.queue(), prompt_id)
        if state == "missing":
            raise AppError("ComfyUI 已重新啟動或清空佇列，這個任務遺失了，請重新送出", 502)
        hub = self._hub(sid)
        events = hub.listen(prompt_id)
        try:
            hub.ensure(wait=3.0)
            entry = self._wait(client, hub, prompt_id, events, graph, on_progress, on_preview, cancelled)
            return self._collect(client, entry, outputs, dst_dir, cancelled)
        finally:
            hub.unlisten(prompt_id)

    def _wait(self, client: ComfyClient, hub: _Hub, prompt_id: str, events: queue.Queue, graph: dict | None,
              on_progress: ProgressFn | None, on_preview: PreviewFn | None, cancelled: CancelFn | None) -> dict:
        cfg = self.settings.section("comfy")
        timeout_min = float(cfg.get("timeoutMin") or 30)
        if not cfg.get("livePreview", True):
            on_preview = None
        total = max(1, len(graph or {}) or 10)
        done_nodes: set[str] = set()
        current: str | None = None
        frac = 0.0
        started = False
        finished = False
        deadline = time.monotonic() + timeout_min * 60
        next_poll = time.monotonic() + 2.0
        misses = 0
        offline_since: float | None = None

        def report(message: str) -> None:
            if on_progress:
                value = 0.05 + 0.9 * min(1.0, (len(done_nodes) + frac) / total)
                on_progress(round(value, 4), message)

        while True:
            if cancelled and cancelled():
                self._cancel(client, prompt_id)
                raise AppError("已取消", 499)
            try:
                msg = events.get(timeout=0.5)
            except queue.Empty:
                msg = None
            if msg is not None:
                mtype = msg.get("type")
                data = msg.get("data") if isinstance(msg.get("data"), dict) else {}
                if mtype == "preview":
                    if on_preview and msg.get("image"):
                        on_preview(msg.get("mime", "image/jpeg"), msg["image"])
                elif mtype == "execution_start":
                    started = True
                    report("開始執行")
                elif mtype == "execution_cached":
                    done_nodes.update(str(n) for n in data.get("nodes") or [])
                    report("沿用快取結果")
                elif mtype == "executing":
                    node = data.get("node")
                    if current is not None:
                        done_nodes.add(current)
                    if node is None:
                        finished = True
                    else:
                        started = True
                        current, frac = str(node), 0.0
                        report(_node_label(graph, current))
                elif mtype == "progress":
                    value, maximum = data.get("value") or 0, data.get("max") or 0
                    if data.get("node") is not None:
                        current = str(data["node"])
                    if maximum:
                        frac = min(1.0, value / maximum)
                        report(f"{_node_label(graph, current or '')}（{value}/{maximum}）")
                elif mtype == "execution_success":
                    finished = True
                elif mtype == "execution_error":
                    raise execution_error(data)
                elif mtype == "execution_interrupted":
                    raise AppError("這個任務在 ComfyUI 端被中斷了", 409)
            if finished:
                return self._final_history(client, prompt_id)
            t = time.monotonic()
            if t >= next_poll:
                next_poll = t + (5.0 if hub.connected else 2.0)
                try:
                    entry = client.history(prompt_id)
                    snapshot = None if entry else client.queue()
                except AppError as exc:
                    offline_since = offline_since or t
                    if t - offline_since > OFFLINE_GRACE:
                        raise AppError(f"與 ComfyUI 的連線中斷超過 1 分鐘：{exc.message}", 502) from None
                    report("連線不穩，重試中")
                else:
                    offline_since = None
                    if entry:  # ComfyUI writes history only once a prompt has ended
                        err = history_error(entry)
                        if err:
                            raise err
                        return entry
                    state, ahead = queue_state(snapshot or {}, prompt_id)
                    if state == "missing":
                        misses += 1
                        if misses >= 3:
                            raise AppError("ComfyUI 的佇列裡找不到這個任務（可能已重新啟動或被清除），請重新送出", 502)
                    else:
                        misses = 0
                        if state == "pending" and not started:
                            report(f"排隊中，前面還有 {ahead} 個任務" if ahead else "排隊中")
                        elif state == "running" and not hub.connected and not started:
                            started = True
                            report("執行中（未接上即時進度）")
            if t > deadline:
                self._cancel(client, prompt_id)
                raise AppError(f"ComfyUI 執行超過 {timeout_min:g} 分鐘仍未完成，已中止（可在「引擎 › ComfyUI」調整逾時）", 504)

    def _final_history(self, client: ComfyClient, prompt_id: str) -> dict:
        for _ in range(20):
            entry = client.history(prompt_id)
            if entry:
                err = history_error(entry)
                if err:
                    raise err
                return entry
            time.sleep(0.5)
        raise AppError("ComfyUI 已執行完畢，但查不到這個任務的輸出紀錄", 502)

    def _collect(self, client: ComfyClient, entry: dict, outputs: list[str] | None, dst_dir: Path,
                 cancelled: CancelFn | None) -> list[dict]:
        node_outputs = {str(k): v for k, v in (entry.get("outputs") or {}).items() if isinstance(v, dict)}
        order = [n for n in outputs or [] if n in node_outputs] or sorted(node_outputs, key=_node_sort)
        found: list[tuple[str, dict]] = []
        for nid in order:
            for key in ("images", "gifs", "videos", "video", "audio"):
                for item in node_outputs[nid].get(key) or []:
                    if isinstance(item, dict) and item.get("filename"):
                        found.append((nid, item))
        saved = [pair for pair in found if pair[1].get("type", "output") == "output"]
        chosen = saved or found
        if not chosen:
            raise AppError("ComfyUI 沒有產生輸出檔（整個流程可能都沿用了快取），請換一個種子再試", 502)
        dst_dir.mkdir(parents=True, exist_ok=True)
        results: list[dict] = []
        for idx, (nid, item) in enumerate(chosen):
            name = safe_filename(item["filename"], "output")
            kind = kind_of(name)
            if kind is None:
                continue
            dst = dst_dir / f"{idx:02d}_{name}"
            client.download(item, dst, cancelled)
            results.append({"path": dst, "kind": kind, "node": nid, "filename": item["filename"],
                            "subfolder": item.get("subfolder", "")})
        if not results:
            raise AppError("ComfyUI 的輸出不是圖片、影片或音訊檔", 502)
        return results
