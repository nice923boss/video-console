"""Measure a picture's colour and brightness, and suggest a white balance block (grade["wb"]) for it.

Behind the grade panel's 取樣 / 自動白平衡 / 自動曝光 buttons and the analyze / auto-color endpoints, so an
editor (or Claude through the API) can read what a shot looks like (average colour, brightness percentiles,
clipped highlights and shadows) and correct it in one call. Frames are decoded small, as sRGB taken as BT.709
like the rest of the app (media.AS_709), from the smallest preview copy a video has (lowres.py), since the
colour of a 1/4 copy is the colour of the original.

White balance: the colour of what should be neutral is estimated from the least saturated mid-tone pixels
(the patch under x/y/size when one is given), and becomes the block's sample colour. A scene lit in strong
colour on purpose (a purple stage) would be turned grey by a full correction, so `amount` is lowered until the
largest channel gain is at most MAX_CAST times the smallest. wb "auto" first decides whether to balance at all,
from how coloured that neutral estimate is (chroma: channel spread over brightness): under NEUTRAL_CHROMA the
camera already got it right, past STAGE_CHROMA the colour is light on purpose (a stage, an LED wall) and is kept;
both leave r/g/b neutral. Tuned on event footage: daylight and house lights 0.03-0.05, a warm or cool cast
0.07-0.19, stage colour 0.22-0.25.

Exposure: blacks pulls a raised floor down, gain spreads dull highlights toward the top, gamma moves the
metered brightness to the target, and when gamma has reached its limit fill lifts the mid-tones the rest of the
way; all solved on the picture as balanced. The corrections are kept gentle (BLACKS_MAX, GAIN_MAX, and gamma
darkens by at most GAMMA_MIN): spreading every shot to a full range and pulling the median back down with gamma
read as over-graded and dark. Clipped highlights get no extra gain, and gain stops where the brightest channel
(not the brightness) reaches the top, so a saturated stage colour does not clip.

What is metered: in a medium shot or a close-up the people are what the eye judges, so a shot whose face size
(the median over the sampled frames of each frame's largest face, a frame without one counting 0) is at least
PERSON_FACE of the picture height is metered on its faces, aiming their brightness at FACE_TARGET; a wide shot,
or a shot with no face found, is metered on the whole picture, where target "auto" keeps a picture's own median
when it is in AUTO_RANGE and brings it to the nearer end otherwise. The face brightness is the median of each
face's own median, weighted by the face's height: a bigger face counts for more, yet one stray box (Haar framing
a head and shoulders as one face in a stage group) cannot outweigh the faces around it, and a box found in one
frame of three does not make a wide shot a person shot. Faces are found with OpenCV's bundled Haar cascades (frontal, and profile both ways) on a
DETECT_WIDTH frame, and a box counts only when its centre looks like skin and sits in the upper part of the
picture: Haar also fires on portraits on a screen, a blue backdrop, white light, a grey object, a hand or a chair
at the bottom. Without OpenCV every shot is metered as a scene. Tuned on indoor event footage against the
editor's own hand grades: faces at 0.44-0.52 looked right, and whole-picture medians of 0.58-0.69 for those
shots, so the scene range reaches 0.60.
"""
from __future__ import annotations

import logging
import math
import statistics
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Sequence

from . import grade as gr
from .media import Media
from .sequence import build_sequence, overlay_speed
from .takes import find_item, item_path
from .util import AppError

log = logging.getLogger("vc.autocolor")

