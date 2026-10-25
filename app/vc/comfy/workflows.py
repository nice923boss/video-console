"""Workflow templates: built-in and user templates, UI-to-API conversion, bindings and availability checks."""
from __future__ import annotations

import copy
import logging
import re
import threading
from collections import defaultdict
from pathlib import Path
from typing import Any

from ..events import EventBus
from ..paths import BUILTIN_WORKFLOWS_DIR
from ..util import AppError, new_id, now, read_json, write_json_atomic

log = logging.getLogger("vc.workflows")

KINDS = ("image", "video", "upscale")
MODES = ("t2i", "i2i", "i2v", "t2v", "upscale")
MODES_BY_KIND = {"image": ("t2i", "i2i"), "video": ("t2v", "i2v"), "upscale": ("upscale",)}
KIND_NAMES = {"image": "生圖", "video": "影片", "upscale": "放大"}
MODE_NAMES = {"t2i": "文字生圖", "i2i": "參考圖生圖", "i2v": "圖轉影片", "t2v": "文字生影片", "upscale": "放大"}
FRAME_RULES = ("any", "4n+1", "8n+1")
PARAM_TYPES = ("int", "float", "select", "bool", "string")
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,63}$")
_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,31}$")

BINDING_LABELS = {
    "prompt": "正向提示詞",
    "negative": "負向提示詞",
    "seed": "種子",
    "width": "寬度",
    "height": "高度",
    "steps": "步數",
    "cfg": "CFG",
    "denoise": "重繪強度",
    "image": "輸入圖片",
    "images": "參考圖（多張）",
    "frames": "影格數",
    "fps": "每秒影格",
    "prefix": "輸出檔名前綴",
}

_VIRTUAL = {"Note", "MarkdownNote", "PrimitiveNode", "Reroute", "GetNode", "SetNode"}
_CONTROL_VALUES = {"fixed", "increment", "decrement", "randomize"}
_WIDGET_TYPES = {"INT", "FLOAT", "STRING", "BOOLEAN", "COMBO"}
_OUTPUT_CLASSES = {"SaveImage", "PreviewImage", "SaveVideo", "SaveAnimatedWEBP", "SaveAnimatedPNG", "SaveWEBM",
                   "VHS_VideoCombine", "Image Comparer (rgthree)"}
_VIDEO_OUTPUT_CLASSES = {"SaveVideo", "SaveAnimatedWEBP", "SaveAnimatedPNG", "SaveWEBM", "VHS_VideoCombine",
                         "CreateVideo"}
_FOLDER_BY_INPUT = {
    "unet_name": "diffusion_models",
    "clip_name": "text_encoders",
    "clip_name1": "text_encoders",
    "clip_name2": "text_encoders",
    "clip_name3": "text_encoders",
    "vae_name": "vae",
    "ckpt_name": "checkpoints",
    "lora_name": "loras",
    "control_net_name": "controlnet",
    "style_model_name": "style_models",
}
_FOLDER_BY_CLASS = {
    ("UpscaleModelLoader", "model_name"): "upscale_models",
    ("CLIPVisionLoader", "clip_name"): "clip_vision",
    ("ModelPatchLoader", "name"): "model_patches",
}


# ------------------------------------------------------------------ helpers
def is_link(value: Any) -> bool:
    return isinstance(value, list) and len(value) == 2 and isinstance(value[1], int) and isinstance(value[0], (str, int))


def input_specs(spec: dict) -> list[tuple[str, Any, dict]]:
    """(name, type, config) for every declared input, in ComfyUI's widget order."""
    out: list[tuple[str, Any, dict]] = []
    declared = spec.get("input") or {}
    order = spec.get("input_order") or {}
    for group in ("required", "optional"):
        items = declared.get(group) or {}
        names = [n for n in order.get(group, []) if n in items] or list(items)
        for name in names:
            val = items[name]
            typ = val[0] if isinstance(val, (list, tuple)) and val else val
            cfg = val[1] if isinstance(val, (list, tuple)) and len(val) > 1 and isinstance(val[1], dict) else {}
            out.append((name, typ, cfg))
    return out


