"""Speech-to-subtitle: the "transcribe" job and the engines-page check, against a fake STS worker."""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from test_server import Client, app, client, find_ffmpeg, needs_ffmpeg, project  # noqa: F401  (shared fixtures)
from vc.app import App
from vc.stt import MIN_CUE, caption_ops, parse_srt, venv_python
from vc.util import AppError

# Speaks protocol 1 like the real transcribe_worker.py; mode.txt next to it picks the behaviour.
FAKE_WORKER = r'''
import json, os, sys, time, wave
from pathlib import Path

here = Path(__file__).parent
mode = (here / "mode.txt").read_text(encoding="utf-8").strip()

def say(msg):
    print(json.dumps(msg, ensure_ascii=False), flush=True)

if sys.argv[1] == "--check":
    say({"t": "check", "protocol": 2 if mode == "protocol" else 1, "cuda": {"ok": True, "devices": 1},
         "model": {"ok": True, "path": "large-v3"}, "ffmpeg": {"ok": True, "path": "ffmpeg.exe"},
         "mfa": {"ok": False, "message": "找不到 MFA"}, "opencc": {"ok": True, "message": "簡轉繁：已啟用"}, "ok": True})
    sys.exit(0)
job = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
(here / "pid.txt").write_text(str(os.getpid()))
say({"t": "progress", "phase": "loading", "percent": 50, "message": "載入模型", "segment_count": 0})
if mode == "hang":
    time.sleep(60)
if mode == "crash":
    sys.stderr.write("native abort")
    sys.exit(3)
if mode == "error":
    say({"t": "error", "message": "RuntimeError: CUDA out of memory"})
    sys.exit(1)
with wave.open(job["input_file"]) as w:
    seen = {"seconds": w.getnframes() / w.getframerate(), "rate": w.getframerate(), "channels": w.getnchannels(),
            "language": job["language"]}
(here / "seen.json").write_text(json.dumps(seen))
srt = "" if mode == "silent" else (
    "1\n00:00:00,200 --> 00:00:00,900\n第一句\n第二行\n\n"
    "2\n00:00:01,000 --> 00:00:09,000\n超出片段\n\n"
    "3\n00:00:01,980 --> 00:00:02,500\n太短\n")
Path(job["output_srt"]).write_text(srt, encoding="utf-8")
say({"t": "done", "path": job["output_srt"], "words": job["output_srt"][:-4] + ".words.json"})
'''


@pytest.fixture()
def sts(tmp_path: Path, client: Client) -> Path:
    folder = tmp_path / "Speech-to-subtitle"
    folder.mkdir()
    (folder / "transcribe_worker.py").write_text(FAKE_WORKER, encoding="utf-8")
    (folder / "mode.txt").write_text("ok", encoding="utf-8")
    client.json("PUT", "/api/settings", {"stt": {"folder": str(folder), "python": sys.executable}})
    return folder


def video_shot(client: Client, project: dict, tmp_path: Path, sound: bool = True) -> str:
    """Shot 1 gets a 3 s video take (with a tone when sound), trimmed to 0.5..2.5 s."""
    pid, sid = project["id"], project["shots"][0]["id"]
    clip = tmp_path / "talk.mp4"
    cmd = [find_ffmpeg(), "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=s=320x180:r=25:d=3"]
    if sound:
        cmd += ["-f", "lavfi", "-i", "sine=frequency=440:duration=3", "-shortest"]
    subprocess.run([*cmd, "-pix_fmt", "yuv420p", str(clip)], check=True)
    job = client.json("POST", f"/api/projects/{pid}/upload?name=talk.mp4&target=shot&shot={sid}",
                      clip.read_bytes())["job"]
    done = client.wait_job(job["id"])
    assert done["status"] == "done", done["error"]
    client.json("POST", f"/api/projects/{pid}/ops",
                {"ops": [{"op": "shot.update", "id": sid, "patch": {"trimIn": 0.5, "trimOut": 2.5}}]})
    return sid


def transcribe(client: Client, pid: str, sid: str) -> dict:
    job = client.json("POST", "/api/jobs", {"kind": "transcribe", "pid": pid, "shotId": sid, "payload": {}})["job"]
    assert job["lane"] == "comfy"  # Whisper never shares the GPU with a ComfyUI render
    return client.wait_job(job["id"])


