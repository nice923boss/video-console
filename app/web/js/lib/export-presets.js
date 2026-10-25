// Export dialog helpers: platform presets (output size and bitrate cap) and the time estimate from past exports.
// YouTube rates follow its upload recommendations for 1080p SDR (8 Mbps up to 30 fps, 12 Mbps above);
// Reels and LINE publish no official bitrate, so theirs are conservative guesses (see GAPS.md).

export const PRESETS = [
  { value: "", label: "不套用（專案尺寸）" },
  { value: "youtube", label: "YouTube 橫式 1920 × 1080" },
  { value: "shorts", label: "YouTube Shorts 直式 1080 × 1920" },
  { value: "reels", label: "Instagram Reels 直式 1080 × 1920" },
  { value: "line", label: "LINE 長邊 1280（保持專案比例）" },
];

const even = (v) => Math.max(16, Math.round(v / 2) * 2);

// { size: [w, h] | null, maxrate: kbps } for the preset; size stays null when it equals the project size.
export function presetOutput(preset, settings) {
  const w = Number(settings.width), h = Number(settings.height), fps = Number(settings.fps) || 30;
  const youtubeRate = fps > 30 ? 12000 : 8000;
  let size = null, maxrate = 0;
  if (preset === "youtube") [size, maxrate] = [[1920, 1080], youtubeRate];
  else if (preset === "shorts") [size, maxrate] = [[1080, 1920], youtubeRate];
  else if (preset === "reels") [size, maxrate] = [[1080, 1920], 8000];
  else if (preset === "line") {
    const scale = 1280 / Math.max(w, h);
    [size, maxrate] = [[even(w * scale), even(h * scale)], 4000];
  }
  if (size && size[0] === w && size[1] === h) size = null;
  return { size, maxrate };
}

// Output sizes of the project's own shape without a preset (EX-12): the short side at 1080 or 2160 pixels.
// The server lays the whole edit out at such a size, so upscaled pictures keep their detail.
export const RESOLUTIONS = [
  { value: "", label: "專案尺寸" },
  { value: "1080", label: "1080p" },
  { value: "2160", label: "4K" },
];

// [w, h] for the resolution, or null when it is the project size.
export function resolutionSize(key, settings) {
  const w = Number(settings.width), h = Number(settings.height);
  const k = Number(key) / Math.min(w, h);
  if (!k) return null;
  const size = [even(w * k), even(h * k)];
  return size[0] === w && size[1] === h ? null : size;
}

// Whether the preset's frame has another shape than the project (then the fit choice decides bars or crop).
export function aspectDiffers(size, settings) {
  if (!size) return false;
  return Math.abs(size[0] / size[1] - Number(settings.width) / Number(settings.height)) > 0.01;
}

// Seconds an export of `duration` seconds at `pixels` (width × height) would take, from the median speed per
// second and pixel of the last five finished MP4 exports (same encoder when one is picked, same quality
// preferred). { seconds, count }, or null without history.
export function estimateExport(jobs, duration, pixels, encoder, quality) {
  const past = (jobs || [])
    .filter((j) => j.kind === "export" && j.status === "done" && j.result && j.result.export)
    .filter((j) => ["mp4", undefined].includes(((j.payload || {}).options || {}).format))
    .filter((j) => { const x = j.result.export; return x.seconds > 0 && x.duration > 0 && x.width > 0 && x.height > 0; })
    .filter((j) => encoder === "auto" || j.result.export.encoder === encoder)
    .sort((a, b) => (b.finished || 0) - (a.finished || 0));
  const same = past.filter((j) => (((j.payload || {}).options || {}).quality || "high") === quality);
  const recent = (same.length ? same : past).slice(0, 5);
  if (!recent.length) return null;
  const ratios = recent.map((j) => { const x = j.result.export; return x.seconds / x.duration / (x.width * x.height); })
    .sort((a, b) => a - b);
  const mid = ratios.length >> 1;
  const ratio = ratios.length % 2 ? ratios[mid] : (ratios[mid - 1] + ratios[mid]) / 2;
  return { seconds: ratio * duration * pixels, count: recent.length };
}
