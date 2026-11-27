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

// A box cut by its clip-path inset() (the wipe transition, an overlay's crop).
function inset(el, box) {
  const m = /^inset\(([^)]*)\)/.exec(el.style.clipPath || "");
  if (!m) return box;
  const v = m[1].trim().split(/\s+/).map(parseFloat);
  const [t, r, b, l] = [v[0], v[1] ?? v[0], v[2] ?? v[0], v[3] ?? v[1] ?? v[0]];
  return { x: box.x + (box.w * l) / 100, y: box.y + (box.h * t) / 100, w: box.w * (1 - (l + r) / 100), h: box.h * (1 - (t + b) / 100) };
}

// An overlay's box before its turn, from its left, top, width and height (percent of its track); the bounding
// rectangle of a turned one is bigger.
function pipBox(el, origin, k) {
  const p = boxOf(el.parentElement, origin, k);
  const f = (v) => (parseFloat(v) || 0) / 100;
  const st = el.style;
  return { x: p.x + p.w * f(st.left), y: p.y + p.h * f(st.top), w: p.w * f(st.width), h: p.h * f(st.height) };
}

// Turns ctx as an overlay's CSS rotate turns it, about its transform-origin (percent of its box).
function turn(ctx, el, box) {
  const m = /rotate\(([-\d.e]+)deg\)/.exec(el.style.transform || "");
  if (!m) return;
  const [ox, oy] = (el.style.transformOrigin || "50% 50%").split(/\s+/).map(parseFloat);
  const cx = box.x + (box.w * ox) / 100;
  const cy = box.y + (box.h * oy) / 100;
  ctx.translate(cx, cy);
  ctx.rotate((parseFloat(m[1]) * Math.PI) / 180);
  ctx.translate(-cx, -cy);
}

function drawMedia(ctx, media, clip, origin, k, stop, box = boxOf(media, origin, k), turned = false) {
  const alpha = opacityOf(media, stop);
  if (!shown(media) || !ready(media) || alpha <= 0) return;
  const r = fitted(media, box);
  ctx.save();
  if (turned) turn(ctx, media, box);
  ctx.beginPath();
  ctx.rect(clip.x, clip.y, clip.w, clip.h);
  ctx.clip();
  ctx.globalAlpha = alpha;
  ctx.filter = media.style.filter || "none";
  ctx.drawImage(media, r.x, r.y, r.w, r.h);
  ctx.restore();
}

const byZ = (a, b) => (Number(a.style.zIndex) || 0) - (Number(b.style.zIndex) || 0);

// A keyed shot's backdrop is the slot's CSS background (placeSlot in components/player-engine.js), covering the
// slot; the picture is loaded once here, so the first grab after it changes can miss it.
const backdrops = new Map();
function drawBackdrop(ctx, slot, clip) {
  const m = /url\("?([^")]+)"?\)/.exec(slot.style.backgroundImage || "");
  if (!m) return;
  let img = backdrops.get(m[1]);
  if (!img) {
    img = new Image();
    img.src = m[1];
    backdrops.set(m[1], img);
  }
  if (!img.complete || !img.naturalWidth) return;
  const s = Math.max(clip.w / img.naturalWidth, clip.h / img.naturalHeight);
  const [w, h] = [img.naturalWidth * s, img.naturalHeight * s];
  ctx.save();
  ctx.beginPath();
  ctx.rect(clip.x, clip.y, clip.w, clip.h);
  ctx.clip();
  ctx.globalAlpha = opacityOf(slot, slot.parentElement);
  ctx.drawImage(img, clip.x + (clip.w - w) / 2, clip.y + (clip.h - h) / 2, w, h);
  ctx.restore();
}

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
    drawBackdrop(ctx, slot, clip);
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
    const box = pipBox(item, origin, k);
    drawMedia(ctx, item, inset(item, box), origin, k, graded, box, true); // trimmed by its crop, then turned
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