WIDTH = 128  # analysis frame width; 128x72 for 16:9
MAX_CAST = 1.45  # largest / smallest channel gain an automatic white balance may use
NEUTRAL_SHARE = 0.12  # share of mid-tone pixels, the least saturated, averaged as the neutral colour
STRENGTH = 0.6  # default share of the colour correction an automatic white balance applies
TARGET = 0.45  # default median brightness auto exposure aims for
FLOOR = 0.03  # where auto exposure puts the darkest 1% of the picture
HIGH, LOW = 0.98, 0.02  # a pixel at or past these is counted as clipped
AUTO = "auto"
NEUTRAL_CHROMA = 0.06  # wb "auto": a neutral estimate less coloured than this is left as it is
STAGE_CHROMA = 0.20  # wb "auto": more coloured than this is coloured light on purpose, kept
AUTO_RANGE = (0.42, 0.60)  # target "auto": a median outside this range is brought to its nearer end
FILL_MAX = 0.6  # the most fill auto exposure adds once gamma is at its limit
BLACKS_MAX, GAIN_MAX, GAMMA_MIN = 0.3, 0.3, -0.3  # how far auto exposure may go: floor, spread, darkening
DETECT_WIDTH = 960  # face detection frame width (a face of PERSON_FACE is then about the cascades' 24 px)
PERSON_FACE = 0.05  # face size (face height / picture height) from which a shot is metered on its people
FACE_TARGET = 0.47  # median face brightness a person shot is brought to
SUBJECTS = (AUTO, "scene")  # body.subject: meter people when there are any, or always the whole picture
CASCADES = ("haarcascade_frontalface_default.xml", "haarcascade_profileface.xml")
Pixel = tuple[float, float, float]


def luma(p: Sequence[float]) -> float:
    return gr.LUMA[0] * p[0] + gr.LUMA[1] * p[1] + gr.LUMA[2] * p[2]


def decode(data: bytes) -> list[Pixel]:
    return [(data[i] / 255, data[i + 1] / 255, data[i + 2] / 255) for i in range(0, len(data) - 2, 3)]


def region_of(pixels: list[Pixel], w: int, h: int, x: float, y: float, size: float) -> list[Pixel]:
    """Pixels of the square centred on (x, y) (picture fractions) whose side is `size` of the picture width."""
    half = max(0.5, size * w / 2)
    cx, cy = x * w, y * h
    x0, x1 = max(0, int(cx - half)), min(w, int(math.ceil(cx + half)))
    y0, y1 = max(0, int(cy - half)), min(h, int(math.ceil(cy + half)))
    if x1 <= x0 or y1 <= y0:
        raise AppError("取樣範圍在畫面外")
    return [pixels[r * w + c] for r in range(y0, y1) for c in range(x0, x1)]


def percentile(sorted_values: list[float], q: float) -> float:
    if not sorted_values:
        return 0.0
    i = min(len(sorted_values) - 1, max(0, int(round(q * (len(sorted_values) - 1)))))
    return sorted_values[i]


def weighted_median(pairs: Sequence[tuple[float, float]]) -> float:
    """The value where (weight, value) pairs, in order of value, reach half their total weight."""
    ordered = sorted(pairs, key=lambda wv: wv[1])
    half, acc = sum(w for w, _ in ordered) / 2, 0.0
    for w, v in ordered:
        acc += w
        if acc >= half:
            return v
    return ordered[-1][1] if ordered else 0.0


def mean(pixels: list[Pixel]) -> Pixel:
    n = max(1, len(pixels))
    return (sum(p[0] for p in pixels) / n, sum(p[1] for p in pixels) / n, sum(p[2] for p in pixels) / n)


def neutral_of(pixels: list[Pixel]) -> Pixel:
    """The colour that should be grey: the least saturated mid-tone pixels, or the plain average (grey world)."""
    mids = [p for p in pixels if 0.12 < luma(p) < 0.92]
    if len(mids) < 20:
        return mean(pixels)
    mids.sort(key=lambda p: (max(p) - min(p)) / max(luma(p), 0.05))
    return mean(mids[:max(10, int(len(mids) * NEUTRAL_SHARE))])


