"""Speech alignment, the "align" job: time one caption row's captions to what is said on the timeline.

The sequence's sound, mixed as the export mixes it, goes to Speech-to-subtitle once as 16 kHz mono WAV and
comes back as words with their times. The captions' text and the heard words are compared as one stream of
tokens (each Chinese character, each English word or number), and a caption that enough of its tokens were
heard for runs from the first to the last of them. The rest are spread over the gaps they fall in, by their
own length, and switched off until someone checks them. A caption linked to an aligned one by `pair`
(bilingual import: the English under its Chinese) takes the same times.
"""
from __future__ import annotations

import difflib
import json
import re
import shutil
import unicodedata
from pathlib import Path
from typing import Callable

from .export import export_options, plan_audio_export, prepare_clips
from .jobs import JobControl
from .media import Media
from .sequence import build_sequence
from .store import ProjectStore
from .stt import LANE, SttClient, parse_srt
from .util import AppError

MIN_HEARD = 0.3  # share of a caption's tokens that must be heard for it to take the heard times
MIN_SHOW = 0.3  # seconds an aligned caption stays up at least
HOLD = 0.5  # a gap shorter than this to the next caption is closed, so the text does not flicker off and on
MIN_SPREAD = 0.2  # seconds each unheard caption gets when the gap it falls in is too short
_TOKEN = re.compile(r"[㐀-䶿一-鿿豈-﫿]|[a-z0-9]+")


def tokens(text: str) -> list[str]:
    """Chinese characters one by one and English words or numbers, ignoring case, width and punctuation."""
    return _TOKEN.findall(unicodedata.normalize("NFKC", text or "").lower())


def heard_tokens(items: list[tuple[str, float, float]]) -> list[tuple[str, float, float]]:
    """(token, start, end) from heard (text, start, end) pieces: words, or whole subtitles when STS gave no
    words. A piece of several tokens has its time shared out evenly."""
    out = []
    for text, start, end in items:
        toks = tokens(text)
        step = (end - start) / len(toks) if toks else 0.0
        out += [(tok, start + k * step, start + (k + 1) * step) for k, tok in enumerate(toks)]
    return out


def match(captions: list[dict], heard: list[tuple[str, float, float]]) -> dict[str, tuple[float, float] | None]:
    """{caption id: (start, end)} of the heard tokens each caption's tokens matched; None when too few did.

    Lone matched tokens are left out when the caption has longer runs (a stray 的 far from the line would
    stretch it), and count only for captions of three tokens or fewer."""
    seq: list[str] = []
    owner: list[int] = []
    for i, cap in enumerate(captions):
        toks = tokens(cap.get("text") or "")
        seq += toks
        owner += [i] * len(toks)
    first: dict[int, int] = {}  # caption index -> index in seq of its first token
    hits: dict[int, list[tuple[int, int, int]]] = {}  # caption index -> [(seq index, heard index, block size)]
    matcher = difflib.SequenceMatcher(None, seq, [t for t, _, _ in heard], autojunk=False)
    for a, b, size in matcher.get_matching_blocks():
        for k in range(size):
            hits.setdefault(owner[a + k], []).append((a + k, b + k, size))
    counts = [0] * len(captions)
    for n, i in enumerate(owner):
        counts[i] += 1
        first.setdefault(i, n)
    out: dict[str, tuple[float, float] | None] = {}
    for i, cap in enumerate(captions):
        got = hits.get(i, [])
        runs = [h for h in got if h[2] >= 2] or (got if counts[i] <= 3 else [])
        if not counts[i] or len(runs) < max(1.0, MIN_HEARD * counts[i]):
            out[cap["id"]] = None
            continue
        (a0, b0, _), (a1, b1, _) = runs[0], runs[-1]
        start, end = heard[b0][1], heard[b1][2]
        # tokens of the caption before the first heard one and after the last were said too, only misheard
        per = (end - start) / (a1 - a0 + 1)
        out[cap["id"]] = (max(0.0, start - (a0 - first[i]) * per), end + (first[i] + counts[i] - 1 - a1) * per)
    return out


