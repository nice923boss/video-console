"""Cut interview footage in Video Console: one project per interviewee.

    python interview_edit.py prepare --name 受訪_A --work <dir> a.mp4 [b.mp4 ...]
        create the project, import the videos as shots in order, transcribe every shot,
        measure loudness, write <work>/transcript.json and transcript.txt for reading
    python interview_edit.py apply <work>/plan.json
        split at the plan's marks, then (mode "mark") colour and annotate those segments or
        (mode "restructure") delete / move them; fix caption text; split captions over 18 chars
    python interview_edit.py verify <work>
        check segments, captions and timing against transcript.json
    python interview_edit.py export <work> [--video]
        write the SRT (and with --video an MP4 with clip audio) into <work>

Times in plan.json are sequence seconds as printed in transcript.txt. Only cut transitions are handled.
"""
from __future__ import annotations

import argparse
import array
import json
import math
import random
import re
import string
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from vc_client import VC, VCError  # noqa: E402

RATE = 8000  # Hz, loudness analysis only
WIN = 0.05  # s per loudness window
MAX_CHARS = 18
EDGE_PUNCT = "，。、！？；：,.!?;:…「」『』（）()\"' "
BREAK_PUNCT = "，。、！？；：,.!?;:"
COLORS = {"red": "#ff6b6b", "orange": "#f5b14c", "lime": "#c6f24e", "green": "#4fd18b", "blue": "#6ea8ff",
          "purple": "#b28cff"}
KIND_COLOR = {"interviewer": "red", "countdown": "red", "ng": "red", "confirm": "red", "pause": "orange"}


def new_id(prefix: str) -> str:
    return f"{prefix}_" + "".join(random.choices(string.ascii_lowercase + string.digits, k=10))


def r3(x: float) -> float:
    return round(x, 3)


# ---------------------------------------------------------------- sequence model (cut transitions only)
def clips(project: dict) -> list[dict]:
    """Video clips in sequence order: shot, take, in, out, start, end (sequence seconds)."""
    out, t = [], 0.0
    for index, shot in enumerate(project["shots"]):
        if not shot.get("inSequence", True):
            continue
        take = project["takes"].get(shot.get("activeVideo") or "")
        if not take:
            raise SystemExit(f"鏡頭 {shot['id']} 沒有影片")
        if (shot.get("transition") or {}).get("type", "cut") != "cut":
            raise SystemExit(f"鏡頭 {shot['id']} 用了轉場，這個腳本只處理直接切換")
        a = float(shot.get("trimIn") or 0)
        b = float(shot["trimOut"]) if shot.get("trimOut") is not None else float(take["duration"])
        out.append({"index": index, "shot": shot, "take": take, "in": a, "out": b, "start": t, "end": t + b - a})
        t += b - a
    return out


def clip_at(seq: list[dict], t: float) -> dict:
    for c in seq:
        if c["start"] - 1e-6 <= t < c["end"] - 1e-6:
            return c
    raise SystemExit(f"時間 {t:.3f} 不在序列裡")


def split_ops(project: dict, times: list[float]) -> tuple[list[dict], dict]:
    """shot.update + shot.add pairs splitting the video track at each time (like the S key); returns ops and
    the project's shots after them (applied to a local copy)."""
    shots = [dict(s) for s in project["shots"]]
    work = {**project, "shots": shots}
    ops = []
    for t in sorted(set(r3(x) for x in times)):
        seq = clips(work)
        c = clip_at(seq, t)
        local = t - c["start"]
        if local < 0.04 or c["end"] - t < 0.04:  # already a cut here
            continue
        shot = c["shot"]
        cut = r3(c["in"] + local)
        head = {"trimOut": cut, "transition": {**(shot.get("transition") or {}), "type": "cut"}}
        tail = {**shot, "id": new_id("s"), "trimIn": cut, "trimOut": shot.get("trimOut")}
        ops.append({"op": "shot.update", "id": shot["id"], "patch": head})
        ops.append({"op": "shot.add", "shot": tail, "index": c["index"] + 1})
        shots[c["index"]] = {**shot, **head}
        shots.insert(c["index"] + 1, tail)
    return ops, work


