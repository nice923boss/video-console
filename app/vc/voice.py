"""AI voice-over through Audio8Voice (local) or VoAI (cloud), and the "voice" job that speaks one caption.

A caption names a speaker (a cast member: name, engine, voice, speed, volume; a VoAI member also has
a style and a model version). The job asks the member's engine for the line, stages the WAV as
project media and puts it on the audio track at the caption's start, replacing the caption's previous
voice-over. Audio8Voice jobs share the "comfy" lane so they never run on the GPU at the same time as a
ComfyUI render; VoAI jobs run on a cloud lane of their own.

A voice job with payload {"prepare": voice} speaks nothing: it has Audio8Voice transcribe and encode
that voice's reference clip now (Whisper on the GPU, hence the same lane), so the voice's first line
does not hold the queue for it.
"""
from __future__ import annotations

import logging
import shutil
import threading
import time
from typing import Any, Callable

from . import net
from .jobs import JobControl
from .media import Media
from .secrets import SecretStore
from .settings import SettingsStore
from .store import ProjectStore
from .takes import IMPORT_DIR, new_media, stage_file
from .util import AppError, new_id

log = logging.getLogger("vc.voice")

BACKEND = "audio8voice"
LANE = "comfy"
LIST_TIMEOUT = 15
SYNTH_TIMEOUT = 600  # the first line of an unprepared voice also transcribes its reference clip
PREPARE_TIMEOUT = 1800  # about 30 s on an idle GPU; 180 s and 600 s measured while ComfyUI held the GPU

VOAI_BACKEND = "voai"
VOAI_LANE = "provider:voai"
VOAI_URL = "https://connect.voai.ai"
VOAI_KEY = "voai.api_key"
VOAI_TIMEOUT = 120
VOAI_SPEED = (0.5, 1.5)
VOAI_RETRIES = 3  # 529 means too many requests at once: back off and retry
VOAI_BACKOFF = 2.0  # seconds before the first retry, doubling after
VOAI_GAIN = 10 ** (6 / 20)  # VoAI lines come back quiet, so they go on the track 6 dB up
SPEAKER_TTL = 600  # the speaker list changes with the contract, not between two lines


class Audio8Voice:
    """Client for Audio8Voice's local HTTP API."""

    def __init__(self, settings: SettingsStore):
        self._settings = settings

    def url(self) -> str:
        return self._settings.section("voice")["url"]

    def _get(self, path: str, base: str | None = None) -> Any:
        base = base or self.url()
        try:
            return net.get_json(base + path, timeout=LIST_TIMEOUT)
        except net.HttpError as exc:
            raise _explain(exc, base) from None

    def health(self, base: str | None = None) -> dict:
        return self._get("/health", base)

    def status(self) -> dict:
        url = self.url()
        try:
            return {"online": True, "url": url, **self.health()}
        except AppError as exc:
            return {"online": False, "url": url, "error": exc.message}

    def voices(self) -> list[dict]:
        return self._get("/voices")

    def presets(self) -> list[dict]:
        return self._get("/presets")

    def synthesize(self, text: str, voice: str, speed: float) -> bytes:
        base = self.url()
        try:
            resp = net.request("POST", base + "/synthesize", json_body={"text": text, "voice": voice, "speed": speed},
                               timeout=SYNTH_TIMEOUT, max_bytes=200 * 1024 * 1024)
        except net.HttpError as exc:
            raise _explain(exc, base) from None
        if not resp.body.startswith(b"RIFF"):
            raise AppError("Audio8Voice 回傳的不是 WAV 音檔", 502)
        return resp.body

    def prepare(self, voice: str) -> dict:
        base = self.url()
        try:
            return net.post_json(base + "/prepare", {"voice": voice}, timeout=PREPARE_TIMEOUT)
        except net.HttpError as exc:
            raise _explain(exc, base) from None


def _explain(exc: net.HttpError, base: str) -> AppError:
    if exc.code == 0 and exc.message.startswith("連線逾時"):
        return AppError(f"Audio8Voice（{base}）太久沒有回應，顯示卡可能正被 ComfyUI 等其他程式佔用，請稍後再試", 504)
    if exc.code == 0:
        return AppError(f"連不到 Audio8Voice（{base}），請先啟動 Audio8Voice", 502)
    return AppError(f"Audio8Voice：{net.upstream_message(exc.body) or exc.message}", 502)