def place(captions: list[dict], times: dict[str, tuple[float, float] | None],
          end: float | None = None) -> list[tuple[str, float, float, bool]]:
    """(id, start, end, heard) for captions in timeline order: heard ones at their heard times, the rest
    in the gap between the heard ones around them, each as long as it is now (shrunk to fit a short gap,
    or to end the trailing ones at end, the timeline's end)."""
    out: list[tuple[str, float, float, bool]] = []
    i = 0
    while i < len(captions):
        cap = captions[i]
        if times.get(cap["id"]):
            start, stop = times[cap["id"]]
            start = max(start, out[-1][2] if out else 0.0)
            out.append((cap["id"], round(start, 3), round(max(stop, start + MIN_SHOW), 3), True))
            i += 1
            continue
        j = i
        while j < len(captions) and not times.get(captions[j]["id"]):
            j += 1
        run = captions[i:j]
        weights = [max(0.1, float(c["end"]) - float(c["start"])) for c in run]
        total = sum(weights)
        lo = out[-1][2] if out else 0.0
        if j < len(captions):
            hi = max(times[captions[j]["id"]][0], lo + MIN_SPREAD * len(run))
            if not out:
                lo = max(0.0, hi - total)  # captions before the first heard one end where the speech starts
        else:
            hi = lo + total if end is None or lo + total <= end else max(end, lo + MIN_SPREAD * len(run))
        scale = min(1.0, (hi - lo) / total)
        t = lo + ((hi - lo) - total * scale) / 2  # a run shorter than its gap sits in the middle of it
        for c, w in zip(run, weights):
            out.append((c["id"], round(t, 3), round(t + w * scale, 3), False))
            t += w * scale
        i = j
    # hold each heard caption up to the next one across a short pause
    for k in range(len(out) - 1):
        cid, start, end, heard = out[k]
        if heard and 0 < out[k + 1][1] - end < HOLD:
            out[k] = (cid, start, out[k + 1][1], heard)
    return out


def align_ops(project: dict, captions: list[dict], heard: list[tuple[str, float, float]],
              end: float | None = None) -> tuple[list[dict], dict]:
    """caption.update ops timing captions (and the captions paired with them) to the heard tokens, and
    {"heard", "unheard"} counts. Heard captions are switched on, unheard ones off; a voice-over bound to a
    caption that moves moves with it, as in linked edits."""
    placed = place(captions, match(captions, heard), end)
    partners: dict[str, list[str]] = {}
    for cap in project["captions"]:
        if cap.get("pair"):
            partners.setdefault(cap["pair"], []).append(cap["id"])
            partners.setdefault(cap["id"], []).append(cap["pair"])
    by_id = {c["id"]: c for c in project["captions"]}
    ops, done = [], set()
    for cid, start, end, heard_ in placed:
        for target in (cid, *partners.get(cid, [])):
            cap = by_id.get(target)
            if cap is None or target in done:
                continue
            done.add(target)
            patch = {"start": start, "end": end, "disabled": not heard_}
            if any(cap.get(k, False if k == "disabled" else None) != v for k, v in patch.items()):
                ops.append({"op": "caption.update", "id": target, "patch": patch})
            shift = start - float(cap["start"])
            if abs(shift) > 0.0005:
                ops += [{"op": "audio.update", "id": a["id"],
                         "patch": {"start": round(max(0.0, float(a.get("start") or 0.0) + shift), 3)}}
                        for a in project.get("audio") or [] if a.get("captionId") == target and not a.get("fixed")]
    heard_count = sum(1 for p in placed if p[3])
    return ops, {"heard": heard_count, "unheard": len(placed) - heard_count}


def row_captions(project: dict, track: str) -> list[dict]:
    """The captions of one caption row ("main" or a captionTracks id) that have words, in timeline order."""
    rows = {t["id"] for t in project.get("captionTracks") or []}
    if track != "main" and track not in rows:
        raise AppError("字幕軌道不存在", 404)
    want = None if track == "main" else track
    caps = [c for c in project["captions"] if (c.get("track") if c.get("track") in rows else None) == want
            and not c.get("title") and tokens(c.get("text") or "")]
    return sorted(caps, key=lambda c: float(c["start"]))


