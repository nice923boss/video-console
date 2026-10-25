"""Caption styles for burnt-in captions: resolve each caption's look, write it as ASS, list the fonts.

Every style becomes three ASS styles drawn on their own layers so libass stacks simultaneous captions
the same way on each: the box (layer 1), the shadow (layer 2) and the text with its outline (layer 3).
Sizes are pixels on a 1080-high frame (PlayResY 1080 with ScaledBorderAndShadow), like the preview.
"""
from __future__ import annotations

import re
from typing import Callable

from . import karaoke
from .fontmetrics import measurer, registry_fonts
from .opfields import DEFAULT_CAPTION_STYLE

PLAY_Y = 1080
# Row (caption "position") and horizontal alignment to the ASS numpad alignment.
_AN = {
    "bottom": {"left": 1, "center": 2, "right": 3},
    "middle": {"left": 4, "center": 5, "right": 6},
    "top": {"left": 7, "center": 8, "right": 9},
}


def resolve_style(project: dict, caption: dict, legacy_size: int = 52) -> dict:
    """The caption's style, else the project default, else the built-in look (size from the old export setting)."""
    styles = {s.get("id"): s for s in project.get("captionStyles") or []}
    style = styles.get(caption.get("style")) or styles.get((project.get("settings") or {}).get("captionStyle"))
    if style is None:
        return {**DEFAULT_CAPTION_STYLE, "id": None, "size": legacy_size}
    merged = {**DEFAULT_CAPTION_STYLE, **style}
    merged["font"] = merged["font"] or DEFAULT_CAPTION_STYLE["font"]
    return merged


def anchor(style: dict, caption: dict) -> tuple[float, float] | None:
    """Where a dragged caption sits (fractions of the frame); None when it follows its row."""
    for item in (caption, style):
        if item.get("x") is not None and item.get("y") is not None:
            return float(item["x"]), float(item["y"])
    return None


def _colour(hex_rgb: str, opacity: float = 1.0) -> str:
    rgb = hex_rgb.lstrip("#")
    alpha = round((1.0 - max(0.0, min(1.0, opacity))) * 255)
    return f"&H{alpha:02X}{(rgb[4:6] + rgb[2:4] + rgb[0:2]).upper()}"


def ass_time(t: float) -> str:
    cs = max(0, int(round(t * 100)))
    h, rem = divmod(cs, 360000)
    m, rem = divmod(rem, 6000)
    s, cs = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def ass_text(text: str) -> str:
    text = text.replace("\\", "＼").replace("{", "｛").replace("}", "｝")
    return text.replace("\r\n", "\n").replace("\n", "\\N")


def _num(v: float) -> str:
    return f"{float(v):.2f}".rstrip("0").rstrip(".")


def _style_lines(key: str, s: dict) -> list[str]:
    """Text, shadow and box ASS styles for one caption style."""
    bold = -1 if s["bold"] else 0
    italic = -1 if s["italic"] else 0
    outline = float(s["outlineWidth"]) if s["outline"] else 0.0
    margins = f"{round(s['marginH'])},{round(s['marginH'])},{round(s['marginV'])},1"

    def line(name: str, primary: str, outline_c: str, back: str, border_style: int, bord: float, shad: float) -> str:
        return (f"Style: {name},{s['font']},{s['size']},{primary},&H00FFFFFF,{outline_c},{back},{bold},{italic},"
                f"0,0,100,100,{_num(s['spacing'])},0,{border_style},{_num(bord)},{_num(shad)},2,{margins}")

    text = _colour(s["color"], s["opacity"])
    lines = [line(f"{key}T", text, _colour(s["outlineColor"]), "&HFF000000", 1, outline, 0)]
    if s["shadow"]:
        shadow = _colour(s["shadowColor"], s["shadowOpacity"])
        # The shadow is cast by text plus outline; without an outline libass needs some border to cast one.
        lines.append(line(f"{key}S", text, shadow, shadow, 1, max(outline, 1.0), s["shadowDistance"]))
    if s["box"]:
        box = _colour(s["boxColor"], s["boxOpacity"])
        lines.append(line(f"{key}B", "&HFF000000", box, box, 3, s["boxPadding"], 0))
    return lines


def _shadow_tags(s: dict) -> str:
    blur = f"\\blur{_num(s['shadowBlur'])}" if s["shadowBlur"] > 0 else ""
    if s["shadowDistance"] > 0:
        # Hide the copy's fill and outline; only its offset shadow shows.
        return f"\\1a&HFF&\\3a&HFF&{blur}"
    # Distance 0: a glow, drawn as the outline in the shadow colour (the text layer covers its inside).
    return f"\\1a&HFF&{blur}"


