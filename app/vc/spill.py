"""Keeping ffmpeg's command line under Windows' limit (CreateProcess takes at most 32,767 characters).

A long timeline gives ffmpeg one -i per clip, overlay and sound; hundreds of them no longer fit. The filtergraph
already goes through a script file, which has no length limit, so the inputs a filter can open itself move into
it as source filters (color / anullsrc, movie / amovie), cheapest first, until the rest fits.
"""
from __future__ import annotations

import math
import re
import subprocess
from collections import Counter
from fractions import Fraction
from typing import Callable

LIMIT = 30000  # under 32,767, with room for what the estimate cannot see (a retry's other encoder options)
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tif", ".tiff")
REF = re.compile(r"\[(\d+):([av])\]")


def command_length(args: list[str]) -> int:
    return len(subprocess.list2cmdline(args))


def escape_path(path: str) -> str:
    """A file name as a movie= option inside a filtergraph: quoted for the option parser, then escaped for the
    graph parser, so spaces, quotes, colons, commas and brackets in it survive."""
    quoted = "'" + path.replace("'", "'\\''") + "'"
    return re.sub(r"([\\'\[\],;])", r"\\\1", quoted)


def _options(inp: list[str]) -> dict[str, str] | None:
    if len(inp) % 2 or inp[-2] != "-i":
        return None
    return dict(zip(inp[::2], inp[1::2]))


def _rank(inp: list[str], types: set[str]) -> int | None:
    """In which round an input moves into the graph (lower first), or None when only -i can open it (an alpha
    decoder, other input options)."""
    if not types:
        return 0  # nothing reads it
    opts = _options(inp)
    if opts is None:
        return None
    keys = set(opts)
    if opts.get("-f") == "lavfi" and keys <= {"-f", "-t", "-i"}:
        return 1
    if keys == {"-i"}:
        return 2
    if (keys == {"-loop", "-framerate", "-t", "-i"} and opts["-loop"] == "1" and types == {"v"}
            and opts["-i"].lower().endswith(IMAGE_EXT)):
        return 3
    if keys <= {"-ss", "-t", "-i"}:
        return 4
    return None


def _rotate(degrees: float) -> str:
    """The filter ffmpeg's autorotate puts after a decoder whose stream says "rotation of <degrees>"; movie= does
    not autorotate."""
    turn = round(-degrees) % 360
    return {90: ",transpose=clock", 180: ",hflip,vflip", 270: ",transpose=cclock"}.get(turn, "")


def _tail(label: str, t: str, n: int) -> str:
    """The end of a source chain: its label, or a split into n labels when the graph reads it n times."""
    if n == 1:
        return f"[{label}]"
    return f",{'a' if t == 'a' else ''}split={n}" + "".join(f"[{label}_{i}]" for i in range(n))


def _source(k: int, inp: list[str], uses: dict[str, int], rotation: Callable[[str], float]) -> list[str]:
    """Filtergraph chains standing in for input k, one per stream type the graph reads (uses: type -> times)."""
    opts = _options(inp) or {}
    src = opts["-i"]
    types = sorted(uses)
    if opts.get("-f") == "lavfi":
        sep = ":" if "=" in src else "="
        return [f"{src}{sep}d={opts['-t']}" + _tail(f"spill{k}{types[0]}", types[0], uses[types[0]])]
    if opts.get("-loop") == "1":
        rate = opts["-framerate"]
        frames = max(1, math.ceil(float(opts["-t"]) * Fraction(rate) - 1e-6))
        return [f"movie={escape_path(src)},loop=loop={frames - 1}:size=1:start=0,settb={1 / Fraction(rate)},setpts=N,fps={rate}"
                + _tail(f"spill{k}v", "v", uses["v"])]
    ss, dur = opts.get("-ss"), opts.get("-t")
    seek = f":sp={ss}" if ss and float(ss) > 0 else ""
    cut = ":".join(x for x in (f"start={ss}" if seek else "", f"duration={dur}" if dur else "") if x)
    # movie= ends a video at its last frame's timestamp where -i ends it a frame later, so a later fps= dropped
    # that frame; a one-frame clone moves the end to where -i puts it (a trim past the file's end lets the clone
    # through, the same picture as the frame an overlay's eof_action=repeat holds).
    post = {"v": f"tpad=stop=1:stop_mode=clone,{f'trim={cut},' if cut else ''}setpts=PTS-STARTPTS",
            "a": f"{f'atrim={cut},' if cut else ''}asetpts=PTS-STARTPTS"}
    if "v" in uses:
        post["v"] += _rotate(rotation(src))
    if types == ["a"]:
        return [f"amovie={escape_path(src)}{seek},{post['a']}" + _tail(f"spill{k}a", "a", uses["a"])]
    if types == ["v"]:
        return [f"movie={escape_path(src)}{seek},{post['v']}" + _tail(f"spill{k}v", "v", uses["v"])]
    return [f"movie={escape_path(src)}{seek}:s=dv+da[spill{k}mv][spill{k}ma]",
            f"[spill{k}mv]{post['v']}" + _tail(f"spill{k}v", "v", uses["v"]),
            f"[spill{k}ma]{post['a']}" + _tail(f"spill{k}a", "a", uses["a"])]


def fit_inputs(inputs: list[list[str]], graph: str, fixed: list[str], rotation: Callable[[str], float],
               limit: int | None = None) -> tuple[list[str], str]:
    """The input arguments and filtergraph to run, given the plan's inputs and graph and the rest of the
    command (`fixed`: the ffmpeg path and every argument but the inputs). Returns them unchanged when they fit;
    otherwise moves inputs into the graph until the command is `limit` characters or shorter (or nothing more
    can move: the caller's run then reports the length). rotation(file) is a video's rotation in degrees."""
    flat = [arg for inp in inputs for arg in inp]
    over = command_length(fixed + flat) - (LIMIT if limit is None else limit)
    if over <= 0:
        return flat, graph
    uses: Counter[tuple[int, str]] = Counter((int(k), t) for k, t in REF.findall(graph))
    types = {k: {t for j, t in uses if j == k} for k in range(len(inputs))}
    ranked = sorted((r, k) for k in range(len(inputs)) if (r := _rank(inputs[k], types[k])) is not None)
    moved: set[int] = set()
    for _, k in ranked:
        if over <= 0:
            break
        moved.add(k)
        over -= command_length(inputs[k]) + 1
    index = {k: n for n, k in enumerate(k for k in range(len(inputs)) if k not in moved)}
    sources = [line for k in sorted(moved) if types[k]
               for line in _source(k, inputs[k], {t: uses[(k, t)] for t in types[k]}, rotation)]
    seen: Counter[tuple[int, str]] = Counter()

    def ref(m: re.Match) -> str:
        k, t = int(m[1]), m[2]
        if k in index:
            return f"[{index[k]}:{t}]"
        i = seen[(k, t)]
        seen[(k, t)] += 1
        return f"[spill{k}{t}]" if uses[(k, t)] == 1 else f"[spill{k}{t}_{i}]"

    kept = [arg for k, inp in enumerate(inputs) if k not in moved for arg in inp]
    return kept, ";\n".join([*sources, REF.sub(ref, graph)])