def mix_wav(media: Media, project: dict, project_dir: Path, out: Path,
            on_progress: Callable[[float], None], cancelled: Callable[[], bool]) -> float:
    """The sequence's sound as heard in the export (without its fades), as 16 kHz mono WAV at out; returns
    its length in seconds."""
    clips = prepare_clips(project, project_dir)
    plan = plan_audio_export(project, project_dir, clips, export_options(project, {}, {"fadeIn": 0, "fadeOut": 0}))
    graph = out.with_suffix(".graph.txt")
    graph.write_text(plan["graph"], encoding="utf-8")
    args = [arg for inp in plan["inputs"] for arg in inp]
    args += media.filter_script_args(graph.relative_to(project_dir))
    args += ["-map", "[aout]", "-t", f"{plan['total']:.3f}", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(out)]
    media.run_progress(args, plan["total"], on_progress, cancelled, cwd=project_dir)
    return float(plan["total"])


def read_heard(done: dict, srt: Path) -> list[tuple[str, float, float]]:
    """(text, start, end) of each word STS heard, or of each subtitle when it wrote no words file."""
    words = Path(done["words"]) if done.get("words") else None
    if words and words.is_file():
        data = json.loads(words.read_text(encoding="utf-8-sig"))
        return [(str(w.get("word") or ""), float(w["start"]), float(w["end"])) for w in data
                if isinstance(w, dict) and isinstance(w.get("start"), (int, float)) and isinstance(w.get("end"), (int, float))]
    return [(text, start, end) for start, end, text in parse_srt(Path(done.get("path") or srt).read_text(encoding="utf-8-sig"))]


class AlignJobs:
    """The "align" job: payload {"track": "main" | a captionTracks id}."""

    def __init__(self, store: ProjectStore, client: SttClient, media: Callable[[], Media]):
        self._store = store
        self._client = client
        self._media = media

    def prepare(self, project: dict, payload: dict) -> dict:
        track = str(payload.get("track") or "main")
        if not row_captions(project, track):
            raise AppError("這條字幕軌沒有可以對齊的字幕")
        if not build_sequence(project):
            raise AppError("時間軸上沒有片段，沒有聲音可以辨識")
        self._client.python(self._client.folder())  # fail now, not after the queue wait
        name = next((t["name"] for t in project.get("captionTracks") or [] if t["id"] == track), "字幕")
        return {"title": f"語音對齊字幕：{name}", "lane": LANE, "shotId": None, "payload": {"track": track}}

    def execute(self, ctl: JobControl) -> dict:
        track = ctl.payload["track"]
        pdir = self._store.project_dir(ctl.pid)
        work = pdir / "media" / "cache" / f"job-{ctl.id}"
        work.mkdir(parents=True, exist_ok=True)
        try:
            ctl.progress(0.01, "混合時間軸的聲音")
            wav, srt = work / "mix.wav", work / "mix.srt"
            total = mix_wav(self._media(), self._store.get(ctl.pid), pdir, wav,
                    lambda f: ctl.progress(0.01 + 0.09 * f, "混合時間軸的聲音"), ctl.cancelled)
            ctl.check()
            job_file = work / "job.json"
            job_file.write_text(json.dumps({"input_file": str(wav), "output_srt": str(srt), "language": None},
                                           ensure_ascii=False), encoding="utf-8")
            done = self._client.run(job_file, ctl, lambda f, m: ctl.progress(0.1 + 0.85 * min(max(f, 0.0), 1.0), m))
            heard = heard_tokens(read_heard(done, srt))
        finally:
            shutil.rmtree(work, ignore_errors=True)
        if not heard:
            return {"message": "時間軸的聲音裡沒有辨識到語音，字幕沒有變動"}
        project = self._store.get(ctl.pid)  # again: the captions may have been edited while the job ran
        ops, counts = align_ops(project, row_captions(project, track), heard, total)
        if ops:
            self._store.apply(ctl.pid, ops, origin="job", undoable=True, label="語音對齊字幕")
        missed = f"，{counts['unheard']} 句對不上語音，已排在空檔並先停用，請檢查" if counts["unheard"] else ""
        return {"message": f"已對齊 {counts['heard']} 句字幕{missed}", **counts}
