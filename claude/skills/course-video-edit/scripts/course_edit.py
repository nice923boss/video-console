"""Course videos in Video Console: green-screen lecturer beside the slides on the TDCC backdrop.

    python course_edit.py slides <course_dir>
        PPTX -> PDF (PowerPoint) -> <course_dir>/slides/slide-NN.png (pdftoppm, 1920 wide, aspect kept)
    python course_edit.py prepare --course <dir> --video <lecturer.mp4> --work <dir> [--name N] [--noface]
                                  [--obs <obs.mp4>] [--assets <TDCC固定素材>]
        create the project, import the lecturer and the library (backdrop, intro, slides, end logo, end sound),
        transcribe, measure the lecturer (head, keying colour), the sound (silence, loudness) and, with --obs,
        the OBS recording's offset; write <work>/transcript.json, transcript.txt and a draft plan.json
    python course_edit.py apply <work>/plan.json
        trim, cut retakes, key and place the lecturer, intro + title slide in front, end logo + end sound after,
        slides (or the OBS clip) on the 投影片 track, caption style and caption fixes, denoise and compressor
    python course_edit.py verify <work>
        check the finished timeline against plan.json and transcript.json

Times in plan.json are seconds of the lecturer's source file (as printed in transcript.txt), so they stay
valid whatever goes in front of it. Never exports: the owner checks the project in VC and exports there.
"""
from __future__ import annotations

import argparse
import difflib
import json
import math
import random
import re
import string
import subprocess
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from vc_client import VC  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")

ASSETS = Path(r"F:\Downloads\集中保管所\TDCC固定素材")
ASSET_FILES = {"backdrop": "集保底圖.png", "intro": "集保數位學習平台-片頭.MP4",
               "endLogo": "集保數位學習平台-片尾LOGO.jpg", "endSound": "集保數位學習平台-片尾音訊.m4a"}
# Slide rectangle on the backdrop (frame fractions: left, top, width), measured on the past courses.
SLIDE_RECT = {"16:9": {"x": 0.0099, "y": 0.1769, "w": 0.70}, "4:3": {"x": 0.0589, "y": 0.137, "w": 0.570}}
HEAD_TOP = 390  # px, lecturer's head top in the 1080p frame (past courses: 375 and 410)
PERSON_CX = 1660  # px, lecturer's centre line
TITLE_LEN = 2.0  # s the title slide shows full frame after the intro
CROSSFADE = 0.5  # s intro -> title slide
# The intro file fades to black from 5.64 s (black 6.00 to 6.64, sound gone by 5.70): cut it there so the
# crossfade runs from the intro picture into the title slide instead of from black.
INTRO_OUT = 5.64
LEAD = 0.3  # s kept before the first word / slide switches ahead of the sentence
TAIL = 1.0  # s kept after the last word
TARGET_LUFS = -16.0
MAX_CHARS = 20
# The app's starter style (opfields.STARTER_CAPTION_STYLE); the server fills the other fields with its defaults.
CAPTION_STYLE = {"name": "常用：白字細黑框", "size": 84, "outlineWidth": 2.5, "shadow": False, "marginV": 42.0}
RATE = 8000
WIN = 0.05
EDGE_PUNCT = "，。、！？；：,.!?;:…「」『』（）()\"' "
BREAK_PUNCT = "，。、！？；：,.!?;:"
CJK = r"[㐀-鿿]"
ASCII = r"[A-Za-z0-9]"


def new_id(prefix: str) -> str:
    return f"{prefix}_" + "".join(random.choices(string.ascii_lowercase + string.digits, k=10))


def r3(x: float) -> float:
    return round(float(x), 3)


def run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, check=True)


# ---------------------------------------------------------------- slides
def slides(args) -> None:
    import win32com.client
    folder = Path(args.course)
    pptx = next(folder.glob("*.pptx"), None)
    if not pptx:
        raise SystemExit(f"{folder} 裡沒有 .pptx")
    out = folder / "slides"
    out.mkdir(exist_ok=True)
    pdf = out / "slides.pdf"
    app = win32com.client.DispatchEx("PowerPoint.Application")
    try:
        pres = app.Presentations.Open(str(pptx.resolve()), True, False, False)  # read only, no title, no window
        hidden = [i + 1 for i in range(pres.Slides.Count) if pres.Slides(i + 1).SlideShowTransition.Hidden]
        pres.SaveAs(str(pdf.resolve()), 32)  # ppSaveAsPDF; hidden slides are left out
        pres.Close()
    finally:
        app.Quit()
    for old in out.glob("slide-*.png"):
        old.unlink()
    run(["pdftoppm", "-png", "-scale-to-x", "1920", "-scale-to-y", "-1", str(pdf), str(out / "slide")])
    pngs = sorted(out.glob("slide-*.png"))
    for p in pngs:  # pdftoppm pads the number to the page count's width; always two digits
        n = int(p.stem.split("-")[1])
        if p.name != f"slide-{n:02d}.png":
            p.rename(out / f"slide-{n:02d}.png")
    print(f"{pptx.name} → {len(pngs)} 張 PNG 在 {out}" + (f"（隱藏投影片 {hidden} 沒有輸出）" if hidden else ""))