def _is_widget(typ: Any, cfg: dict) -> bool:
    if cfg.get("forceInput"):
        return False
    return isinstance(typ, list) or typ in _WIDGET_TYPES


def combo_options(typ: Any, cfg: dict) -> list | None:
    if isinstance(typ, list):
        return typ
    if typ == "COMBO":
        return list(cfg.get("options") or [])
    return None


def guess_folder(class_type: str, input_name: str) -> str:
    return _FOLDER_BY_CLASS.get((class_type, input_name)) or _FOLDER_BY_INPUT.get(input_name, "")


# ------------------------------------------------------------------ validation
def _clean_param(p: Any) -> dict:
    if not isinstance(p, dict) or not _KEY_RE.match(str(p.get("key", ""))):
        raise AppError("參數設定格式不正確")
    ptype = p.get("type", "float")
    if ptype not in PARAM_TYPES:
        raise AppError(f"不支援的參數類型：{ptype}")
    out = {"key": p["key"], "label": str(p.get("label") or p["key"])[:40], "type": ptype}
    for k in ("min", "max", "step"):
        if isinstance(p.get(k), (int, float)) and not isinstance(p.get(k), bool):
            out[k] = p[k]
    if ptype == "select":
        opts = p.get("options")
        if not isinstance(opts, list) or not opts:
            raise AppError(f"參數 {p['key']} 缺少選項")
        out["options"] = [o if isinstance(o, dict) else {"value": o, "label": str(o)} for o in opts][:60]
    if "default" in p:
        out["default"] = p["default"]
    if p.get("help"):
        out["help"] = str(p["help"])[:200]
    if ptype == "string" and p.get("multiline"):
        out["multiline"] = True
    return out


def frames_for(seconds: float, fps: float, rule: str = "any", max_frames: int = 0, min_frames: int = 1) -> int:
    """Frame count for a clip length, snapped to the model's frame rule (4n+1 / 8n+1)."""
    n = max(1, int(round(max(0.1, seconds) * max(1.0, fps))))
    if max_frames:
        n = min(n, max_frames)
    step = {"4n+1": 4, "8n+1": 8}.get(rule)
    if step:
        k = max(1, int(round((n - 1) / step)))
        n = k * step + 1
        while max_frames and n > max_frames and k > 1:
            k -= 1
            n = k * step + 1
    return max(min_frames, n)


