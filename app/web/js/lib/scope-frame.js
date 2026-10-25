// The preview picture for the scopes (GR-07): draws the engine's graded box (components/player-engine.js) into a
// small canvas the way the browser composes it (clips with their grades, transition flashes, picture-in-picture),
// then the adjustment layers over the lot. Captions, the project fade and blur or mosaic masks are left out.

// Where el sits inside the graded box, in canvas pixels (transforms included, as in the slide transition).
function boxOf(el, origin, k) {
  const r = el.getBoundingClientRect();
  return { x: (r.left - origin.left) * k, y: (r.top - origin.top) * k, w: r.width * k, h: r.height * k };
}

// el's opacity as painted: its own times every parent's below stop (a dissolve fades the slot, a muted track the layers).
function opacityOf(el, stop) {
  let a = 1;
  for (let n = el; n && n !== stop; n = n.parentElement) a *= Number(getComputedStyle(n).opacity);
  return a;
}

const shown = (el) => el.getClientRects().length > 0 && getComputedStyle(el).visibility !== "hidden";

const ready = (media) => (media.tagName === "VIDEO" ? media.readyState >= 2 && media.videoWidth > 0
  : media.complete && media.naturalWidth > 0);

// The rectangle media fills inside its box under object-fit contain or cover.
function fitted(media, box) {
  const mw = media.videoWidth || media.naturalWidth;
  const mh = media.videoHeight || media.naturalHeight;
  const s = (getComputedStyle(media).objectFit === "cover" ? Math.max : Math.min)(box.w / mw, box.h / mh);
  return { x: box.x + (box.w - mw * s) / 2, y: box.y + (box.h - mh * s) / 2, w: mw * s, h: mh * s };
}

// A box cut by its clip-path inset() (the wipe transition).
function inset(el, box) {
  const m = /^inset\(([^)]*)\)/.exec(el.style.clipPath || "");
  if (!m) return box;
  const v = m[1].trim().split(/\s+/).map(parseFloat);
  const [t, r, b, l] = [v[0], v[1] ?? v[0], v[2] ?? v[0], v[3] ?? v[1] ?? v[0]];
  return { x: box.x + (box.w * l) / 100, y: box.y + (box.h * t) / 100, w: box.w * (1 - (l + r) / 100), h: box.h * (1 - (t + b) / 100) };
}

function drawMedia(ctx, media, clip, origin, k, stop) {
  const alpha = opacityOf(media, stop);
  if (!shown(media) || !ready(media) || alpha <= 0) return;
  const r = fitted(media, boxOf(media, origin, k));
  ctx.save();
  ctx.beginPath();
  ctx.rect(clip.x, clip.y, clip.w, clip.h);
  ctx.clip();
  ctx.globalAlpha = alpha;
  ctx.filter = media.style.filter || "none";
  ctx.drawImage(media, r.x, r.y, r.w, r.h);
  ctx.restore();
}

const byZ = (a, b) => (Number(a.style.zIndex) || 0) - (Number(b.style.zIndex) || 0);

function compose(graded, canvas, width) {
  const origin = graded.getBoundingClientRect();
  if (!origin.width || !origin.height) return false;
  const k = width / origin.width;
  const h = Math.max(1, Math.round(origin.height * k));
  if (canvas.width !== width || canvas.height !== h) {
    canvas.width = width;
    canvas.height = h;
  }
  const ctx = canvas.getContext("2d", { willReadFrequently: true });
  ctx.fillStyle = "#000";
  ctx.fillRect(0, 0, width, h);
  for (const slot of [...graded.querySelectorAll(".pl-layers > .pl-slot")].sort(byZ)) {
    if (!shown(slot)) continue;
    const clip = inset(slot, boxOf(slot, origin, k));
    for (const media of slot.querySelectorAll(":scope > .pl-video, :scope > .pl-img")) drawMedia(ctx, media, clip, origin, k, graded);
  }
  const flash = graded.querySelector(".pl-flash");
  const fa = flash ? Number(flash.style.opacity) || 0 : 0;
  if (fa > 0) {
    ctx.globalAlpha = fa;
    ctx.fillStyle = getComputedStyle(flash).backgroundColor;
    ctx.fillRect(0, 0, width, h);
    ctx.globalAlpha = 1;
  }
  for (const item of [...graded.querySelectorAll(".pl-pip-item")].sort(byZ)) {
    drawMedia(ctx, item, boxOf(item, origin, k), origin, k, graded);
  }
  return true;
}

// The graded picture `width` pixels wide as ImageData, or null when the preview has no size. raw and out are
// scratch canvases the caller keeps between calls.
export function grabFrame(graded, raw, out, width) {
  if (!compose(graded, raw, width)) return null;
  const css = graded.style.filter;
  if (!css) return raw.getContext("2d", { willReadFrequently: true }).getImageData(0, 0, raw.width, raw.height);
  if (out.width !== raw.width || out.height !== raw.height) {
    out.width = raw.width;
    out.height = raw.height;
  }
  const ctx = out.getContext("2d", { willReadFrequently: true });
  ctx.filter = css;
  ctx.drawImage(raw, 0, 0);
  ctx.filter = "none";
  return ctx.getImageData(0, 0, out.width, out.height);
}