def stats(pixels: list[Pixel]) -> dict:
    """Average colour, brightness percentiles and clipped shares of a set of pixels (0..1 values)."""
    ls = sorted(luma(p) for p in pixels)
    m = mean(pixels)
    n = max(1, len(pixels))
    return {
        "mean": [round(c, 4) for c in m],
        "luma": round(sum(ls) / n, 4),
        "p01": round(percentile(ls, 0.01), 4), "p05": round(percentile(ls, 0.05), 4),
        "p50": round(percentile(ls, 0.5), 4), "p95": round(percentile(ls, 0.95), 4),
        "p99": round(percentile(ls, 0.99), 4),
        "max99": round(percentile(sorted(max(p) for p in pixels), 0.99), 4),  # brightest channel: what clips
        "clipHigh": round(sum(1 for p in pixels if max(p) >= HIGH) / n, 4),
        "clipLow": round(sum(1 for v in ls if v <= LOW) / n, 4),
        "cast": [round(c - luma(m), 4) for c in m],  # how far the average leans to each channel
        "neutral": [round(c, 4) for c in neutral_of(pixels)],
    }


def chroma_of(p: Sequence[float]) -> float:
    """How coloured a pixel is: channel spread over brightness (0 for a grey)."""
    return round((max(p) - min(p)) / max(luma(p), 0.05), 4)


def auto_target(st: dict) -> float:
    """The median target "auto" picks for a picture with these stats."""
    return round(min(AUTO_RANGE[1], max(AUTO_RANGE[0], st["p50"])), 3)


def _cast_ratio(wb: dict) -> float:
    g = gr.wb_gains(wb)
    return max(g) / max(min(g), 1e-6)


def balance(sample: Sequence[float], strength: float = STRENGTH) -> dict:
    """r/g/b/amount of a white balance that greys `sample`, the amount lowered to keep the cast in MAX_CAST."""
    r, g, b = (min(1.0, max(0.01, float(c))) for c in sample)
    wb = {**gr.NEUTRAL_WB, "r": round(r, 4), "g": round(g, 4), "b": round(b, 4)}
    amount = min(1.0, max(0.0, float(strength)))
    lo, hi = 0.0, amount
    if _cast_ratio({**wb, "amount": amount}) > MAX_CAST:
        for _ in range(24):
            mid = (lo + hi) / 2
            if _cast_ratio({**wb, "amount": mid}) > MAX_CAST:
                hi = mid
            else:
                lo = mid
        amount = lo
    return {"r": wb["r"], "g": wb["g"], "b": wb["b"], "amount": round(amount, 3)}


def exposure(st: dict, target: float = TARGET) -> dict:
    """blacks / gain / gamma / fill that bring these brightness stats (as balanced) toward a clean floor and a
    full top, as far as BLACKS_MAX / GAIN_MAX allow, and p50 (the median, or the faces' median) to the target,
    darkening by at most GAMMA_MIN."""
    target = min(0.7, max(0.2, float(target)))
    p01, p50 = st["p01"], st["p50"]
    # Gain lifts every channel, so a saturated colour reaches the top before the brightness does: spread to the
    # brightest channel's 99th percentile, not the brightness'.
    p99 = max(st["p99"], st.get("max99", st["p99"]))
    blacks = gain = gamma = 0.0
    # Gamma lifts the floor blacks pulled down and blacks moves the median gamma aims at, so settle them together;
    # gamma is solved last, so the median lands on the target and the floor near FLOOR.
    for _ in range(8):
        q = FLOOR ** (2 ** gamma) / (1 + 0.5 * gain)  # the floor before gain and gamma that ends at FLOOR
        blacks = min(BLACKS_MAX, max(0.0, (p01 - q) / (1 - q) / 0.1)) if p01 > q else 0.0
        blk = 0.1 * blacks

        def lifted(x: float) -> float:
            return max(0.0, (x - blk) / (1 - blk))

        top = lifted(p99)
        gain = 0.0
        if st["clipHigh"] < 0.01 and 0.05 < top < 0.9:
            gain = min(GAIN_MAX, max(0.0, (0.95 / top - 1) / 0.5))
        med = min(0.98, max(0.02, lifted(p50) * (1 + 0.5 * gain)))
        gamma = 0.0
        if abs(med - target) > 0.01:
            gamma = min(1.0, max(GAMMA_MIN, -math.log2(math.log(target) / math.log(med))))
    fill = 0.0
    after = med ** (2 ** -gamma)
    if gamma >= 1.0 and after < target - 0.01:  # gamma at its limit: fill (x + 1.2 f x (1-x)^2) does the rest
        fill = min(FILL_MAX, (target - after) / (1.2 * after * (1 - after) ** 2))
    return {"blacks": round(blacks, 3), "gain": round(gain, 3), "gamma": round(gamma, 3), "fill": round(fill, 3)}