def validate_template(data: Any) -> dict:
    if not isinstance(data, dict):
        raise AppError("範本格式不正確")
    wid = str(data.get("id", ""))
    if not _ID_RE.match(wid):
        raise AppError("範本代號只能用小寫英數字、底線、連字號（2 到 64 字）")
    graph = data.get("graph")
    if not isinstance(graph, dict) or not graph:
        raise AppError("範本缺少工作流程內容")
    clean_graph: dict[str, dict] = {}
    for nid, node in graph.items():
        if not isinstance(node, dict) or not isinstance(node.get("class_type"), str):
            raise AppError(f"節點 #{nid} 格式不正確")
        inputs = node.get("inputs") if isinstance(node.get("inputs"), dict) else {}
        clean_graph[str(nid)] = {k: v for k, v in node.items() if k in ("class_type", "_meta")} | {"inputs": inputs}
    bindings: dict[str, list[dict]] = {}
    for key, targets in (data.get("bindings") or {}).items():
        if not _KEY_RE.match(str(key)):
            raise AppError(f"綁定名稱不正確：{key}")
        items = []
        for t in targets if isinstance(targets, list) else [targets]:
            if not isinstance(t, dict) or str(t.get("node")) not in clean_graph or not t.get("input"):
                raise AppError(f"綁定「{key}」指向不存在的節點")
            items.append({"node": str(t["node"]), "input": str(t["input"])})
        if items:
            bindings[key] = items
    kind = data.get("kind", "image")
    mode = data.get("mode", "t2i")
    if kind not in KINDS or mode not in MODES:
        raise AppError("範本類型設定不正確")
    if mode not in MODES_BY_KIND[kind]:
        allowed = "、".join(MODE_NAMES[m] for m in MODES_BY_KIND[kind])
        raise AppError(f"{KIND_NAMES[kind]}範本的用途只能是{allowed}，目前是{MODE_NAMES[mode]}")
    rule = data.get("frameRule", "any")
    if rule not in FRAME_RULES:
        raise AppError("影格規則只能是 any、4n+1、8n+1")
    outputs = [str(o) for o in data.get("outputs") or [] if str(o) in clean_graph]
    return {
        "id": wid,
        "name": str(data.get("name") or wid)[:80],
        "kind": kind,
        "mode": mode,
        "description": str(data.get("description") or "")[:400],
        "builtin": bool(data.get("builtin", False)),
        "verified": bool(data.get("verified", False)),
        "frameRule": rule,
        "maxFrames": int(data.get("maxFrames") or 0),
        "defaultFps": int(data.get("defaultFps") or 0),
        "maxShortSide": int(data.get("maxShortSide") or 0),
        # "en": the text encoder reads English only (LTX-Video's T5), so Chinese prompts are translated first
        "promptLanguage": "en" if data.get("promptLanguage") == "en" else "",
        # True: the template draws lettering on purpose (a layout poster), so the no-text filter stays off
        "allowText": bool(data.get("allowText", False)),
        "graph": clean_graph,
        "bindings": bindings,
        "params": [_clean_param(p) for p in data.get("params") or []],
        "outputs": outputs,
        "notes": str(data.get("notes") or "")[:2000],
        "download": [d for d in data.get("download") or [] if isinstance(d, dict) and d.get("file")][:12],
        "updated": data.get("updated") or now(),
    }


# ------------------------------------------------------------------ graph building
def _coerce(value: Any, current: Any) -> Any:
    if isinstance(current, bool):
        return bool(value)
    if isinstance(current, int) and isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(round(value))
    if isinstance(current, float) and isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(current, str) and not isinstance(value, str):
        return str(value)
    return value


def build_graph(template: dict, values: dict[str, Any]) -> dict:
    """Copy the template graph and write bound values into it."""
    graph = copy.deepcopy(template["graph"])
    for key, value in values.items():
        if value is None:
            continue
        for target in template["bindings"].get(key) or []:
            node = graph.get(target["node"])
            if node is None:
                continue
            if key == "images":
                # One LoadImage per uploaded file, wired to numbered inputs: images.image0, images.image1...
                for i, name in enumerate(value):
                    load_id = f"{target['node']}_{key}{i}"
                    graph[load_id] = {"class_type": "LoadImage", "inputs": {"image": name}}
                    node["inputs"][f"{target['input']}{i}"] = [load_id, 0]
                continue
            node["inputs"][target["input"]] = _coerce(value, node["inputs"].get(target["input"]))
    return graph


def param_defaults(template: dict) -> dict[str, Any]:
    return {p["key"]: p["default"] for p in template.get("params") or [] if "default" in p}


def check_availability(template: dict, object_info: dict) -> dict:
    """Compare a template against a server's /object_info: missing nodes and missing model files."""
    missing_nodes: list[str] = []
    missing_models: list[dict] = []
    bound = {(t["node"], t["input"]) for targets in template["bindings"].values() for t in targets}
    downloads = {d["file"]: d for d in template.get("download") or []}
    for nid, node in template["graph"].items():
        cls = node["class_type"]
        spec = object_info.get(cls)
        if spec is None:
            if cls not in missing_nodes:
                missing_nodes.append(cls)
            continue
        specs = {name: (typ, cfg) for name, typ, cfg in input_specs(spec)}
        for name, val in node["inputs"].items():
            if is_link(val) or (nid, name) in bound or name not in specs:
                continue
            options = combo_options(*specs[name])
            if options is None or not isinstance(val, str):
                continue
            normalized = {str(o).replace("\\", "/") for o in options}
            if val.replace("\\", "/") not in normalized:
                missing_models.append({
                    "node": nid,
                    "class": cls,
                    "input": name,
                    "value": val,
                    "folder": guess_folder(cls, name),
                    "url": (downloads.get(val) or {}).get("url", ""),
                })
    return {"ok": not missing_nodes and not missing_models, "missingNodes": missing_nodes,
            "missingModels": missing_models}


