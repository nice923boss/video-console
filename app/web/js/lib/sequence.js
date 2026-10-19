// Mirror of app/vc/sequence.py: how shots become clips on the timeline. The preview player and the
// timeline use this so what you see matches what the export renders.

const round4 = (v) => Math.round(v * 10000) / 10000;

export function videoTake(project, shot) {
  const tid = shot.activeVideo;
  const take = tid ? project.takes[tid] : null;
  return take && take.kind === "video" && take.duration ? take : null;
}

export function imageTake(project, shot) {
  const tid = shot.activeImage;
  const take = tid ? project.takes[tid] : null;
  return take && take.kind === "image" ? take : null;
}

// Picture shown for a shot on cards: the active image, else the active video's thumbnail.
export function shotCover(project, shot) {
  const img = imageTake(project, shot);
  if (img) return { take: img, path: img.thumb || img.file };
  const vid = videoTake(project, shot);
  if (vid) return { take: vid, path: vid.thumb };
  return null;
}

export function clipBounds(project, shot) {
  const vid = videoTake(project, shot);
  if (vid) {
    const dur = Number(vid.duration);
    const tIn = Math.min(Math.max(0, Number(shot.trimIn || 0)), Math.max(0, dur - 0.1));
    let tOut = shot.trimOut == null ? dur : Math.min(dur, Number(shot.trimOut));
    if (tOut - tIn < 0.1) tOut = Math.min(dur, tIn + 0.1);
    return [tIn, tOut];
  }
  return [0, Number(shot.duration || 0.1)];
}

export function buildSequence(project, includeAll = false) {
  const shots = project.shots.filter((s) => includeAll || s.inSequence !== false);
  const clips = shots.map((shot) => {
    const vid = videoTake(project, shot);
    const img = vid ? null : imageTake(project, shot);
    const [a, b] = clipBounds(project, shot);
    return {
      shot,
      index: project.shots.indexOf(shot),
      source: vid ? "video" : img ? "image" : "empty",
      take: vid || img,
      in: a,
      out: b,
      length: b - a,
    };
  });
  let start = 0;
  clips.forEach((clip, i) => {
    clip.start = round4(start);
    let trans = { ...(clip.shot.transition || { type: "cut", duration: 0.5 }) };
    if (i === clips.length - 1 || (trans.type || "cut") === "cut") {
      trans = { type: "cut", duration: 0 };
    } else {
      const limit = Math.min(clip.length, clips[i + 1].length) * 0.5;
      trans.duration = round4(Math.max(0.04, Math.min(Number(trans.duration ?? 0.5), limit)));
    }
    clip.transition = trans;
    start += clip.length - trans.duration;
  });
  return clips;
}

export function sequenceDuration(clips) {
  if (!clips.length) return 0;
  const last = clips[clips.length - 1];
  return last.start + last.length;
}

// Index of the clip that owns time t (the later clip wins inside a transition overlap).
export function clipIndexAt(clips, t) {
  let idx = -1;
  for (let i = 0; i < clips.length; i += 1) {
    if (clips[i].start <= t + 1e-6) idx = i;
    else break;
  }
  return idx;
}

// Length of an audio item on the timeline.
export function audioLength(item, media) {
  const full = media && media.duration ? Number(media.duration) : 0;
  const tIn = Number(item.in || 0);
  const tOut = item.out == null ? full : Number(item.out);
  return Math.max(0.1, tOut - tIn);
}

// Mirror of fit_frame in app/vc/ops.py: the largest size that shows the whole picture, centered.
export function fitFrame(media, frameW, frameH) {
  const full = overlayHeight({ w: 1 }, media, frameW, frameH);
  const w = full > 1 ? 1 / full : 1;
  const h = Math.min(1, full);
  return { x: round4((1 - w) / 2), y: round4((1 - h) / 2), w: round4(w) };
}

// Mirror of new_overlay in app/vc/ops.py: a clip on an upper track that fills the frame.
export function newOverlay(id, media, layer, start, frameW, frameH) {
  const video = media.kind === "video" && media.duration;
  return {
    id, mediaId: media.id, layer, name: String(media.name || "").slice(0, 120),
    start: Math.round(Math.max(0, start) * 1000) / 1000, in: 0,
    length: video ? Math.round(Math.max(0.1, Number(media.duration)) * 1000) / 1000 : 5,
    ...fitFrame(media, frameW, frameH), opacity: 1, volume: 1,
  };
}

// Height of an overlay as a fraction of the frame height; its width is overlay.w of the frame width.
export function overlayHeight(overlay, media, frameW, frameH) {
  const aspect = media && media.width && media.height ? media.width / media.height : 16 / 9;
  return (overlay.w * frameW) / frameH / aspect;
}
