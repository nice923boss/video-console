"""Model lists fetched from the vendors, so the settings page offers current models and flags retired ones.

One detection result per provider, kept in data/model-catalog.json:
    {"fetchedAt": epoch, "source": <config signature, e.g. the API base URL>,
     "fields": {field key: {"options": [{"value", "label", "date", "hint", "preview"}],  # callable, newest first
                            "suggested": id or None,  # newest model of the saved model's family and tier
                            "for": id,                # the model the suggestion was picked for
                            "known": {id: status},    # statuses learned for specific ids
                            "complete": bool,         # True: an id that is not listed is gone
                            "message": str,           # optional remark for the settings page
                            "durations": {id: [length as the vendor spells it]},  # video fields only
                            "ratios": {id: ["16:9", ...]}}}}  # fal video fields, models with a ratio list
Statuses: ok, deprecated, incompatible, missing (absent from a complete list), unlisted (a fal or Replicate
lookup found nothing: retired, a typo or a private app) and unknown (never checked).
"""
from __future__ import annotations

import datetime as dt
import re
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, NamedTuple

from .. import net
from ..util import AppError, read_json, write_json_atomic
from .base import Context, duration_seconds

FAL_MODELS_API = "https://api.fal.ai/v1/models"
FAL_PAGE = 10  # fal refuses more than 10 results per request once the input schema is included
FAL_FAMILIES = ("kling", "hailuo", "minimax", "seedance", "seedream", "veo", "wan", "ltx", "vidu", "pixverse",
                "hunyuan", "luma", "flux", "stable-diffusion", "nano-banana", "imagen", "ideogram", "recraft",
                "qwen-image", "hidream", "bria")
TIERS = frozenset({"mini", "nano", "lite", "flash", "pro", "fast", "turbo", "standard", "std", "master", "dev",
                   "schnell", "klein", "max", "ultra", "plus"})
OPENAI_IMAGE = re.compile(r"^(gpt-image|chatgpt-image|dall-e)")
_PREVIEW = re.compile(r"(^|[-_.])(preview|exp|experimental)($|[-_.\d])")


class FalWant(NamedTuple):
    category: str  # fal category, e.g. "image-to-video"
    sends: frozenset[str]  # parameters the app will send for this field
    current: str  # the model saved in the settings
    image: bool  # the app sends image_url


# ------------------------------------------------------------------ families, tiers, statuses
def family_of(model: str) -> str:
    """'fal-ai/kling-video/v2.1/pro/image-to-video' -> 'kling'; 'gpt-image-1-mini' -> 'gpt-image'."""
    low = model.lower().removeprefix("models/")
    if "/" in low:
        for fam in FAL_FAMILIES:
            if re.search(rf"(^|[/_-]){re.escape(fam)}", low):
                return fam
        owner, _, rest = low.partition("/")
        found = re.match(r"[a-z]+", rest.split("/")[0] if owner == "fal-ai" else owner)
        return found.group(0) if found else ""
    found = re.match(r"[a-z]+(?:-[a-z]+)*", low)
    return found.group(0) if found else ""


def tier_of(model: str) -> frozenset[str]:
    return frozenset(t for t in re.split(r"[/_.-]", model.lower()) if t in TIERS)


def pick_newest(current: str, options: list[dict]) -> str | None:
    """Newest option (options are newest first) of the current model's family, stable before preview.
    Tier first: the same tier, else one that keeps it ("standard" -> "turbo/standard"), else any, so a cheap
    tier is not swapped for an expensive one while a cheap one exists."""
    fam = family_of(current)
    if not fam:
        return None
    tier = tier_of(current)
    same_family = [o for o in options if family_of(o["value"]) == fam]
    same_tier = [o for o in same_family if tier_of(o["value"]) == tier]
    keeps_tier = [o for o in same_family if tier <= tier_of(o["value"])]
    pool = same_tier or keeps_tier or same_family
    if not pool:
        return None
    return next((o for o in pool if not o.get("preview")), pool[0])["value"]