def describe_missing(check: dict) -> str:
    parts = []
    if check.get("missingNodes"):
        parts.append("缺少節點：" + "、".join(check["missingNodes"]) + "（需安裝對應的自訂節點）")
    for m in check.get("missingModels") or []:
        where = f"放到 models/{m['folder']}" if m.get("folder") else f"節點 {m['class']} 的 {m['input']}"
        parts.append(f"缺少 {m['value']}（{where}）")
    return "；".join(parts)


# ------------------------------------------------------------------ UI to API conversion
def _ui_links(ui: dict) -> dict[int, tuple[int, int, int, int, str]]:
    out: dict[int, tuple[int, int, int, int, str]] = {}
    for link in ui.get("links") or []:
        if isinstance(link, list) and len(link) >= 5:
            out[int(link[0])] = (int(link[1]), int(link[2]), int(link[3]), int(link[4]),
                                 str(link[5]) if len(link) > 5 else "")
        elif isinstance(link, dict) and "id" in link:
            out[int(link["id"])] = (int(link["origin_id"]), int(link["origin_slot"]), int(link["target_id"]),
                                    int(link["target_slot"]), str(link.get("type", "")))
    return out


def ui_to_api(ui: dict, object_info: dict) -> tuple[dict, list[str]]:
    """Convert a ComfyUI UI-format workflow (nodes + links) to API format. Returns (graph, warnings)."""
    subgraphs = {str(s.get("id")) for s in ((ui.get("definitions") or {}).get("subgraphs") or []) if isinstance(s, dict)}
    nodes = {int(n["id"]): n for n in ui.get("nodes") or [] if isinstance(n, dict) and "id" in n}
    if any(str(n.get("type")) in subgraphs for n in nodes.values()):
        raise AppError("這個工作流程含有子圖（Subgraph），請在 ComfyUI 選單用「匯出（API）」另存後再匯入")
    links = _ui_links(ui)
    setters = {}
    for n in nodes.values():
        if n.get("type") == "SetNode" and n.get("widgets_values"):
            setters[str(n["widgets_values"][0])] = n
    warnings: list[str] = []
    missing: list[str] = []

    def resolve(link_id: Any, depth: int = 0) -> tuple | None:
        if link_id is None:
            return None
        if depth > 64:
            raise AppError("工作流程的連線過深或形成循環")
        link = links.get(int(link_id))
        if link is None:
            return None
        origin_id, origin_slot, _, _, link_type = link
        node = nodes.get(origin_id)
        if node is None:
            return None
        ntype = str(node.get("type"))
        first = (node.get("inputs") or [{}])[0] if node.get("inputs") else {}
        if ntype == "Reroute" or (ntype not in object_info and "reroute" in ntype.lower()):
            return resolve(first.get("link"), depth + 1)
        if ntype == "PrimitiveNode":
            values = node.get("widgets_values") or []
            return ("value", values[0]) if values else None
        if ntype == "SetNode":
            return resolve(first.get("link"), depth + 1)
        if ntype == "GetNode":
            name = str((node.get("widgets_values") or [""])[0])
            setter = setters.get(name)
            if setter is None:
                raise AppError(f"找不到對應的 SetNode：{name}")
            return resolve(((setter.get("inputs") or [{}])[0]).get("link"), depth + 1)
        mode = node.get("mode", 0)
        if mode == 2:
            return None
        if mode == 4:
            outputs = node.get("outputs") or []
            out_type = (outputs[origin_slot].get("type") if origin_slot < len(outputs) else "") or link_type
            ins = node.get("inputs") or []
            ordered = ([ins[origin_slot]] if origin_slot < len(ins) else []) + ins
            for inp in ordered:
                if inp.get("link") is not None and inp.get("type") == out_type:
                    return resolve(inp["link"], depth + 1)
            return None
        return ("link", origin_id, origin_slot)

    api: dict[str, dict] = {}
    for nid in sorted(nodes):
        node = nodes[nid]
        ntype = str(node.get("type"))
        if ntype in _VIRTUAL or node.get("mode", 0) in (2, 4):
            continue
        spec = object_info.get(ntype)
        if spec is None:
            if any(out.get("links") for out in node.get("outputs") or []):
                missing.append(ntype)
            else:
                warnings.append(f"略過介面專用或未安裝的節點：{ntype}")
            continue
        specs = input_specs(spec)
        inputs: dict[str, Any] = {}
        widgets = node.get("widgets_values")
        if isinstance(widgets, dict):
            for name, typ, cfg in specs:
                if _is_widget(typ, cfg) and name in widgets:
                    inputs[name] = widgets[name]
        else:
            values = list(widgets or [])
            i = 0
            for name, typ, cfg in specs:
                if not _is_widget(typ, cfg):
                    continue
                if i >= len(values):
                    break
                inputs[name] = values[i]
                i += 1
                if i < len(values) and isinstance(values[i], str) and values[i] in _CONTROL_VALUES and (
                        typ == "INT" or cfg.get("control_after_generate")):
                    i += 1
        for inp in node.get("inputs") or []:
            if inp.get("link") is None:
                continue
            name = str(inp.get("name"))
            src = resolve(inp["link"])
            if src is None:
                inputs.pop(name, None)
            elif src[0] == "value":
                inputs[name] = src[1]
            else:
                inputs[name] = [str(src[1]), int(src[2])]
        api[str(nid)] = {"class_type": ntype, "inputs": inputs, "_meta": {"title": str(node.get("title") or ntype)}}
    if missing:
        raise AppError("連線中的 ComfyUI 缺少這些節點：" + "、".join(sorted(set(missing))) + "，請先安裝對應的自訂節點")
    for node in api.values():
        for name, val in list(node["inputs"].items()):
            if is_link(val) and str(val[0]) not in api:
                node["inputs"].pop(name)
    if not api:
        raise AppError("工作流程裡沒有可執行的節點")
    return api, warnings