def captions(client: Client, pid: str) -> list[dict]:
    return client.json("GET", f"/api/projects/{pid}")["project"]["captions"]


def alive(pid: int) -> bool:
    out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
    return str(pid) in out


def test_parse_srt_joins_lines_and_skips_broken_blocks() -> None:
    text = ("\ufeff1\r\n00:00:01,500 --> 00:00:02.250\r\n你好\r\n世界\r\n\r\n"
            "2\n00:00:03,000 --> 00:00:02,000\n倒著的\n\n"
            "3\n不是時間碼\n\n"
            "4\n01:00:00,000 --> 01:00:01,000\n\n")
    assert parse_srt(text.lstrip("\ufeff")) == [(1.5, 2.25, "你好 世界")]


def test_caption_ops_place_cues_at_the_clip_and_clamp_to_it() -> None:
    clip = {"start": 10.0, "length": 2.0, "shot": {"id": "s_one000001"}}
    ops = caption_ops([(0.2, 0.9, "一"), (1.0, 9.0, "二"), (2.0 - MIN_CUE / 2, 2.5, "三")], clip)
    assert [(o["item"]["start"], o["item"]["end"], o["item"]["text"]) for o in ops] == [(10.2, 10.9, "一"),
                                                                                         (11.0, 12.0, "二")]
    assert all(o["op"] == "caption.add" and o["item"]["shot"] is None for o in ops)
    assert all(o["item"]["sttShot"] == "s_one000001" for o in ops)  # marks this run for the next re-run


def test_venv_python_reads_the_launcher_home(tmp_path: Path) -> None:
    assert venv_python(tmp_path) is None
    (tmp_path / "pyvenv.cfg").write_text("include-system-site-packages = false\nhome = C:\\Py312\n", encoding="utf-8")
    assert venv_python(tmp_path) == Path("C:\\Py312") / "python.exe"


@needs_ffmpeg
def test_transcribe_adds_the_clip_subtitles_as_one_undoable_batch(
        app: App, client: Client, project: dict, sts: Path, tmp_path: Path) -> None:
    pid = project["id"]
    sid = video_shot(client, project, tmp_path)
    done = transcribe(client, pid, sid)
    assert done["status"] == "done", done["error"]
    assert done["result"]["message"] == "已加入 2 句字幕"
    seen = json.loads((sts / "seen.json").read_text())
    assert abs(seen["seconds"] - 2.0) < 0.05 and seen["rate"] == 16000 and seen["channels"] == 1
    assert seen["language"] is None
    got = [(c["start"], c["end"], c["text"]) for c in captions(client, pid)]
    assert got == [(0.2, 0.9, "第一句 第二行"), (1.0, 2.0, "超出片段")]
    cache = app.store.project_dir(pid) / "media" / "cache"
    assert not [f for f in cache.iterdir() if f.name.startswith("job-")]
    client.json("POST", f"/api/projects/{pid}/undo")
    assert captions(client, pid) == []

    (sts / "mode.txt").write_text("silent", encoding="utf-8")
    done = transcribe(client, pid, sid)
    assert done["status"] == "done" and "沒有辨識到語音" in done["result"]["message"]


@needs_ffmpeg
def test_transcribing_again_replaces_the_last_run_and_keeps_other_captions(
        client: Client, project: dict, sts: Path, tmp_path: Path) -> None:
    pid = project["id"]
    sid = video_shot(client, project, tmp_path)
    assert transcribe(client, pid, sid)["status"] == "done"
    first = {c["id"] for c in captions(client, pid)}
    mine = {"id": "c_mine00001", "start": 0.5, "end": 1.5, "text": "手打的", "position": "bottom"}
    client.json("POST", f"/api/projects/{pid}/ops", {"ops": [{"op": "caption.add", "item": mine}]})
    done = transcribe(client, pid, sid)
    assert done["result"]["message"] == "已加入 2 句字幕，取代上次的 2 句"
    now = {c["id"] for c in captions(client, pid)}
    assert len(now) == 3 and "c_mine00001" in now and not first & now
    client.json("POST", f"/api/projects/{pid}/undo")
    assert {c["id"] for c in captions(client, pid)} == first | {"c_mine00001"}