def same_line(current: str, other: str) -> bool:
    """`other` is a newer version of the same product at the same tier, not a lighter or pricier variant."""
    tier, other_tier = tier_of(current), tier_of(other)
    return (bool(family_of(current)) and family_of(current) == family_of(other)
            and (other_tier == tier or bool(tier) and tier <= other_tier))


def model_status(entry: dict | None, key: str, value: str) -> str:
    field = ((entry or {}).get("fields") or {}).get(key)
    value = (value or "").strip().removeprefix("models/")
    if not field or not value:
        return "unknown"
    if any(o.get("value") == value for o in field.get("options") or []):
        return "ok"
    return (field.get("known") or {}).get(value) or ("missing" if field.get("complete") else "unknown")


_WARNINGS = {
    "deprecated": "模型 {model} 已被{vendor}標記為停用",
    "missing": "{vendor}的模型清單裡已找不到 {model}（可能已下架）",
    "unlisted": "{vendor}的公開模型清單查不到 {model}（可能已下架、打錯字，或是私人部署）",
    "incompatible": "模型 {model} 需要本程式不會送出的參數，送出後可能失敗",
}
UNUSABLE = frozenset(_WARNINGS)


def model_warning(entry: dict | None, key: str, value: str, vendor: str) -> str | None:
    """A job note when the saved model is retired or unusable; None when it is fine or never checked."""
    status = model_status(entry, key, value)
    if status not in _WARNINGS:
        return None
    text = _WARNINGS[status].format(model=value, vendor=vendor)
    suggested = (((entry or {}).get("fields") or {}).get(key) or {}).get("suggested")
    if suggested and suggested != value:
        return f"{text}，建議到「引擎 › 雲端 API」改用 {suggested}"
    return f"{text}，請到「引擎 › 雲端 API」重新偵測模型"


def model_durations(entry: dict | None, key: str, model: str) -> list | None:
    """Detected video lengths of `model`; [] when it takes none, None when they were never detected."""
    field = ((entry or {}).get("fields") or {}).get(key) or {}
    lengths = (field.get("durations") or {}).get((model or "").strip().strip("/"))
    return list(lengths) if isinstance(lengths, list) else None


def model_ratios(entry: dict | None, key: str, model: str) -> list[str]:
    """Detected aspect ratios of `model`; [] when they were never detected."""
    field = ((entry or {}).get("fields") or {}).get(key) or {}
    ratios = (field.get("ratios") or {}).get((model or "").strip().strip("/"))
    return [r for r in ratios if isinstance(r, str)] if isinstance(ratios, list) else []


def _field(options: list[dict], current: str, *, known: dict | None = None, complete: bool,
           message: str = "") -> dict:
    out = {"options": options, "suggested": pick_newest(current, options), "for": current, "known": known or {},
           "complete": complete}
    return {**out, "message": message} if message else out


# ------------------------------------------------------------------ fal.ai
def _fal_get(ctx: Context, params: dict) -> dict:
    key = ctx.secret("api_key")
    headers = {"Authorization": f"Key {key}"} if key else {}
    url = f"{FAL_MODELS_API}?{urllib.parse.urlencode(params, doseq=True)}"
    try:
        data = net.request("GET", url, headers=headers, timeout=30).json()
    except net.HttpError as exc:
        if exc.code == 429:
            raise AppError("fal.ai 暫時限制模型清單的查詢次數（429），請過一兩分鐘再按「重新偵測」", 429) from None
        raise
    return data if isinstance(data, dict) else {}


def _fal_input(model: dict) -> dict | None:
    schemas = ((model.get("openapi") or {}).get("components") or {}).get("schemas") or {}
    return next((s for k, s in schemas.items() if k.endswith("Input") and isinstance(s, dict)), None)


def _fal_fits(model: dict, want: FalWant, extra: set[str]) -> bool:
    """The model takes a prompt (and image_url for image modes) and needs nothing the app does not send."""
    schema = _fal_input(model)
    if schema is None:
        return False
    props = set(schema.get("properties") or {})
    required = set(schema.get("required") or [])
    return "prompt" in props and required <= (want.sends | extra) and (not want.image or "image_url" in props)


