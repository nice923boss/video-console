// Background music ducking (AU-02), mirror of app/vc/duck.py: music on the rows marked 背景音樂 dips while a voice
// plays. Voice is what the user marked: a clip's 人聲 switch, or a whole row. The dip follows the clips' spans,
// not the waveform.
import { audioLength, audioMedia, audioTrack, VOICE_TRACK, SOURCE_TRACK } from "./sequence.js";

export const ATTACK = 0.3; // seconds the music takes to go down, ending as the voice starts
export const RELEASE = 0.8; // seconds it takes to come back once the voice ends
const GAP = ATTACK + RELEASE; // voices closer than this share one dip
export const DEFAULT_DB = 10;
export const DUCK_LEVELS = [6, 8, 10, 12];

// Rows whose every clip is voice; never set: the 配音 rows.
export const speechRows = (tracks) => (tracks || {}).speechRows ?? [VOICE_TRACK];
export const bgmRows = (tracks) => (tracks || {}).bgmRows || [];
export const duckDb = (settings) => (settings || {}).duckDb ?? DEFAULT_DB;

export function mergeSpans(spans) {
  const out = [];
  for (const [s, e] of [...spans].sort((a, b) => a[0] - b[0] || a[1] - b[1])) {
    const last = out[out.length - 1];
    if (last && s - last[1] < GAP) last[1] = Math.max(last[1], e);
    else out.push([s, e]);
  }
  return out;
}

// Where voice plays, merged. source: spans of the picture clips whose own sound is heard (the 原聲 row).
export function voiceSpans(project, source) {
  const tracks = project.tracks || {};
  const bgm = bgmRows(tracks);
  if (!bgm.length) return [];
  const rows = speechRows(tracks);
  const spans = [];
  for (const item of project.audio || []) {
    const key = audioTrack(item);
    if (bgm.includes(key) || !(item.speech || rows.includes(key))) continue;
    const media = audioMedia(project, item);
    if (item.muted || tracks[key === VOICE_TRACK ? "voiceMuted" : "musicMuted"] || !media) continue;
    const start = Number(item.start || 0);
    spans.push([start, start + audioLength(item, media)]);
  }
  if (rows.includes(SOURCE_TRACK) && !tracks.videoMuted) spans.push(...source);
  return mergeSpans(spans);
}

const dip = ([s, e], t) => Math.max(0, Math.min(1, (t - (s - ATTACK)) / ATTACK, (e + RELEASE - t) / RELEASE));

// The music's gain at timeline time t: 1 away from voice, 10^(-db/20) under it.
export function duckGain(spans, t, db) {
  const depth = 1 - 10 ** (-db / 20);
  return 1 - depth * spans.reduce((sum, span) => sum + dip(span, t), 0);
}

// What a sound row is for: "speech" (人聲), "bgm" (背景音樂) or "" (一般). key: a row key (see audioTrack).
export const ROLE_LABELS = { "": "一般", speech: "人聲", bgm: "背景音樂" };

export function rowRole(tracks, key) {
  if (bgmRows(tracks).includes(key)) return "bgm";
  return speechRows(tracks).includes(key) ? "speech" : "";
}

// The project.update op giving row `key` the role; only music tracks may be 背景音樂.
export function roleOp(tracks, key, role) {
  const speech = speechRows(tracks).filter((k) => k !== key);
  const bgm = bgmRows(tracks).filter((k) => k !== key);
  if (role === "speech") speech.push(key);
  if (role === "bgm") bgm.push(key);
  return { op: "project.update", patch: { tracks: { speechRows: speech, bgmRows: bgm } } };
}
