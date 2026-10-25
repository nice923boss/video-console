"""Speech-to-subtitle (STS): turn a clip's own audio into captions, the "transcribe" job.

STS is a separate app, a sibling folder by default. VC runs its transcribe_worker.py with STS's own
Python, one child process per job, as its docs/cli-contract.md describes (protocol 1): a native
CUDA crash then kills only the worker, and a run without a final {"t": "done"} line is a failure.

The job cuts the clip's trimmed range out of its video take as 16 kHz mono WAV, has STS transcribe
it, then adds every subtitle as a caption at the clip's place on the timeline (clamped to the clip,
not bound to the shot). Whisper runs on the GPU, so the job shares the "comfy" lane with ComfyUI
renders and Audio8Voice.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable

from .jobs import JobControl
from .media import NO_WINDOW, Media
from .paths import ROOT_DIR
from .sequence import VOICE_TRACK, build_sequence
from .settings import SettingsStore
from .store import ProjectStore
from .takes import find_shot, item_path, shot_label
from .util import AppError, new_id
from .voice import replace_ops

log = logging.getLogger("vc.stt")

PROTOCOL = 1
LANE = "comfy"
WORKER = "transcribe_worker.py"
CHECK_TIMEOUT = 180  # MFA's cold start alone took about 31 s in the STS spike
RUN_TIMEOUT = 3600
MIN_CUE = 0.05  # seconds; a subtitle clamped shorter than this at the clip's end is dropped
_SRT_TIME = re.compile(r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)")


class SttClient:
    """Finds STS and its Python, and runs its worker."""

    def __init__(self, settings: SettingsStore):
        self._settings = settings

    def folder(self) -> Path:
        configured = self._settings.section("stt")["folder"]
        folder = Path(configured) if configured else ROOT_DIR.parent / "Speech-to-subtitle"
        if not (folder / WORKER).is_file():
            raise AppError(f"找不到 Speech-to-subtitle：{folder} 裡沒有 {WORKER}，請到「引擎 › 語音轉字幕」設定資料夾")
        return folder

    def python(self, folder: Path) -> Path:
        configured = self._settings.section("stt")["python"]
        path = Path(configured) if configured else venv_python(folder)
        if path is None or not path.is_file():
            raise AppError(f"找不到 Speech-to-subtitle 的 Python（{path or folder / 'pyvenv.cfg'}），"
                           "請先執行它的安裝依賴，或在「引擎 › 語音轉字幕」指定 python.exe")
        return path

    def check(self) -> dict:
        folder = self.folder()
        python = self.python(folder)
        proc = _start([str(python), str(folder / WORKER), "--check"], folder, subprocess.PIPE)
        try:
            out, err = proc.communicate(timeout=CHECK_TIMEOUT)
        except subprocess.TimeoutExpired:
            kill_tree(proc)
            raise AppError(f"Speech-to-subtitle 檢查超過 {CHECK_TIMEOUT} 秒沒有回應", 504) from None
        report = next((m for m in map(json_line, out.splitlines()) if m and m.get("t") == "check"), None)
        if report is None:
            raise AppError("Speech-to-subtitle 沒有回報檢查結果：" + _tail(err))
        _require_protocol(report.get("protocol"))
        return {**report, "folder": str(folder), "python": str(python)}

    def run(self, job_file: Path, ctl: JobControl, on_progress: Callable[[float, str], None]) -> dict:
        """Run one transcription; returns the worker's "done" message. Cancelling kills the whole tree."""
        folder = self.folder()
        python = self.python(folder)
        log_path = job_file.with_name("worker.log")
        with open(log_path, "wb") as err:
            proc = _start([str(python), str(folder / WORKER), str(job_file)], folder, err)
            stop = threading.Event()
            timed_out = threading.Event()

            def watch() -> None:
                deadline = time.monotonic() + RUN_TIMEOUT
                while not stop.wait(0.5):
                    over = time.monotonic() > deadline
                    if over or ctl.cancelled():
                        if over:
                            timed_out.set()
                        kill_tree(proc)
                        return

            threading.Thread(target=watch, name=f"stt-watch-{ctl.id}", daemon=True).start()
            done, error = None, None
            try:
                for raw in proc.stdout:
                    msg = json_line(raw)
                    if not msg:
                        continue
                    if msg.get("t") == "progress":
                        on_progress(float(msg.get("percent") or 0) / 100, str(msg.get("message") or "轉錄中"))
                    elif msg.get("t") == "done":
                        done = msg
                    elif msg.get("t") == "error":
                        error = str(msg.get("message") or "")
                code = proc.wait()
            finally:
                stop.set()
        ctl.check()
        if timed_out.is_set():
            raise AppError(f"轉錄超過 {RUN_TIMEOUT // 60} 分鐘，已停止", 504)
        if done is None:
            reason = error or f"轉錄程式異常結束（結束碼 0x{code & 0xFFFFFFFF:08X}）"
            log.warning("stt worker failed (%s): %s", code, _tail(log_path.read_bytes()))
            raise AppError(f"Speech-to-subtitle 轉錄失敗：{reason}", 502)
        return done


