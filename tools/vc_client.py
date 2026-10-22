"""Small client for the Video Console (VC) local HTTP API. Standard library only.

VC listens on 127.0.0.1 only; its address is in <vc>/data/run/server.json. State-changing requests
need the `X-VC: 1` header. VC exits on its own after 120 s with no window and no job, so this client
holds an SSE connection to /api/events while it is in use (that counts as an open window).
Full API reference: docs/API.md.

    from vc_client import VC
    vc = VC()                                   # starts VC with --no-window when it is not running
    p = vc.create_project("受訪 A", 1920, 1080, 30)
    shot = p["shots"][0]["id"]
    vc.wait(vc.upload(p["id"], "a.mp4", shot=shot)["id"])
    vc.wait(vc.job("transcribe", p["id"], shot)["id"])
    print(vc.srt(p["id"]))

Command line check (starts VC if needed, prints what is ready):

    python tools/vc_client.py status [--no-stt]

VC folder: $VC_ROOT, else the folder this file sits in when it is <vc>/tools, else the owner's default.
"""
from __future__ import annotations

import http.client
import json
import os
import subprocess
import sys
import threading
import time
import urllib.parse
from pathlib import Path

EXE_NAME = "Video Console.exe"
FINISHED = ("done", "failed", "canceled", "interrupted")


def _is_vc(folder: Path) -> bool:
    return (folder / "app" / "main.py").is_file() or (folder / EXE_NAME).is_file()


def _default_root() -> Path:
    if os.environ.get("VC_ROOT"):
        return Path(os.environ["VC_ROOT"])
    here = Path(__file__).resolve().parent.parent
    return here if _is_vc(here) else Path(r"F:\AI Project\Video Console")


DEFAULT_ROOT = _default_root()


class VCError(RuntimeError):
    pass