@needs_ffmpeg
def test_transcribe_failures_are_explained(client: Client, project: dict, sts: Path, tmp_path: Path) -> None:
    pid = project["id"]
    sid = video_shot(client, project, tmp_path)
    (sts / "mode.txt").write_text("crash", encoding="utf-8")
    done = transcribe(client, pid, sid)
    assert done["status"] == "failed" and "異常結束（結束碼 0x00000003）" in done["error"]
    (sts / "mode.txt").write_text("error", encoding="utf-8")
    done = transcribe(client, pid, sid)
    assert done["status"] == "failed" and done["error"] == "Speech-to-subtitle 轉錄失敗：RuntimeError: CUDA out of memory"
    assert captions(client, pid) == []


@needs_ffmpeg
@pytest.mark.skipif(sys.platform != "win32", reason="checks the process with tasklist")
def test_cancelling_a_transcription_kills_the_worker(client: Client, project: dict, sts: Path, tmp_path: Path) -> None:
    pid = project["id"]
    sid = video_shot(client, project, tmp_path)
    (sts / "mode.txt").write_text("hang", encoding="utf-8")
    job = client.json("POST", "/api/jobs", {"kind": "transcribe", "pid": pid, "shotId": sid, "payload": {}})["job"]
    deadline = time.monotonic() + 30
    while not (sts / "pid.txt").exists() and time.monotonic() < deadline:
        time.sleep(0.1)
    worker = int((sts / "pid.txt").read_text())
    assert alive(worker)
    client.json("POST", f"/api/jobs/{job['id']}/cancel")
    assert client.wait_job(job["id"], timeout=15)["status"] == "canceled"
    assert not alive(worker)


@needs_ffmpeg
def test_transcribe_is_refused_for_a_shot_without_sound(client: Client, project: dict, sts: Path,
                                                        tmp_path: Path) -> None:
    pid = project["id"]
    sid = video_shot(client, project, tmp_path, sound=False)
    code, _, body = client.request("POST", "/api/jobs", {"kind": "transcribe", "pid": pid, "shotId": sid,
                                                         "payload": {}})
    assert code == 400 and "沒有聲音" in body.decode("utf-8")


@needs_ffmpeg
def test_video_takes_with_sound_get_a_waveform(app: App, client: Client, project: dict, tmp_path: Path) -> None:
    pid = project["id"]
    sid = video_shot(client, project, tmp_path)
    video_shot(client, project, tmp_path, sound=False)
    takes =[t for t in client.json("GET", f"/api/projects/{pid}")["project"]["takes"].values() if t["shot"] == sid]
    loud = next(t for t in takes if t["hasAudio"])
    mute = next(t for t in takes if not t["hasAudio"])
    wave = json.loads(app.store.resolve_file(pid, loud["wave"]).read_text(encoding="utf-8"))
    assert abs(wave["duration"] - 3.0) < 0.1 and max(wave["peaks"]) == 100
    assert 0 < wave["level"] <= 1  # the loudest sample, which the timeline paints red when a clip pushes it over 0 dB
    assert "wave" not in mute


def test_check_reports_the_worker_and_refuses_another_protocol(client: Client, sts: Path) -> None:
    report = client.json("POST", "/api/stt/check")
    assert report["ok"] and report["mfa"]["ok"] is False and report["folder"] == str(sts)
    assert report["python"] == sys.executable
    (sts / "mode.txt").write_text("protocol", encoding="utf-8")
    code, _, body = client.request("POST", "/api/stt/check")
    assert code == 400 and "介面版本 2" in body.decode("utf-8")
    client.json("PUT", "/api/settings", {"stt": {"folder": str(sts / "missing")}})
    code, _, body = client.request("POST", "/api/stt/check")
    assert code == 400 and "找不到 Speech-to-subtitle" in body.decode("utf-8")


def test_stt_settings_are_trimmed(client: Client) -> None:
    saved = client.json("PUT", "/api/settings", {"stt": {"folder": "  D:\\STS  ", "python": None}})
    assert saved["stt"] == {"folder": "D:\\STS", "python": ""}


def test_unknown_folder_error_is_an_app_error(tmp_path: Path) -> None:
    from vc.settings import SettingsStore
    from vc.stt import SttClient
    store = SettingsStore(tmp_path / "s.json")
    store.update({"stt": {"folder": str(tmp_path)}})
    with pytest.raises(AppError, match="transcribe_worker.py"):
        SttClient(store).folder()
