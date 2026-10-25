"""CP-13 spike on real VoAI lines of the "Video Console 介紹（黑貓）" project.

Each L01..L14 line covers 2 to 4 captions. The project's caption starts (made before this spike, method unknown)
are the only reference, so this checks phrase starts only. Also writes a karaoke page for listening checks.

Usage: python cp13_real.py <output folder> [floor]   (the report uses floor 30)
"""
import base64
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from cp13_align import align, frame_db, read_wav  # noqa: E402

PROJECT = Path(__file__).resolve().parents[2] / "data" / "projects" / "p_7ijfziw642"
p = json.loads((PROJECT / "project.json").read_text(encoding="utf-8"))
caps = sorted(p["captions"], key=lambda c: c["start"])
FLOOR = float(sys.argv[2]) if len(sys.argv) > 2 else 40
rows, page, errs, seen = [], [], {"naive": [], "phrase": [], "snap": []}, set()
for clip in sorted(p["audio"], key=lambda a: a["start"]):
    media = p["media"][clip["mediaId"]]
    name = media["name"]
    if not (name.startswith("L") or name.startswith("Mosa")) or media["id"] in seen:
        continue
    seen.add(media["id"])
    start, dur = clip["start"] - float(clip.get("in") or 0), media["duration"]
    if name.startswith("Mosa"):  # a stand-alone take of one caption, named after its first words
        mine = [c for c in caps if c["text"].startswith(name.split("-", 1)[1][:-4])]
    else:
        mine = [c for c in caps if start - 0.3 <= c["start"] < start + dur - 0.3]
    text = "，".join(c["text"].rstrip("！？。") for c in mine) + "。"
    x, sr = read_wav(PROJECT / media["file"])
    db = frame_db(x, sr)
    est = {m: align(text, db, m, floor=FLOOR) for m in errs}
    first, pos = [], 0
    for c in mine:  # index of each caption's first character inside the joined text
        first.append(pos)
        pos += len(c["text"].rstrip("！？。")) + 1
    for m in errs:
        if name.startswith("L"):  # a Mosa take does not sit where its caption starts, so it has no reference
            errs[m] += [est[m][i] - (c["start"] - start) for i, c in zip(first, mine)][1:]
    rows.append((name, len(mine), [round(c["start"] - start, 2) for c in mine],
                 [round(est["snap"][i], 2) for i in first]))
    times = sorted(est["snap"].items())
    page.append({"name": name, "text": text, "times": times,
                 "audio": base64.b64encode((PROJECT / media["file"]).read_bytes()).decode()})

for r in rows:
    print(r)
for m, e in errs.items():
    a = np.abs(np.array(e)) * 1000
    print(f"{m:6s} n={len(a)} median {np.median(a):.0f} ms  p90 {np.percentile(a, 90):.0f} ms  max {a.max():.0f} ms"
          f"  <=150ms {np.mean(a <= 150):.0%}  bias {np.mean(e) * 1000:+.0f} ms")

html = """<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8"><title>CP-13 逐字高亮試聽</title>
<style>body{font:18px/1.6 system-ui,sans-serif;max-width:900px;margin:24px auto;padding:0 16px;background:#111;color:#ddd}
.line{margin:18px 0;padding:12px;border:1px solid #333;border-radius:8px}.t{font-size:28px;letter-spacing:2px}
.t span{color:#666}.t span.on{color:#ffd54a}small{color:#888}</style></head><body>
<h1>CP-13 逐字高亮試聽（估算時間，未用語音模型）</h1>
<p>按播放，聽字有沒有在念到時變黃。黃色比聲音早或晚超過約一個字，就記下是哪一句哪個字。</p>
<div id="list"></div><script>
const DATA = __DATA__;
const list = document.getElementById("list");
for (const d of DATA) {
  const box = document.createElement("div"); box.className = "line";
  const t = document.createElement("div"); t.className = "t";
  const spans = [...d.text].map(ch => { const s = document.createElement("span"); s.textContent = ch; t.append(s); return s; });
  const au = document.createElement("audio"); au.controls = true; au.src = "data:audio/wav;base64," + d.audio;
  const tick = () => { for (const [i, s] of d.times) spans[i].classList.toggle("on", au.currentTime >= s);
    if (!au.paused) requestAnimationFrame(tick); };
  au.addEventListener("play", tick); au.addEventListener("seeked", tick);
  box.innerHTML = "<small>" + d.name + "</small>"; box.append(t, au); list.append(box);
}
</script></body></html>"""
out = Path(sys.argv[1]) / "karaoke.html"
out.write_text(html.replace("__DATA__", json.dumps(page, ensure_ascii=False)), encoding="utf-8")
print("wrote", out, out.stat().st_size // 1024, "KB")