class VC:
    def __init__(self, root: Path | str = DEFAULT_ROOT, start: bool = True, keepalive: bool = True):
        self.root = Path(root)
        if not _is_vc(self.root):
            raise VCError(f"{self.root} 不是 Video Console 資料夾，請設定環境變數 VC_ROOT")
        self.port = self._find_port()
        if self.port is None:
            if not start:
                raise VCError("Video Console 沒有在執行")
            self.port = self._start()
        if keepalive:
            threading.Thread(target=self._hold_events, name="vc-keepalive", daemon=True).start()

    # ------------------------------------------------------------ server lifecycle
    def _find_port(self) -> int | None:
        info = self.root / "data" / "run" / "server.json"
        try:
            port = int(json.loads(info.read_text(encoding="utf-8"))["port"])
        except (OSError, ValueError, KeyError):
            return None
        try:
            self._request("GET", "/api/ping", port=port, timeout=5)
        except (OSError, VCError):
            return None
        return port

    def _start(self) -> int:
        if (self.root / "app" / "main.py").is_file():
            exe = Path(sys.executable)
            pyw = exe.with_name("pythonw.exe")
            cmd = [str(pyw if pyw.is_file() else exe), "app/main.py", "--no-window"]
        else:
            cmd = [str(self.root / EXE_NAME), "--no-window"]
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
        subprocess.Popen(cmd, cwd=self.root, creationflags=flags, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            time.sleep(1)
            port = self._find_port()
            if port:
                return port
        raise VCError(f"Video Console 60 秒內沒有啟動，請看 {self.root / 'data' / 'logs' / 'app.log'}")

    def _hold_events(self) -> None:
        while True:
            try:
                conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=60)
                conn.request("GET", "/api/events")
                resp = conn.getresponse()
                while resp.readline():
                    pass
            except OSError:
                pass
            time.sleep(2)

    # ------------------------------------------------------------ HTTP
    def _request(self, method: str, path: str, body=None, *, port: int | None = None, timeout: float = 120,
                 raw: bool = False, length: int | None = None):
        conn = http.client.HTTPConnection("127.0.0.1", port or self.port, timeout=timeout)
        headers = {"X-VC": "1"}
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if length is not None:
            headers["Content-Length"] = str(length)
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        if resp.status >= 400:
            try:
                message = json.loads(data).get("error") or data.decode("utf-8", "replace")
            except ValueError:
                message = data.decode("utf-8", "replace")
            raise VCError(f"{method} {path} -> {resp.status}: {message}")
        if raw:
            return data
        return json.loads(data) if data else None

    def get(self, path: str):
        return self._request("GET", path)

    def post(self, path: str, body=None, timeout: float = 120):
        return self._request("POST", path, body if body is not None else {}, timeout=timeout)

    def delete(self, path: str):
        return self._request("DELETE", path)

    # ------------------------------------------------------------ projects and edits
    def projects(self) -> list[dict]:
        return self.get("/api/projects")

    def create_project(self, name: str, width: int = 1920, height: int = 1080, fps: int = 30) -> dict:
        settings = {"width": width, "height": height, "fps": fps, "aspect": f"{width}:{height}"}
        return self.post("/api/projects", {"name": name, "settings": settings})["project"]

    def project(self, pid: str) -> dict:
        return self.get(f"/api/projects/{pid}")["project"]

    def ops(self, pid: str, ops: list[dict], label: str = "", undoable: bool = True) -> dict:
        """Apply one undoable batch. Wrap shot ops as {"op": "linked", "ops": [...]} to carry captions along."""
        return self.post(f"/api/projects/{pid}/ops", {"ops": ops, "label": label, "undoable": undoable})

    # ------------------------------------------------------------ files and jobs
    def upload(self, pid: str, path: str | Path, *, shot: str | None = None, target: str | None = None) -> dict:
        """Upload a file; returns the ingest job. With shot= the video becomes that shot's active take."""
        path = Path(path)
        query = {"name": path.name, "target": target or ("shot" if shot else "library")}
        if shot:
            query["shot"] = shot
        size = path.stat().st_size
        with open(path, "rb") as f:
            return self._request("POST", f"/api/projects/{pid}/upload?{urllib.parse.urlencode(query)}", f,
                                 timeout=3600, length=size)["job"]

    def job(self, kind: str, pid: str, shot_id: str | None = None, payload: dict | None = None) -> dict:
        return self.post("/api/jobs", {"kind": kind, "pid": pid, "shotId": shot_id, "payload": payload or {}})["job"]

    def wait(self, jid: str, timeout: float = 7200, every: float = 2.0, quiet: bool = False) -> dict:
        """Poll until the job finishes; raise VCError unless it is done."""
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            job = self.get(f"/api/jobs/{jid}")
            note = (job["status"], round((job.get("progress") or 0) * 100))
            if not quiet and note != last:
                print(f"  [{job['kind']}] {job['status']} {note[1]}% {job.get('message') or ''}", flush=True)
                last = note
            if job["status"] in FINISHED:
                if job["status"] != "done":
                    raise VCError(f"工作 {jid}（{job['kind']}）{job['status']}：{job.get('error') or job.get('message')}")
                return job
            time.sleep(every)
        raise VCError(f"工作 {jid} 超過 {timeout} 秒沒有完成")

    def srt(self, pid: str) -> str:
        return self._request("GET", f"/api/projects/{pid}/captions.srt", raw=True).decode("utf-8-sig")

    def export(self, pid: str, **options) -> Path:
        """Render the sequence (options: burnCaptions, clipAudio, quality, encoder); returns the file path."""
        job = self.wait(self.job("export", pid, payload={"options": options})["id"], quiet=True)
        return self.project_dir(pid) / job["result"]["file"]

    def project_dir(self, pid: str) -> Path:
        return self.root / "data" / "projects" / pid


def status(check_stt: bool = True) -> bool:
    """Print what this computer can do with VC; True when the required parts are ready."""
    vc = VC()
    ping = vc.get("/api/ping")
    print(f"Video Console {ping['version']}：執行中，127.0.0.1:{vc.port}（{vc.root}）")
    ok = True
    for item in vc.get("/api/system/deps")["items"]:
        state = "就緒" if item["ready"] else ("缺少（必要）" if item["required"] else "未安裝（選用）")
        print(f"  {item['id']}：{state} {item.get('version') or ''}")
        ok = ok and (item["ready"] or not item["required"])
    if check_stt:
        try:
            report = vc.post("/api/stt/check", timeout=200)
        except VCError as e:
            print(f"  語音轉字幕：無法使用（{e}）")
        else:
            parts = ", ".join(f"{k} {'OK' if report[k].get('ok') else 'NG'}"
                              for k in ("cuda", "model", "ffmpeg", "mfa", "opencc") if isinstance(report.get(k), dict))
            print(f"  語音轉字幕：{'就緒' if report.get('ok') else '未就緒'}（{parts}）")
            print(f"    資料夾 {report['folder']}，Python {report['python']}")
    print(f"  專案數：{len(vc.projects())}")
    return ok


if __name__ == "__main__":
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = sys.argv[1:]
    if not args or args[0] != "status":
        print("用法：python tools/vc_client.py status [--no-stt]")
        sys.exit(2)
    try:
        sys.exit(0 if status(check_stt="--no-stt" not in args) else 1)
    except VCError as e:
        print(f"錯誤：{e}")
        sys.exit(1)
