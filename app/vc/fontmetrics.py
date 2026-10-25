"""Glyph advance widths read straight from installed TrueType/OpenType files (standard library only).

Burnt-in captions need them: libass in the bundled ffmpeg only breaks lines at spaces, so a Chinese sentence
longer than the frame stays on one line and runs off both edges, while the preview (the browser) wraps it.
captions.py breaks the lines itself with these widths, the way the browser does, and hands libass the result.
"""
from __future__ import annotations

import bisect
import logging
import os
import re
import struct
import sys
import unicodedata
from functools import lru_cache
from pathlib import Path
from typing import Callable

log = logging.getLogger("vc.fontmetrics")

_FONT_SUFFIX = re.compile(r"\s*\((TrueType|OpenType|All res|VGA res|8514/a res)\)\s*$", re.I)


def registry_fonts() -> list[tuple[str, str]]:
    """(registry value name, font file) for every installed font; [] off Windows."""
    if sys.platform != "win32":
        return []
    import winreg

    out = []
    key_path = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts"
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            with winreg.OpenKey(hive, key_path) as key:
                i = 0
                while True:
                    try:
                        value_name, file_name, _ = winreg.EnumValue(key, i)
                    except OSError:
                        break
                    out.append((value_name, str(file_name)))
                    i += 1
        except OSError as exc:
            log.debug("Font registry key unreadable in hive %s: %s", hive, exc)
    return out


@lru_cache(maxsize=1)
def _font_files() -> dict[str, Path]:
    """Lower-case face name ("microsoft jhenghei bold") to its file."""
    fonts_dir = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    files = {}
    for value_name, file_name in registry_fonts():
        path = Path(file_name)
        if not path.is_absolute():
            path = fonts_dir / path
        for name in _FONT_SUFFIX.sub("", value_name).split(" & "):
            files.setdefault(name.strip().lower(), path)
    return files


class FontMetrics:
    """Advance widths of one face in a .ttf/.otf/.ttc, scaled the way libass scales an ASS font size."""

    def __init__(self, data: bytes, index: int = 0) -> None:
        base = struct.unpack_from(">I", data, 12 + 4 * index)[0] if data[:4] == b"ttcf" else 0
        count = struct.unpack_from(">H", data, base + 4)[0]
        tables = {}
        for i in range(count):
            tag, _, offset, _ = struct.unpack_from(">4sIII", data, base + 12 + 16 * i)
            tables[tag.decode("latin-1")] = offset
        hhea = tables["hhea"]
        if "OS/2" in tables:  # libass (like VSFilter) makes usWinAscent + usWinDescent equal the font size
            ascent, descent = struct.unpack_from(">HH", data, tables["OS/2"] + 74)
        else:
            ascent, neg_descent = struct.unpack_from(">hh", data, hhea + 4)
            descent = -neg_descent
        self.height = max(1, ascent + descent)  # font units per ASS size unit
        self.units_per_em = struct.unpack_from(">H", data, tables["head"] + 18)[0]
        n_metrics = struct.unpack_from(">H", data, hhea + 34)[0]
        self.advances = struct.unpack_from(f">{n_metrics * 2}H", data, tables["hmtx"])[::2]
        self.glyph = _cmap(data, tables["cmap"])

    def advance(self, char: str) -> float:
        """The character's advance in ASS size units (multiply by the font size)."""
        glyph = self.glyph(ord(char))
        if glyph:
            return self.advances[min(glyph, len(self.advances) - 1)] / self.height
        return fallback_advance(char) * self.units_per_em / self.height  # libass draws it from another font


def _cmap(data: bytes, table: int) -> Callable[[int], int]:
    """Code point to glyph id (0: none) from the Unicode cmap subtable (format 12 preferred, else format 4)."""
    count = struct.unpack_from(">H", data, table + 2)[0]
    subtables = {}
    for i in range(count):
        platform, encoding, offset = struct.unpack_from(">HHI", data, table + 4 + 8 * i)
        fmt = struct.unpack_from(">H", data, table + offset)[0]
        if (platform, fmt) in ((3, 12), (0, 12), (3, 4), (0, 4)) and (platform != 3 or encoding in (1, 10)):
            subtables.setdefault(fmt, table + offset)
    if 12 in subtables:
        sub = subtables[12]
        n = struct.unpack_from(">I", data, sub + 12)[0]
        groups = [struct.unpack_from(">III", data, sub + 16 + 12 * g) for g in range(n)]
        starts = [g[0] for g in groups]

        def glyph12(code: int) -> int:
            i = bisect.bisect_right(starts, code) - 1
            return groups[i][2] + code - groups[i][0] if i >= 0 and code <= groups[i][1] else 0

        return glyph12
    glyphs: dict[int, int] = {}
    if 4 in subtables:
        sub = subtables[4]
        segs = struct.unpack_from(">H", data, sub + 6)[0] // 2
        ends = struct.unpack_from(f">{segs}H", data, sub + 14)
        starts = struct.unpack_from(f">{segs}H", data, sub + 16 + 2 * segs)
        deltas = struct.unpack_from(f">{segs}h", data, sub + 16 + 4 * segs)
        range_at = sub + 16 + 6 * segs
        ranges = struct.unpack_from(f">{segs}H", data, range_at)
        for i in range(segs):
            for code in range(starts[i], ends[i] + 1):
                if code == 0xFFFF:
                    continue
                if ranges[i] == 0:
                    glyph = (code + deltas[i]) & 0xFFFF
                else:
                    glyph = struct.unpack_from(">H", data, range_at + 2 * i + ranges[i] + 2 * (code - starts[i]))[0]
                    glyph = (glyph + deltas[i]) & 0xFFFF if glyph else 0
                if glyph:
                    glyphs[code] = glyph
    return lambda code: glyphs.get(code, 0)


def fallback_advance(char: str) -> float:
    """Advance in ems when the font is unknown: CJK and full-width characters are square, others about half."""
    return 1.0 if unicodedata.east_asian_width(char) in ("W", "F") else 0.55


@lru_cache(maxsize=32)
def _metrics(family: str, bold: bool) -> FontMetrics | None:
    files = _font_files()
    path = (files.get(f"{family} bold".lower()) if bold else None) or files.get(family.lower())
    if path is None:
        return None
    try:
        return FontMetrics(path.read_bytes())
    except (OSError, KeyError, struct.error) as exc:
        log.warning("Cannot read font metrics from %s: %s", path, exc)
        return None


def measurer(family: str, bold: bool, size: float, spacing: float = 0.0) -> Callable[[str], float]:
    """A function giving a string's width in ASS pixels at this font size (plus letter spacing per character)."""
    metrics = _metrics(family, bool(bold))
    # Unknown font: sized as libass sizes Microsoft JhengHei, whose em is 0.75 of its ascent + descent.
    advance = metrics.advance if metrics else (lambda ch: fallback_advance(ch) * 0.75)
    cache: dict[str, float] = {}

    def width(text: str) -> float:
        total = 0.0
        for ch in text:
            if ch not in cache:
                cache[ch] = advance(ch) * size + spacing
            total += cache[ch]
        return total

    return width