class VoAI:
    """Client for the VoAI cloud TTS (https://connect.voai.ai/docs/). The API key never leaves this side."""

    def __init__(self, secrets: SecretStore):
        self._secrets = secrets
        self._lock = threading.Lock()
        self._cache: tuple[float, str, list[dict]] | None = None  # (fetched at, key used, models)

    def configured(self) -> bool:
        return bool(self._secrets.get(VOAI_KEY))

    def _key(self) -> str:
        key = self._secrets.get(VOAI_KEY)
        if not key:
            raise AppError("還沒有設定 VoAI API 金鑰，請到「引擎 › 配音」貼上金鑰")
        return key

    def models(self, refresh: bool = False) -> list[dict]:
        """[{version, remark, speakers: [{name, styles, gender, age, category}]}], cached for SPEAKER_TTL."""
        key = self._key()
        with self._lock:
            cached = self._cache
        if cached and not refresh and cached[1] == key and time.monotonic() - cached[0] < SPEAKER_TTL:
            return cached[2]
        try:
            data = net.get_json(VOAI_URL + "/TTS/GetSpeaker", headers={"x-api-key": key}, timeout=LIST_TIMEOUT)
        except net.HttpError as exc:
            raise _explain_voai(exc) from None
        models = [{"version": m["info"]["version"], "remark": m["info"].get("remark") or "",
                   "speakers": [{k: s.get(k) for k in ("name", "styles", "gender", "age", "category")}
                                for s in m.get("speakers") or []]}
                  for m in data["data"]["models"]]
        with self._lock:
            self._cache = (time.monotonic(), key, models)
        return models

    def status(self, refresh: bool = False) -> dict:
        if not self.configured():
            return {"configured": False, "online": False, "error": "", "models": []}
        try:
            return {"configured": True, "online": True, "error": "", "models": self.models(refresh)}
        except AppError as exc:
            return {"configured": True, "online": False, "error": exc.message, "models": []}

    def synthesize(self, text: str, speaker: str, style: str, model: str, speed: float) -> tuple[bytes, str]:
        """(WAV bytes, points this line used)."""
        body = {"version": model, "text": text, "speaker": speaker, "style": style,
                "speed": min(max(speed, VOAI_SPEED[0]), VOAI_SPEED[1])}
        headers = {"x-api-key": self._key(), "x-output-format": "wav"}
        for attempt in range(VOAI_RETRIES):
            try:
                resp = net.request("POST", VOAI_URL + "/TTS/Speech", json_body=body, headers=headers,
                                   timeout=VOAI_TIMEOUT, max_bytes=200 * 1024 * 1024)
                break
            except net.HttpError as exc:
                if exc.code != 529 or attempt == VOAI_RETRIES - 1:
                    raise _explain_voai(exc) from None
                time.sleep(VOAI_BACKOFF * 2 ** attempt)
        if not resp.body.startswith(b"RIFF"):
            raise AppError("VoAI 回傳的不是 WAV 音檔", 502)
        return resp.body, resp.headers.get("x-used-quota", "")


def _explain_voai(exc: net.HttpError) -> AppError:
    if exc.code == 0:
        return AppError(f"連不到 VoAI 雲端：{exc.message}", 502)
    if exc.code == 401:
        return AppError("VoAI 金鑰無效或已失效，請到「引擎 › 配音」重新貼上金鑰", 502)
    if exc.code == 529:
        return AppError("VoAI 同時進行中的請求太多，請稍後再試", 503)
    return AppError(f"VoAI：{net.upstream_message(exc.body) or exc.message}", 502)


def is_voai(member: dict) -> bool:
    return member.get("engine") == VOAI_BACKEND


def voice_line(project: dict, caption_id: str) -> tuple[dict, dict, str]:
    """(caption, cast member, text) for a caption that is ready to be voiced; raises why it is not."""
    cap = next((c for c in project.get("captions") or [] if c.get("id") == caption_id), None)
    if cap is None:
        raise AppError("找不到這句字幕", 404)
    text = (cap.get("text") or "").strip()
    if not text:
        raise AppError("這句字幕沒有文字可以配音")
    member = next((m for m in project.get("cast") or [] if m.get("id") == cap.get("speaker")), None)
    if member is None:
        raise AppError("請先替這句字幕指定人物")
    if not member.get("voice") or (is_voai(member) and not (member.get("style") and member.get("model"))):
        raise AppError(f"人物「{member.get('name') or '未命名'}」還沒有設定配音聲音，請到人物表選擇")
    return cap, member, text


def voice_end(project: dict, cap: dict, duration: float) -> float:
    """Where a caption should end to cover its voice-over (AU-12): the voice's end, but never past the start of
    the next caption, so lines do not pile up; a caption already long enough keeps its end."""
    start, end = float(cap["start"]), float(cap["end"])
    later = [float(c["start"]) for c in project["captions"] if c["id"] != cap["id"] and float(c["start"]) >= start]
    return round(max(end, min([start + duration, *later])), 3)


def replace_ops(project: dict, caption_id: str) -> list[dict]:
    """Ops removing a caption's earlier voice-over: its clips, and generated media nothing else plays."""
    ops: list[dict] = []
    old = [c for c in project["audio"] if c.get("captionId") == caption_id]
    for clip in old:
        ops.append({"op": "audio.delete", "id": clip["id"]})
        media = project["media"].get(clip.get("mediaId")) or {}
        shared = any(c.get("mediaId") == media.get("id") and c not in old for c in project["audio"])
        if (media.get("source") or {}).get("backend") in (BACKEND, VOAI_BACKEND) and not shared:
            ops.append({"op": "media.delete", "id": media["id"]})
    return ops


