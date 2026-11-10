"""Lyrics alignment worker for the "lyrics" job (vc/lyrics.py): times known lyrics to the singing in a song.

Video Console runs this file with Speech-to-subtitle's Python, which has torch with CUDA; it also needs stable-ts
(pip install stable-ts). It imports nothing from vc. Usage: python lyrics_worker.py job.json, where job.json is
{"input_file": 16 kHz mono WAV, "lines": [lyric lines], "language": "zh" | "en" | ..., "model": "medium"}.
It prints one JSON object per line on stdout, as STS's transcribe_worker.py does (vc/stt.py reads them):
{"t": "progress", "percent", "message"}, then {"t": "done", "words": [{"w", "s", "e", "p"}]} with each aligned
word's text, start and end in seconds and its probability, or {"t": "error", "message"}.
"""
import json
import sys
import traceback
from pathlib import Path

# Python puts this file's folder (Video Console's vc package) first on sys.path, where vc/secrets.py would stand in
# for the standard library's secrets that numpy imports: take it off, the worker imports only from STS's Python.
HERE = Path(__file__).resolve().parent
sys.path = [p for p in sys.path if Path(p or ".").resolve() != HERE]


def say(**msg) -> None:
    print(json.dumps(msg), flush=True)


def main(job_path: str) -> int:
    with open(job_path, encoding="utf-8") as f:
        job = json.load(f)
    say(t="progress", percent=2, message="載入歌詞對齊模型")
    try:
        import torch  # noqa: I001 - torch's CUDA DLLs must load before stable-ts brings in whisper
        import stable_whisper
    except ModuleNotFoundError as exc:
        if exc.name not in ("torch", "stable_whisper"):
            raise  # something else is broken: the job shows the error, worker.log the traceback
        say(t="error", message=f"Speech-to-subtitle 的 Python 沒有 {exc.name}，請用它的 Python 執行 "
                               "pip install stable-ts")
        return 1
    if not torch.cuda.is_available():
        say(t="error", message="找不到可用的 NVIDIA GPU（CUDA），歌詞對齊需要 GPU")
        return 1
    model = stable_whisper.load_model(job.get("model") or "medium", device="cuda")
    say(t="progress", percent=30, message="對齊歌詞與歌聲")
    result = model.align(job["input_file"], "\n".join(job["lines"]), language=job.get("language") or "zh",
                         original_split=True)
    words = [{"w": w.word, "s": round(float(w.start), 3), "e": round(float(w.end), 3),
              "p": round(float(getattr(w, "probability", 0) or 0), 3)}
             for seg in result.segments for w in seg.words]
    say(t="done", words=words)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1]))
    except Exception as exc:  # noqa: BLE001 - the job shows the reason; the traceback goes to worker.log
        traceback.print_exc()
        say(t="error", message=f"{type(exc).__name__}: {exc}"[:300])
        sys.exit(1)