# ---------------------------------------------------------------- loudness
def loudness(video: Path) -> list[float]:
    """dBFS per WIN-second window of the video's first audio track."""
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(video), "-map", "0:a:0", "-ac", "1", "-ar", str(RATE),
                          "-f", "s16le", "-"], capture_output=True, check=True).stdout
    samples = array.array("h")
    samples.frombytes(raw[: len(raw) // 2 * 2])
    n = int(RATE * WIN)
    out = []
    for i in range(0, len(samples) - n + 1, n):
        w = samples[i:i + n]
        power = sum(x * x for x in w) / n
        out.append(10 * math.log10(power / 32768 ** 2) if power > 0 else -100.0)
    return out


def mean_db(levels: list[float], a: float, b: float) -> float:
    w = levels[int(a / WIN):max(int(a / WIN) + 1, int(b / WIN))]
    if not w:
        return -100.0
    power = sum(10 ** (x / 10) for x in w) / len(w)
    return round(10 * math.log10(power), 1) if power > 0 else -100.0


def silences(levels: list[float], threshold: float, min_len: float = 0.5) -> list[list[float]]:
    out, start = [], None
    for i, x in enumerate(levels + [0.0]):
        if x < threshold and start is None:
            start = i
        elif x >= threshold and start is not None:
            if (i - start) * WIN >= min_len:
                out.append([r3(start * WIN), r3(i * WIN)])
            start = None
    return out


def percentile(values: list[float], q: float) -> float:
    s = sorted(values)
    return s[min(len(s) - 1, int(q * len(s)))] if s else -100.0


# ---------------------------------------------------------------- prepare
def prepare(args) -> None:
    videos = [Path(v) for v in args.videos]
    work = Path(args.work)
    work.mkdir(parents=True, exist_ok=True)
    vc = VC()
    if args.pid:
        project = vc.project(args.pid)
    else:
        project = vc.create_project(args.name, 1920, 1080, 30)
        print(f"建立專案 {project['id']} {args.name}", flush=True)
        first = project["shots"][0]
        extra = [{**first, "id": new_id("s"), "name": v.stem} for v in videos[1:]]
        ops = [{"op": "shot.update", "id": first["id"], "patch": {"name": videos[0].stem}}]
        ops += [{"op": "shot.add", "shot": s, "index": i + 1} for i, s in enumerate(extra)]
        vc.ops(project["id"], ops, label="建立鏡頭", undoable=False)
        project = vc.project(project["id"])
        for shot, video in zip(project["shots"], videos):
            print(f"匯入 {video.name} → {shot['id']}", flush=True)
            vc.wait(vc.upload(project["id"], video, shot=shot["id"])["id"])
    pid = project["id"]
    project = vc.project(pid)
    if not project.get("captions"):
        for shot in project["shots"]:
            print(f"語音轉字幕 {shot['name']}", flush=True)
            vc.wait(vc.job("transcribe", pid, shot["id"])["id"])
        project = vc.project(pid)

    seq = clips(project)
    levels: list[float] = []
    for c, video in zip(seq, videos):
        lv = loudness(video)
        levels += [-100.0] * max(0, int(c["start"] / WIN) - len(levels))
        levels = levels[: int(c["start"] / WIN)] + lv[int(c["in"] / WIN):int(c["out"] / WIN)]
    caps = sorted(project["captions"], key=lambda x: x["start"])
    cap_db = [mean_db(levels, x["start"], x["end"]) for x in caps]
    speech = percentile(cap_db, 0.5)
    noise = percentile(levels, 0.1)
    threshold = round(noise + (speech - noise) * 0.3, 1)
    quiet = silences(levels, threshold)
    data = {
        "pid": pid, "name": project["name"], "videos": [str(v) for v in videos], "duration": r3(seq[-1]["end"]),
        "shots": [{"id": c["shot"]["id"], "name": c["shot"].get("name"), "take": c["take"]["id"], "in": c["in"],
                   "start": r3(c["start"]), "end": r3(c["end"])} for c in seq],
        "speechDb": speech, "noiseDb": round(noise, 1), "silenceDb": threshold, "silences": quiet,
        "captions": [{"id": x["id"], "start": x["start"], "end": x["end"], "text": x["text"], "db": d}
                     for x, d in zip(caps, cap_db)],
    }
    (work / "transcript.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    (work / "transcript.txt").write_text(transcript_text(data), encoding="utf-8")
    print(f"完成：{len(caps)} 句字幕，片長 {data['duration']:.1f}s → {work / 'transcript.txt'}", flush=True)


def transcript_text(data: dict) -> str:
    lines = [f"{data['name']}  pid={data['pid']}  片長 {data['duration']:.2f}s",
             f"說話音量中位 {data['speechDb']} dB，底噪 {data['noiseDb']} dB，靜音門檻 {data['silenceDb']} dB",
             "鏡頭：" + "，".join(f"{s['name']} {s['start']:.2f}-{s['end']:.2f}" for s in data["shots"]), ""]
    caps = data["captions"]
    for i, c in enumerate(caps):
        prev_end = caps[i - 1]["end"] if i else 0.0
        for a, b in data["silences"]:
            lo, hi = max(a, prev_end), min(b, c["start"])
            if hi - lo >= 1.0:
                lines.append(f"        [靜音 {hi - lo:.1f}s {lo:.2f}-{hi:.2f}]")
        rel = c["db"] - data["speechDb"]
        lines.append(f"{i + 1:03d} {c['id']} {c['start']:8.2f}-{c['end']:8.2f} {c['db']:6.1f}dB"
                     f"{'(小聲)' if rel <= -8 else ''} | {c['text']}")
    tail = [s for s in data["silences"] if s[1] - max(s[0], caps[-1]["end"] if caps else 0) >= 1.0
            and s[1] > (caps[-1]["end"] if caps else 0)]
    for a, b in tail:
        lo = max(a, caps[-1]["end"] if caps else 0)
        lines.append(f"        [靜音 {b - lo:.1f}s {lo:.2f}-{b:.2f}]")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- plan -> segments
PAD = 0.3  # s kept around speech when a segment boundary sits in a caption gap


def segment_of(m: dict, caps: list[dict], idx: dict, dur: float) -> tuple[float, float]:
    """A plan item's [start, end): explicit times, or the captions [first, last] with boundaries in the gaps."""
    if "captions" not in m:
        return r3(float(m["start"])), r3(float(m["end"]))
    try:
        i, j = idx[m["captions"][0]], idx[m["captions"][-1]]
    except KeyError as e:
        raise SystemExit(f"plan 裡的字幕 {e} 不在 transcript.json")
    first, last = caps[i], caps[j]
    a = 0.0 if i == 0 else min(first["start"], max((caps[i - 1]["end"] + first["start"]) / 2, first["start"] - PAD))
    b = dur if j == len(caps) - 1 else max(last["end"], min((last["end"] + caps[j + 1]["start"]) / 2, last["end"] + PAD))
    return r3(a), r3(b)


def pause_marks(data: dict, taken: list[tuple[float, float]], min_len: float, pad: float) -> list[dict]:
    """Silences of at least min_len inside caption gaps (and before the first / after the last caption)."""
    caps, dur = data["captions"], data["duration"]
    edges = [0.0] + [x for c in caps for x in (c["start"], c["end"])] + [dur]
    out = []
    for g0, g1 in zip(edges[::2], edges[1::2]):
        runs = [[max(a, g0), min(b, g1)] for a, b in data["silences"] if min(b, g1) - max(a, g0) > 0]
        merged: list[list[float]] = []
        for run in runs:
            if merged and run[0] - merged[-1][1] < 0.2:
                merged[-1][1] = run[1]
            else:
                merged.append(run)
        if not merged:
            continue
        r0, r1 = max(merged, key=lambda r: r[1] - r[0])
        if r1 - r0 < min_len:
            continue
        a = r0 if r0 <= 0.01 else r0 + pad
        b = r1 if r1 >= dur - 0.01 else r1 - pad
        for t0, t1 in taken:
            if t0 <= a < t1:
                a = t1
            if t0 < b <= t1:
                b = t0
            if a < t0 and t1 < b:  # a mark inside the pause: keep the longer side
                a, b = (a, t0) if t0 - a >= b - t1 else (t1, b)
        if b - a >= max(1.0, min_len - 2 * pad):
            where = "開頭" if r0 <= 0.01 else "結尾" if r1 >= dur - 0.01 else "停頓"
            out.append({"start": r3(a), "end": r3(b), "kind": "pause",
                        "note": f"{where}無聲 {r1 - r0:.1f} 秒，評估是否刪除"})
    return out


def plan_segments(plan: dict, data: dict) -> list[dict]:
    caps = data["captions"]
    idx = {c["id"]: i for i, c in enumerate(caps)}
    segs = []
    for m in plan.get("marks", []):
        a, b = segment_of(m, caps, idx, data["duration"])
        segs.append({**m, "start": a, "end": b})
    auto = plan.get("autoPauses")
    if auto:
        segs += pause_marks(data, [(s["start"], s["end"]) for s in segs], float(auto.get("min", 2.0)),
                            float(auto.get("pad", 0.25)))
    segs.sort(key=lambda s: s["start"])
    for s, nxt in zip(segs, segs[1:]):
        if nxt["start"] < s["end"] - 1e-3:
            raise SystemExit(f"plan 的片段重疊：{s['start']}-{s['end']} 與 {nxt['start']}-{nxt['end']}")
    return segs


def shots_in(work: dict, a: float, b: float) -> list[str]:
    return [c["shot"]["id"] for c in clips(work) if c["start"] >= a - 0.02 and c["end"] <= b + 0.02]


# ---------------------------------------------------------------- caption text
CJK = r"[㐀-鿿]"
ASCII = r"[A-Za-z0-9]"


def tidy(text: str) -> str:
    """One line, CJK-ASCII spacing, no punctuation at either end."""
    text = re.sub(r"\s+", " ", text.replace("\n", " "))
    text = re.sub(f"({CJK})({ASCII})", r"\1 \2", text)
    text = re.sub(f"({ASCII})({CJK})", r"\1 \2", text)
    return text.strip(EDGE_PUNCT)


def clen(text: str) -> int:
    return len(text.replace(" ", ""))


def split_text(text: str, limit: int) -> list[str]:
    text = tidy(text)
    if clen(text) <= limit or len(text) < 2:
        return [text] if text else []
    target = len(text) / math.ceil(clen(text) / limit)
    cands = [i for i in range(1, len(text)) if (text[i - 1] in BREAK_PUNCT or text[i] == " ") and clen(text[:i]) <= limit]
    if cands:
        i = min(cands, key=lambda k: abs(k - target))
    else:
        i = max(1, min(len(text) - 1, round(target)))
        while clen(text[:i]) > limit:
            i -= 1
        while 1 < i and re.match(ASCII, text[i - 1]) and re.match(ASCII, text[i]):  # do not cut a word
            i -= 1
    return split_text(text[:i], limit) + split_text(text[i:], limit)


def caption_ops(project: dict, plan: dict, limit: int) -> list[dict]:
    fix = plan.get("captions") or {}
    deleted = set(fix.get("delete") or [])
    edits = fix.get("edit") or {}
    ops = [{"op": "caption.delete", "id": cid} for cid in deleted]
    kept = sorted((c for c in project["captions"] if c["id"] not in deleted), key=lambda c: c["start"])
    next_start = {a["id"]: b["start"] for a, b in zip(kept, kept[1:])}
    for cap in project["captions"]:
        if cap["id"] in deleted:
            continue
        end_at = min(cap["end"], next_start.get(cap["id"], cap["end"]))  # recognition can overlap the next line by a few ms
        parts = split_text(edits.get(cap["id"], cap["text"]), limit)
        if not parts:
            ops.append({"op": "caption.delete", "id": cap["id"]})
            continue
        if len(parts) == 1:
            patch = {**({"text": parts[0]} if parts[0] != cap["text"] else {}),
                     **({"end": end_at} if end_at != cap["end"] else {})}
            if patch:
                ops.append({"op": "caption.update", "id": cap["id"], "patch": patch})
            continue
        total = sum(clen(p) for p in parts)
        t, span = cap["start"], end_at - cap["start"]
        for k, part in enumerate(parts):
            end = end_at if k == len(parts) - 1 else r3(t + span * clen(part) / total)
            if k == 0:
                ops.append({"op": "caption.update", "id": cap["id"], "patch": {"text": part, "end": end}})
            else:
                ops.append({"op": "caption.add", "item": {**cap, "id": new_id("c"), "start": r3(t), "end": end,
                                                          "text": part}})
            t = end
    return ops


# ---------------------------------------------------------------- apply
def reorder_ops(order: list[str], desired: list[str]) -> list[dict]:
    cur, ops = list(order), []
    for k, sid in enumerate(desired):
        if cur[k] != sid:
            cur.remove(sid)
            cur.insert(k, sid)
            ops.append({"op": "shot.move", "id": sid, "index": k})
    return ops


def apply(args) -> None:
    plan_path = Path(args.plan)
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    data = json.loads((plan_path.parent / "transcript.json").read_text(encoding="utf-8"))
    mode = plan.get("mode", "mark")
    if mode not in ("mark", "restructure"):
        raise SystemExit("plan.mode 要是 mark 或 restructure")
    vc = VC()
    pid = data["pid"]
    project = vc.project(pid)
    if len(project["shots"]) != len(data["shots"]):
        raise SystemExit("專案已經分割過；要重來請先在 VC 復原（Ctrl+Z）到分割前")
    segs = plan_segments(plan, data)
    caps = data["captions"]
    idx = {c["id"]: i for i, c in enumerate(caps)}
    moves = [{**m, "range": segment_of(m, caps, idx, data["duration"]),
              "at": data["duration"] if m["before"] == "end" else segment_of({"captions": [m["before"]]}, caps, idx,
                                                                                   data["duration"])[0]}
             for m in plan.get("moves", [])] if mode == "restructure" else []

    text_ops = caption_ops(project, plan, int(plan.get("maxChars", MAX_CHARS)))
    if text_ops:
        vc.ops(pid, text_ops, label="整理字幕")
        print(f"字幕：{len(text_ops)} 個修改", flush=True)
    project = vc.project(pid)
    times = [t for s in segs for t in (s["start"], s["end"])]
    times += [t for m in moves for t in (*m["range"], m["at"])]
    ops, work = split_ops(project, [t for t in times if 0 < t < data["duration"]])

    if mode == "mark":
        for s in segs:
            color = COLORS[s.get("color") or KIND_COLOR.get(s.get("kind"), "orange")]
            for sid in shots_in(work, s["start"], s["end"]):
                ops.append({"op": "shot.update", "id": sid, "patch": {"color": color, "notes": s.get("note", "")}})
        vc.ops(pid, ops, label="標記待評估片段")
    else:
        order = [s["id"] for s in work["shots"]]
        desired = list(order)
        for m in moves:
            seg = shots_in(work, *m["range"])
            anchor = next((c["shot"]["id"] for c in clips(work) if abs(c["start"] - m["at"]) < 0.02), None)
            desired = [x for x in desired if x not in seg]
            k = desired.index(anchor) if anchor in desired else len(desired)
            desired[k:k] = seg
        dropped = [sid for s in segs for sid in shots_in(work, s["start"], s["end"])]
        inner = reorder_ops(order, desired) + [{"op": "shot.delete", "id": sid} for sid in dropped]
        vc.ops(pid, ops + [{"op": "linked", "ops": inner}], label="重組剪輯")
    for s in segs:
        print(f"  {s['start']:8.2f}-{s['end']:8.2f} {s.get('kind', '')} {s.get('note', '')}", flush=True)
    print(f"{'標記' if mode == 'mark' else '刪除'} {len(segs)} 段" + (f"，移動 {len(moves)} 段" if moves else ""))


# ---------------------------------------------------------------- verify / export
def verify(args) -> None:
    work_dir = Path(args.work)
    data = json.loads((work_dir / "transcript.json").read_text(encoding="utf-8"))
    vc = VC()
    project = vc.project(data["pid"])
    seq = clips(project)
    problems, notes = [], []
    length = seq[-1]["end"] if seq else 0.0
    if not args.restructure and abs(length - data["duration"]) > 0.01:
        problems.append(f"片長 {length:.3f}s 與原始 {data['duration']:.3f}s 不同")
    notes.append(f"片長 {length:.2f}s（原始 {data['duration']:.2f}s），{len(seq)} 個片段，{len(project['captions'])} 句字幕")
    orig = []  # (take, take time) of each original caption start
    for c in data["captions"]:
        s = next(x for x in data["shots"] if x["start"] - 1e-6 <= c["start"] < x["end"] + 1e-6)
        orig.append((s["take"], c["start"] - s["start"] + s["in"], c["end"] - s["start"] + s["in"]))
    caps = sorted(project["captions"], key=lambda x: x["start"])
    prev = None
    for cap in caps:
        label = f"{cap['start']:.2f} 「{cap['text']}」"
        try:
            clip = clip_at(seq, cap["start"])
        except SystemExit:
            problems.append(f"字幕在序列外：{label}")
            continue
        if cap["end"] > clip["end"] + 0.01:
            problems.append(f"字幕跨片段：{label}")
        if prev and cap["start"] < prev["end"] - 0.001:
            problems.append(f"字幕重疊：{label}")
        if clen(cap["text"]) > MAX_CHARS:
            problems.append(f"超過 {MAX_CHARS} 字：{label}")
        if cap["text"] != tidy(cap["text"]):
            problems.append(f"標點、空格或換行不符規則：{label}")
        take_t = clip["in"] + cap["start"] - clip["start"]
        if not any(t == clip["take"]["id"] and a - 0.01 <= take_t <= b + 0.01 for t, a, b in orig):
            problems.append(f"時間漂移（不在任何原始字幕的時間內）：{label}")
        prev = cap
    for c in seq:
        if c["shot"].get("color"):
            notes.append(f"  {c['start']:8.2f}-{c['end']:8.2f} {c['shot']['color']} {c['shot'].get('notes') or ''}")
    print("\n".join(notes))
    if problems:
        print(f"問題 {len(problems)} 項：")
        print("\n".join(f"  {p}" for p in problems))
        sys.exit(1)
    print("檢查通過")


def export(args) -> None:
    work_dir = Path(args.work)
    data = json.loads((work_dir / "transcript.json").read_text(encoding="utf-8"))
    vc = VC()
    srt = work_dir / f"{data['name']}.srt"
    srt.write_text(vc.srt(data["pid"]), encoding="utf-8-sig")
    print(f"字幕：{srt}")
    if args.video:
        print(f"影片：{vc.export(data['pid'], clipAudio=True, burnCaptions=args.burn, quality='high')}")


if __name__ == "__main__":
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prepare")
    p.add_argument("videos", nargs="+")
    p.add_argument("--name", required=True)
    p.add_argument("--work", required=True)
    p.add_argument("--pid", help="reuse an existing project (resume after a failure)")
    p.set_defaults(func=prepare)
    p = sub.add_parser("apply")
    p.add_argument("plan")
    p.set_defaults(func=apply)
    p = sub.add_parser("verify")
    p.add_argument("work")
    p.add_argument("--restructure", action="store_true", help="length may differ from the original")
    p.set_defaults(func=verify)
    p = sub.add_parser("export")
    p.add_argument("work")
    p.add_argument("--video", action="store_true")
    p.add_argument("--burn", action="store_true", help="burn captions into the video")
    p.set_defaults(func=export)
    args = ap.parse_args()
    try:
        args.func(args)
    except VCError as e:
        sys.exit(f"錯誤：{e}")