def solve(pixels: list[Pixel], current: dict | None = None, *, wb: bool | str = True, expo: bool = True,
          target: float | str = TARGET, strength: float = STRENGTH, sample: Sequence[float] | None = None,
          faces: list[tuple[float, list[Pixel]]] | None = None) -> tuple[dict, dict]:
    """The white balance block for these pixels: `current` (a grade's wb block) with the balance and/or the
    exposure fields replaced, and what was decided: {"wb": "balance" | "neutral" | "stage" | "off", "chroma",
    "subject"?: "person" | "scene", "face"?, "target"?}. wb True balances, "auto" balances only a neutral
    estimate between NEUTRAL_CHROMA and STAGE_CHROMA (r/g/b go back to neutral otherwise). Exposure is solved on
    the picture after the new balance, metered on `faces` ((weight, centre pixels) per face, see people()) when
    given, as the weighted median of each face's median, else on the whole picture; target "auto" picks
    FACE_TARGET for faces and auto_target of the picture otherwise."""
    out = gr.clean_wb(current)
    grey = sample or neutral_of(pixels)
    chroma = chroma_of(grey)
    if wb == AUTO:
        mode = "neutral" if chroma < NEUTRAL_CHROMA else "stage" if chroma > STAGE_CHROMA else "balance"
    else:
        mode = "balance" if wb else "off"
    if mode == "balance":
        out.update(balance(grey, strength))
    elif mode != "off":
        out.update({k: gr.NEUTRAL_WB[k] for k in ("r", "g", "b", "amount")})
    note: dict[str, Any] = {"wb": mode, "chroma": chroma}
    if expo:
        probe = {**out, "blacks": 0.0, "gain": 0.0, "gamma": 0.0, "fill": 0.0}
        apply = gr.wb_apply(probe)
        st = stats([apply(*p) for p in pixels] if apply else pixels)
        if faces:
            seen = [(w, percentile(sorted(luma(apply(*p) if apply else p) for p in px), 0.5)) for w, px in faces]
            face = round(weighted_median(seen), 4)
            note.update(subject="person", face=face, target=FACE_TARGET if target == AUTO else float(target))
            out.update(exposure({**st, "p50": face}, note["target"]))
        else:
            note.update(subject="scene", target=auto_target(st) if target == AUTO else float(target))
            out.update(exposure(st, note["target"]))
    return out, note


def suggest(pixels: list[Pixel], current: dict | None = None, **kw: Any) -> dict:
    """solve's white balance block alone."""
    return solve(pixels, current, **kw)[0]


def graded(pixels: list[Pixel], grade: dict | None) -> list[Pixel]:
    """The pixels through a grade (as preview and export show them)."""
    if not grade:
        return pixels
    full = gr.clean_grade(grade)
    if gr.is_neutral(full):
        return pixels
    fn = gr.grader(full)
    return [fn(*p) for p in pixels]


# ---------------------------------------------------------------- reading frames from a project
def source_file(project_dir: Path, item: dict) -> Path:
    """The smallest copy of a take or media item that has the same picture."""
    for key in ("preview4", "preview2"):
        if item.get(key) and (project_dir / item[key]).is_file():
            return project_dir / item[key]
    return item_path(project_dir, item)


def frame(media: Media, path: Path, at: float, still: bool) -> tuple[int, int, list[Pixel]]:
    data = media.rgb_frame(path, 0.0 if still else at, WIDTH)
    w = WIDTH
    h = len(data) // (3 * w)
    if h <= 0:
        raise AppError(f"讀不到畫面：{path.name}", 500)
    return w, h, decode(data[: w * h * 3])