class SttJobs:
    """The "transcribe" job: payload {} on a shot whose video take has sound."""

    def __init__(self, store: ProjectStore, client: SttClient, media: Callable[[], Media]):
        self._store = store
        self._client = client
        self._media = media

    def prepare(self, project: dict, shot_id: str | None) -> dict:
        index, shot = find_shot(project, shot_id)
        clip_of(project, shot_id)
        self._client.python(self._client.folder())  # fail now, not after the queue wait
        return {"title": f"語音轉字幕：{shot_label(index, shot)}", "lane": LANE, "shotId": shot["id"], "payload": {}}

    def execute(self, ctl: JobControl) -> dict:
        # read the clip again: its trim may have changed while the job was queued
        project = self._store.get(ctl.pid)
        clip = clip_of(project, ctl.shot_id)
        pdir = self._store.project_dir(ctl.pid)
        src = item_path(pdir, clip["take"])
        work = pdir / "media" / "cache" / f"job-{ctl.id}"
        work.mkdir(parents=True, exist_ok=True)
        try:
            ctl.progress(0.02, "擷取片段原聲")
            wav, srt = work / "clip.wav", work / "clip.srt"
            self._media().run(["-ss", f"{clip['in']:.3f}", "-i", str(src), "-t", f"{clip['length']:.3f}", "-vn",
                               "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav)])
            ctl.check()
            job_file = work / "job.json"
            # language None: forcing zh skipped an English speaker in the STS spike
            job_file.write_text(json.dumps({"input_file": str(wav), "output_srt": str(srt), "language": None},
                                           ensure_ascii=False), encoding="utf-8")
            done = self._client.run(job_file, ctl, lambda f, m: ctl.progress(0.05 + 0.9 * min(max(f, 0.0), 1.0), m))
            cues = parse_srt(Path(done.get("path") or srt).read_text(encoding="utf-8-sig"))
        finally:
            shutil.rmtree(work, ignore_errors=True)
        ops = caption_ops(cues, clip)
        if not ops:
            return {"message": "片段裡沒有辨識到語音，沒有新增字幕"}
        # a second run replaces what the last run of this shot added, instead of stacking a copy
        old = [{"op": "caption.delete", "id": c["id"]} for c in self._store.get(ctl.pid)["captions"]
               if c.get("sttShot") == ctl.shot_id]
        # undoable: one Ctrl+Z takes back the whole batch (and brings the replaced captions back)
        self._store.apply(ctl.pid, old + ops, origin="job", undoable=True, label="語音轉字幕")
        replaced = f"，取代上次的 {len(old)} 句" if old else ""
        return {"message": f"已加入 {len(ops)} 句字幕{replaced}"}


def clip_of(project: dict, shot_id: str | None) -> dict:
    """The shot's clip in the sequence; it must show a video take that has sound."""
    find_shot(project, shot_id)
    clip = next((c for c in build_sequence(project) if c["shot"]["id"] == shot_id), None)
    if clip is None:
        raise AppError("這個鏡頭不在剪輯序列裡，請先把它加回序列")
    if clip["source"] != "video":
        raise AppError("這個鏡頭沒有影片，無法轉成字幕")
    if not clip["take"].get("hasAudio"):
        raise AppError("這個鏡頭的影片沒有聲音")
    return clip


def parse_srt(text: str, sep: str = " ") -> list[tuple[float, float, str]]:
    """(start, end, text) per subtitle; multi-line text is joined with sep."""
    cues = []
    for block in re.split(r"\r?\n\s*\r?\n", text.strip()):
        lines = block.splitlines()
        for i, line in enumerate(lines):
            m = _SRT_TIME.search(line)
            if not m:
                continue
            g = m.groups()
            start = int(g[0]) * 3600 + int(g[1]) * 60 + int(g[2]) + float("0." + g[3])
            end = int(g[4]) * 3600 + int(g[5]) * 60 + int(g[6]) + float("0." + g[7])
            body = sep.join(s.strip() for s in lines[i + 1:] if s.strip())
            if body and end > start:
                cues.append((start, end, body))
            break
    return cues


def caption_ops(cues: list[tuple[float, float, str]], clip: dict) -> list[dict]:
    """caption.add ops placing clip-relative subtitles on the timeline, clamped to the clip."""
    base = float(clip["start"])
    limit = base + float(clip["length"])
    speed = float(clip.get("speed") or 1.0)  # cue times are in the source; a faster clip shortens them
    ops = []
    for start, end, text in cues:
        a = round(base + max(0.0, start) / speed, 3)
        b = round(min(base + end / speed, limit), 3)
        if b - a < MIN_CUE:
            continue
        ops.append({"op": "caption.add", "item": {"id": new_id("c"), "start": a, "end": b, "text": text[:500],
                                                   "position": "bottom", "shot": None, "sttShot": clip["shot"]["id"]}})
    return ops


_SRT_TAG = re.compile(r"<[^>]*>|\{\\[^}]*\}")  # <i>, <font …>, {\an8}


def srt_text(raw: bytes) -> str:
    """An .srt file's text: UTF-16 with a BOM, UTF-8 (BOM optional), or else Big5 as older Taiwanese subtitles are."""
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("cp950", errors="replace")


def srt_ops(project: dict, text: str, replace: bool, offset: float = 0.0, voices: str = "delete") -> list[dict]:
    """caption.add ops putting an .srt's subtitles on the sequence, offset seconds after their own times.

    replace drops the old captions first; voices says what happens to voice-overs bound to them:
    "delete" removes them, "keep" leaves them on the voice rows as ordinary clips, "regen" removes them and
    gives each new caption the speaker of the old caption it overlaps most, so its voice-over can be redone.
    """
    old = project["captions"] if replace else []
    ops: list[dict] = []
    for cap in old:
        if voices == "keep":
            ops += [{"op": "audio.update", "id": a["id"], "patch": {"captionId": None, "track": VOICE_TRACK}}
                    for a in project["audio"] if a.get("captionId") == cap["id"]]
        else:
            ops += [op for op in replace_ops(project, cap["id"]) if op not in ops]
    ops += [{"op": "caption.delete", "id": c["id"]} for c in old]
    speakers = [c for c in old if c.get("speaker")] if voices == "regen" else []
    for start, end, body in parse_srt(text, "\n"):
        body = "\n".join(line.strip() for line in _SRT_TAG.sub("", body).splitlines() if line.strip())
        if not body:
            continue
        a, b = round(start + offset, 3), round(end + offset, 3)
        item = {"id": new_id("c"), "start": a, "end": b, "text": body[:500], "position": "bottom", "shot": None}
        overlap = max(speakers, key=lambda c: min(b, c["end"]) - max(a, c["start"]), default=None)
        if overlap is not None and min(b, overlap["end"]) > max(a, overlap["start"]):
            item["speaker"] = overlap["speaker"]
        ops.append({"op": "caption.add", "item": item})
    return ops


def venv_python(folder: Path) -> Path | None:
    """python.exe of the interpreter STS's launcher points at (the `home` line of its pyvenv.cfg)."""
    try:
        text = (folder / "pyvenv.cfg").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    m = re.search(r"^\s*home\s*=\s*(.+?)\s*$", text, re.MULTILINE)
    return Path(m.group(1)) / "python.exe" if m else None


def json_line(raw: bytes | str) -> dict | None:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    raw = raw.strip()
    if not raw.startswith("{"):
        return None
    try:
        msg = json.loads(raw)
    except ValueError:
        return None
    return msg if isinstance(msg, dict) else None


def kill_tree(proc: subprocess.Popen) -> None:
    """The worker starts MFA (mamba, mfa) processes of its own: killing only the worker orphans them."""
    if proc.poll() is not None:
        return
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, creationflags=NO_WINDOW)
    else:
        proc.kill()


def _start(cmd: list[str], cwd: Path, stderr) -> subprocess.Popen:
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    for key in ("PYTHONHOME", "PYTHONPATH"):  # VC's own interpreter must not leak into STS's
        env.pop(key, None)
    try:
        return subprocess.Popen(cmd, cwd=str(cwd), env=env, stdout=subprocess.PIPE, stderr=stderr,
                                stdin=subprocess.DEVNULL, creationflags=NO_WINDOW)
    except OSError as exc:
        raise AppError(f"無法執行 Speech-to-subtitle：{exc}") from None


def _require_protocol(version) -> None:
    if version != PROTOCOL:
        raise AppError(f"Speech-to-subtitle 介面版本 {version} 與 Video Console 支援的 {PROTOCOL} 不同，請把兩邊更新到同一版")


def _tail(data: bytes, limit: int = 400) -> str:
    text = data.decode("utf-8", "replace").strip()
    return text[-limit:] or "（沒有輸出）"
