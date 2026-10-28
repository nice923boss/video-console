// Imperative half of the preview player. Two picture slots take turns (clip i plays in slot i % 2), so
// the next clip is already loaded when its transition starts. Transitions, captions, the project fade
// and the audio mix follow export.py (ffmpeg xfade, ASS styles, afade lengths), so the preview matches
// the exported file. Picture-in-picture clips sit above the picture and below the captions, bottom track
// first; the track switches (hide, mute) apply here as they do in the export. Driven by the playback
// clock; nothing here re-renders through Preact.
import { fileUrl } from "../lib/format.js";
import { clipIndexAt, audioLength, audioSpeed, audioMedia, audioTrack, VOICE_TRACK, SOURCE_TRACK, sequenceDuration, overlayHeight } from "../lib/sequence.js";
import { valueAt, steppedAt } from "../lib/keyframes.js";
import { resolveStyle, anchorOf, textCss, blockCss, zonePad, stackDragged, shownCaptions } from "../lib/caption-style.js";
import { litCount, fillCaption } from "../lib/karaoke.js";
import { releaseFocus } from "../lib/keys.js";
import { filterMarkup, isNeutral } from "../lib/grade.js";
import { chromaMarkup } from "../lib/chroma.js";
import { maskMarkup } from "../lib/masks.js";
import { wakeMix, routeGain, unroute, setMonitor } from "../lib/audio-mix.js";
import { voiceSpans, duckGain, duckDb, bgmRows } from "../lib/duck.js";
import { release, playSafe, seekTo, voicePlay, trackElements } from "./player-media.js";

const DRIFT_PLAYING = 0.15;
const DRIFT_PAUSED = 0.02;
const EDGE = 0.001;
const IDEO_SPACE = String.fromCharCode(0x3000);
const ZONES = ["top", "middle", "bottom"];
const DRAG_START_PX = 3;
const HOLD_MS = 2000;
const MAX_RATE = 16; // the fastest playbackRate browsers accept
const SLATE_FADE = 0.2; // export.py's ad(200,200) on slate text

const clamp01 = (v) => Math.min(1, Math.max(0, v));
let engineSeq = 0; // keeps SVG filter ids unique when more than one player exists

function el(tag, cls, parent) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (parent) parent.appendChild(node);
  return node;
}

function slateText(clip) {
  const n = clip.index + 1;
  const name = (clip.shot.name || "").trim();
  return name ? `#${n}${IDEO_SPACE}${name}` : `鏡頭 ${n}`;
}

function makeSlot(host, rerender) {
  const root = el("div", "pl-slot", host);
  const video = el("video", "pl-video", root);
  video.preload = "auto";
  video.playsInline = true;
  video.disablePictureInPicture = true;
  const img = el("img", "pl-img", root);
  img.alt = "";
  img.draggable = false;
  const slate = el("div", "pl-slate", root);
  const title = el("div", "pl-slate-title", slate);
  const sub = el("div", "pl-slate-sub", slate);
  const masks = el("div", "pl-masks", root);
  const slot = { root, video, img, title, sub, masks, key: "", clip: null, mode: "", pendingSeek: null, filterCss: "",
    maskKey: "" };

  const setMode = (mode) => {
    slot.mode = mode;
    root.className = `pl-slot is-${mode}`;
  };
  const fail = (message) => {
    if (!slot.clip) return;
    title.textContent = slateText(slot.clip);
    sub.textContent = message;
    setMode("error");
  };
  video.addEventListener("error", () => video.hasAttribute("src") && fail("無法播放這個影片檔"));
  img.addEventListener("error", () => img.hasAttribute("src") && fail("無法載入這張圖片"));
  video.addEventListener("seeked", () => {
    if (slot.pendingSeek != null) seekTo(video, slot, slot.pendingSeek);
    else rerender();
  });
  video.addEventListener("loadedmetadata", rerender);

  slot.load = (clip, pid, scale) => {
    slot.clip = clip;
    const take = clip ? clip.take : null;
    const off = clip && clip.shot.disabled;
    const file = take ? playFile(take, scale) : "";
    const key = clip ? `${off ? "off" : clip.source}|${file}|${slateText(clip)}` : "";
    if (key === slot.key) return;
    slot.key = key;
    slot.pendingSeek = null;
    if (!clip || off) {
      // A disabled shot keeps its place as black, as in the export.
      release(video);
      img.removeAttribute("src");
      setMode(off ? "off" : "none");
    } else if (clip.source === "video") {
      img.removeAttribute("src");
      video.src = fileUrl(pid, file);
      setMode("video");
    } else if (clip.source === "image") {
      release(video);
      img.src = fileUrl(pid, take.file);
      setMode("image");
    } else {
      release(video);
      img.removeAttribute("src");
      title.textContent = slateText(clip);
      sub.textContent = "尚未產生畫面";
      setMode("slate");
    }
  };
  return slot;
}