# ---------------------------------------------------------------- finding the people in a picture
FACE_TOP = 0.6  # a face box's centre must be in this top share of the picture (lower down: hands, chairs)
SKIN_CHROMA = (0.35, 1.4)  # colour of a face box's centre: real faces read 0.45-1.18, grey objects and logos < 0.25
SKIN_LUMA = (0.08, 0.85)
_KIT_LOCK = threading.Lock()  # guards loading and using the classifiers (not safe to share between threads)
_KIT: Any = None  # (cv2, numpy, [frontal, profile]) once loaded; False when OpenCV or its cascades are missing


def _load_kit() -> Any:
    try:
        import cv2
        import numpy as np

        cascades = []
        for name in CASCADES:
            # Through memory: the cascade loader cannot open a file under a non-ASCII folder.
            text = (Path(cv2.data.haarcascades) / name).read_text(encoding="utf-8")
            storage = cv2.FileStorage(text, cv2.FILE_STORAGE_READ | cv2.FILE_STORAGE_MEMORY)
            cascade = cv2.CascadeClassifier()
            loaded = cascade.read(storage.getFirstTopLevelNode())
            storage.release()
            if not loaded:
                raise RuntimeError(f"無法讀取 {name}")
            cascades.append(cascade)
    except Exception as e:  # noqa: BLE001 - any failure here only turns person metering off
        log.warning("找不到 OpenCV 或它的人臉偵測檔，自動曝光改以整體畫面測光：%s", e)
        return False
    return cv2, np, cascades


def _kit() -> Any:
    global _KIT
    with _KIT_LOCK:
        if _KIT is None:
            _KIT = _load_kit()
    return _KIT or None


def detect_source(project_dir: Path, item: dict) -> Path:
    """The smallest copy of a take or media item at least DETECT_WIDTH wide: small faces need the detail."""
    width = item.get("width") or 0
    for key, scale in (("preview4", 4), ("preview2", 2)):
        if item.get(key) and width / scale >= DETECT_WIDTH and (project_dir / item[key]).is_file():
            return project_dir / item[key]
    return item_path(project_dir, item)