def looks_like_api(data: Any) -> bool:
    return isinstance(data, dict) and bool(data) and all(
        isinstance(v, dict) and "class_type" in v for v in data.values())


# ------------------------------------------------------------------ binding detection
def detect_bindings(graph: dict, object_info: dict | None = None) -> dict[str, list[dict]]:
    consumers: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for nid, node in graph.items():
        for name, val in node["inputs"].items():
            if is_link(val):
                consumers[str(val[0])].append((nid, name))

    def polarity(start: str) -> str | None:
        seen = {start}
        frontier = [start]
        while frontier:
            nxt = []
            for cur in frontier:
                for dst, name in consumers.get(cur, []):
                    low = name.lower()
                    if low.startswith("positive") or low == "pos":
                        return "prompt"
                    if low.startswith("negative") or low == "neg":
                        return "negative"
                    if dst not in seen:
                        seen.add(dst)
                        nxt.append(dst)
            frontier = nxt
        return None

    bindings: dict[str, list[dict]] = defaultdict(list)

    def add(key: str, nid: str, name: str) -> None:
        bindings[key].append({"node": nid, "input": name})

    def order(nid: str) -> tuple[int, str]:
        return (int(nid), "") if nid.isdigit() else (1 << 30, nid)

    text_nodes = []
    for nid in sorted(graph, key=order):
        node = graph[nid]
        inputs = node["inputs"]
        for name in ("positive_prompt", "negative_prompt"):
            if isinstance(inputs.get(name), str):
                add("prompt" if name.startswith("positive") else "negative", nid, name)
        for name in ("text", "prompt"):
            if isinstance(inputs.get(name), str):
                text_nodes.append((nid, name))
                break
    for nid, name in text_nodes:
        role = polarity(nid)
        if role:
            add(role, nid, name)
    if "prompt" not in bindings and text_nodes:
        nid, name = text_nodes[0]
        add("prompt", nid, name)
    for nid in sorted(graph, key=order):
        node = graph[nid]
        cls = node["class_type"]
        for name, val in node["inputs"].items():
            if is_link(val) or isinstance(val, bool):
                continue
            number = isinstance(val, (int, float))
            if name in ("seed", "noise_seed") and isinstance(val, int):
                add("seed", nid, name)
            elif name in ("width", "height") and isinstance(val, int):
                add(name, nid, name)
            elif name == "steps" and isinstance(val, int):
                add("steps", nid, name)
            elif name == "cfg" and number:
                add("cfg", nid, name)
            elif name == "denoise" and number:
                add("denoise", nid, name)
            elif name in ("length", "num_frames", "frames", "video_length", "frame_count") and isinstance(val, int):
                add("frames", nid, name)
            elif name in ("fps", "frame_rate") and number:
                add("fps", nid, name)
            elif name == "filename_prefix" and isinstance(val, str):
                add("prefix", nid, name)
            elif cls == "LoadImage" and name == "image" and "image" not in bindings:
                add("image", nid, name)
    return dict(bindings)