def _fal_option(model: dict) -> dict:
    meta = model.get("metadata") or {}
    option = {"value": model["endpoint_id"], "label": str(meta.get("display_name") or model["endpoint_id"]),
              "date": str(meta.get("date") or "")[:10]}
    duration = ((_fal_input(model) or {}).get("properties") or {}).get("duration") or {}
    if isinstance(duration.get("enum"), list) and duration["enum"]:
        option["hint"] = "秒數 " + "、".join(str(v) for v in duration["enum"])
    return option


def _lengths(spec: Any) -> list | None:
    """Lengths a `duration` parameter takes, as the vendor spells them ("5", 8 or "8s"). [] when there is no
    such parameter (Wan counts frames instead); None when the parameter is open-ended."""
    if spec is None:
        return []
    if not isinstance(spec, dict):
        return None
    if isinstance(spec.get("enum"), list):
        lengths = [v for v in spec["enum"] if duration_seconds(v)]
        return lengths or None
    low, high = spec.get("minimum"), spec.get("maximum")
    if spec.get("type") == "integer" and isinstance(low, int) and isinstance(high, int) and 0 < low <= high <= low + 60:
        return list(range(low, high + 1))
    return None


def _fal_durations(model: dict) -> list | None:
    """Lengths of the endpoint's `duration` (see _lengths); None when the schema is missing."""
    schema = _fal_input(model)
    return None if schema is None else _lengths((schema.get("properties") or {}).get("duration"))


def _fal_ratios(model: dict) -> list[str]:
    """The "16:9" style values of the endpoint's `aspect_ratio` enum; [] when it has none."""
    spec = ((_fal_input(model) or {}).get("properties") or {}).get("aspect_ratio")
    enum = spec.get("enum") if isinstance(spec, dict) else None
    return [v for v in enum if isinstance(v, str) and re.fullmatch(r"\d+:\d+", v)] if isinstance(enum, list) else []


def _fal_lookup(ctx: Context, ids: list[str]) -> dict[str, dict]:
    """Saved models by endpoint id. fal answers 404 for the whole batch when one id is unknown."""
    if not ids:
        return {}
    try:
        data = _fal_get(ctx, {"endpoint_id": ids, "expand": "openapi-3.0"})
    except net.HttpError as exc:
        if exc.code != 404:
            raise
        if len(ids) == 1:
            return {}
        found: dict[str, dict] = {}
        for one in ids:
            found.update(_fal_lookup(ctx, [one]))
        return found
    return {m["endpoint_id"]: m for m in data.get("models") or [] if isinstance(m, dict) and m.get("endpoint_id")}


def detect_fal(ctx: Context, wants: dict[str, FalWant], extra: set[str]) -> dict[str, dict]:
    """Per field: the newest active models of its category and of the saved model's family, plus the saved
    model's own status. fal lists thousands of models, so the list is never complete."""
    base = {"status": "active", "sort": "recent", "limit": FAL_PAGE, "expand": "openapi-3.0"}
    searches: dict[tuple[str, str], dict] = {}
    for want in wants.values():
        searches[(want.category, "")] = {**base, "category": want.category}
        fam = family_of(want.current)
        if fam:
            searches[(want.category, fam)] = {**base, "category": want.category, "q": fam}
    current = sorted({w.current for w in wants.values() if w.current})
    with ThreadPoolExecutor(max_workers=4) as pool:
        lookup = pool.submit(_fal_lookup, ctx, current)
        pages = {k: pool.submit(_fal_get, ctx, params) for k, params in searches.items()}
        found = lookup.result()
        results = {k: f.result().get("models") or [] for k, f in pages.items()}

    out: dict[str, dict] = {}
    for key, want in wants.items():
        seen: dict[str, dict] = {}
        lengths: dict[str, list] = {}
        ratios: dict[str, list] = {}
        for (category, _q), models in results.items():
            for m in models:
                if (category == want.category and isinstance(m, dict) and m.get("endpoint_id")
                        and (m.get("metadata") or {}).get("status", "active") == "active" and _fal_fits(m, want, extra)):
                    seen[m["endpoint_id"]] = _fal_option(m)
                    lengths[m["endpoint_id"]] = _fal_durations(m)
                    ratios[m["endpoint_id"]] = _fal_ratios(m)
        if want.current in found:
            lengths[want.current] = _fal_durations(found[want.current])
            ratios[want.current] = _fal_ratios(found[want.current])
        lengths = {k: v for k, v in lengths.items() if v is not None}
        ratios = {k: v for k, v in ratios.items() if v}
        options = sorted(seen.values(), key=lambda o: o["date"], reverse=True)
        known: dict[str, str] = {}
        if want.current:
            m = found.get(want.current)
            # a lookup sometimes comes back without the OpenAPI document; that proves nothing
            known[want.current] = ("unlisted" if m is None
                                   else "deprecated" if (m.get("metadata") or {}).get("status") != "active"
                                   else "ok" if _fal_input(m) is None or _fal_fits(m, want, extra)
                                   else "incompatible")
        field = _field(options, want.current, known=known, complete=False)
        out[key] = {**field, "durations": lengths, "ratios": ratios} if "video" in want.category else field
    return out