def _centre(rgb: Any, box: Sequence[int]) -> Any:
    x, y, w, h = box
    return rgb[y + h // 4: y + 3 * h // 4, x + w // 4: x + 3 * w // 4]


def face_boxes(kit: Any, rgb: Any) -> list[tuple[int, int, int, int]]:
    """Boxes (x, y, w, h) of the real faces in an RGB uint8 array, largest first. Call with _KIT_LOCK held."""
    cv2, np, (front, profile) = kit
    h, w = rgb.shape[:2]
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    size = (max(16, int(0.04 * h)),) * 2
    found = [tuple(b) for b in front.detectMultiScale(gray, 1.1, 6, minSize=size)]
    found += [tuple(b) for b in profile.detectMultiScale(gray, 1.1, 6, minSize=size)]
    # The profile cascade finds faces turned one way; the mirrored picture gives the other.
    found += [(w - x - bw, y, bw, bh)
              for x, y, bw, bh in profile.detectMultiScale(np.ascontiguousarray(gray[:, ::-1]), 1.1, 6, minSize=size)]
    kept: list[tuple[int, int, int, int]] = []
    for box in sorted(found, key=lambda b: b[3], reverse=True):
        x, y, bw, bh = (int(v) for v in box)
        cx, cy = x + bw / 2, y + bh / 2
        if cy > FACE_TOP * h or any(kx <= cx <= kx + kw and ky <= cy <= ky + kh for kx, ky, kw, kh in kept):
            continue
        patch = _centre(rgb, (x, y, bw, bh)).reshape(-1, 3) / 255.0
        r, g, b = (float(v) for v in np.median(patch, axis=0))
        light = float(np.median(patch @ np.array(gr.LUMA)))
        colour = (max(r, g, b) - min(r, g, b)) / max(light, 0.05)
        # Skin is reddest; screens, blue backdrops and white light are not, grey things and logos are too plain.
        if r >= max(g, b) and SKIN_CHROMA[0] <= colour <= SKIN_CHROMA[1] and SKIN_LUMA[0] <= light <= SKIN_LUMA[1]:
            kept.append((x, y, bw, bh))
    return kept


def people(media: Media, path: Path, times: Sequence[float], still: bool) -> dict | None:
    """The people in frames of a video at file times (a still is read once): {"size": the median over the
    frames of each frame's largest face height / picture height, a frame without a face counting 0, "count":
    most faces in one frame, "faces": (height share, centre pixels) of every face found}; size 0 when there is
    none. None when OpenCV is not available."""
    kit = _kit()
    if kit is None:
        return None
    np = kit[1]
    largest: list[float] = []
    faces: list[tuple[float, list[Pixel]]] = []
    count = 0
    for at in [0.0] if still else times:
        data = media.rgb_frame(path, at, DETECT_WIDTH)
        h = len(data) // (3 * DETECT_WIDTH)
        if h <= 0:
            raise AppError(f"讀不到畫面：{path.name}", 500)
        rgb = np.frombuffer(data[: DETECT_WIDTH * h * 3], np.uint8).reshape(h, DETECT_WIDTH, 3)
        with _KIT_LOCK:
            boxes = face_boxes(kit, rgb)
        largest.append(boxes[0][3] / h if boxes else 0.0)
        count = max(count, len(boxes))
        for box in boxes:
            patch = _centre(rgb, box)
            step = max(1, patch.shape[0] // 24)  # about 24 x 24 pixels a face is plenty for a median
            faces.append((box[3] / h, [tuple(q) for q in (patch[::step, ::step].reshape(-1, 3) / 255.0).tolist()]))
    size = statistics.median(largest) if largest else 0.0
    return {"size": round(size, 4), "count": count, "faces": faces}


def meter(media: Media, path: Path, times: Sequence[float], still: bool
          ) -> tuple[list[tuple[float, list[Pixel]]] | None, dict]:
    """The faces to meter exposure on, as solve takes them (None: a wide shot, or no face, is metered on the
    whole picture) and what was found, for the decision note."""
    found = people(media, path, times, still)
    if found is None:
        return None, {"detector": "missing"}
    extra = {"faces": found["count"], "faceSize": found["size"]}
    return (found["faces"] if found["size"] >= PERSON_FACE and found["faces"] else None), extra


def shot_source(project: dict, shot_id: str) -> tuple[dict, dict, float, float, float]:
    """(shot, take, file in, speed, length) of a shot with a picture."""
    for clip in build_sequence(project, include_all=True):
        if clip["shot"]["id"] == shot_id:
            if clip["source"] == "empty":
                raise AppError("這個鏡頭沒有畫面可以分析")
            return clip["shot"], clip["take"], clip["in"], clip["speed"], clip["length"]
    raise AppError("找不到這個鏡頭", 404)


def shot_frames(media: Media, project_dir: Path, project: dict, shot_id: str,
                times: Sequence[float]) -> tuple[int, int, list[list[Pixel]], list[float]]:
    """Frames of a shot at clip times (seconds from the clip's start on the timeline)."""
    shot, take, t_in, speed, length = shot_source(project, shot_id)
    path = source_file(project_dir, take)
    still = take.get("kind") == "image"
    out: list[list[Pixel]] = []
    at_file: list[float] = []
    w = h = 0
    for t in times:
        local = min(max(0.0, float(t)), max(0.0, length - 0.05))
        at = t_in + local * speed
        w, h, px = frame(media, path, at, still)
        out.append(px)
        at_file.append(round(at, 3))
    return w, h, out, at_file


def spread(length: float, count: int) -> list[float]:
    """`count` clip times spread over a clip, away from its first and last frames."""
    count = max(1, min(9, int(count)))
    return [round(length * (i + 1) / (count + 1), 3) for i in range(count)]


def number(value: Any, what: str) -> float:
    """float(value) of a request field, or an AppError naming it."""
    try:
        out = float(value)
    except (TypeError, ValueError):
        out = math.nan
    if not math.isfinite(out):
        raise AppError(f"{what} 要是數字")
    return out


def options(body: dict) -> tuple[bool | str, bool, float | str, float]:
    """(white balance on / "auto", exposure on, target median or "auto", strength) from a request body."""
    do_wb = AUTO if body.get("wb") == AUTO else body.get("wb", True) is not False
    do_expo = body.get("exposure", True) is not False
    if not do_wb and not do_expo:
        raise AppError("wb 與 exposure 至少要開一個")
    raw = body.get("target")
    try:
        target = AUTO if raw == AUTO else min(0.7, max(0.2, float(raw or TARGET)))
    except (TypeError, ValueError):
        raise AppError('target 要是 0.2~0.7 的數字或 "auto"') from None
    strength = min(1.0, max(0.0, number(STRENGTH if body.get("strength") is None else body["strength"], "strength")))
    return do_wb, do_expo, target, strength


def meter_people(body: dict) -> bool:
    """body.subject: "auto" (default) meters exposure on the people where there are any, "scene" always on the
    whole picture."""
    subject = body.get("subject") or AUTO
    if subject not in SUBJECTS:
        raise AppError('subject 要是 "auto" 或 "scene"')
    return subject == AUTO


def analyze(media: Media, project_dir: Path, project: dict, body: dict) -> dict:
    """One picture's stats: a shot at clip time t, an overlay at t, a library item at file time t, or whatever
    shot is on the timeline at `at`. x/y/size also measure that patch (the 取樣 spot); graded true measures
    the shot through its own grade, as the preview shows it. The suggestion (wb / exposure / target / strength
    as in auto_color) starts from body.current or the shot's wb block, and is solved on the source picture, or
    with basis "graded" on the picture through the shot's grade (for an adjustment layer above it). Exposure is
    metered on the people in the frame as in auto_color (subject)."""
    do_wb, do_expo, target_l, strength = options(body)
    by_people = meter_people(body)
    t = number(body.get("t") or 0.0, "t")
    shot_id = body.get("shot")
    if body.get("at") is not None and not shot_id:
        at = number(body["at"], "at")
        for clip in build_sequence(project):
            if clip["start"] <= at < clip["start"] + clip["length"] and clip["source"] != "empty":
                shot_id, t = clip["shot"]["id"], at - clip["start"]
                break
        else:
            raise AppError("這個時間點沒有畫面")
    grade = None
    if shot_id:
        shot, take, t_in, speed, length = shot_source(project, str(shot_id))
        w, h, frames, at_file = shot_frames(media, project_dir, project, shot["id"], [t])
        target = {"shot": shot["id"], "t": round(min(max(0.0, t), length), 3), "fileTime": at_file[0]}
        grade = shot.get("grade")
        item, at_item = take, at_file[0]
    else:
        item = None
        if body.get("overlay"):
            ov = next((o for o in project.get("overlays", []) if o.get("id") == body["overlay"]), None)
            if not ov:
                raise AppError("找不到這個畫面軌片段", 404)
            item = find_item(project, ov.get("mediaId") or ov.get("takeId"))
            t = float(ov.get("in") or 0.0) + max(0.0, t) * overlay_speed(project, ov)
        elif body.get("media"):
            item = find_item(project, str(body["media"]))
        else:
            raise AppError("請指定 shot、overlay、media 或 at")
        if not item or item.get("kind") not in ("video", "image"):
            raise AppError("找不到可以分析的畫面", 404)
        w, h, px = frame(media, source_file(project_dir, item), t, item.get("kind") == "image")
        frames = [px]
        target = {"item": item["id"], "fileTime": round(t, 3)}
        at_item = t
    pixels = frames[0]
    out: dict[str, Any] = {**target, "width": w, "height": h, "stats": stats(pixels)}
    seen = graded(pixels, grade) if grade else pixels
    if body.get("graded") and grade:
        out["graded"] = stats(seen)
    basis = seen if body.get("basis") == "graded" else pixels
    sample = None
    if body.get("x") is not None and body.get("y") is not None:
        x, y = number(body["x"], "x"), number(body["y"], "y")
        size = number(body.get("size") or gr.NEUTRAL_WB["size"], "size")
        patch = region_of(basis, w, h, min(1.0, max(0.0, x)), min(1.0, max(0.0, y)), min(1.0, max(0.01, size)))
        sample = mean(patch)
        out["region"] = {"x": round(x, 4), "y": round(y, 4), "size": round(size, 4), **stats(patch)}
    current = body["current"] if isinstance(body.get("current"), dict) else (grade or {}).get("wb")
    faces, extra = None, {}
    if do_expo and by_people:
        faces, extra = meter(media, detect_source(project_dir, item), [at_item], item.get("kind") == "image")
        if faces and body.get("basis") == "graded" and grade:
            faces = [(w, graded(px, grade)) for w, px in faces]
    out["suggest"], out["decision"] = solve(basis, current, wb=do_wb, expo=do_expo, target=target_l,
                                            strength=strength, sample=sample, faces=faces)
    out["decision"].update(extra)
    if sample is not None:
        out["suggest"].update({"x": out["region"]["x"], "y": out["region"]["y"], "size": out["region"]["size"]})
    return out


def auto_color(media: Media, project_dir: Path, project: dict, body: dict) -> tuple[list[dict], list[dict]]:
    """White balance and/or exposure for each shot in body.ids (default: every shot in the sequence with a
    picture), from `samples` frames spread over each clip, exposure metered on the people in them (subject).
    Returns (shot.update ops, per-shot report)."""
    do_wb, do_expo, target, strength = options(body)
    by_people = meter_people(body)
    samples = int(number(body.get("samples") or 3, "samples"))
    ids = body.get("ids")
    clips = [c for c in build_sequence(project, include_all=bool(ids)) if c["source"] != "empty"]
    if ids:
        if not isinstance(ids, list):
            raise AppError("ids 必須是鏡頭 id 清單")
        wanted = {str(i) for i in ids}
        clips = [c for c in clips if c["shot"]["id"] in wanted]
        missing = wanted - {c["shot"]["id"] for c in clips}
        if missing:
            raise AppError(f"找不到有畫面的鏡頭：{'、'.join(sorted(missing))}", 404)
    if not clips:
        raise AppError("沒有可以調整的鏡頭")

    def one(clip: dict) -> tuple[dict, dict]:
        shot = clip["shot"]
        still = clip["take"].get("kind") == "image"
        times = [0.0] if still else spread(clip["length"], samples)
        _, _, frames, at_file = shot_frames(media, project_dir, project, shot["id"], times)
        pixels = [p for f in frames for p in f]
        old = gr.clean_grade(shot.get("grade"))
        faces, extra = None, {}
        if do_expo and by_people:
            faces, extra = meter(media, detect_source(project_dir, clip["take"]), at_file, still)
        wb, note = solve(pixels, old["wb"], wb=do_wb, expo=do_expo, target=target, strength=strength, faces=faces)
        note.update(extra)
        new = {**old, "wb": wb}
        report = {"id": shot["id"], "name": shot.get("name") or "", "before": stats(graded(pixels, old)),
                  "after": stats(graded(pixels, new)), "wb": wb, "decision": note}
        return {"op": "shot.update", "id": shot["id"], "patch": {"grade": new}}, report

    with ThreadPoolExecutor(max_workers=4) as pool:
        done = list(pool.map(one, clips))
    return [d[0] for d in done], [d[1] for d in done]


def brief(st: dict) -> str:
    """A one-line reading of stats, for the agent's tool results."""
    return (f"亮度中位 {st['p50']:.2f}（1% {st['p01']:.2f}、99% {st['p99']:.2f}），過曝 {st['clipHigh'] * 100:.1f}%，"
            f"平均色 R{st['mean'][0]:.2f} G{st['mean'][1]:.2f} B{st['mean'][2]:.2f}")


def metered(note: dict) -> str:
    """What exposure was metered on, from a decision note, for the agent's tool results."""
    if note.get("subject") == "person":
        return f"以人物測光：{note.get('faces', 0)} 張臉，臉部亮度 {note['face']:.2f}，目標 {note['target']:.2f}"
    if note.get("subject") != "scene":
        return "未調曝光"
    why = "（找不到 OpenCV，未偵測人物）" if note.get("detector") == "missing" else ""
    return f"以整體畫面測光{why}：目標中位 {note['target']:.2f}"