def guess_template_meta(graph: dict, bindings: dict, object_info: dict | None = None) -> dict:
    classes = {n["class_type"] for n in graph.values()}
    has_image = "image" in bindings
    if classes & _VIDEO_OUTPUT_CLASSES:
        kind, mode = "video", ("i2v" if has_image else "t2v")
    elif has_image and "prompt" not in bindings and any("Upscale" in c for c in classes):
        kind, mode = "upscale", "upscale"
    elif has_image:
        kind, mode = "image", "i2i"
    else:
        kind, mode = "image", "t2i"
    rule = "any"
    if any(c.startswith("Wan") or "Hunyuan" in c for c in classes):
        rule = "4n+1"
    elif any("LTXV" in c for c in classes):
        rule = "8n+1"
    info = object_info or {}
    outputs = [nid for nid, n in graph.items()
               if (info.get(n["class_type"]) or {}).get("output_node") or n["class_type"] in _OUTPUT_CLASSES]
    return {"kind": kind, "mode": mode, "frameRule": rule, "outputs": outputs}


# ------------------------------------------------------------------ store
class WorkflowStore:
    def __init__(self, user_dir: Path, builtin_dir: Path = BUILTIN_WORKFLOWS_DIR, bus: EventBus | None = None):
        self.user_dir = user_dir
        self.builtin_dir = builtin_dir
        self.bus = bus
        self._lock = threading.Lock()
        self._items: dict[str, dict] = {}
        self.reload()

    def reload(self) -> None:
        items: dict[str, dict] = {}
        for folder, builtin in ((self.builtin_dir, True), (self.user_dir, False)):
            if not folder.is_dir():
                continue
            for path in sorted(folder.glob("*.json")):
                try:
                    data = read_json(path)
                    data["builtin"] = builtin
                    tpl = validate_template(data)
                except (AppError, ValueError, OSError, TypeError, KeyError, AttributeError) as exc:
                    message = exc.message if isinstance(exc, AppError) else str(exc)
                    log.warning("Skipping workflow template %s: %s", path.name, message)
                    continue
                if not builtin and tpl["id"] in items and items[tpl["id"]]["builtin"]:
                    log.warning("User template %s clashes with a built-in id; skipped", path.name)
                    continue
                items[tpl["id"]] = tpl
        with self._lock:
            self._items = items

    def _publish(self) -> None:
        if self.bus:
            self.bus.publish("workflows", self.list())

    @staticmethod
    def summary(t: dict) -> dict:
        return {k: t[k] for k in ("id", "name", "kind", "mode", "description", "builtin", "verified", "frameRule",
                                  "maxFrames", "defaultFps", "maxShortSide", "promptLanguage", "allowText", "params", "notes", "download",
                                  "outputs", "updated")} | {
            "bindings": {k: len(v) for k, v in t["bindings"].items()}}

    def list(self) -> list[dict]:
        with self._lock:
            items = list(self._items.values())
        items.sort(key=lambda t: (not t["builtin"], KINDS.index(t["kind"]), t["name"]))
        return [self.summary(t) for t in items]

    def get(self, wid: str) -> dict:
        with self._lock:
            tpl = self._items.get(wid)
        if tpl is None:
            raise AppError(f"找不到工作流程範本：{wid}", 404)
        return copy.deepcopy(tpl)

    def exists(self, wid: str) -> bool:
        with self._lock:
            return wid in self._items

    def save(self, data: dict) -> dict:
        tpl = validate_template({**data, "builtin": False, "updated": now()})
        with self._lock:
            current = self._items.get(tpl["id"])
        if current and current["builtin"]:
            raise AppError("內建範本不能直接修改，請先複製一份再編輯", 409)
        self.user_dir.mkdir(parents=True, exist_ok=True)
        write_json_atomic(self.user_dir / f"{tpl['id']}.json", tpl)
        with self._lock:
            self._items[tpl["id"]] = tpl
        self._publish()
        return copy.deepcopy(tpl)

    def delete(self, wid: str) -> None:
        tpl = self.get(wid)
        if tpl["builtin"]:
            raise AppError("內建範本不能刪除", 409)
        path = self.user_dir / f"{wid}.json"
        if path.exists():
            path.unlink()
        with self._lock:
            self._items.pop(wid, None)
        self._publish()

    def _free_id(self, base: str) -> str:
        base = re.sub(r"[^a-z0-9_-]+", "-", base.lower()).strip("-")[:48] or "workflow"
        if len(base) < 2:
            base = f"wf-{base}"
        candidate = base
        while self.exists(candidate):
            candidate = f"{base}-{new_id('x')[2:8]}"
        return candidate

    def duplicate(self, wid: str) -> dict:
        tpl = self.get(wid)
        tpl["id"] = self._free_id(f"{wid}-copy")
        tpl["name"] = (tpl["name"] + "（副本）")[:80]
        tpl["builtin"] = False
        return self.save(tpl)

    def import_data(self, data: Any, name: str, object_info: dict | None) -> tuple[dict, list[str]]:
        """Import a template JSON, an API-format graph or a UI-format workflow."""
        warnings: list[str] = []
        if isinstance(data, dict) and isinstance(data.get("graph"), dict) and "bindings" in data:
            tpl = dict(data)
            tpl["id"] = self._free_id(str(tpl.get("id") or name or "workflow"))
            return self.save(tpl), warnings
        if isinstance(data, dict) and looks_like_api(data.get("prompt")):
            data = data["prompt"]
        if looks_like_api(data):
            graph = {str(k): {"class_type": v["class_type"], "inputs": dict(v.get("inputs") or {}),
                              **({"_meta": v["_meta"]} if isinstance(v.get("_meta"), dict) else {})}
                     for k, v in data.items()}
        elif isinstance(data, dict) and "nodes" in data:
            if not object_info:
                raise AppError("匯入介面格式的工作流程需要先連上 ComfyUI（用來對照節點定義）；或在 ComfyUI 用「匯出（API）」後再匯入")
            graph, warnings = ui_to_api(data, object_info)
        else:
            raise AppError("看不懂這個檔案：請匯入 ComfyUI 工作流程 JSON（介面格式或 API 格式）")
        bindings = detect_bindings(graph, object_info)
        meta = guess_template_meta(graph, bindings, object_info)
        if not meta["outputs"]:
            warnings.append("找不到輸出節點（SaveImage／SaveVideo 等），執行時會改抓所有節點的輸出")
        if "prompt" not in bindings and meta["kind"] != "upscale":
            warnings.append("沒有找到提示詞節點，請在綁定編輯器手動指定")
        clean_name = (name or "匯入的工作流程").rsplit(".", 1)[0][:80]
        tpl = {
            "id": self._free_id(new_id("w")),
            "name": clean_name,
            "description": "從 ComfyUI 匯入",
            "graph": graph,
            "bindings": bindings,
            "params": [],
            **meta,
        }
        return self.save(tpl), warnings