# ---------------------------------------------------------------- measuring
def probe(video: Path) -> dict:
    out = json.loads(run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                          "stream=width,height,r_frame_rate:format=duration", "-of", "json", str(video)]).stdout)
    s = out["streams"][0]
    a, b = s["r_frame_rate"].split("/")
    fps = int(a) / int(b)
    return {"width": s["width"], "height": s["height"], "duration": float(out["format"]["duration"]),
            "fps": int(round(fps)) if abs(fps - round(fps)) < 0.01 else round(fps, 2)}


def grab(video: Path, t: float, path: Path) -> np.ndarray:
    from PIL import Image
    run(["ffmpeg", "-v", "error", "-y", "-ss", f"{t:.2f}", "-i", str(video), "-frames:v", "1",
         "-vf", "scale=1920:1080", str(path)])
    return np.asarray(Image.open(path).convert("RGB")).astype(int)


def key_uv(rgb: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """U/V of 0..255 RGB as ffmpeg's chromakey compares them (full-range BT.601, 0..1)."""
    r, g, b = (rgb[..., k] / 255 for k in range(3))
    return 0.5 - 0.16874 * r - 0.33126 * g + 0.5 * b, 0.5 + 0.5 * r - 0.41869 * g - 0.08131 * b


def erode(mask: np.ndarray, n: int) -> np.ndarray:
    for _ in range(n):
        mask = mask & np.roll(mask, 1, 0) & np.roll(mask, -1, 0) & np.roll(mask, 1, 1) & np.roll(mask, -1, 1)
    return mask


def key_params(frames: list[tuple[np.ndarray, np.ndarray, np.ndarray]], color: str) -> dict:
    """Similarity / blend between the screen and the lecturer: chromakey's distance to the key colour,
    sqrt((du² + dv²) / 2), keys pixels under similarity and fades them out over blend. A fixed 0.18 keyed
    a whole lecturer away when the screen was a dark, greyish green (skin and black clothes sat at 0.08)."""
    ku, kv = key_uv(np.array([int(color[i:i + 2], 16) for i in (1, 3, 5)], dtype=np.float32))
    screen, person = [], []
    for im, green, core in frames:
        # a video frame holds U/V in limited range, as chromakey (and the preview, lib/chroma.js) reads them,
        # while the key colour is taken full range
        u, v = (128 / 255 + (c - 0.5) * 224 / 255 for c in key_uv(im))
        d = np.sqrt(((u - ku) ** 2 + (v - kv) ** 2) / 2)
        screen.append(d[green])
        person.append(d[core])
    far = float(np.percentile(np.concatenate(screen), 99.9))  # the screen, wrinkles and shadows included
    near = float(np.percentile(np.concatenate(person), 0.1))  # the lecturer's colour closest to the screen
    sim = round(far + 0.005, 3)
    if near <= sim + 0.01:
        print(f"注意：綠幕與講師的顏色太接近（綠幕 {far:.3f}、講師 {near:.3f}），去背可能吃掉講師，要在預覽調", flush=True)
        return {"similarity": round(max(0.01, (far + near) / 2), 3), "blend": 0.01, "screen": far, "person": near}
    return {"similarity": sim, "blend": round(min(0.1, (near - sim) * 0.8), 3), "screen": far, "person": near}


def measure_person(video: Path, duration: float, work: Path) -> dict:
    """Head top, centre line and key colour / strength of the lecturer, medians over three frames (1080p pixels)."""
    tops, cxs, greens, frames = [], [], [], []
    for k, f in enumerate((0.25, 0.5, 0.75)):
        im = grab(video, duration * f, work / f"frame_{k + 1}.png")
        r, g, b = im[..., 0], im[..., 1], im[..., 2]
        green = (g > r + 25) & (g > b + 20)
        greens.append(np.median(im[green], axis=0))
        person = ~green
        person[:, :240] = False  # stands and the studio past the screen's edges
        person[:, 1680:] = False
        frames.append((im, green, erode(person, 6)))  # 6 px in from the edge, where pixels mix with the screen
        rows = np.where(person.sum(axis=1) > 15)[0]
        if not len(rows):
            continue
        top = int(rows.min())
        xs = np.where(person[min(1079, top + 100)])[0]
        tops.append(top)
        cxs.append((xs.min() + xs.max()) / 2 if len(xs) else 960)
    if not tops:
        raise SystemExit("畫面裡找不到講師（不是綠幕？）")
    top, cx = float(np.median(tops)), float(np.median(cxs))
    color = "#%02x%02x%02x" % tuple(int(c) for c in np.median(greens, axis=0))
    s = (1080 - HEAD_TOP) / (1080 - top) if top < HEAD_TOP else 1.0
    transform = {"scale": round(s, 4), "x": round((PERSON_CX - 960 - (cx - 960) * s) / 1920, 4),
                 "y": round(-(1 - s) / 2, 4)}
    return {"headTop": top, "centre": cx, "key": color, "keyParams": key_params(frames, color), "transform": transform}


def pcm(video: Path) -> np.ndarray:
    raw = run(["ffmpeg", "-v", "error", "-i", str(video), "-map", "0:a:0", "-ac", "1", "-ar", str(RATE),
               "-f", "s16le", "-"]).stdout
    return np.frombuffer(raw[: len(raw) // 2 * 2], dtype=np.int16).astype(np.float32) / 32768


def levels_db(x: np.ndarray) -> np.ndarray:
    n = int(RATE * WIN)
    w = x[: len(x) // n * n].reshape(-1, n)
    p = (w * w).mean(axis=1)
    return np.where(p > 0, 10 * np.log10(np.maximum(p, 1e-12)), -100.0)


def integrated_lufs(video: Path) -> float:
    err = subprocess.run(["ffmpeg", "-nostats", "-i", str(video), "-map", "0:a:0", "-af", "ebur128", "-f", "null", "-"],
                         capture_output=True).stderr.decode("utf-8", "replace")
    m = re.findall(r"I:\s+(-?[\d.]+) LUFS", err)
    return float(m[-1]) if m else -23.0


def silences(levels: np.ndarray, threshold: float, min_len: float = 0.5) -> list[list[float]]:
    out, start = [], None
    for i, x in enumerate(list(levels) + [0.0]):
        if x < threshold and start is None:
            start = i
        elif x >= threshold and start is not None:
            if (i - start) * WIN >= min_len:
                out.append([r3(start * WIN), r3(i * WIN)])
            start = None
    return out


def envelope(x: np.ndarray, rate: int = 200) -> np.ndarray:
    n = RATE // rate
    w = x[: len(x) // n * n].reshape(-1, n)
    e = np.log10(np.sqrt((w * w).mean(axis=1)) + 1e-4)
    return (e - e.mean()) / (e.std() + 1e-9)


def offset_of(lecturer: np.ndarray, obs: np.ndarray, rate: int = 200) -> tuple[float, float]:
    """Seconds d with obs time = lecturer time + d (FFT cross-correlation of the loudness envelopes), and the
    peak's height over the next best one far from it (above ~1.5 is a clear match)."""
    a, b = envelope(lecturer, rate), envelope(obs, rate)
    n = 1 << (len(a) + len(b)).bit_length()
    c = np.fft.irfft(np.fft.rfft(b, n) * np.conj(np.fft.rfft(a, n)), n)
    c = np.concatenate([c[-(len(a) - 1):], c[: len(b)]]) if len(a) > 1 else c[: len(b)]
    lags = np.arange(-(len(a) - 1), len(b)) if len(a) > 1 else np.arange(len(b))
    k = int(np.argmax(c))
    far = np.abs(lags - lags[k]) > rate * 2
    clear = float(c[k] / max(1e-9, c[far].max())) if far.any() else 1.0
    return r3(lags[k] / rate), round(clear, 2)


# ---------------------------------------------------------------- transcript and slide timing
def norm(text: str) -> str:
    return re.sub(r"[\W_]+", "", text).lower()


def read_docx(path: Path) -> list[dict]:
    """The transcript's P sections: [{"p": n, "text"}], from paragraphs "P1", "P2", ..."""
    from docx import Document
    out: list[dict] = []
    for para in Document(str(path)).paragraphs:
        t = para.text.strip()
        m = re.fullmatch(r"[Pp]\s*(\d+)", t)
        if m:
            out.append({"p": int(m.group(1)), "text": ""})
        elif t and out:
            out[-1]["text"] += t
    return out


def slide_times(sections: list[dict], caps: list[dict]) -> list[dict]:
    """Where each P section starts in the captions: match the transcript to the captions character by
    character and take the caption holding the section's first matched character."""
    owner, joined = [], []
    for i, c in enumerate(caps):
        n = norm(c["text"])
        joined.append(n)
        owner += [(i, k, len(n)) for k in range(len(n))]
    cap_text = "".join(joined)
    starts, doc = [], ""
    for s in sections:
        starts.append(len(doc))
        doc += norm(s["text"])
    sm = difflib.SequenceMatcher(None, doc, cap_text, autojunk=False)
    pos = {}
    for a, b, size in sm.get_matching_blocks():
        for k in range(size):
            pos[a + k] = b + k
    out = []
    for s, st, nxt in zip(sections, starts, starts[1:] + [len(doc)]):
        hits = [pos[k] for k in range(st, min(nxt, st + 60)) if k in pos]
        if not hits or not owner:
            out.append({"p": s["p"], "at": None, "cap": None, "score": 0.0, "head": s["text"][:24]})
            continue
        first = hits[0] - (min(k for k in range(st, nxt) if k in pos) - st)  # back up over unmatched head
        i, k, n = owner[max(0, min(len(owner) - 1, first))]
        c = caps[i]
        at = c["start"] if k <= 2 else c["start"] + (c["end"] - c["start"]) * k / max(1, n)
        prev_end = caps[i - 1]["end"] if i else 0.0
        at = max(prev_end, at - LEAD) if k <= 2 else at
        out.append({"p": s["p"], "at": r3(at), "cap": c["id"], "score": round(len(hits) / max(1, min(60, nxt - st)), 2),
                    "head": s["text"][:24]})
    return out


def retake_hints(caps: list[dict]) -> list[dict]:
    """Neighbouring captions that say nearly the same thing within 20 s: the earlier one is likely a retake."""
    out = []
    for i, a in enumerate(caps):
        for j in range(i + 1, min(len(caps), i + 4)):
            b = caps[j]
            if b["start"] - a["end"] > 20:
                break
            na, nb = norm(a["text"]), norm(b["text"])
            if len(na) < 4 or len(nb) < 4:
                continue
            short = min(len(na), len(nb))
            ratio = difflib.SequenceMatcher(None, na, nb).ratio()
            head = difflib.SequenceMatcher(None, na[:short], nb[:short]).ratio()
            if ratio > 0.6 or head > 0.75:
                out.append({"a": a["id"], "b": b["id"], "score": round(max(ratio, head), 2)})
    return out


# ---------------------------------------------------------------- prepare
def prepare(args) -> None:
    course = Path(args.course)
    video = Path(args.video)
    work = Path(args.work)
    work.mkdir(parents=True, exist_ok=True)
    assets = Path(args.assets)
    files = {k: assets / v for k, v in ASSET_FILES.items()}
    for k, f in files.items():
        if not f.exists():
            raise SystemExit(f"找不到固定素材 {f}")
    slide_files = sorted((course / "slides").glob("slide-*.png"))
    obs = Path(args.obs) if args.obs else None
    if not slide_files and not obs:
        raise SystemExit("沒有投影片 PNG（先跑 slides）也沒有 --obs 錄影")
    info = probe(video)
    if info["height"] != 1080:
        print(f"注意：講師影片是 {info['width']}x{info['height']}，量測與版面以 1080p 為準", flush=True)
    vc = VC()
    name = args.name or course.name
    project = vc.create_project(name, 1920, 1080, info["fps"])
    pid = project["id"]
    print(f"建立專案 {pid} {name}（{info['fps']} fps）", flush=True)
    lecturer = project["shots"][0]["id"]
    vc.ops(pid, [{"op": "shot.update", "id": lecturer, "patch": {"name": "講師"}}], label="講師鏡頭", undoable=False)
    print(f"匯入講師 {video.name}", flush=True)
    vc.wait(vc.upload(pid, video, shot=lecturer)["id"], quiet=True)
    library = [*files.values(), *slide_files, *([obs] if obs else [])]
    for f in library:
        vc.wait(vc.upload(pid, f)["id"], quiet=True)
    print(f"素材庫匯入 {len(library)} 個檔", flush=True)
    print("語音轉字幕", flush=True)
    vc.wait(vc.job("transcribe", pid, lecturer)["id"])
    project = vc.project(pid)
    by_name = {m["name"]: mid for mid, m in project["media"].items()}
    media = {k: by_name[f.name] for k, f in files.items()}
    slide_media = [{"n": int(f.stem.split("-")[1]), "id": by_name[f.name], "file": f.name,
                    "aspect": "4:3" if abs(project["media"][by_name[f.name]]["width"]
                                           / project["media"][by_name[f.name]]["height"] - 4 / 3) < 0.05 else "16:9"}
                   for f in slide_files]

    caps = sorted(({"id": c["id"], "start": r3(c["start"]), "end": r3(c["end"]), "text": c["text"]}
                   for c in project["captions"]), key=lambda c: c["start"])
    person = measure_person(video, info["duration"], work)
    x = pcm(video)
    lv = levels_db(x)
    speech = [float(np.mean(lv[int(c["start"] / WIN):max(int(c["start"] / WIN) + 1, int(c["end"] / WIN))]))
              for c in caps]
    speech_db = float(np.median(speech)) if speech else -30.0
    noise_db = float(np.percentile(lv, 10))
    threshold = round(noise_db + (speech_db - noise_db) * 0.3, 1)
    quiet = silences(lv, threshold)
    lufs = integrated_lufs(video)
    data = {"pid": pid, "name": name, "course": str(course), "video": str(video), "lecturer": lecturer,
            "duration": r3(info["duration"]), "fps": info["fps"], "media": media, "slides": slide_media,
            "person": person, "lufs": lufs, "speechDb": round(speech_db, 1), "noiseDb": round(noise_db, 1),
            "silenceDb": threshold, "silences": quiet, "captions": caps}
    if obs:
        d, clear = offset_of(x, pcm(obs))
        data["obs"] = {"file": str(obs), "media": by_name[obs.name], "offset": d, "clear": clear}
        print(f"OBS 對齊：OBS 時間 = 講師時間 {d:+.3f}s（明確度 {clear}）", flush=True)
    docx = next((p for p in course.glob("*.docx") if not p.name.startswith("~$")), None)
    sections = read_docx(docx) if docx else []
    data["sections"] = slide_times(sections, caps) if sections and caps else []
    data["retakes"] = retake_hints(caps)
    (work / "transcript.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    (work / "transcript.txt").write_text(transcript_text(data), encoding="utf-8")
    plan_path = work / "plan.json"
    if not plan_path.exists():
        plan_path.write_text(json.dumps(draft_plan(data, args.noface), ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"完成：{len(caps)} 句字幕、{len(data['sections'])} 段逐字稿、{len(slide_media)} 張投影片、"
          f"疑似重講 {len(data['retakes'])} 處 → {work / 'transcript.txt'}", flush=True)


def noise_sample(data: dict) -> dict | None:
    """The longest stretch of silence outside the captions, at most 2 s, as the denoise sample."""
    caps = data["captions"]
    best = None
    for a, b in data["silences"]:
        for c in caps:
            if c["start"] < b and c["end"] > a:  # trim the silence to the part before this caption
                b = min(b, c["start"]) if c["start"] > a else a
        a, b = a + 0.1, b - 0.1
        if b - a >= 0.5 and (best is None or b - a > best[1] - best[0]):
            best = (a, b)
    if not best:
        return None
    a, b = best
    mid = (a + b) / 2
    half = min(1.0, (b - a) / 2)
    return {"from": r3(mid - half), "to": r3(mid + half)}


def draft_plan(data: dict, noface: bool) -> dict:
    caps = data["captions"]
    start = r3(max(0.0, caps[0]["start"] - LEAD)) if caps else 0.0
    end = r3(min(data["duration"], caps[-1]["end"] + TAIL)) if caps else data["duration"]
    by_p = {s["p"]: s for s in data["sections"]}
    slides = []
    for sm in data["slides"]:
        s = by_p.get(sm["n"])
        at = start if sm["n"] == 1 else (s or {}).get("at")
        slides.append({"slide": sm["n"], "at": at})
    makeup = round(max(-6.0, min(20.0, TARGET_LUFS - data["lufs"])), 1)
    return {
        "mode": "noface" if noface else "face",
        "start": start, "end": end,
        "cuts": [],
        "slides": slides,
        "chroma": {"on": True, "color": data["person"]["key"], "similarity": data["person"]["keyParams"]["similarity"],
                   "blend": data["person"]["keyParams"]["blend"]},
        "transform": data["person"]["transform"],
        "denoise": {"on": True, "sample": noise_sample(data)},
        "comp": {"makeup": makeup, "limiter": True, "limiterThreshold": -1.0},
        "captions": {"edit": {}, "delete": []},
        "maxChars": MAX_CHARS,
    }


def transcript_text(data: dict) -> str:
    p = data["person"]
    lines = [f"{data['name']}  pid={data['pid']}  片長 {data['duration']:.2f}s  {data['fps']} fps",
             f"講師：頭頂 {p['headTop']:.0f}px、中線 {p['centre']:.0f}px、綠幕 {p['key']}（去背 {p['keyParams']['similarity']}／{p['keyParams']['blend']}）、位移縮放 {p['transform']}",
             f"聲音：整體 {data['lufs']} LUFS，說話中位 {data['speechDb']} dB，底噪 {data['noiseDb']} dB，"
             f"靜音門檻 {data['silenceDb']} dB",
             f"投影片 {len(data['slides'])} 張（{', '.join(sorted({s['aspect'] for s in data['slides']})) or '無'}），"
             f"逐字稿 {len(data['sections'])} 段", ""]
    if data.get("obs"):
        o = data["obs"]
        lines.insert(4, f"OBS：OBS 時間 = 講師時間 {o['offset']:+.3f}s，明確度 {o['clear']}（低於 1.5 要人工確認）")
    marks: dict[str, list[str]] = {}
    for s in data["sections"]:
        if s["cap"]:
            marks.setdefault(s["cap"], []).append(f"  ▶ P{s['p']} 換頁建議 {s['at']:.2f}（吻合 {s['score']}）{s['head']}")
    missing = [s["p"] for s in data["sections"] if not s["cap"]]
    if missing:
        lines.append(f"找不到位置的逐字稿段落：{missing}")
    hint = {}
    for h in data["retakes"]:
        hint.setdefault(h["a"], []).append(f"  ⚠ 疑似重講：與 {h['b']} 相似 {h['score']}")
    caps = data["captions"]
    for i, c in enumerate(caps):
        gap = c["start"] - (caps[i - 1]["end"] if i else 0.0)
        if gap >= 1.5:
            lines.append(f"        [空 {gap:.1f}s]")
        lines += marks.get(c["id"], [])
        lines.append(f"{i + 1:03d} {c['id']} {c['start']:8.2f}-{c['end']:8.2f} | {c['text']}")
        lines += hint.get(c["id"], [])
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- caption text
def tidy(text: str) -> str:
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
        while 1 < i and re.match(ASCII, text[i - 1]) and re.match(ASCII, text[i]):
            i -= 1
    return split_text(text[:i], limit) + split_text(text[i:], limit)


def caption_ops(project: dict, plan: dict, limit: int) -> list[dict]:
    fix = plan.get("captions") or {}
    deleted = set(fix.get("delete") or [])
    edits = fix.get("edit") or {}
    ops = [{"op": "caption.delete", "id": cid} for cid in deleted if any(c["id"] == cid for c in project["captions"])]
    kept = sorted((c for c in project["captions"] if c["id"] not in deleted), key=lambda c: c["start"])
    next_start = {a["id"]: b["start"] for a, b in zip(kept, kept[1:])}
    for cap in kept:
        end_at = min(cap["end"], next_start.get(cap["id"], cap["end"]))
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
def lecturer_clips(vc: VC, pid: str, ids: set[str]) -> list[dict]:
    return [c for c in vc.timeline(pid)["clips"] if c["shotId"] in ids]


def to_seq(clips: list[dict], t: float) -> float:
    """Sequence seconds of lecturer source second t; inside a cut, the start of the next clip."""
    for c in clips:
        if t < c["in"] - 1e-6:
            return c["start"]
        if t <= c["out"] + 1e-6:
            return c["start"] + (t - c["in"])
    return clips[-1]["start"] + clips[-1]["length"]


def caption_drift(project: dict, data: dict, clips: list[dict]) -> float:
    """Median of (actual - expected) sequence start over the transcribed captions still on the lecturer."""
    src = {c["id"]: c["start"] for c in data["captions"]}
    offs = sorted(c["start"] - to_seq(clips, src[c["id"]]) for c in project["captions"]
                  if c["id"] in src and any(k["in"] <= src[c["id"]] <= k["out"] for k in clips))
    return offs[len(offs) // 2] if offs else 0.0


def check_plan(plan: dict, data: dict) -> None:
    if plan.get("mode") not in ("face", "noface"):
        raise SystemExit("plan.mode 要是 face 或 noface")
    a, b = float(plan["start"]), float(plan["end"])
    if not 0 <= a < b <= data["duration"] + 0.01:
        raise SystemExit("plan 的 start／end 不在影片範圍內")
    last = a
    for c0, c1 in sorted(plan.get("cuts") or []):
        if not last <= c0 < c1 <= b:
            raise SystemExit(f"cuts 的 [{c0}, {c1}] 重疊或超出 start／end")
        last = c1
    if not data.get("obs"):
        ats = [s.get("at") for s in plan["slides"]]
        if any(x is None for x in ats):
            raise SystemExit("plan.slides 還有 at 是 null：每張投影片都要有換頁時間（不用的投影片刪掉）")
        if ats != sorted(ats):
            raise SystemExit("plan.slides 的 at 要由小到大")


def apply(args) -> None:
    plan_path = Path(args.plan)
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    data = json.loads((plan_path.parent / "transcript.json").read_text(encoding="utf-8"))
    check_plan(plan, data)
    vc = VC()
    pid, lid = data["pid"], data["lecturer"]
    project = vc.project(pid)
    if len(project["shots"]) != 1 or project.get("overlays"):
        raise SystemExit("專案已經套用過；要重來請先在 VC 復原到套用前，或重新 prepare")
    noface = plan["mode"] == "noface"
    start, end = r3(plan["start"]), r3(plan["end"])

    # 1. lecturer: trim, key, place, sound
    comp = {**plan.get("comp", {}), "on": True}
    denoise = {"on": True, "strength": 0.6, "low": 1.0, "mid": 1.0, "high": 1.0, **plan.get("denoise", {})}
    patch = {"trimIn": start, "trimOut": end, "volume": 1, "denoise": denoise, "comp": comp,
             "chroma": plan["chroma"], "backdrop": data["media"]["backdrop"], "transform": plan["transform"]}
    if not denoise.get("sample"):
        patch["denoise"] = {**denoise, "on": False}
        print("注意：找不到夠長的靜音當降噪樣本，降噪先關閉", flush=True)
    vc.ops(pid, [{"op": "linked", "ops": [{"op": "shot.update", "id": lid, "patch": patch}]}], label="講師鏡頭設定")

    # 2. retakes, latest first so earlier sequence times stay put
    for c0, c1 in sorted(plan.get("cuts") or [], reverse=True):
        vc.edit(pid, "split", t=r3(c1 - start))
        mid = vc.edit(pid, "split", t=r3(c0 - start))["id"]
        vc.ops(pid, [{"op": "linked", "ops": [{"op": "shot.delete", "id": mid}]}], label="刪除重講")
    project = vc.project(pid)
    ids = {s["id"] for s in project["shots"]}

    # 3. intro + title slide in front, end logo after
    first_slide = next((s for s in data["slides"] if s["n"] == 1), None)
    front = [data["media"]["intro"], *([first_slide["id"]] if first_slide else [])]
    vc.edit(pid, "place", media=front, at=0)
    vc.edit(pid, "place", media=[data["media"]["endLogo"]])
    project = vc.project(pid)
    shots = project["shots"]
    intro, title = shots[0], (shots[1] if first_slide else None)
    tail = shots[-1]
    end_len = r3(project["media"][data["media"]["endSound"]]["duration"])
    ops = [{"op": "shot.update", "id": intro["id"], "patch": {"name": "片頭", "trimOut": INTRO_OUT,
                                                                     "transition": {"type": "crossfade", "duration": CROSSFADE}}},
           {"op": "shot.update", "id": tail["id"], "patch": {"name": "片尾", "duration": end_len}}]
    if title:
        ops.append({"op": "shot.update", "id": title["id"], "patch": {"name": "標題", "duration": TITLE_LEN}})
    vc.ops(pid, ops, label="片頭片尾")
    timeline = vc.timeline(pid)
    end_clip = next(c for c in timeline["clips"] if c["shotId"] == tail["id"])
    vc.edit(pid, "place", media=[data["media"]["endSound"]], at=end_clip["start"])
    clips = lecturer_clips(vc, pid, ids)
    # place ripples captions by the inserted lengths, the later duration / crossfade changes do not:
    # measure where the captions ended up and move them back onto the lecturer
    drift = caption_drift(vc.project(pid), data, clips)
    if abs(drift) > 1e-3:
        vc.ops(pid, [{"op": "caption.update", "id": c["id"],
                      "patch": {"start": r3(c["start"] - drift), "end": r3(c["end"] - drift)}}
                     for c in vc.project(pid)["captions"]], label="字幕跟著講師")

    # 4. slides (or the OBS recording) on their own track
    layer = new_id("l")
    ops = [{"op": "layer.add", "layer": {"id": layer, "name": "投影片"}}]
    lect_end = clips[-1]["start"] + clips[-1]["length"]
    if data.get("obs"):
        o = data["obs"]
        rect = SLIDE_RECT["16:9"]
        for c in clips:
            ops.append({"op": "overlay.add", "item": {
                "id": new_id("o"), "mediaId": o["media"], "layer": layer, "name": "OBS 簡報", "start": r3(c["start"]),
                "in": r3(max(0.0, c["in"] + o["offset"])), "length": r3(c["length"]), **rect, "opacity": 1,
                "volume": 0, "full": noface}})
    else:
        by_n = {s["n"]: s for s in data["slides"]}
        rows = plan["slides"]
        for k, s in enumerate(rows):
            sm = by_n[s["slide"]]
            a = clips[0]["start"] if k == 0 else to_seq(clips, float(s["at"]))
            b = lect_end if k == len(rows) - 1 else to_seq(clips, float(rows[k + 1]["at"]))
            if b - a < 0.1:
                print(f"注意：投影片 {s['slide']} 只有 {b - a:.2f}s（落在剪掉的段落？），略過", flush=True)
                continue
            ops.append({"op": "overlay.add", "item": {
                "id": new_id("o"), "mediaId": sm["id"], "layer": layer, "name": sm["file"], "start": r3(a),
                "in": 0, "length": r3(b - a), **SLIDE_RECT[sm["aspect"]], "opacity": 1, "volume": 1, "full": noface}})
    vc.ops(pid, ops, label="投影片")

    # 5. captions: style and text
    project = vc.project(pid)
    # The owner's usual style, which new projects start with; add it when the library replaced it.
    style = next((s for s in project.get("captionStyles") or [] if s["name"] == CAPTION_STYLE["name"]), None)
    style_ops = []
    if style is None:
        style = {**CAPTION_STYLE, "id": new_id("cs")}
        style_ops.append({"op": "captionStyle.add", "item": style})
    if project["settings"].get("captionStyle") != style["id"]:
        style_ops.append({"op": "project.update", "patch": {"settings": {"captionStyle": style["id"]}}})
    if style_ops:
        vc.ops(pid, style_ops, label="字幕樣式")
    ops = caption_ops(project, plan, int(plan.get("maxChars", MAX_CHARS)))
    if ops:
        vc.ops(pid, ops, label="字幕整理")

    # 6. no face: black picture, sound on its own clips (V on the shot brings the picture back, still in sync)
    if noface:
        vc.ops(pid, [{"op": "shot.update", "id": c["shotId"], "patch": {"pictureHidden": True}} for c in clips],
               label="隱藏講師畫面")
        for c in clips:
            vc.edit(pid, "detach", id=c["shotId"])
    out = vc.timeline(pid)
    overlays = vc.project(pid).get("overlays") or []
    print(f"完成：片長 {out['duration']:.1f}s，講師 {len(clips)} 段，投影片軌 {len(overlays)} 段", flush=True)


# ---------------------------------------------------------------- verify
def verify(args) -> None:
    work = Path(args.work)
    data = json.loads((work / "transcript.json").read_text(encoding="utf-8"))
    plan = json.loads((work / "plan.json").read_text(encoding="utf-8"))
    vc = VC()
    pid = data["pid"]
    project = vc.project(pid)
    tl = vc.timeline(pid)
    problems: list[str] = []
    names = [c["name"] for c in tl["clips"]]
    print("畫面軌：" + "，".join(f"{c['name']} {c['start']:.2f}+{c['length']:.2f}" for c in tl["clips"]))
    lect = [c for c in tl["clips"] if c["name"] == "講師"]
    if names[0] != "片頭" or names[-1] != "片尾" or not lect:
        problems.append("畫面軌順序不是 片頭、(標題)、講師…、片尾")
    elif tl["clips"][0]["out"] > INTRO_OUT + 0.01:
        problems.append(f"片頭沒裁掉尾端黑畫面（出點 {tl['clips'][0]['out']:.2f}s，應 ≤ {INTRO_OUT}s）")
    shots = {s["id"]: s for s in project["shots"]}
    for c in lect:
        s = shots[c["shotId"]]
        if not (s.get("chroma") or {}).get("on") or not s.get("backdrop"):
            problems.append(f"講師鏡頭 {s['id']} 沒有去背或背景")
        if not (s.get("comp") or {}).get("on"):
            problems.append(f"講師鏡頭 {s['id']} 沒有開壓縮器")
        if plan["mode"] == "noface" and not (s.get("pictureHidden") and s.get("audioDetached")):
            problems.append(f"無露臉模式：講師鏡頭 {s['id']} 沒有隱藏畫面或分離原聲")
    lect_a, lect_b = lect[0]["start"], lect[-1]["start"] + lect[-1]["length"]
    cut = sum(b - a for a, b in plan.get("cuts") or [])
    want = plan["end"] - plan["start"] - cut
    if abs((lect_b - lect_a) - want) > 0.05:
        problems.append(f"講師總長 {lect_b - lect_a:.2f}s，預期 {want:.2f}s")
    overlays = sorted((o for o in project.get("overlays") or []), key=lambda o: o["start"])
    t = lect_a
    for o in overlays:
        if o["start"] - t > 0.05:
            problems.append(f"投影片空檔 {t:.2f}-{o['start']:.2f}")
        if o["start"] < t - 0.05:
            problems.append(f"投影片重疊 {o['start']:.2f}（前一張到 {t:.2f}）")
        t = o["start"] + o["length"]
    if overlays and abs(t - lect_b) > 0.05:
        problems.append(f"最後一張投影片停在 {t:.2f}，講師到 {lect_b:.2f}")
    # captions follow the lecturer: each surviving caption sits where its source time maps to
    src = {c["id"]: c for c in data["captions"]}
    off = []
    for c in project["captions"]:
        if c["id"] in src and (plan.get("captions") or {}).get("edit", {}).get(c["id"]) is None:
            s0 = src[c["id"]]["start"]
            if plan["start"] <= s0 and all(not (a <= s0 < b) for a, b in plan.get("cuts") or []):
                off.append(c["start"] - to_seq(lect, s0))
    if off and max(abs(x) for x in off) > 0.05:
        problems.append(f"字幕與講師錯位，最大 {max(off, key=abs):+.2f}s")
    default = next((s for s in project.get("captionStyles") or [] if s["id"] == project["settings"].get("captionStyle")), None)
    if not default or default["name"] != CAPTION_STYLE["name"]:
        problems.append(f"字幕預設樣式不是「{CAPTION_STYLE['name']}」（目前：{default['name'] if default else '內建樣式'}）")
    long_caps = [c["text"] for c in project["captions"] if clen(c["text"]) > int(plan.get("maxChars", MAX_CHARS))]
    if long_caps:
        problems.append(f"{len(long_caps)} 句字幕超過 {plan.get('maxChars', MAX_CHARS)} 字")
    outside = [c["text"] for c in project["captions"] if c["start"] < lect_a - 0.05 or c["end"] > lect_b + 0.05]
    if outside:
        problems.append(f"{len(outside)} 句字幕在講師段落外：{outside[:3]}")
    end_sound = [a for a in project.get("audio") or [] if a.get("mediaId") == data["media"]["endSound"]]
    if not end_sound or abs(end_sound[0]["start"] - tl["clips"][-1]["start"]) > 0.05:
        problems.append("片尾音訊沒有對齊片尾鏡頭")
    print(f"片長 {tl['duration']:.2f}s，講師 {len(lect)} 段 {lect_b - lect_a:.2f}s，投影片 {len(overlays)} 段，"
          f"字幕 {len(project['captions'])} 句，字幕位置誤差最大 {max((abs(x) for x in off), default=0):.3f}s")
    print("問題：\n  " + "\n  ".join(problems) if problems else "檢查通過")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("slides")
    p.add_argument("course")
    p = sub.add_parser("prepare")
    p.add_argument("--course", required=True)
    p.add_argument("--video", required=True)
    p.add_argument("--work", required=True)
    p.add_argument("--name")
    p.add_argument("--noface", action="store_true")
    p.add_argument("--obs")
    p.add_argument("--assets", default=str(ASSETS))
    p = sub.add_parser("apply")
    p.add_argument("plan")
    p = sub.add_parser("verify")
    p.add_argument("work")
    args = ap.parse_args()
    {"slides": slides, "prepare": prepare, "apply": apply, "verify": verify}[args.cmd](args)


if __name__ == "__main__":
    main()
