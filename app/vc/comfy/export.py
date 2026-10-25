"""Workflow templates as files a user opens in their own ComfyUI: API-format JSON, a ZIP of all and a readme."""
from __future__ import annotations

import io
import json
import zipfile

from ..util import safe_filename
from .workflows import KIND_NAMES, MODE_NAMES, build_graph, guess_folder, is_link, param_defaults

PLACEHOLDER_IMAGE = "example.png"  # ComfyUI's own default LoadImage value; not every input folder has it
README_NAME = "使用說明.txt"

_USAGE = """這些檔案是 Video Console 呼叫 ComfyUI 時用的工作流程（API 格式），可以在你自己的 ComfyUI 開啟使用。

使用方式
1. 把 .json 檔拖進 ComfyUI 畫面，或從左上角 C 標誌選單「檔案」>「開啟」（Ctrl+O）選檔。節點會自動排列，位置和 Video Console 無關。
2. 填正向提示詞。要輸入圖片的工作流程，「Load Image」節點預設填 example.png 當佔位，你的 ComfyUI 沒有這張圖時節點會顯示紅框，點節點上傳或選自己的圖即可。
3. 想放進左側「工作流程」清單：開啟後按 Ctrl+S 存檔。直接把 .json 複製到 ComfyUI/user/default/workflows，從清單開啟會是空白畫面，請用拖曳開啟再存檔。
4. 出現紅框節點：缺少自訂節點或 ComfyUI 版本太舊。請用 ComfyUI Manager 的「Install Missing Custom Nodes」安裝，或更新 ComfyUI。
5. 模型檔放到下方列出的 ComfyUI/models 子資料夾。沒有附下載網址的模型，請用檔名到 Hugging Face 或 Civitai 搜尋。
"""


def comfy_prompt(template: dict) -> dict:
    """The template's graph with its default values, in the API format ComfyUI opens by drag and drop.
    A multi-image input gets one placeholder LoadImage, and every node id is a number."""
    values = param_defaults(template)
    if template["bindings"].get("images"):
        values["images"] = [PLACEHOLDER_IMAGE]
    graph = build_graph(template, values)
    numeric = [int(nid) for nid in graph if nid.isdigit()]
    next_id = max(numeric, default=0) + 1
    renamed: dict[str, str] = {}
    for nid in graph:
        if not nid.isdigit():
            renamed[nid] = str(next_id)
            next_id += 1
    out: dict[str, dict] = {}
    for nid, node in graph.items():
        inputs = {name: [renamed.get(str(val[0]), str(val[0])), val[1]] if is_link(val) else val
                  for name, val in node["inputs"].items()}
        out[renamed.get(nid, nid)] = {**node, "inputs": inputs}
    return out


def model_files(template: dict) -> list[dict]:
    """Model files the graph loads, with the models/ folder each goes in and the template's download link."""
    downloads = {d["file"]: d for d in template.get("download") or []}
    seen: dict[str, dict] = {}
    for node in template["graph"].values():
        for name, val in node["inputs"].items():
            folder = guess_folder(node["class_type"], name)
            if folder and isinstance(val, str) and val and val not in seen:
                seen[val] = {"file": val, "folder": folder}
    for d in downloads.values():
        seen.setdefault(d["file"], {"file": d["file"], "folder": d.get("folder") or ""})
    return [{**m, "url": (downloads.get(m["file"]) or {}).get("url", ""),
             "size": (downloads.get(m["file"]) or {}).get("size", 0)} for m in seen.values()]


def _size(n: int) -> str:
    return f"約 {n / 1e9:.1f} GB" if n >= 1e9 else f"約 {max(1, round(n / 1e6))} MB"


def file_names(templates: list[dict]) -> dict[str, str]:
    """A .json name per template id, from the template's name, unique within the list."""
    out: dict[str, str] = {}
    taken: set[str] = set()
    for t in templates:
        stem = safe_filename(t["name"].replace("/", "_").replace("\\", "_"), t["id"], 80)  # "2x/4x" keeps both parts
        if stem.lower() in taken:
            stem = f"{stem} ({t['id']})"
        taken.add(stem.lower())
        out[t["id"]] = f"{stem}.json"
    return out


def comfy_readme(templates: list[dict], names: dict[str, str]) -> str:
    lines = ["Video Console 的 ComfyUI 工作流程", "", _USAGE, "工作流程清單", ""]
    for t in templates:
        kind = KIND_NAMES[t["kind"]]
        mode = MODE_NAMES[t["mode"]]
        lines += [f"■ {t['name']}", f"檔案：{names[t['id']]}",
                  f"類型：{kind if kind == mode else f'{kind}，{mode}'}{'（內建）' if t['builtin'] else '（自訂）'}"]
        if t["description"]:
            lines.append(f"說明：{t['description']}")
        models = model_files(t)
        if models:
            lines.append("需要的模型：")
            for m in models:
                where = f"放到 models/{m['folder']}" if m["folder"] else "放到對應的 models 資料夾"
                lines.append(f"  - {m['file']}，{where}")
                if m["url"]:
                    lines.append(f"    下載：{m['url']}" + (f"（{_size(m['size'])}）" if m["size"] else ""))
        if t["notes"]:
            note = [line for line in t["notes"].splitlines() if line.strip()]
            lines += [f"備註：{note[0]}"] + [f"  {line}" for line in note[1:]]
        lines.append("")
    return "\n".join(lines)


def comfy_json(template: dict) -> str:
    return json.dumps(comfy_prompt(template), ensure_ascii=False, indent=2)


def comfy_zip(templates: list[dict]) -> bytes:
    """Every template as its own .json, plus the readme listing what each needs."""
    names = file_names(templates)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(README_NAME, "﻿" + comfy_readme(templates, names).replace("\n", "\r\n"))
        for t in templates:
            z.writestr(names[t["id"]], comfy_json(t))
    return buf.getvalue()
