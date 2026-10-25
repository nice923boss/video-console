// YouTube chapters from the timeline markers, for pasting into the video description.
// YouTube only shows chapters when the list starts at 0:00, has at least 3 timestamps and every chapter
// lasts 10 seconds or more (YouTube Help, "Video chapters"); the copy still goes through, with a warning.
import { buildSequence, sequenceDuration } from "./sequence.js";
import { toast } from "../components/toast.js";

function stamp(sec, long) {
  const s = Math.floor(sec);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), r = String(s % 60).padStart(2, "0");
  return long ? `${h}:${String(m).padStart(2, "0")}:${r}` : `${h * 60 + m}:${r}`;
}

// { text, warnings } for markers [{ start, name }] over a video of `duration` seconds.
export function youtubeChapters(markers, duration) {
  const rows = (markers || [])
    .filter((m) => m.start < duration)
    .map((m) => ({ t: Math.floor(m.start), name: (m.name || "").trim() }))
    .sort((a, b) => a.t - b.t);
  if (!rows.length || rows[0].t > 0) rows.unshift({ t: 0, name: "開頭" });
  const long = duration >= 3600;
  const text = rows.map((r, i) => `${stamp(r.t, long)} ${r.name || `章節 ${i + 1}`}`).join("\n");
  const warnings = [];
  if (rows.length < 3) warnings.push("YouTube 至少要 3 個章節才會顯示");
  const short = rows.filter((r, i) => (i + 1 < rows.length ? rows[i + 1].t : duration) - r.t < 10).length;
  if (short) warnings.push(`${short} 個章節短於 10 秒，YouTube 不會顯示章節`);
  return { text, warnings };
}

export function copyChapters(project) {
  if (!(project.markers || []).length) {
    toast("時間軸還沒有標記，請先在章節開頭加標記", { level: "warn" });
    return;
  }
  const { text, warnings } = youtubeChapters(project.markers, sequenceDuration(buildSequence(project)));
  navigator.clipboard.writeText(text).then(
    () => toast(warnings.length ? `已複製章節。注意：${warnings.join("；")}` : "已複製 YouTube 章節，貼到影片說明即可",
      { level: warnings.length ? "warn" : undefined }),
    () => toast("無法存取剪貼簿", { level: "warn" }),
  );
}
