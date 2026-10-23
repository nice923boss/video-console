"""Generation jobs (image, video, upscale) through ComfyUI or a cloud API, Video2X upscales of existing videos;
ffmpeg-only jobs go to LocalJobs."""
from __future__ import annotations

import json
import logging
import re
import shutil
import time
from pathlib import Path
from typing import Any, Callable

from . import notext, providers, video2x
from .comfy import workflows as wf
from .comfy.manager import ComfyManager
from .jobs import JobControl
from .llm import LLMClient
from .localjobs import LOCAL_KINDS, LocalJobs, number
from .media import Media
from .ops import MAX_REFS, MAX_SEED, snap16
from .providers import catalog
from .providers.base import duration_seconds, pick_duration
from .providers.catalog import ModelCatalog
from .secrets import SecretStore
from .sequence import compose_prompt
from .settings import SettingsStore
from .store import ProjectStore
from .stt import SttClient, SttJobs
from .takes import (TAKE_DIR, commit_takes, find_item, find_shot, item_path, new_take, reference_image,
                    shot_label, stage_file)
from .util import AppError, clamp, kind_of, new_id, random_seed
from .voice import Audio8Voice, VoAI, VoiceJobs

log = logging.getLogger("vc.runners")

KIND_LABEL = {"image": "生圖", "video": "影片", "upscale": "放大", "upscalevideo": "影片放大"}
GEN_SHORT_SIDE = 720  # a 1080P project still generates at 720P; the export scales clips up to the project size
PROVIDER_SEED_RANGE = 2**31 - 1  # the strictest seed range among the vendors (signed 32-bit)
_CJK_RE = re.compile(r"[぀-ヿ㐀-䶿一-鿿가-힯豈-﫿]")


def _text(value: Any, limit: int) -> str:
    return value.strip()[:limit] if isinstance(value, str) else ""


def coerce_param(spec: dict, value: Any) -> Any:
    """A template parameter value checked against its definition; numbers are clamped to the allowed range."""
    label = spec.get("label") or spec["key"]
    ptype = spec.get("type", "float")
    if ptype in ("int", "float"):
        try:
            num = number(value, 0)
        except AppError:
            raise AppError(f"參數「{label}」必須是數字") from None
        if isinstance(spec.get("min"), (int, float)):
            num = max(num, float(spec["min"]))
        if isinstance(spec.get("max"), (int, float)):
            num = min(num, float(spec["max"]))
        return int(round(num)) if ptype == "int" else num
    if ptype == "bool":
        return value.strip().lower() in ("1", "true", "yes", "on") if isinstance(value, str) else bool(value)
    if ptype == "select":
        for option in spec.get("options") or []:
            if option.get("value") == value or str(option.get("value")) == str(value):
                return option.get("value")
        raise AppError(f"參數「{label}」的選項不正確")
    return str(value)[:2000]


def template_params(template: dict, saved: Any = None, requested: Any = None) -> dict[str, Any]:
    """Parameter values for a template: its defaults, then the shot's saved values, then the request's.

    Only keys the template declares are taken. A saved value that no longer fits (it was chosen for another
    template) falls back to the default; an invalid value in the request itself is an error.
    """
    specs = {p["key"]: p for p in template.get("params") or []}
    values = wf.param_defaults(template)
    for layer, strict in ((saved, False), (requested, True)):
        if not isinstance(layer, dict):
            continue
        for key, value in layer.items():
            if key not in specs or value is None or value == "":
                continue
            try:
                values[key] = coerce_param(specs[key], value)
            except AppError:
                if strict:
                    raise
    return values


def snap32(value: float) -> int:
    """Video models (Wan, LTXV) need both sides divisible by 32."""
    return max(64, int(round(value / 32.0)) * 32)