# ------------------------------------------------------------------ Replicate
class ReplicateWant(NamedTuple):
    collections: tuple[str, ...]  # Replicate collections whose models suit the field, e.g. "text-to-image"
    sends: frozenset[str]  # inputs the app will send
    current: str  # the model saved in the settings
    image_field: str  # the input the reference picture goes in ("" sends none)


REPLICATE_NAME = re.compile(r"^[\w.-]+/[\w.-]+(:[0-9a-f]{64})?$")
_COLLECTION_HINTS = {"image-to-video": "圖轉影片", "text-to-video": "文字轉影片"}


def _replicate_props(model: dict) -> tuple[dict, set[str]] | None:
    """Input parameters of the model's latest version, with Cog's enums resolved (Cog writes an enum as
    allOf: [{"$ref": ...}]), and the required ones. None when Replicate left the schema out, as lists do."""
    schemas = ((((model.get("latest_version") or {}).get("openapi_schema") or {}).get("components") or {})
               .get("schemas") or {})
    schema = schemas.get("Input")
    if not isinstance(schema, dict):
        return None
    props = {}
    for name, spec in (schema.get("properties") or {}).items():
        refs = spec.get("allOf") if isinstance(spec, dict) else None
        ref = refs[0] if isinstance(refs, list) and refs and isinstance(refs[0], dict) else {}
        target = schemas.get(str(ref.get("$ref") or "").rsplit("/", 1)[-1])
        props[name] = {**target, **spec} if isinstance(target, dict) else spec
    return props, set(schema.get("required") or [])


def _replicate_fits(found: tuple[dict, set[str]], want: ReplicateWant, extra: set[str]) -> bool:
    props, required = found
    return "prompt" in props and required <= (want.sends | extra)


def _name_version(model_id: str) -> tuple[int, ...]:
    """'lightricks/ltx-2.5-fast' -> (2, 5), 'kwaivgi/kling-v2.1' -> (2, 1); Replicate lists carry no release
    date, only the date of each model's latest push."""
    found = re.search(r"(?:^|[-_v])(\d+(?:\.\d+)*)", model_id.rsplit("/", 1)[-1])
    return tuple(int(x) for x in found.group(1).split(".")) if found else ()


def _replicate_get(url: str, auth: dict) -> dict | None:
    """None when Replicate answers 404 (an unknown model or collection)."""
    try:
        data = net.request("GET", url, headers=auth, timeout=30).json()
    except net.HttpError as exc:
        if exc.code == 404:
            return None
        raise
    return data if isinstance(data, dict) else {}


