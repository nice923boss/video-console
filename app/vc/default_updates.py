"""Saved settings that still hold a default the app has since replaced.

Defaults only fill fields that were never saved, so a computer that saved its routes, text model or
fal.ai / Replicate video settings before 2026-10-23 keeps the old Kling and Qwen picks. Those are offered
once instead of being changed silently; the answer is remembered in settings.ui.defaultsSeen.
"""
from __future__ import annotations

from collections.abc import Iterable

from . import providers
from .providers.catalog import family_of
from .settings import DEFAULTS, SettingsStore

REVISION = "2026-10-23"
OLD_LLM_MODEL = "qwen2.5:7b-instruct"
OLD_VIDEO_FAMILY = "kling"
ROUTE_ID = "B"
# provider -> model fields that defaulted to Kling before the revision
MODEL_FIELDS = {"fal-video": ("model", "modelT2V"), "replicate-video": ("model",)}
# reset together with the model: the Kling seconds and first-frame field do not fit the new default
COMPANION_FIELDS = {"fal-video": ("durations",), "replicate-video": ("imageField", "durations")}


def _new_route_video() -> dict:
    return next(r for r in DEFAULTS["routes"] if r["id"] == ROUTE_ID)["video"]


def pending(cfg: dict) -> list[dict]:
    """[{key, label, current, suggested}] for each saved value still on the old default; [] once answered."""
    if cfg["ui"].get("defaultsSeen") == REVISION:
        return []
    out = []
    route = next((r for r in cfg["routes"] if r.get("id") == ROUTE_ID), None)
    video = (route or {}).get("video") or {}
    if video.get("backend") == "provider" and video.get("provider") == OLD_VIDEO_FAMILY:
        out.append({"key": "route", "label": f"路由「{route.get('name') or ROUTE_ID}」的影片供應商",
                    "current": providers.get(OLD_VIDEO_FAMILY).name,
                    "suggested": providers.get(_new_route_video()["provider"]).name})
    for pid, keys in MODEL_FIELDS.items():
        p = providers.get(pid)
        stored = (cfg.get("providers") or {}).get(pid) or {}
        labels = {f["key"]: f["label"] for f in p.fields}
        for k in keys:
            if family_of(str(stored.get(k) or "")) == OLD_VIDEO_FAMILY:
                out.append({"key": f"{pid}.{k}", "label": f"{p.name}的{labels[k]}",
                            "current": stored[k], "suggested": p.defaults()[k]})
    if cfg["llm"].get("model") == OLD_LLM_MODEL:
        out.append({"key": "llm", "label": "文字模型", "current": OLD_LLM_MODEL, "suggested": DEFAULTS["llm"]["model"]})
    return out


def apply(settings: SettingsStore, keys: Iterable[str]) -> dict:
    """Switch the chosen items to the new defaults, keep the rest, and remember the question was answered."""
    cfg = settings.get()
    chosen = {item["key"] for item in pending(cfg)} & set(keys)
    patch: dict = {"ui": {"defaultsSeen": REVISION}}
    if "route" in chosen:
        patch["routes"] = [{**r, "video": dict(_new_route_video())} if r.get("id") == ROUTE_ID else r
                           for r in cfg["routes"]]
    for pid, fields in MODEL_FIELDS.items():
        switched = [k for k in fields if f"{pid}.{k}" in chosen]
        if switched:
            p = providers.get(pid)
            defaults = p.defaults()
            changes = {k: defaults[k] for k in (*switched, *COMPANION_FIELDS[pid])}
            current = p.resolve_config((cfg.get("providers") or {}).get(pid))
            patch.setdefault("providers", {})[pid] = p.validate_config({**current, **changes})
    if "llm" in chosen:
        patch["llm"] = {"model": DEFAULTS["llm"]["model"]}
    return settings.update(patch)
