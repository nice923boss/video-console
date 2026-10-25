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

// Picture shown for a shot on cards: the active video's thumbnail (what the sequence plays and the
// card's hover preview shows), else the active image.
export function shotCover(project, shot) {
  const vid = videoTake(project, shot);
  if (vid && vid.thumb) return { take: vid, path: vid.thumb };
  const img = imageTake(project, shot);
  if (img) return { take: img, path: img.thumb || img.file };
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

// Mirror of clip_speed: playback speed of a shot's video; stills always play at 1.
export const clipSpeed = (shot, source) => (source === "video" ? Math.min(4, Math.max(0.25, Number(shot.speed || 1))) : 1);

// Mirror of audio_media: a library item, or the take a shot's sound was detached from.
export const audioMedia = (project, item) =>
  (project.media || {})[item.mediaId] || (item.takeId ? (project.takes || {})[item.takeId] : null) || null;

// Voice lines (a caption's clip, or a clip dragged onto the voice rows) share the 配音 rows; every other clip sits
// on its music track: its own, unless `track` names one it shares. A track lives only while a clip is on it.
export const VOICE_TRACK = "voice";
// The video clips' own sound (the 原聲 row) as a row key for the preview's meters and solo.
export const SOURCE_TRACK = "source";
export const audioTrack = (item) => (item.captionId || item.track === VOICE_TRACK ? VOICE_TRACK : item.track || item.id);

export function buildSequence(project, includeAll = false) {
  const shots = project.shots.filter((s) => includeAll || s.inSequence !== false);
  const clips = shots.map((shot) => {
    const vid = videoTake(project, shot);
    const img = vid ? null : imageTake(project, shot);
    const [a, b] = clipBounds(project, shot);
    const source = vid ? "video" : img ? "image" : "empty";
    const speed = clipSpeed(shot, source);
    return {
      shot,
      index: project.shots.indexOf(shot),
      source,
      take: vid || img,
      in: a,
      out: b,
      speed,
      length: (b - a) / speed,
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

// The captions bound to a shot in time order (text only; their timing stays each caption's own). A long shot can
// carry several lines (CP-06).
export function shotCaptions(project, shotId) {
  return (project.captions || []).filter((c) => c.shot === shotId).sort((a, b) => a.start - b.start);
}

// The cast members with a look that a shot's text names (SB-11); their looks go into its picture prompt.
export function castInText(project, text) {
  return (project.cast || []).filter((m) => {
    const name = (m.name || "").trim();
    return name && (m.look || "").trim() && (text || "").includes(name);
  });
}

// The look lines a prompt carries for that cast (runners.py _prompts, sequence.cast_looks).
export function castLooks(project, text) {
  return castInText(project, text).map((m) => `${m.name.trim()}：${m.look.trim()}`).join("\n");
}

// Length of an audio item on the timeline (a sped item, the detached sound of a sped shot, plays its source faster).
export function audioLength(item, media) {
  const full = media && media.duration ? Number(media.duration) : 0;
  const tIn = Number(item.in || 0);
  const tOut = item.out == null ? full : Number(item.out);
  return Math.max(0.1, (tOut - tIn) / audioSpeed(item));
}

// Source seconds per timeline second of an audio item.
export const audioSpeed = (item) => Number(item.speed || 1);

// One {key, items} per music track, in the order of each track's first clip (the server keeps audio sorted by start).
// `order` (project.tracks.musicOrder) is the order the user set; tracks it leaves out go below.
export function musicTracks(audio, order) {
  const music = [];
  for (const a of audio) {
    const key = audioTrack(a);
    if (key === VOICE_TRACK) continue;
    const found = music.find((m) => m.key === key);
    if (found) found.items.push(a);
    else music.push({ key, items: [a] });
  }
  const saved = order || [];
  const rank = (m) => (saved.includes(m.key) ? saved.indexOf(m.key) : saved.length);
  return music.map((m, i) => [m, i]).sort(([a, i], [b, j]) => rank(a) - rank(b) || i - j).map(([m]) => m);
}

// 合併音軌: each music track moves onto the first track above it that plays nothing while it does, so sounds that
// never overlap share a row and only ones that play together keep rows of their own. Tracks only join one with the
// same 用途 (lib/duck.js), so sound effects never land on a row that dips under voice. Returns the audio.update ops
// and how many music tracks are left.
export function mergeTracks(project) {
  const spans = (items) => items.map((a) => {
    const start = Number(a.start || 0);
    return [start, start + audioLength(a, audioMedia(project, a))];
  });
  const clash = (a, b) => a.some(([s, e]) => b.some(([bs, be]) => s < be - 1e-3 && bs < e - 1e-3));
  const tracks = project.tracks || {};
  const role = (key) => ((tracks.bgmRows || []).includes(key) ? "bgm" : (tracks.speechRows || []).includes(key) ? "speech" : "");
  const groups = [];
  const ops = [];
  for (const { key, items } of musicTracks(project.audio || [], tracks.musicOrder)) {
    const mine = spans(items);
    const into = groups.find((g) => g.role === role(key) && !clash(mine, g.spans));
    if (!into) {
      groups.push({ key, role: role(key), spans: mine });
      continue;
    }
    into.spans.push(...mine);
    for (const a of items) ops.push({ op: "audio.update", id: a.id, patch: { track: into.key } });
  }
  return { ops, tracks: groups.length };
}

// Mirror of fit_frame in app/vc/opfields.py: the largest size that shows the whole picture, centered.
export function fitFrame(media, frameW, frameH) {
  const full = overlayHeight({ w: 1 }, media, frameW, frameH);
  const w = full > 1 ? 1 / full : 1;
  const h = Math.min(1, full);
  return { x: round4((1 - w) / 2), y: round4((1 - h) / 2), w: round4(w) };
}

// The smallest size that covers the whole frame, centered; what lies past the frame is cropped.
export function fillFrame(media, frameW, frameH) {
  const full = overlayHeight({ w: 1 }, media, frameW, frameH);
  const w = full < 1 ? Math.min(4, 1 / full) : 1;
  const h = full * w;
  return { x: round4((1 - w) / 2), y: round4(Math.max(-4, (1 - h) / 2)), w: round4(w) };
}

// Mirror of new_overlay in app/vc/opfields.py: a clip on an upper track that fills the frame.
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