// The smaller preview copy at this scale (PV-06, lowres.py) once it is made; the original until then.
function playFile(item, scale) {
  return (scale > 1 && item[`preview${scale}`]) || item.file;
}

function resetLook(slot, z) {
  const s = slot.root.style;
  s.visibility = "visible";
  s.zIndex = String(z);
  s.opacity = "";
  s.transform = "";
  s.clipPath = "";
}

function hide(slot) {
  slot.root.style.visibility = "hidden";
}

// hooks.onCaptionMove(caption, {x, y}): a caption was dragged to a new anchor (frame fractions); it returns
// the ops promise. hooks.captionDragLabel(caption): the text shown on the caption while it is dragged.
// hooks.dragDisabled(): true while captions cannot be dragged (watch mode); a press is then a click on the stage.
export function createEngine(host, hooks = {}) {
  // Adjustment layers grade the composed picture (clips, flashes, picture-in-picture) as export.py does
  // with lut3d, so captions and the project fade stay outside the graded box.
  const graded = el("div", "pl-graded", host);
  const layers = el("div", "pl-layers", graded);
  let slots = [];
  const flash = el("div", "pl-flash", graded);
  const pip = el("div", "pl-pip", graded);
  const defs = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  defs.setAttribute("class", "pl-grade-defs");
  defs.setAttribute("aria-hidden", "true");
  host.appendChild(defs);
  const filterPrefix = `vc-grade-${++engineSeq}`;
  const filters = new Map(); // adjustment id, "shot:" + shot id or "chroma:" + overlay id -> {key, fid, node}
  let filterSeq = 0;
  let filterCss = "";
  let capsFilterCss = "";
  let gradeDraft = null; // {id, grade} while a grade control (adjustment layer or shot) is being dragged
  let maskDraft = null; // {id, masks} while a shot's mask control is being dragged
  let compare = false; // grades filter only the right half of the picture (GR-06)
  const caps = el("div", "pl-caps", host);
  const zones = Object.fromEntries(ZONES.map((z) => [z, el("div", `pl-cap-zone pos-${z}`, caps)]));
  const fade = el("div", "pl-fade", host);
  const tracks = new Map();
  const pips = new Map();
  let project = null;
  let clips = [];
  let total = 0;
  let level = 1;
  let muted = false;
  let solo = []; // row keys heard alone; empty: every row
  let stems = {}; // audio clip id -> its 降噪 / 去回音 stems (lib/voice-stems.js)
  let monitor = null; // {id, mode}: that clip plays its source ("dry") or only what the cleanup takes out ("delta")
  let mixOn = false; // the Web Audio mix runs, so stems can be remixed
  let voice = []; // merged voice spans the 背景音樂 rows dip under (lib/duck.js)
  let scale = 1; // preview resolution: 1, 2 (half) or 4 (quarter)
  let capKey = "";
  let legacy = 52;
  let shiftCache = { project: null, height: 0, shifts: new Map() }; // dragShifts() for this project and size
  let drag = null;
  let held = false; // a dropped caption stays put until the moved project arrives
  let last = { t: 0, playing: false, rate: 1 };

  const rerender = () => render(last.t, last.playing, last.rate);
  slots = [makeSlot(layers, rerender), makeSlot(layers, rerender)];

  function projectFade(t) {
    const fi = Math.min(10, Math.max(0, Number(project.settings.fadeIn) || 0));
    const fo = Math.min(10, Math.max(0, Number(project.settings.fadeOut) || 0));
    let g = 1;
    if (fi > 0 && t < fi) g = Math.min(g, t / fi);
    if (fo > 0 && t > total - fo) g = Math.min(g, (total - t) / fo);
    return clamp01(g);
  }

  const clipSounds = (clip) => clip && clip.source === "video" && clip.take.hasAudio && !clip.shot.disabled && !clip.shot.audioDetached
    && Number(clip.shot.volume ?? 1) > 0;

  // Original sound of a video clip at the shot's volume, with the same short fades export.py puts at each cut.
  function clipGain(i, t) {
    const clip = clips[i];
    if (!clipSounds(clip)) return 0;
    const local = t - clip.start;
    const len = clip.length;
    const fi = Math.max(0.03, Math.min(i > 0 ? clips[i - 1].transition.duration : 0, len / 2));
    const fo = Math.max(0.03, Math.min(clip.transition.duration, len / 2));
    return clamp01(Math.min(local / fi, (len - local) / fo)) * Number(clip.shot.volume ?? 1);
  }

  // Sound goes through the Web Audio mix once it runs (it plays gains above 1 and feeds the meters);
  // before that, and where Web Audio is missing, through the element's own volume, capped at 1.
  // track: the timeline row the sound belongs to (its own meter, and silent when another row is soloed).
  function setGain(media, gain, track, voice = null) {
    if (solo.length && !solo.includes(track)) gain = 0;
    if (routeGain(media, gain, track, voice)) {
      media.volume = 1;
      media.muted = false;
    } else {
      media.volume = clamp01(gain * level);
      media.muted = muted || gain <= 0;
    }
  }

  function syncVideo(slot, t, playing, gain) {
    const { clip, video } = slot;
    if (slot.mode !== "video") return;
    const target = clip.in + Math.min(Math.max(0, t - clip.start), Math.max(0, clip.length - EDGE)) * clip.speed;
    setGain(video, gain, SOURCE_TRACK);
    if (playing) {
      const speed = Math.min(MAX_RATE, last.rate * clip.speed);
      if (video.playbackRate !== speed) video.playbackRate = speed;
      if (Math.abs(video.currentTime - target) > DRIFT_PLAYING * Math.max(1, speed)) seekTo(video, slot, target);
      if (video.paused) playSafe(video);
    } else {
      if (!video.paused) video.pause();
      if (Math.abs(video.currentTime - target) > DRIFT_PAUSED) seekTo(video, slot, target);
    }
  }

  function park(slot, clip) {
    slot.load(clip, project.id, scale);
    hide(slot);
    if (clip) syncVideo(slot, clip.start, false, 0);
  }

  // Draw the outgoing clip A and incoming clip B the way ffmpeg xfade does at progress p.
  function drawTransition(type, a, b, p) {
    resetLook(a, 1);
    resetLook(b, 2);
    if (type === "fade_black" || type === "fade_white") {
      flash.className = `pl-flash is-${type === "fade_white" ? "white" : "black"}`;
      flash.style.opacity = String(1 - Math.abs(2 * p - 1));
      hide(p < 0.5 ? b : a);
    } else if (type === "wipe_left") {
      b.root.style.clipPath = `inset(0 0 0 ${((1 - p) * 100).toFixed(3)}%)`;
    } else if (type === "slide_left") {
      a.root.style.transform = `translateX(${(-p * 100).toFixed(3)}%)`;
      b.root.style.transform = `translateX(${((1 - p) * 100).toFixed(3)}%)`;
    } else {
      b.root.style.opacity = String(p);
    }
  }

  function drawPicture(t, playing, g) {
    flash.style.opacity = "0";
    const ci = clipIndexAt(clips, t);
    if (ci < 0) {
      slots.forEach((s) => park(s, null));
      return;
    }
    const cur = clips[ci];
    const prev = ci > 0 ? clips[ci - 1] : null;
    const curSlot = slots[ci % 2];
    const other = slots[(ci + 1) % 2];
    curSlot.load(cur, project.id, scale);
    const overlap = prev && prev.transition.duration > 0 && t < prev.start + prev.length - 1e-6;
    if (overlap) {
      other.load(prev, project.id, scale);
      const p = clamp01((t - cur.start) / prev.transition.duration);
      drawTransition(prev.transition.type, other, curSlot, p);
      syncVideo(other, t, playing, clipGain(ci - 1, t) * g);
    } else {
      resetLook(curSlot, 2);
      park(other, clips[ci + 1] || null);
    }
    syncVideo(curSlot, t, playing, clipGain(ci, t) * g);
    slateFade(curSlot, ci, t);
    if (overlap) slateFade(other, ci - 1, t);
    slots.forEach(shadeSlot);
    slots.forEach(maskSlot);
  }

  // A shot's own grade (GR-01) on the picture of the slot showing it. It filters the video or picture element
  // alone, so letterbox bars stay black, as export.py grades the clip's source before fitting it to the frame.
  function shadeSlot(slot) {
    const shot = slot.clip && slot.clip.shot;
    let css = "";
    if (shot && (slot.mode === "video" || slot.mode === "image")) {
      const grade = gradeDraft && gradeDraft.id === shot.id ? gradeDraft.grade : shot.grade;
      if (grade && !isNeutral(grade)) css = `url(#${filterFor(`shot:${shot.id}`, grade)})`;
    }
    if (css === slot.filterCss) return;
    slot.filterCss = css;
    slot.video.style.filter = css;
    slot.img.style.filter = css;
  }

  // A shot's blur and mosaic boxes (GR-12) over the slot showing it, in the frame as export.py cuts them after
  // fitting; backdrop-filter works on what the slot paints under them, the graded picture.
  function maskSlot(slot) {
    const shot = slot.clip && slot.clip.shot;
    const shown = shot && (slot.mode === "video" || slot.mode === "image");
    const list = !shown ? [] : maskDraft && maskDraft.id === shot.id ? maskDraft.masks : shot.masks || [];
    const { width, height } = project.settings;
    const px = list.length ? slot.root.clientHeight / Number(height) : 0; // CSS pixels per frame pixel
    const key = list.length ? `${px.toFixed(4)}|${JSON.stringify(list)}` : "";
    if (key === slot.maskKey) return;
    slot.maskKey = key;
    slot.masks.innerHTML = maskMarkup(list, Number(width), Number(height), px, () => `${filterPrefix}-${++filterSeq}`);
  }

  // Slate text as export.py burns it: off the frames a transition shares with a neighbour, fading at both ends.
  function slateFade(slot, i, t) {
    const clip = clips[i];
    let start = clip.start + (i > 0 ? clips[i - 1].transition.duration : 0);
    let end = clip.start + clip.length - clip.transition.duration;
    if (end - start < 0.3) [start, end] = [clip.start, clip.start + clip.length];
    // A playback error keeps its message up; export.py has no such slate.
    const a = slot.mode === "slate" ? String(clamp01(Math.min((t - start) / SLATE_FADE, (end - t) / SLATE_FADE))) : "";
    slot.title.style.opacity = a;
    slot.sub.style.opacity = a;
  }

  // How far each dragged caption moves so captions shown together do not cover each other (CP-08), measured
  // the way the export lays them out: each one's widest line by its lines one size apart around its anchor, grown
  // by its box padding. Worked out for every caption the export burns in, since one that is not showing now can
  // still push one that is; kept until the project or the preview's height changes.
  function dragShifts() {
    const height = caps.clientHeight;
    if (!height) return new Map(); // captions hidden
    if (shiftCache.project === project && shiftCache.height === height) return shiftCache.shifts;
    const k = 1080 / height;
    const frame = caps.getBoundingClientRect();
    const probes = [];
    shownCaptions(project).forEach((c) => {
      if (!(c.text || "").trim() || !(c.end > c.start) || c.disabled) return;
      const s = resolveStyle(project, c, legacy);
      const point = anchorOf(s, c);
      if (!point) return;
      const node = el("div", "pl-cap", caps);
      node.style.cssText = `${blockCss(s, point)};visibility:hidden`;
      const text = el("span", "pl-cap-text", node);
      text.style.cssText = textCss(s);
      text.textContent = c.text.trim();
      probes.push({ c, s, point, node, text });
    });
    const boxes = probes.map(({ c, s, point, node, text }) => {
      const r = node.getBoundingClientRect();
      const lines = Math.max(1, text.getClientRects().length);
      node.remove();
      const pad = s.box ? s.boxPadding : 0;
      const y = point.y * 1080;
      const h = lines * s.size;
      return { key: c.id, start: c.start, end: c.end, left: (r.left - frame.left) * k - pad,
        right: (r.right - frame.left) * k + pad, top: y - h / 2 - pad, bottom: y + h / 2 + pad };
    });
    shiftCache = { project, height, shifts: stackDragged(boxes) };
    return shiftCache.shifts;
  }

  // Each caption in its style: in its top/middle/bottom row, or pinned where it was dragged (lib/caption-style.js),
  // moved off any dragged caption shown before it.
  function drawCaptions(t) {
    if (drag || held) return;
    const active = shownCaptions(project).filter((c) => c.start <= t && t < c.end && (c.text || "").trim() && !c.disabled);
    const shifts = dragShifts();
    const looks = active.map((c) => {
      const s = resolveStyle(project, c, legacy);
      const at = anchorOf(s, c);
      const point = at && { x: at.x, y: at.y + (shifts.get(c.id) || 0) / 1080 };
      return { c, s, point, row: ZONES.includes(c.position) ? c.position : "bottom", lit: s.highlight ? litCount(project, c, t) : null };
    });
    const key = JSON.stringify(looks.map((k) => [k.c.id, k.row, k.c.text, k.s, k.point, k.lit]));
    if (key === capKey) return;
    capKey = key;
    ZONES.forEach((z) => zones[z].replaceChildren());
    caps.querySelectorAll(":scope > .pl-cap").forEach((n) => n.remove());
    for (const look of looks) {
      const node = el("div", "pl-cap", look.point ? caps : zones[look.row]);
      node.style.cssText = blockCss(look.s, look.point);
      const text = el("span", "pl-cap-text", node);
      text.style.cssText = textCss(look.s);
      fillCaption(text, look.c.text.trim(), look.lit, look.s); // lit: CP-13 karaoke
      node.addEventListener("pointerdown", (e) => startDrag(e, node, look));
    }
    ZONES.forEach((z) => {
      zones[z].style.cssText = zonePad(z, looks.filter((k) => !k.point && k.row === z).map((k) => k.s));
    });
  }

  // Dragging a caption: it leaves its row, follows the pointer and is dropped at a new anchor. A press
  // without movement stays a click on the stage (play/pause).
  function startDrag(e, node, look) {
    if (e.button !== 0 || !hooks.onCaptionMove || drag || (hooks.dragDisabled && hooks.dragDisabled())) return;
    e.stopPropagation();
    e.preventDefault();
    releaseFocus();
    const box = caps.getBoundingClientRect();
    const r = node.getBoundingClientRect();
    const edge = { left: r.left, right: r.right }[look.s.align] ?? (r.left + r.right) / 2;
    drag = {
      node, look, box, id: e.pointerId, x0: e.clientX, y0: e.clientY, moved: false, point: null,
      ax: (edge - box.left) / box.width, ay: ((r.top + r.bottom) / 2 - box.top) / box.height,
    };
    window.addEventListener("pointermove", onDragMove);
    window.addEventListener("pointerup", onDragEnd);
    window.addEventListener("pointercancel", onDragEnd);
  }

  function onDragMove(e) {
    const d = drag;
    if (!d || e.pointerId !== d.id) return;
    const dx = e.clientX - d.x0;
    const dy = e.clientY - d.y0;
    if (!d.moved && Math.hypot(dx, dy) < DRAG_START_PX) return;
    if (!d.moved) {
      d.moved = true;
      caps.appendChild(d.node);
      d.node.classList.add("is-dragging");
      d.node.dataset.scope = hooks.captionDragLabel ? hooks.captionDragLabel(d.look.c) : "";
    }
    d.point = { x: clamp01(d.ax + dx / d.box.width), y: clamp01(d.ay + dy / d.box.height) };
    d.node.style.cssText = blockCss(d.look.s, d.point);
    d.node.classList.toggle("label-below", d.point.y < 0.15);
  }

  function onDragEnd(e) {
    const d = drag;
    if (!d || e.pointerId !== d.id) return;
    drag = null;
    window.removeEventListener("pointermove", onDragMove);
    window.removeEventListener("pointerup", onDragEnd);
    window.removeEventListener("pointercancel", onDragEnd);
    if (!d.moved) return;
    swallowClick();
    d.node.classList.remove("is-dragging", "label-below");
    capKey = "";
    if (e.type !== "pointerup" || !d.point) {
      rerender();
      return;
    }
    held = true;
    const unhold = () => {
      if (!held) return;
      held = false;
      capKey = "";
      rerender();
    };
    // Normally setProject ends the hold; this covers a failed or unchanged move.
    Promise.resolve(hooks.onCaptionMove(d.look.c, d.point)).then(
      (res) => (res && res.changed ? setTimeout(unhold, HOLD_MS) : unhold()),
      unhold,
    );
  }

  // The click that follows a drag must not reach the stage, which would toggle playback.
  function swallowClick() {
    const stop = (ev) => ev.stopPropagation();
    window.addEventListener("click", stop, { capture: true, once: true });
    setTimeout(() => window.removeEventListener("click", stop, { capture: true }), 0);
  }

  function syncTracks() {
    const seen = new Set();
    for (const item of project.audio || []) {
      const media = audioMedia(project, item);
      if (!media) continue;
      seen.add(item.id);
      let tr = tracks.get(item.id);
      if (!tr) {
        const audio = new Audio();
        audio.preload = "auto";
        tr = { audio, src: "", pendingSeek: null };
        tracks.set(item.id, tr);
      }
      const src = fileUrl(project.id, media.file);
      if (tr.src !== src) {
        tr.src = src;
        tr.audio.src = src;
      }
      tr.item = item;
      tr.media = media;
    }
    for (const [id, tr] of tracks) {
      if (!seen.has(id)) {
        for (const audio of trackElements(tr)) {
          release(audio);
          unroute(audio);
        }
        tracks.delete(id);
      }
    }
  }

  // One element per picture-in-picture clip, stacked by track; a clip whose file changed gets a new one.
  function syncOverlays() {
    const seen = new Set();
    (project.layers || []).forEach((layer, z) => {
      for (const item of project.overlays || []) {
        const media = (project.media || {})[item.mediaId];
        if (item.layer !== layer.id || !media || !["image", "video"].includes(media.kind) || item.disabled) continue;
        seen.add(item.id);
        const src = fileUrl(project.id, playFile(media, scale));
        let ov = pips.get(item.id);
        if (ov && ov.src !== src) {
          if (ov.video) release(ov.node);
          unroute(ov.node);
          ov.node.remove();
          ov = null;
        }
        if (!ov) {
          const video = media.kind === "video";
          const node = el(video ? "video" : "img", "pl-pip-item", pip);
          ov = { node, src, video, pendingSeek: null };
          if (video) {
            node.preload = "auto";
            node.playsInline = true;
            node.disablePictureInPicture = true;
            const holder = ov;
            node.addEventListener("seeked", () => (holder.pendingSeek != null ? seekTo(node, holder, holder.pendingSeek) : rerender()));
            node.addEventListener("loadedmetadata", rerender);
          } else {
            node.alt = "";
            node.draggable = false;
          }
          node.src = src;
          pips.set(item.id, ov);
        }
        ov.item = item;
        ov.layer = layer;
        ov.media = media;
        ov.node.style.zIndex = String(z + 1);
        const chroma = item.chroma && item.chroma.on ? item.chroma : null;
        ov.node.style.filter = chroma
          ? `url(#${cachedFilter(`chroma:${item.id}`, JSON.stringify(chroma), (fid) => chromaMarkup(fid, chroma))})` : "";
        place(ov, 0);
      }
    });
    for (const [id, ov] of pips) {
      if (seen.has(id)) continue;
      if (ov.video) release(ov.node);
      unroute(ov.node);
      ov.node.remove();
      pips.delete(id);
    }
  }

  // Position, size and opacity of a picture-in-picture clip local seconds into it (keyframes move them).
  function place(ov, local) {
    const { item, media, node } = ov;
    const { width, height } = project.settings;
    const pct = (v) => `${(Number(v) * 100).toFixed(3)}%`;
    const w = Number(valueAt(item, "w", local, 0.35));
    const st = node.style;
    st.left = pct(valueAt(item, "x", local, 0));
    st.top = pct(valueAt(item, "y", local, 0));
    st.width = pct(w);
    st.height = pct(overlayHeight({ w }, media, width, height));
    st.opacity = String(clamp01(Number(valueAt(item, "opacity", local, 1))));
  }

  // An SVG filter kept under id, rebuilt from markup(filter id) under a new filter id whenever key changes.
  function cachedFilter(id, key, markup) {
    let f = filters.get(id);
    if (f && f.key === key) return f.fid;
    if (f) f.node.remove();
    const fid = `${filterPrefix}-${++filterSeq}`;
    defs.insertAdjacentHTML("beforeend", markup(fid));
    f = { key, fid, node: defs.lastElementChild };
    filters.set(id, f);
    return fid;
  }

  // The SVG filter of one adjustment's or shot's grade.
  function filterFor(id, grade) {
    return cachedFilter(id, `${compare}${JSON.stringify(grade)}`, (fid) => filterMarkup(fid, grade, compare));
  }

  function dropUnusedFilters() {
    const ids = new Set((project.adjustments || []).map((a) => a.id));
    for (const s of project.shots || []) if (s.grade) ids.add(`shot:${s.id}`);
    for (const o of project.overlays || []) if (o.chroma && o.chroma.on) ids.add(`chroma:${o.id}`);
    for (const [id, f] of filters) {
      if (ids.has(id)) continue;
      f.node.remove();
      filters.delete(id);
    }
  }

  // Every adjustment layer over t, earliest start first (the order export.py chains its LUTs in).
  function drawGrade(t) {
    const refs = [];
    if (!(project.tracks || {}).adjustHidden) {
      const active = (project.adjustments || [])
        .filter((a) => !a.hidden && t >= Number(a.start || 0) && t < Number(a.start || 0) + Number(a.length || 0))
        .sort((a, b) => Number(a.start || 0) - Number(b.start || 0));
      const fps = Math.max(1, Number(project.settings.fps) || 24);
      for (const a of active) {
        const start = Number(a.start || 0);
        const grade = gradeDraft && gradeDraft.id === a.id
          ? gradeDraft.grade
          : steppedAt(a, "grade", t - start, Math.min(Number(a.length || 0), total - start), fps);
        if (!isNeutral(grade)) refs.push(`url(#${filterFor(a.id, grade)})`);
      }
    }
    const css = refs.join(" ");
    const capsCss = project.settings.gradeCaptions ? css : "";
    if (css !== filterCss) {
      filterCss = css;
      graded.style.filter = css;
    }
    if (capsCss !== capsFilterCss) {
      capsFilterCss = capsCss;
      caps.style.filter = capsCss;
    }
  }

  // A hidden track keeps its sound and a muted one its picture, as in the export.
  function drawOverlays(t, playing, g) {
    for (const ov of pips.values()) {
      const { item, layer, node } = ov;
      const local = t - Number(item.start || 0);
      const on = local >= 0 && local < Number(item.length || 0) && t < total;
      node.style.display = on && !layer.hidden ? "block" : "";
      if (on && item.keyframes && item.keyframes.length) place(ov, local);
      if (!ov.video) continue;
      if (!on || !playing) {
        if (!node.paused) node.pause();
        if (!on) continue;
      }
      const target = Number(item.in || 0) + local;
      setGain(node, layer.muted ? 0 : Number(valueAt(item, "volume", local, 1)) * g, layer.id);
      if (playing) {
        const speed = Math.min(MAX_RATE, last.rate);
        if (node.playbackRate !== speed) node.playbackRate = speed;
        if (Math.abs(node.currentTime - target) > DRIFT_PLAYING * speed) seekTo(node, ov, target);
        if (node.paused) playSafe(node);
      } else if (Math.abs(node.currentTime - target) > DRIFT_PAUSED) {
        seekTo(node, ov, target);
      }
    }
  }

  function drawAudio(t, playing, g) {
    const flags = project.tracks || {};
    const bgm = bgmRows(flags);
    const db = duckDb(project.settings);
    for (const tr of tracks.values()) {
      const { item, media, audio } = tr;
      const start = Number(item.start || 0);
      const len = Math.min(audioLength(item, media), total - start);
      const local = t - start;
      const track = audioTrack(item);
      const off = item.muted || flags[track === VOICE_TRACK ? "voiceMuted" : "musicMuted"] || (solo.length && !solo.includes(track));
      if (!playing || off || len <= 0.05 || local < 0 || local >= len) {
        for (const el of trackElements(tr)) if (!el.paused) el.pause();
        continue;
      }
      const fi = Math.min(Number(item.fadeIn || 0), len / 2);
      const fo = Math.min(Number(item.fadeOut || 0), len / 2);
      let gain = Number(valueAt(item, "volume", local, 1)) * g;
      if (fi > 0 && local < fi) gain *= local / fi;
      if (fo > 0 && local > len - fo) gain *= (len - local) / fo;
      if (bgm.includes(track)) gain *= duckGain(voice, t, db);
      const sp = audioSpeed(item);
      const speed = Math.min(MAX_RATE, last.rate * sp);
      const target = Number(item.in || 0) + local * sp;
      const mode = monitor && monitor.id === item.id ? monitor.mode : "fx";
      const { el, holder, at, voice: fx } = voicePlay(tr, target, project.id, mixOn && stems[item.id], mode);
      setGain(el, gain, track, fx);
      if (el.playbackRate !== speed) el.playbackRate = speed;
      if (Math.abs(el.currentTime - at) > DRIFT_PLAYING * Math.max(1, speed)) seekTo(el, holder, at);
      if (el.paused) playSafe(el);
    }
  }

  // rate: the clock's playback speed (J/L pressed again plays faster).
  function render(t, playing, rate = 1) {
    last = { t, playing, rate: Math.max(1, Math.abs(rate)) };
    if (!project) return;
    if (playing) mixOn = wakeMix();
    const g = projectFade(t);
    const flags = project.tracks || {};
    drawPicture(t, playing, flags.videoMuted ? 0 : g);
    layers.style.opacity = flags.videoHidden ? "0" : "";
    drawOverlays(t, playing, g);
    drawGrade(t);
    drawCaptions(t);
    drawAudio(t, playing, g);
    fade.style.opacity = String(1 - g);
  }

  return {
    render,
    // nextLegacy: the old export caption size, still the size of the built-in caption look.
    setProject(next, nextClips, nextLegacy = 52) {
      project = next;
      clips = nextClips;
      legacy = nextLegacy;
      total = sequenceDuration(clips);
      voice = voiceSpans(project, clips.filter(clipSounds).map((c) => [c.start, c.start + c.length]));
      capKey = "";
      held = false;
      syncTracks();
      syncOverlays();
      dropUnusedFilters();
      rerender();
    },
    // draft: {id, grade} shown in place of that adjustment's or shot's saved grade while it is being edited;
    // null ends it.
    setGradeDraft(draft) {
      gradeDraft = draft;
      rerender();
    },
    // draft: {id, masks} shown in place of that shot's saved masks while one is being edited; null ends it.
    setMaskDraft(draft) {
      maskDraft = draft;
      rerender();
    },
    // on: every grade (shots and adjustment layers) shows on the right half only, the left half ungraded.
    setCompare(on) {
      compare = !!on;
      rerender();
    },
    setSolo(keys) {
      solo = keys || [];
      rerender();
    },
    // next: the store's voiceStems; mode: its voiceMonitor.
    setVoice(next, mode) {
      stems = next || {};
      monitor = mode;
      rerender();
    },
    setPreviewScale(n) {
      scale = n;
      if (project) syncOverlays();
      rerender();
    },
    setVolume(nextLevel, nextMuted) {
      level = clamp01(Number(nextLevel));
      muted = !!nextMuted;
      setMonitor(level, muted);
      rerender();
    },
    // True while a visible video is still seeking or buffering; the player holds the clock meanwhile.
    // Near the end of a file there is no "future data" to buffer, so that is not counted as waiting.
    isWaiting() {
      return slots.some((s) => {
        const v = s.video;
        if (s.mode !== "video" || s.root.style.visibility === "hidden") return false;
        if (v.seeking) return true;
        return v.readyState < 3 && !v.ended && !(v.duration - v.currentTime < 0.25);
      });
    },
    destroy() {
      if (drag) onDragEnd({ pointerId: drag.id, type: "pointercancel" });
      const drop = (media) => {
        release(media);
        unroute(media);
      };
      slots.forEach((s) => drop(s.video));
      tracks.forEach((tr) => trackElements(tr).forEach(drop));
      tracks.clear();
      pips.forEach((ov) => ov.video && drop(ov.node));
      pips.clear();
      host.replaceChildren();
    },
  };
}