# Chrome keeps these off the start of a line (closing punctuation) and the end of one (opening brackets).
_NO_START = set("，。、．,.!?！？;；:：」』）)》〉】]}…%％〜～・")
_NO_END = set("「『（(《〈【[{")
# A run of spaces, a word of non-CJK letters (with its punctuation), or one CJK / full-width character.
_TOKEN = re.compile(r"\s+|[^\s⺀-鿿가-힯豈-﫿＀-￯]+|.")


def wrap_text(text: str, limit: float, width: Callable[[str], float]) -> list[str]:
    """The caption's lines as the preview wraps them: its own line breaks, then greedy breaks at limit pixels.

    Chinese breaks between any two characters, other text only at spaces (a word longer than the line stays
    whole), and closing punctuation pulls the character before it down rather than starting a line.
    """
    lines = []
    for para in text.split("\n"):
        line: list[str] = []
        used = 0.0
        for tok in _TOKEN.findall(para):
            if line and not tok.isspace() and used + width(tok) > limit:
                carry = []
                if tok[0] in _NO_START and len(line) > 1 and not line[-1].isspace():
                    carry.append(line.pop())
                while len(line) > 1 and line[-1][-1] in _NO_END:
                    carry.insert(0, line.pop())
                lines.append("".join(line).rstrip())
                line = carry
                used = sum(width(t) for t in line)
            if line or not tok.isspace():
                line.append(tok)
                used += width(tok)
        lines.append("".join(line).rstrip())
    return lines


def wrap_width(style: dict, point: tuple[float, float] | None, play_x: int) -> float:
    """How wide a line may run: the frame less the side margins, or for a dragged caption the room between its
    anchor and the frame edge it grows toward (at least three font sizes), so it never leaves the frame."""
    margin = float(style["marginH"])
    full = play_x - 2 * margin
    if point is None:
        return full
    x = point[0] * play_x
    room = {"left": play_x - x - margin, "right": x - margin}.get(style["align"], 2 * (min(x, play_x - x) - margin))
    return max(min(full, room), 3 * float(style["size"]))


def stack_dragged(boxes: list[dict]) -> dict:
    """How far each dragged caption moves down (frame pixels; negative is up) so two shown at once do not cover
    each other (CP-08). libass leaves \\pos captions where they are, so this does what it does for rows: in order of
    start (list order for a tie), a caption that would cover one placed before it moves away from the nearer frame
    edge, up in the lower half and down in the upper half, until it is clear. The preview runs the same steps.

    boxes: {key, start, end, left, right, top, bottom} in frame pixels.
    """
    placed: list[dict] = []
    shifts: dict = {}
    for b in sorted(boxes, key=lambda b: b["start"]):
        up = b["top"] + b["bottom"] >= PLAY_Y
        dy = 0.0
        while True:
            hit = next((p for p in placed if p["start"] < b["end"] and b["start"] < p["end"]
                        and p["left"] < b["right"] and b["left"] < p["right"]
                        and p["top"] < b["bottom"] + dy and b["top"] + dy < p["bottom"]), None)
            if hit is None:
                break
            # Each step clears the caption it hit for good, so this ends after at most len(placed) steps.
            dy = hit["top"] - b["bottom"] if up else hit["bottom"] - b["top"]
        placed.append({**b, "top": b["top"] + dy, "bottom": b["bottom"] + dy})
        shifts[b["key"]] = dy
    return shifts


def _drag_box(key: int, cap: dict, s: dict, point: tuple[float, float], lines: list[str],
              width: Callable[[str], float], play_x: int) -> dict:
    """A dragged caption's box in frame pixels: its widest line by its lines one size apart around the anchor
    (\\an4/5/6 centre it vertically), grown by the box padding when it has a box."""
    w = max(width(line) for line in lines)
    h = len(lines) * float(s["size"])
    x, y = point[0] * play_x, point[1] * PLAY_Y
    left = {"left": x, "right": x - w}.get(s["align"], x - w / 2)
    pad = float(s["boxPadding"]) if s["box"] else 0.0
    return {"key": key, "start": float(cap["start"]), "end": float(cap["end"]),
            "left": left - pad, "right": left + w + pad, "top": y - h / 2 - pad, "bottom": y + h / 2 + pad}


