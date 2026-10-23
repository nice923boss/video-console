"""Import a storyboard .docx the way Video Console does, offline, and compare every shot with shots.json.

    python check_import.py <shots.json> [--docx <file.docx>] [--vc <Video Console folder>]

Runs VC's own import code (app/vc/script.py, app/vc/pacing.py) from the VC source folder: --vc, else VC_ROOT, else
the folder this SKILL sits in, else F:\\AI Project\\Video Console. It never calls the running APP. Writes
檢查報告.txt next to shots.json; exit code 0 when every shot imports exactly as planned.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys
from pathlib import Path

from build_docx import COLUMNS, default_out, rows

FIELDS = [field for _, field in COLUMNS if field != "index"]
DASHES = ("\u2014", "\u2015")
LETTERING = re.compile(r"字樣|文字|寫著|字卡|標語|標題|招牌上|印著")
BUCKETS = ((1.5, "1.5 秒以下"), (3.0, "1.5 到 3 秒"), (5.0, "3 到 5 秒"), (float("inf"), "5 秒以上"))


def vc_root(arg: Path | None) -> Path:
    here = Path(__file__).resolve()
    candidates = [arg, Path(os.environ["VC_ROOT"]) if os.environ.get("VC_ROOT") else None,
                  *here.parents, Path(r"F:\AI Project\Video Console")]
    for c in candidates:
        if c and (c / "app" / "vc" / "script.py").is_file():
            return c
    raise SystemExit("找不到 Video Console 原始碼（需要 app/vc/script.py），請用 --vc 指定資料夾")


def _short(text: str, n: int = 60) -> str:
    text = text.replace("\n", " / ")
    return text if len(text) <= n else text[:n] + "…"


def compare(planned: list[dict], shots: list[dict], label: str) -> list[str]:
    """One line per field VC imported differently from the plan."""
    out = []
    if len(shots) != len(planned):
        out.append(f"{label}：匯入 {len(shots)} 鏡，計畫是 {len(planned)} 鏡")
    for n, (want, got) in enumerate(zip(planned, shots), 1):
        for field in FIELDS:
            expected = float(want[field]) if field == "duration" else want[field]
            actual = got.get(field) or ("" if field != "duration" else None)
            if actual != expected:
                out.append(f"{label} 第 {n} 鏡「{field}」\n    計畫：{_short(str(expected))}\n    匯入：{_short(str(actual))}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="離線用 VC 的匯入程式檢查分鏡表")
    ap.add_argument("shots", type=Path)
    ap.add_argument("--docx", type=Path)
    ap.add_argument("--vc", type=Path)
    args = ap.parse_args()
    root = vc_root(args.vc)
    sys.path.insert(0, str(root / "app"))
    from vc.pacing import _reading_time, break_down
    from vc.script import MAX_SHOTS, docx_to_text, parse_script
    from vc.util import AppError

    data = json.loads(args.shots.read_text(encoding="utf-8"))
    docx = args.docx or default_out(args.shots, data)
    planned = rows(data)
    errors: list[str] = []
    warnings: list[str] = []
    try:
        read = docx_to_text(docx.read_bytes())
        plain = parse_script(read["text"], "auto")
        detail = break_down(parse_script(read["text"], "auto"))
    except (AppError, OSError) as exc:
        print(f"VC 無法匯入 {docx}：{exc}", file=sys.stderr)
        return 1

    for note in read["notes"]:
        if not note.startswith(("讀入 ", "表格外的 ")):
            errors.append(f"Word 讀取：{note}")
    if plain["mode"] != "table":
        errors.append(f"VC 沒有把它當成分鏡表讀（讀成 {plain['mode']}）")
    for note in plain["notes"]:
        errors.append(f"匯入：{note}")
    errors += compare(planned, plain["shots"], "一般匯入")
    errors += compare(planned, detail["shots"], "細拆分鏡")
    if len(planned) > MAX_SHOTS:
        errors.append(f"共 {len(planned)} 鏡，VC 一次最多匯入 {MAX_SHOTS} 鏡")

    lengths = [float(r["duration"]) for r in planned]
    for n, r in enumerate(planned, 1):
        need = _reading_time(r["caption"])
        if need and float(r["duration"]) < need:
            errors.append(f"第 {n} 鏡 {r['duration']} 秒，字幕「{_short(r['caption'], 30)}」要 {need:g} 秒才讀得完")
        if any(d in r[f] for f in FIELDS if f != "duration" for d in DASHES):
            errors.append(f"第 {n} 鏡含破折號，請改用逗號、冒號或括號")
        if LETTERING.search(r["prompt"]):
            warnings.append(f"第 {n} 鏡畫面提到文字（{LETTERING.search(r['prompt']).group()}），AI 生圖會被要求不畫字，"
                            "字卡內容請放備註「後製字卡：…」")
    run = 1
    for n in range(1, len(lengths)):
        run = run + 1 if lengths[n] == lengths[n - 1] else 1
        if run == 4:
            warnings.append(f"第 {n - 2} 到 {n + 1} 鏡秒數都是 {lengths[n]:g} 秒，節奏會平，請拉開長短")

    total = sum(lengths)
    counts = [sum(1 for x in lengths if lo < x <= hi) for (lo, hi) in
              zip([0.0] + [b for b, _ in BUCKETS[:-1]], [b for b, _ in BUCKETS])]
    acts: dict[str, list[float]] = {}
    for r in planned:
        acts.setdefault(r["act"] or "（未分段）", []).append(float(r["duration"]))
    lines = [
        f"分鏡表：{docx}",
        f"VC 原始碼：{root}",
        f"結果：{'通過' if not errors else f'{len(errors)} 個問題'}，{len(warnings)} 個提醒",
        "",
        f"共 {len(planned)} 鏡，總長 {total:.1f} 秒，中位數 {statistics.median(lengths):.1f} 秒，"
        f"最短 {min(lengths):g} 秒，最長 {max(lengths):g} 秒",
        "長度分布：" + "，".join(f"{label} {c} 鏡" for (_, label), c in zip(BUCKETS, counts)),
        "各段：" + "；".join(f"{a} {len(v)} 鏡 {sum(v):.1f} 秒" for a, v in acts.items()),
        "VC 細拆節奏提示：" + "；".join(n for n in detail["notes"] if "秒數" not in n),
    ]
    if errors:
        lines += ["", "問題（要修到 0 個）："] + [f"  {e}" for e in errors]
    if warnings:
        lines += ["", "提醒："] + [f"  {w}" for w in warnings]
    report = args.shots.parent / "檢查報告.txt"
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"已寫出 {report}：{'通過' if not errors else f'{len(errors)} 個問題'}，{len(warnings)} 個提醒")
    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())