class Runners:
    """The executor handed to JobManager: generation jobs here, ffmpeg-only jobs in LocalJobs."""

    def __init__(self, *, store: ProjectStore, settings: SettingsStore, secrets: SecretStore, comfy: ComfyManager,
                 workflows: wf.WorkflowStore, media: Callable[[], Media], catalog: ModelCatalog):
        self._store = store
        self._settings = settings
        self._secrets = secrets
        self._comfy = comfy
        self._workflows = workflows
        self._media = media
        self._catalog = catalog
        self._local = LocalJobs(store, settings, media)
        self._voice = VoiceJobs(store, Audio8Voice(settings), media, VoAI(secrets))
        self._stt = SttJobs(store, SttClient(settings), media)
        self._llm = LLMClient(settings, secrets)

    # ---------------------------------------------------------------- executor protocol
    def prepare(self, kind: str, pid: str, shot_id: str | None, payload: dict) -> dict:
        project = self._store.get(pid)
        if kind in LOCAL_KINDS:
            return self._local.prepare(kind, project, shot_id, payload)
        if kind == "voice":
            return self._voice.prepare(project, payload)
        if kind == "transcribe":
            return self._stt.prepare(project, shot_id)
        if kind == "upscale":
            return self._prepare_upscale(project, shot_id, payload)
        if kind == "upscalevideo":
            return self._prepare_upscale_video(project, shot_id, payload)
        if kind in ("image", "video"):
            return self._prepare_gen(kind, project, shot_id, payload)
        raise AppError(f"不支援的工作類型：{kind}")

    def execute(self, ctl: JobControl) -> dict:
        if ctl.kind in LOCAL_KINDS:
            return self._local.execute(ctl)
        if ctl.kind == "voice":
            return self._voice.execute(ctl)
        if ctl.kind == "transcribe":
            return self._stt.execute(ctl)
        work = self._store.project_dir(ctl.pid) / "media" / "cache" / f"job-{ctl.id}"
        work.mkdir(parents=True, exist_ok=True)
        try:
            if ctl.kind == "upscalevideo":
                return self._run_upscale_video(ctl, work)
            if ctl.payload.get("backend") == "provider":
                return self._run_provider(ctl, work)
            return self._run_comfy(ctl, work)
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def after_restart(self, job: dict) -> str:
        if job.get("kind") in (*LOCAL_KINDS, "voice", "transcribe"):
            return "restart"
        remote = job.get("remote") or {}
        if remote.get("type") == "comfy":
            return "resume"
        if remote.get("type") == "provider":
            try:
                return "resume" if providers.get(str(remote.get("provider"))).resumable else "interrupt"
            except AppError:
                return "interrupt"
        # Nothing was handed over yet. Repeating a ComfyUI run is free; a cloud call may already be billed.
        return "interrupt" if (job.get("payload") or {}).get("backend") == "provider" else "restart"

    # ---------------------------------------------------------------- image / video requests
    def _prepare_gen(self, kind: str, project: dict, shot_id: str | None, payload: dict) -> dict:
        index, shot = find_shot(project, shot_id)
        defaults = self._settings.section("defaults") or {}
        route = self._settings.route(payload.get("route") or shot.get("route") or defaults.get("route"))
        if payload.get("provider"):
            backend = "provider"
        elif payload.get("workflow"):
            backend = "comfy"
        else:
            backend = (route.get(kind) or {}).get("backend") or "none"
        if backend not in ("comfy", "provider"):
            raise AppError(f"路由「{route['name']}」沒有設定{KIND_LABEL[kind]}後端，請到「引擎」頁設定")

        prompt, negative = self._prompts(kind, project, shot, payload, defaults)
        item = self._source(kind, project, shot, payload)
        allow_text = backend == "comfy" and self._allows_text(kind, shot, payload, route, item)
        prompt, dropped = (prompt, []) if allow_text else notext.strip_text_directions(prompt)
        if not prompt and dropped and (kind == "image" or item is None):
            raise AppError("提示詞只有字卡或字幕的指示，畫面不畫文字（請在剪輯台加），請改寫成畫面內容")
        if not prompt and (kind == "image" or item is None):
            raise AppError("請先填寫鏡頭描述或生圖提示詞" if kind == "image" else "請先填寫鏡頭描述，或先準備鏡頭圖片")
        fallback = shot.get("duration") or defaults.get("shotDuration") or 5.0
        duration = round(clamp(number(payload.get("duration"), fallback), 0.5, 60.0), 2)

        notes: list[str] = [notext.dropped_note(dropped)] if dropped else []
        if backend == "comfy":
            plan, item = self._plan_comfy(kind, project, shot, payload, route, item, duration, notes)
        else:
            plan, item = self._plan_provider(kind, project, payload, route, item, notes)
        if not prompt and item is None:
            raise AppError("這個生成方式只能用文字生成，請先填寫鏡頭描述")
        lengths = plan.pop("lengths", None)
        uses_denoise = plan.pop("usesDenoise", True)
        if lengths:
            duration = self._video_seconds(shot, payload, duration, lengths, plan["provider"], notes)
        if plan.get("frames"):
            duration = round(plan["frames"] / plan["fps"], 2)

        seed, locked = self._seed(shot, payload)
        if backend == "provider":
            seed %= PROVIDER_SEED_RANGE
        settings = project["settings"]
        width = int(round(clamp(number(payload.get("width"), settings["width"]), 64, 4096)))
        height = int(round(clamp(number(payload.get("height"), settings["height"]), 64, 4096)))
        wanted = (width, height)
        if min(wanted) > GEN_SHORT_SIDE:
            width, height = (snap16(v * GEN_SHORT_SIDE / min(wanted)) for v in wanted)
        template_capped = False
        if backend == "comfy":
            # A template made for a lower resolution (LTX-Video at 480P) gets a scaled-down frame of the
            # same shape; the export scales every clip to the project size anyway.
            cap, short = plan.pop("maxShortSide", 0), min(width, height)
            if cap and short > cap:
                width, height = width * cap / short, height * cap / short
            snap = snap32 if kind == "video" else snap16
            width, height = snap(width), snap(height)
            if cap and short > cap:
                notes.append(f"這個範本的短邊上限是 {cap}，以 {width}×{height} 生成，匯出時會放大成專案尺寸")
                template_capped = True
        if min(wanted) > GEN_SHORT_SIDE and not template_capped:
            notes.append(f"專案尺寸是 {wanted[0]}×{wanted[1]}，以 {width}×{height} 生成，匯出時等比放大成專案尺寸")
        denoise = None
        if kind == "image" and item is not None and uses_denoise:
            strength = clamp(number(payload.get("refStrength"), shot.get("refStrength") or 0.6), 0.05, 1.0)
            denoise = round(clamp(number(payload.get("denoise"), 1.0 - strength * 0.75), 0.05, 1.0), 2)

        lane = plan.pop("lane")
        job_payload = {
            "backend": backend, "route": route["id"], **plan, "prompt": prompt, "negative": negative,
            "seed": seed, "seedLocked": locked, "width": width, "height": height, "duration": duration,
            "image": item["id"] if item else None, "denoise": denoise,
        }
        if kind == "image":
            job_payload["chainVideo"] = bool(payload.get("chainVideo"))
        upscale = self._settings.section("video2x") or {}
        if kind == "video" and backend == "comfy" and upscale.get("enabled"):
            video2x.require()
            method = upscale.get("method") if upscale.get("method") in video2x.METHODS else "anime4k"
            job_payload["upscale1080"] = method
            notes.append(f"生成後用 Video2X（{video2x.METHODS[method]}）放大到 1080P，再放上時間軸")
            if method == "realesrgan":
                notes.append("Real-ESRGAN 較慢，5 秒的片段約要 5 到 6 分鐘")
        return {"title": f"{KIND_LABEL[kind]}：{shot_label(index, shot)}", "lane": lane, "shotId": shot["id"],
                "notes": notes, "payload": job_payload}

    @staticmethod
    def _video_seconds(shot: dict, payload: dict, fallback: float, lengths: list, pid: str, notes: list[str]) -> float:
        """The model length closest to the shot's chosen video seconds, or to the shot length when none is chosen."""
        chosen = number(payload.get("duration"), 0) or shot.get("videoDuration")
        want = float(chosen or fallback)
        picked = duration_seconds(pick_duration(want, lengths))
        if chosen and abs(picked - want) > 0.01:
            notes.append(f"{providers.get(pid).name} 的這個模型沒有 {want:g} 秒，改用 {picked:g} 秒")
        return picked

    @staticmethod
    def _prompts(kind: str, project: dict, shot: dict, payload: dict, defaults: dict) -> tuple[str, str]:
        """Shot text plus the project style; a video prompt also carries the motion and camera notes."""
        style = project.get("style") or {}
        base = _text(payload.get("prompt"), 8000) or shot.get("prompt") or ""
        if kind == "image":
            # The shot's own picture prompt is the whole text, style included, so it goes out unchanged
            own = "" if payload.get("prompt") else (shot.get("imagePrompt") or "").strip()
            prompt = own or compose_prompt(base, style.get("prompt") or "")
        else:
            motion = _text(payload.get("motion"), 4000) or shot.get("motion") or ""
            prompt = compose_prompt(base, motion, shot.get("camera") or "", style.get("prompt") or "")
        negative = (_text(payload.get("negative"), 4000)
                    or compose_prompt(shot.get("negative") or "", style.get("negative") or "")
                    or defaults.get("negative") or "")
        return prompt, negative

    def _source(self, kind: str, project: dict, shot: dict, payload: dict) -> dict | None:
        """The reference image (image jobs) or start frame (video jobs) of the request, checked on disk."""
        if kind == "image":
            wanted = None if payload.get("useRef") is False else (payload.get("ref") or shot.get("ref"))
            missing, wrong = "找不到參考圖（可能已被刪除）", "參考圖必須是圖片"
        else:
            use = payload.get("useImage")
            if not isinstance(use, bool):  # the request does not say: the shot's own setting decides
                use = shot.get("videoUseImage") is not False
            wanted = (payload.get("image") or shot.get("activeImage")) if use else None
            missing, wrong = "找不到鏡頭圖片（可能已被刪除）", "影片的起始畫面必須是圖片"
        if not wanted:
            return None
        item = find_item(project, str(wanted))
        if item is None:
            raise AppError(missing, 404)
        if item.get("kind") != "image":
            raise AppError(wrong)
        item_path(self._store.project_dir(project["id"]), item)
        return item

    @staticmethod
    def _seed(shot: dict, payload: dict) -> tuple[int, bool]:
        """(seed, locked): an explicit seed, the shot's locked seed (plus an offset for variations) or a random one."""
        if payload.get("seed") not in (None, ""):
            return int(clamp(number(payload["seed"], 0), 0, MAX_SEED)), True
        if shot.get("seedLocked"):
            offset = int(number(payload.get("seedOffset"), 0))
            return (int(shot.get("seed") or 0) + offset) % (MAX_SEED + 1), True
        return random_seed(), False

    @staticmethod
    def _workflow_id(kind: str, shot: dict, payload: dict, route: dict, item: dict | None) -> str:
        """The ComfyUI template a request runs: its own, the shot's, else the route's (I2I one with a reference)."""
        section = route.get(kind) or {}
        wid = payload.get("workflow") or shot.get(f"{kind}Workflow")
        if not wid and kind == "image" and item is not None:
            wid = section.get("workflowI2I")
        return str(wid or section.get("workflow") or "")

    def _allows_text(self, kind: str, shot: dict, payload: dict, route: dict, item: dict | None) -> bool:
        wid = self._workflow_id(kind, shot, payload, route, item)
        try:
            return bool(wid) and bool(self._workflows.get(wid).get("allowText"))
        except AppError:  # a missing template is reported by _plan_comfy
            return False

    def _plan_comfy(self, kind: str, project: dict, shot: dict, payload: dict, route: dict, item: dict | None,
                    duration: float, notes: list[str]) -> tuple[dict, dict | None]:
        section = route.get(kind) or {}
        wid = self._workflow_id(kind, shot, payload, route, item)
        if not wid:
            raise AppError(f"路由「{route['name']}」還沒有指定 ComfyUI 範本，請到「引擎」頁設定")
        tpl = self._workflows.get(wid)
        name, mode = tpl["name"], tpl["mode"]
        if tpl["kind"] != kind:
            raise AppError(f"範本「{name}」不是{KIND_LABEL[kind]}範本")
        if mode in ("i2i", "i2v") and item is None:
            raise AppError(f"範本「{name}」需要參考圖，請先在鏡頭設定參考圖" if kind == "image"
                           else f"範本「{name}」需要鏡頭圖片，請先生成或匯入圖片")
        if mode in ("t2i", "t2v") and item is not None:
            notes.append(f"範本「{name}」不使用參考圖，已略過" if kind == "image"
                         else f"範本「{name}」不使用鏡頭圖片，改用文字生成")
            item = None
        fps = round(float(tpl.get("defaultFps") or project["settings"]["fps"]))
        frames = 0
        if kind == "video":
            # The shot's chosen video seconds win over the shot length, like on the cloud providers
            chosen = number(payload.get("duration"), 0) or shot.get("videoDuration")
            want = clamp(float(chosen or duration), 0.5, 60.0)
            max_frames = int(tpl.get("maxFrames") or 0)
            frames = wf.frames_for(want, fps, tpl.get("frameRule", "any"), max_frames)
            if max_frames and want > max_frames / fps + 0.05:
                notes.append(f"這個範本一次最多 {max_frames / fps:.1f} 秒，影片會比"
                             f"{'指定的秒數' if chosen else '鏡頭長度'}短，可在剪輯台調整")
        plan = {
            "lane": "comfy",
            "workflow": tpl["id"],
            "server": str(payload.get("server") or section.get("server") or "auto"),
            "provider": None,
            "mode": mode,
            "params": template_params(tpl, shot.get(f"{kind}Params"), payload.get("params")),
            "fps": fps,
            "frames": frames,
            "maxShortSide": int(tpl.get("maxShortSide") or 0),  # taken out again by _prepare_gen
            "usesDenoise": "denoise" in tpl["bindings"],  # taken out again by _prepare_gen
            "allowText": bool(tpl.get("allowText")),
        }
        layout = plan["params"].get("layout")
        if isinstance(layout, str):
            try:
                json.loads(layout)
            except json.JSONDecodeError as exc:
                raise AppError(f"元素表不是正確的 JSON（第 {exc.lineno} 行第 {exc.colno} 個字附近），"
                               "請檢查引號、逗號與括號") from None
        if item is not None and kind == "image":
            if "images" in tpl["bindings"]:
                plan["images"] = self._ref_images(project, shot, item)
            elif shot.get("extraRefs"):
                notes.append(f"範本「{name}」只讀 1 張參考圖，額外參考圖已略過")
        return plan, item

    def _ref_images(self, project: dict, shot: dict, item: dict) -> list[str]:
        """The reference picture of the request followed by the shot's extra ones, checked on disk."""
        ids = list(dict.fromkeys([item["id"], *(shot.get("extraRefs") or [])]))[:MAX_REFS]
        for rid in ids[1:]:
            extra = find_item(project, rid)
            if extra is None:
                raise AppError("找不到額外參考圖（可能已被刪除）", 404)
            if extra.get("kind") != "image":
                raise AppError("額外參考圖必須是圖片")
            item_path(self._store.project_dir(project["id"]), extra)
        return ids

    def _plan_provider(self, kind: str, project: dict, payload: dict, route: dict, item: dict | None,
                       notes: list[str]) -> tuple[dict, dict | None]:
        section = route.get(kind) or {}
        pid = payload.get("provider") or section.get("provider")
        if not pid:
            raise AppError(f"路由「{route['name']}」還沒有指定雲端供應商，請到「引擎」頁設定")
        prov = providers.get(str(pid))
        if prov.kind != kind:
            raise AppError(f"「{prov.name}」不是{KIND_LABEL[kind]}供應商")
        text_mode, image_mode = ("t2i", "i2i") if kind == "image" else ("t2v", "i2v")
        mode = image_mode if item is not None else text_mode
        if mode not in prov.modes and item is not None:
            notes.append(f"{prov.name} 不支援參考圖，已略過" if kind == "image"
                         else f"{prov.name} 不支援圖生影片，改用文字生成")
            item, mode = None, text_mode
        if mode not in prov.modes:
            what = "鏡頭圖片" if kind == "video" else "參考圖"
            raise AppError(f"{prov.name} 需要{what}才能生成，請先準備好{what}")
        # The custom HTTP providers work without a key; every vendor API needs its keys before anything is sent.
        if not prov.id.startswith("custom-"):
            owner = prov.secret_owner()
            for s in prov.secrets:
                if not self._secrets.get(f"provider.{owner}.{s['key']}").strip():
                    where = prov.name if owner == prov.id else providers.get(owner).name
                    raise AppError(f"尚未填寫{where}的{s['label']}，請到「引擎 › 雲端 API」補上")
        config = prov.resolve_config((self._settings.section("providers") or {}).get(prov.id))
        entry = self._catalog.get(prov.id, prov.catalog_source(config)) if prov.model_keys else None
        model_key = prov.model_keys.get(mode)
        if model_key:
            # A retired model is only flagged: the vendor has the final word, so the job still goes out.
            warning = catalog.model_warning(entry, model_key, config[model_key], prov.vendor)
            if warning:
                notes.append(warning)
        plan = {
            "lane": f"provider:{prov.id}",
            "workflow": None,
            "server": None,
            "provider": prov.id,
            "mode": mode,
            "params": {},
            "fps": round(float(project["settings"]["fps"])),
            "frames": 0,
        }
        if kind == "video":
            plan["lengths"] = prov.durations(config, mode, entry)  # taken out again by _prepare_gen
        return plan, item

    # ---------------------------------------------------------------- upscale requests
    def _prepare_upscale(self, project: dict, shot_id: str | None, payload: dict) -> dict:
        index, shot = find_shot(project, shot_id)
        wanted = payload.get("source") or shot.get("activeImage")
        if not wanted:
            raise AppError("這個鏡頭還沒有圖片，請先生成或匯入圖片")
        item = find_item(project, str(wanted))
        if item is None:
            raise AppError("找不到要放大的圖片（可能已被刪除）", 404)
        if item.get("kind") != "image":
            raise AppError("放大需要圖片")
        item_path(self._store.project_dir(project["id"]), item)
        comfy = self._settings.section("comfy") or {}
        tpl = self._workflows.get(str(payload.get("workflow") or comfy.get("upscaleWorkflow") or "upscale-ultrasharp"))
        if tpl["kind"] != "upscale":
            raise AppError(f"範本「{tpl['name']}」不是放大範本")
        defaults = self._settings.section("defaults") or {}
        style = project.get("style") or {}
        prompt, dropped = notext.strip_text_directions(
            item.get("prompt") or compose_prompt(shot.get("prompt") or "", style.get("prompt") or ""))
        negative = (item.get("negative") or compose_prompt(shot.get("negative") or "", style.get("negative") or "")
                    or defaults.get("negative") or "")
        known = item.get("seed")
        seed = int(clamp(number(payload.get("seed"), known if known is not None else random_seed()), 0, MAX_SEED))
        return {
            "title": f"放大：{shot_label(index, shot)}",
            "lane": "comfy",
            "shotId": shot["id"],
            "notes": [notext.dropped_note(dropped)] if dropped and "prompt" in tpl["bindings"] else [],
            "payload": {
                "backend": "comfy", "route": None, "workflow": tpl["id"],
                "server": str(payload.get("server") or "auto"), "provider": None, "mode": tpl["mode"],
                "params": template_params(tpl, None, payload.get("params")), "fps": 0, "frames": 0,
                "prompt": prompt, "negative": negative, "seed": seed, "seedLocked": True,
                "width": int(item.get("width") or 0), "height": int(item.get("height") or 0), "duration": 0.0,
                "image": item["id"], "denoise": None,
            },
        }

    def _prepare_upscale_video(self, project: dict, shot_id: str | None, payload: dict) -> dict:
        """Video2X on a video the shot already has; it shares the "comfy" lane so the two never fight over VRAM."""
        index, shot = find_shot(project, shot_id)
        item = find_item(project, payload.get("source") or shot.get("activeVideo"))
        if not item:
            raise AppError("這個鏡頭還沒有影片")
        if item.get("kind") != "video":
            raise AppError("Video2X 放大需要影片")
        item_path(self._store.project_dir(project["id"]), item)
        if video2x.target_size(int(item.get("width") or 0), int(item.get("height") or 0)) is None:
            raise AppError(f"這段影片是 {item.get('width')}×{item.get('height')}，已經是 1080P 以上，不需要放大")
        video2x.require()
        method = payload.get("method") or (self._settings.section("video2x") or {}).get("method")
        method = method if method in video2x.METHODS else "anime4k"
        notes = ["Real-ESRGAN 較慢，5 秒的片段約要 5 到 6 分鐘"] if method == "realesrgan" else []
        return {"title": f"Video2X 放大：{shot_label(index, shot)}", "lane": "comfy", "shotId": shot["id"],
                "notes": notes, "payload": {"backend": "local", "source": item["id"], "method": method}}

    def _run_upscale_video(self, ctl: JobControl, work: Path) -> dict:
        p = ctl.payload
        pdir = self._store.project_dir(ctl.pid)
        item = find_item(self._store.get(ctl.pid), p["source"])
        if not item:
            raise AppError("來源影片已被刪除", 410)
        media = self._media()
        media.require()
        src = item_path(pdir, item)
        info = media.probe(src)
        size = video2x.target_size(info["width"] or 0, info["height"] or 0)
        if size is None:
            raise AppError(f"這段影片是 {info['width']}×{info['height']}，已經是 1080P 以上，不需要放大")
        label = video2x.METHODS[p["method"]]
        out = self._video2x_clip(ctl, media, src, work / "upscaled.mp4", size, p["method"], 0.02, 0.9)
        ctl.check()
        tid = new_id("t")
        staged = stage_file(media, pdir, out, TAKE_DIR, tid, cancelled=ctl.cancelled, on_progress=ctl.progress)
        source = {**(item.get("source") or {}), "upscaler": f"Video2X {label}"}
        take = new_take(
            ctl.shot_id, staged, take_id=tid, job_id=ctl.id, parent=item["id"], label=f"放大 {size[0]}×{size[1]}",
            source=source, prompt=item.get("prompt") or "", negative=item.get("negative") or "",
            seed=item.get("seed"), params={**(item.get("params") or {}), "upscaler": p["method"]},
        )
        commit_takes(self._store, ctl.pid, [take], "activeVideo")
        return {"takeIds": [tid], "message": "影片放大完成"}

    def _video2x_clip(self, ctl: JobControl, media: Media, src: Path, dst: Path, size: tuple[int, int],
                      method: str, start: float, span: float) -> Path:
        """Upscale one clip to `size`, reporting progress from `start` over `span`; raises when it fails."""
        step = f"用 Video2X（{video2x.METHODS[method]}）放大到 {size[0]}×{size[1]}"
        ctl.progress(start, step)
        video2x.upscale(media, src, dst, *size, method, cancelled=ctl.cancelled,
                        on_frames=lambda done, total: ctl.progress(
                            start + span * done / max(total, 1), f"{step}：{done}/{total} 格"))
        got = media.probe(dst)
        if (got["width"], got["height"]) != size:
            raise AppError(f"輸出尺寸是 {got['width']}×{got['height']}，不是 {size[0]}×{size[1]}", 500)
        return dst

    # ---------------------------------------------------------------- running
    def _run_comfy(self, ctl: JobControl, work: Path) -> dict:
        p = ctl.payload
        tpl = self._workflows.get(p["workflow"])
        remote = ctl.remote_data or {}
        if remote.get("type") == "comfy":
            sid = str(remote["server"])
            results = self._comfy.resume(sid, str(remote["promptId"]), dst_dir=work, outputs=remote.get("outputs"),
                                         on_progress=ctl.progress, on_preview=ctl.preview, cancelled=ctl.cancelled)
        else:
            sid = self._comfy.pick_server(p.get("server") or "auto", tpl)
            check = self._comfy.check_template(sid, tpl)
            if not check["ok"]:
                raise AppError(f"ComfyUI 還不能執行「{tpl['name']}」：{wf.describe_missing(check)}", 409)
            if tpl.get("promptLanguage") == "en":
                self._english_prompt(ctl)
            graph = wf.build_graph(tpl, self._graph_values(ctl, sid))
            outputs = tpl["outputs"] or None
            results = self._comfy.run(
                sid, graph, dst_dir=work, outputs=outputs, on_progress=ctl.progress, on_preview=ctl.preview,
                cancelled=ctl.cancelled,
                on_submitted=lambda prompt_id: ctl.remote(
                    {"type": "comfy", "server": sid, "promptId": prompt_id, "outputs": outputs}),
            )
        want = "video" if ctl.kind == "video" else "image"
        files = [r["path"] for r in results if r["kind"] == want]
        if not files:
            raise AppError(f"ComfyUI 沒有產生{'影片' if want == 'video' else '圖片'}，請確認範本的輸出節點", 502)
        source = {"backend": "comfy", "server": sid, "workflow": tpl["id"], "workflowName": tpl["name"],
                  "route": p.get("route")}
        if p.get("upscale1080"):
            method = p["upscale1080"] if p["upscale1080"] in video2x.METHODS else "anime4k"
            files = self._upscale_videos(ctl, files, source, method)
        return self._commit(ctl, files, source)

    def _english_prompt(self, ctl: JobControl) -> None:
        """A Chinese prompt goes through the text model first; the job and its take keep the English that was sent.

        Without a working text model the original is sent and the job notes say why the motion may be ignored.
        """
        text = ctl.payload.get("prompt") or ""
        if not _CJK_RE.search(text):
            return
        ctl.progress(0.01, "把提示詞翻成英文")
        try:
            english = self._llm.translate(text)
        except AppError as exc:
            ctl.note(f"這個範本只讀得懂英文，提示詞含中文但沒有翻譯，照原文送出，動作與運鏡描述可能被忽略：{exc.message}")
            return
        ctl.check()
        ctl.update_payload({"prompt": english, "promptOriginal": text})
        ctl.note("這個範本只讀得懂英文，提示詞已用文字模型翻成英文再送出")

    def _graph_values(self, ctl: JobControl, sid: str) -> dict[str, Any]:
        """Values for the template bindings. Template parameters may override denoise, never the core inputs."""
        p = ctl.payload
        values: dict[str, Any] = {"denoise": p.get("denoise"), **(p.get("params") or {})}
        # The time suffix makes every run (a retry too) a different graph, so ComfyUI writes a fresh output
        # file instead of answering from its cache with the previous run's file.
        # The no-text clause is added only here, so the job and its take keep the prompt as written.
        # A template that draws lettering on purpose (allowText) gets the prompt unchanged.
        prompt, negative = p.get("prompt", ""), p.get("negative", "")
        if not p.get("allowText"):
            prompt, negative = notext.with_guard(prompt), notext.with_text_negative(negative)
        values.update(prompt=prompt, negative=negative, seed=p.get("seed"),
                      width=p.get("width"), height=p.get("height"), prefix=f"vc/{ctl.id}-{int(time.time())}")
        if p.get("frames"):
            values.update(frames=p["frames"], fps=p["fps"])
        if p.get("images"):
            ctl.progress(0.01, f"上傳 {len(p['images'])} 張參考圖到 ComfyUI")
            values["images"] = [self._comfy.upload_image(sid, self._reference(ctl, rid)[0]) for rid in p["images"]]
        elif p.get("image"):
            ctl.progress(0.01, "上傳圖片到 ComfyUI")
            data, _mime = self._reference(ctl)
            values["image"] = self._comfy.upload_image(sid, data)
        return values

    def _upscale_videos(self, ctl: JobControl, files: list[Path], source: dict, method: str) -> list[Path]:
        """Video2X brings each clip up to 1080P before it becomes a take; a failed upscale keeps the original clip."""
        media = self._media()
        label = video2x.METHODS[method]
        out: list[Path] = []
        for path in files:
            ctl.check()
            try:
                info = media.probe(path)
                size = video2x.target_size(info["width"] or 0, info["height"] or 0)
                if size is None:
                    out.append(path)
                    continue
                dst = self._video2x_clip(ctl, media, path, path.with_name(f"{path.stem}-1080p.mp4"), size, method,
                                         0.95, 0.04)
            except AppError as exc:
                if exc.status == 499:
                    raise
                ctl.note(f"Video2X 放大失敗，保留原本的影片：{exc.message}")
                out.append(path)
                continue
            source["upscaler"] = f"Video2X {label}"
            out.append(dst)
        return out

    def _run_provider(self, ctl: JobControl, work: Path) -> dict:
        p = ctl.payload
        prov = providers.get(p["provider"])
        owner = prov.secret_owner()
        config = prov.resolve_config((self._settings.section("providers") or {}).get(prov.id))
        ctx = providers.Context(
            prov.id, config, lambda key: self._secrets.get(f"provider.{owner}.{key}"), dst_dir=work,
            on_progress=ctl.progress, cancelled=ctl.cancelled,
            on_remote=lambda data: ctl.remote({"type": "provider", "provider": prov.id, "data": data}),
            models=self._catalog.get(prov.id, prov.catalog_source(config)) if prov.model_keys else None,
        )
        remote = ctl.remote_data or {}
        try:
            if remote.get("type") == "provider":
                files = prov.resume(ctx, remote.get("data") or {})
            else:
                image, mime = self._reference(ctl) if p.get("image") else (None, "image/png")
                req = providers.Request(
                    kind=ctl.kind, prompt=notext.with_guard(p["prompt"]),
                    negative=notext.with_text_negative(p["negative"]), width=p["width"], height=p["height"],
                    seed=p["seed"], image=image, image_mime=mime, denoise=p.get("denoise") or 0.55,
                    duration=p["duration"], fps=p["fps"],
                )
                files = prov.run(ctx, prov.check_mode(ctx, req))
        except AppError as exc:
            handle = ctl.remote_data or {}
            if exc.status == 499 and handle.get("type") == "provider":
                try:
                    prov.cancel_remote(ctx, handle.get("data") or {})
                except Exception as cancel_exc:  # noqa: BLE001 - best effort, the job is canceled either way
                    # Only the message or the type: error texts of other exceptions may carry a key-bearing URL.
                    log.warning("Could not cancel the %s task: %s", prov.id,
                                getattr(cancel_exc, "message", type(cancel_exc).__name__))
            raise
        finally:
            for text in ctx.notes:
                ctl.note(text)
        want = "video" if ctl.kind == "video" else "image"
        files = [f for f in files if kind_of(f.name) == want]
        if not files:
            raise AppError(f"{prov.name} 沒有回傳{'影片' if want == 'video' else '圖片'}", 502)
        model = config.get("modelT2V") if p.get("mode") == "t2v" and config.get("modelT2V") else config.get("model")
        source = {"backend": "provider", "provider": prov.id, "providerName": prov.name, "route": p.get("route"),
                  "model": str(model or "")}
        return self._commit(ctl, files, source)

    def _reference(self, ctl: JobControl, item_id: str | None = None) -> tuple[bytes, str]:
        item = find_item(self._store.get(ctl.pid), item_id or ctl.payload.get("image"))
        if item is None:
            raise AppError("來源圖片已被刪除", 410)
        return reference_image(self._media(), item_path(self._store.project_dir(ctl.pid), item))

    def _commit(self, ctl: JobControl, files: list[Path], source: dict) -> dict:
        """Stage the outputs as takes; the first becomes the shot's active picture or video."""
        p = ctl.payload
        pdir = self._store.project_dir(ctl.pid)
        media = self._media()
        used = dict(p.get("params") or {})
        if ctl.kind != "upscale":
            used.update(width=p.get("width"), height=p.get("height"))
        if p.get("denoise") is not None:
            used["denoise"] = p["denoise"]
        if ctl.kind == "video":
            used.update(duration=p.get("duration"), fps=p.get("fps"))
            if p.get("frames"):
                used["frames"] = p["frames"]
        # A translated prompt: the take keeps the English that was sent plus the original text
        original = {"promptOriginal": p["promptOriginal"]} if p.get("promptOriginal") else {}
        ctl.progress(0.97, "整理輸出檔")
        takes: list[dict] = []
        for path in files:
            ctl.check()
            tid = new_id("t")
            staged = stage_file(media, pdir, path, TAKE_DIR, tid, cancelled=ctl.cancelled, on_progress=ctl.progress)
            takes.append({**new_take(
                ctl.shot_id, staged, take_id=tid, job_id=ctl.id, source=source, prompt=p.get("prompt", ""),
                negative=p.get("negative", ""), seed=p.get("seed"), params=used, parent=p.get("image"),
                label="放大" if ctl.kind == "upscale" else "",
            ), **original})
        slot = "activeVideo" if ctl.kind == "video" else "activeImage"
        # An unlocked shot remembers the seed of its newest picture, so locking it keeps that look.
        extra = {"seed": p["seed"]} if ctl.kind == "image" and not p.get("seedLocked") else None
        commit_takes(self._store, ctl.pid, takes, slot, extra)
        result: dict[str, Any] = {"takeIds": [t["id"] for t in takes], "message": f"{KIND_LABEL[ctl.kind]}完成"}
        if ctl.kind == "image" and p.get("chainVideo"):
            try:
                result["followUp"] = ctl.spawn("video", {"image": takes[0]["id"], "route": p.get("route")},
                                               ctl.shot_id)["id"]
            except AppError as exc:
                ctl.note(f"接續生成影片失敗：{exc.message}")
        return result