def detect_replicate(ctx: Context, base: str, wants: dict[str, ReplicateWant], extra: set[str]) -> dict[str, dict]:
    """Per field: the models of Replicate's hand-picked collections for it, plus the saved model's status, its
    video lengths and a remark when its picture input has another name. The collections are a small part of
    Replicate, so the list is never complete."""
    auth = {"Authorization": f"Bearer {ctx.require_secret('api_key', 'Replicate API Token')}"}
    slugs = sorted({s for w in wants.values() for s in w.collections})
    saved = sorted({w.current for w in wants.values() if REPLICATE_NAME.match(w.current)})

    def lookup(model: str) -> dict | None:
        name, _, version = model.partition(":")
        if not version:
            return _replicate_get(f"{base}/models/{name}", auth)
        found = _replicate_get(f"{base}/models/{name}/versions/{version}", auth)
        return None if found is None else {"latest_version": found}

    with ThreadPoolExecutor(max_workers=4) as pool:
        pages = {s: pool.submit(_replicate_get, f"{base}/collections/{s}", auth) for s in slugs}
        looks = {m: pool.submit(lookup, m) for m in saved}
        lists = {s: (f.result() or {}).get("models") or [] for s, f in pages.items()}
        found = {m: f.result() for m, f in looks.items()}

    out: dict[str, dict] = {}
    for key, want in wants.items():
        seen: dict[str, dict] = {}
        tags: dict[str, list[str]] = {}
        lengths: dict[str, list | None] = {}
        pictures: set[str] = set()  # models known to take the picture where the settings send it
        for slug in want.collections:
            for m in lists.get(slug) or []:
                if not isinstance(m, dict) or not m.get("owner") or not m.get("name"):
                    continue
                spec = _replicate_props(m)
                if spec is not None and not _replicate_fits(spec, want, extra):
                    continue
                mid = f"{m['owner']}/{m['name']}"
                seen.setdefault(mid, {"value": mid, "label": mid,
                                      "date": str((m.get("latest_version") or {}).get("created_at") or "")[:10]})
                tags.setdefault(mid, []).extend([_COLLECTION_HINTS[slug]] if slug in _COLLECTION_HINTS else [])
                if spec is not None:
                    lengths[mid] = _lengths(spec[0].get("duration"))
                    if not want.image_field or want.image_field in spec[0]:
                        pictures.add(mid)
        for mid, option in seen.items():
            hint = "、".join(tags[mid]) if len(want.collections) > 1 else ""
            secs = lengths.get(mid)
            hint = "；".join(x for x in (hint, "秒數 " + "、".join(str(v) for v in secs) if secs else "") if x)
            if hint:
                option["hint"] = hint
        known: dict[str, str] = {}
        message = ""
        if want.current in found:
            model = found[want.current]
            spec = None if model is None else _replicate_props(model)
            known[want.current] = ("unlisted" if model is None else "ok" if spec is None or _replicate_fits(spec, want, extra)
                                   else "incompatible")
            if spec is not None:
                lengths[want.current] = _lengths(spec[0].get("duration"))
                if want.image_field and want.image_field not in spec[0]:
                    files = [n for n, s in spec[0].items() if isinstance(s, dict) and s.get("format") == "uri"]
                    message = (f"{want.current} 沒有「{want.image_field}」參數，"
                               + (f"它的圖片參數是 {'、'.join(files)}，請改設定裡的圖片欄位名稱" if files
                                  else "看起來不收圖片"))
        options = sorted(seen.values(), key=lambda o: o["date"], reverse=True)
        field = _field(options, want.current, known=known, complete=False, message=message)
        # Push dates say little about which model is newer: pick by the version in the name, only among models
        # whose schema shows the picture input (an untouched default may be switched to the suggestion), and
        # never an older version than the saved one while that one still works.
        ready = [o for o in options if o["value"] in pictures]
        suggested = pick_newest(want.current, sorted(ready, key=lambda o: (_name_version(o["value"]), o["date"]),
                                                     reverse=True))
        if suggested and known.get(want.current) not in UNUSABLE and \
                _name_version(suggested) < _name_version(want.current.partition(":")[0]):
            suggested = None
        field = {**field, "suggested": suggested}
        if any("video" in s for s in want.collections):
            field = {**field, "durations": {k: v for k, v in lengths.items() if v is not None}}
        out[key] = field
    return out