def caption_ass(project: dict, captions: list[dict], play_x: int, legacy_size: int) -> tuple[list[str], list[str]]:
    """ASS style lines and dialogue lines for the captions to burn in.

    captions: the caption items to draw, already filtered (text present, end after start).
    """
    keys: dict[str | None, str] = {}
    styles: list[str] = []
    events: list[str] = []
    laid = []
    for cap in captions:
        s = resolve_style(project, cap, legacy_size)
        point = anchor(s, cap)
        width = measurer(s["font"], s["bold"], float(s["size"]), float(s["spacing"]))
        lines = wrap_text((cap.get("text") or "").strip().replace("\r\n", "\n"), wrap_width(s, point, play_x), width)
        laid.append((cap, s, point, lines, width))
    shifts = stack_dragged([_drag_box(i, cap, s, point, lines, width, play_x)
                            for i, (cap, s, point, lines, width) in enumerate(laid) if point is not None])
    for i, (cap, s, point, lines, _) in enumerate(laid):
        if s["id"] not in keys:
            keys[s["id"]] = f"C{len(keys) + 1}"
            styles += _style_lines(keys[s["id"]], s)
        key = keys[s["id"]]
        if point is None:
            an = _AN.get(cap.get("position") or "bottom", _AN["bottom"])[s["align"]]
            place = f"\\an{an}"
        else:
            an = _AN["middle"][s["align"]]
            place = f"\\an{an}\\pos({round(point[0] * play_x)},{round(point[1] * PLAY_Y + shifts[i])})"
        times = f"{ass_time(float(cap['start']))},{ass_time(float(cap['end']))}"
        text = ass_text("\n".join(lines))
        if s["box"]:
            events.append(f"Dialogue: 1,{times},{key}B,,0,0,0,,{{{place}}}{text}")
        if s["shadow"]:
            events.append(f"Dialogue: 2,{times},{key}S,,0,0,0,,{{{place}{_shadow_tags(s)}}}{text}")
        if s.get("highlight"):  # CP-13: lit as the voice-over says each character
            lit = _fill_tags(1, s["highlightColor"], s["opacity"]) + _fill_tags(2, s["color"], s["opacity"])
            text = karaoke.karaoke_text(project, cap, lines, lit) or text
        events.append(f"Dialogue: 3,{times},{key}T,,0,0,0,,{{{place}}}{text}")
    return styles, events


def _fill_tags(n: int, hex_rgb: str, opacity: float) -> str:
    r"""\1c\1a (primary) or \2c\2a (secondary) override tags for a fill colour."""
    c = _colour(hex_rgb, opacity)
    return f"\\{n}c&H{c[4:]}&\\{n}a&H{c[2:4]}&"


# ---------------------------------------------------------------- installed fonts
# Chinese names for common CJK fonts; the value is the family name libass and the browser resolve.
FONT_LABELS = {
    "Microsoft JhengHei": "微軟正黑體",
    "PMingLiU": "新細明體",
    "MingLiU": "細明體",
    "DFKai-SB": "標楷體",
    "Microsoft YaHei": "微軟雅黑",
    "KaiTi": "楷體",
    "SimSun": "宋體",
    "NSimSun": "新宋體",
    "SimHei": "黑體",
    "FangSong": "仿宋",
    "DengXian": "等線",
    "Noto Sans TC": "思源黑體 TC",
    "Noto Serif TC": "思源宋體 TC",
    "Noto Sans CJK TC": "思源黑體 CJK TC",
    "Source Han Sans TC": "思源黑體",
    "Source Han Serif TC": "思源宋體",
    "Yu Gothic": "游ゴシック",
    "Malgun Gothic": "맑은 고딕",
}
_FONT_SUFFIX = re.compile(r"\s*\((TrueType|OpenType|All res|VGA res|8514/a res)\)\s*$", re.I)
_WEIGHT = re.compile(
    r"\s+(Regular|Bold|Italic|Oblique|Light|Medium|Black|Thin|Heavy|Semibold|SemiBold|Semilight|SemiLight|"
    r"ExtraLight|ExtraBold|UltraLight|UltraBold|Demibold|DemiLight|Book|Condensed|Narrow)(\s.*)?$", re.I)
_SKIP_FILES = (".fon", ".fnt")


def _family_names(value_name: str, file_name: str) -> list[str]:
    if str(file_name).lower().endswith(_SKIP_FILES):
        return []
    names = []
    for part in _FONT_SUFFIX.sub("", value_name).split(" & "):
        part = _WEIGHT.sub("", part.strip()).strip()
        if part and not part.startswith("@") and "," not in part:
            names.append(part)
    return names


def list_fonts() -> list[dict]:
    """Installed font families, CJK ones with a Chinese name first; [] off Windows."""
    families: set[str] = set()
    for value_name, file_name in registry_fonts():
        families.update(_family_names(value_name, file_name))
    labelled = sorted((f for f in families if f in FONT_LABELS), key=list(FONT_LABELS).index)
    rest = sorted((f for f in families if f not in FONT_LABELS), key=str.lower)
    return [{"name": f, "label": FONT_LABELS.get(f, f)} for f in labelled + rest]