class VoiceJobs:
    def __init__(self, store: ProjectStore, client: Audio8Voice, media: Callable[[], Media], cloud: VoAI):
        self._store = store
        self._client = client
        self._media = media
        self._cloud = cloud

    def prepare(self, project: dict, payload: dict) -> dict:
        voice = str(payload.get("prepare") or "")
        if voice:
            name = str(payload.get("name") or voice)[:80]
            return {"title": f"準備聲音：{name}", "lane": LANE, "shotId": None,
                    "payload": {"prepare": voice, "name": name}}
        caption_id = str(payload.get("captionId") or "")
        _, member, text = voice_line(project, caption_id)
        if is_voai(member) and not self._cloud.configured():
            raise AppError("還沒有設定 VoAI API 金鑰，請到「引擎 › 配音」貼上金鑰")
        short = text if len(text) <= 16 else text[:15] + "…"
        return {"title": f"配音：{member.get('name') or '未命名'}「{short}」",
                "lane": VOAI_LANE if is_voai(member) else LANE, "shotId": None, "payload": {"captionId": caption_id}}

    def execute(self, ctl: JobControl) -> dict:
        if ctl.payload.get("prepare"):
            return self._prepare_voice(ctl)
        caption_id = ctl.payload["captionId"]
        # read the caption again: its text or speaker may have changed while the job was queued
        cap, member, text = voice_line(self._store.get(ctl.pid), caption_id)
        speed = float(member.get("speed") or 1.0)
        source = {"backend": BACKEND, "captionId": caption_id, "castId": member["id"], "voice": member["voice"],
                  "voiceName": member.get("voiceName") or "", "text": text, "speed": speed}
        quota = ""
        if is_voai(member):
            ctl.progress(0.1, f"VoAI 雲端合成中：{member.get('name')}")
            wav, quota = self._cloud.synthesize(text, member["voice"], member["style"], member["model"], speed)
            source.update(backend=VOAI_BACKEND, style=member["style"], model=member["model"])
        else:
            ctl.progress(0.1, f"Audio8Voice 合成中：{member.get('name')}")
            wav = self._client.synthesize(text, member["voice"], speed)
        ctl.check()
        ctl.progress(0.8, "加入音軌")
        pdir = self._store.project_dir(ctl.pid)
        work = pdir / "media" / "cache" / f"job-{ctl.id}"
        work.mkdir(parents=True, exist_ok=True)
        try:
            src = work / "voice.wav"
            src.write_bytes(wav)
            mid = new_id("m")
            staged = stage_file(self._media(), pdir, src, IMPORT_DIR, mid)
        finally:
            shutil.rmtree(work, ignore_errors=True)
        name = f"{member.get('name') or '配音'}：{text}"[:120]
        item = {**new_media(staged, name, mid), "source": source}
        project = self._store.get(ctl.pid)
        if not any(c.get("id") == caption_id for c in project["captions"]):
            raise AppError("字幕在配音期間被刪除了，配音沒有加入音軌", 410)
        volume = float(member.get("volume", 1.0))
        if is_voai(member):
            volume = min(4.0, volume * VOAI_GAIN)
        cap = next(c for c in project["captions"] if c["id"] == caption_id)  # its times as they are now
        duration = float(staged.get("duration") or 0.0)
        end = voice_end(project, cap, duration)
        ops = [*replace_ops(project, caption_id), {"op": "media.add", "media": item},
               {"op": "audio.add", "item": {"id": new_id("a"), "mediaId": mid, "name": name, "start": cap["start"],
                                            "volume": round(volume, 4), "captionId": caption_id}}]
        if end > float(cap["end"]) + 1e-3:
            ops.append({"op": "caption.update", "id": caption_id, "patch": {"end": end}})
        self._store.apply(ctl.pid, ops, origin="job", undoable=False)
        over = float(cap["start"]) + duration - end
        message = f"配音完成：{duration:.1f} 秒"
        if end > float(cap["end"]) + 0.05:
            message += f"，字幕延長 {end - float(cap['end']):.1f} 秒"
        if over > 0.3:
            message += f"，比字幕長 {over:.1f} 秒（下一句字幕擋住）"
        if quota:
            message += f"，VoAI 扣 {quota} 點"
        return {"mediaIds": [mid], "message": message}

    def _prepare_voice(self, ctl: JobControl) -> dict:
        name = ctl.payload["name"]
        ctl.progress(0.1, f"Audio8Voice 轉錄參考音：{name}")
        done = self._client.prepare(ctl.payload["prepare"])
        return {"mediaIds": [], "message": f"聲音已準備好：{name}（{float(done.get('sec') or 0):.0f} 秒）"}