# ------------------------------------------------------------------ OpenAI
def detect_openai(ctx: Context, base: str, key: str, current: str) -> dict:
    """GET /models: image models by creation date; a shutdown date marks a model as retiring."""
    data = net.request("GET", f"{base}/models", headers={"Authorization": f"Bearer {key}"}, timeout=20).json()
    rows = [m for m in (data or {}).get("data") or [] if isinstance(m, dict) and isinstance(m.get("id"), str)]
    image = [m for m in rows if OPENAI_IMAGE.match(m["id"])]
    if not image:
        # A compatible service with other names: list everything, but an absent model proves nothing.
        options = [{"value": m["id"], "label": m["id"], "date": _day(m.get("created"))} for m in rows]
        return _field(sorted(options, key=lambda o: o["value"]), current, complete=False,
                      message="這個服務的模型清單裡沒有 OpenAI 圖片模型的名稱，以下列出全部模型")
    image.sort(key=lambda m: m.get("created") or 0, reverse=True)
    known = {m["id"]: "deprecated" for m in image if m.get("shutdown_date")}
    options = [{"value": m["id"], "label": m["id"], "date": _day(m.get("created"))}
               for m in image if m["id"] not in known]
    return _field(options, current, known=known, complete=True)


def _day(epoch: Any) -> str:
    try:
        return dt.datetime.fromtimestamp(float(epoch), dt.timezone.utc).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


# ------------------------------------------------------------------ Google (Gemini API)
def _version(name: str) -> tuple[int, ...]:
    found = re.search(r"-(\d+(?:\.\d+)*)", name)
    return tuple(int(x) for x in found.group(1).split(".")) if found else ()


def detect_google(ctx: Context, base: str, key: str, what: str, current: str) -> dict:
    """GET /models (all pages). what: "image" (Gemini image models) or "video" (Gemini Omni). Google gives no
    dates, so models sort by the version in their name. Veo ids still listed count as retired: the video
    provider only speaks the Interactions API, and Google shuts every Veo model on the Gemini API down."""
    rows: list[dict] = []
    token = ""
    for _ in range(10):
        query = "pageSize=1000" + (f"&pageToken={urllib.parse.quote(token)}" if token else "")
        data = net.request("GET", f"{base}/models?{query}", headers={"x-goog-api-key": key}, timeout=20).json() or {}
        rows += [m for m in data.get("models") or [] if isinstance(m, dict) and isinstance(m.get("name"), str)]
        token = str(data.get("nextPageToken") or "")
        if not token:
            break
    picked, known = [], {}
    for m in rows:
        name, methods = m["name"].removeprefix("models/"), m.get("supportedGenerationMethods") or []
        if what == "video":
            # Omni is called through POST /interactions, so its listed methods are not checked.
            fits = name.startswith("gemini-omni")
            if name.startswith("veo"):
                known[name] = "deprecated"
        else:
            fits = name.startswith("gemini") and "image" in name and "generateContent" in methods and "omni" not in name
        if fits:
            preview = bool(_PREVIEW.search(name))
            label = str(m.get("displayName") or name)
            picked.append({"value": name, "label": label, "date": "", "preview": preview})
    picked.sort(key=lambda o: (_version(o["value"]), not o["preview"], o["value"]), reverse=True)
    if not picked:
        return _field([], current, known=known, complete=False,
                      message="這把金鑰的模型清單裡沒有" + ("Gemini Omni 影片模型" if what == "video" else "Gemini 圖片模型"))
    field = _field(picked, current, known=known, complete=True)
    if what == "video" and not field["suggested"]:
        # A saved Veo id has no Omni of its own family: point it at the newest stable Omni.
        field = {**field, "suggested": next((o["value"] for o in picked if not o["preview"]), picked[0]["value"])}
    return field


# ------------------------------------------------------------------ store
class ModelCatalog:
    """Detected model lists per provider. An entry only counts for the config it was fetched with."""

    def __init__(self, path: Path):
        self._path = path
        self._lock = threading.Lock()
        try:
            data = read_json(path, {})
        except ValueError:
            data = {}  # a damaged cache is simply fetched again
        self._data: dict = data if isinstance(data, dict) else {}

    def get(self, pid: str, source: str) -> dict | None:
        with self._lock:
            entry = self._data.get(pid)
        return entry if isinstance(entry, dict) and entry.get("source") == source else None

    def put(self, pid: str, source: str, fields: dict) -> dict:
        entry = {"fetchedAt": round(time.time(), 3), "source": source, "fields": fields}
        with self._lock:
            self._data = {**self._data, pid: entry}
            write_json_atomic(self._path, self._data)
        return entry
