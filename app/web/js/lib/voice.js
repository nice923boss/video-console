// AI voice-over: the Audio8Voice (local) and VoAI (cloud) catalogs the cast editor picks from, whether
// a caption's voice-over still matches its line, and submitting voice jobs (one per caption; Audio8Voice
// runs on the ComfyUI lane, VoAI on its own cloud lane).
import { api } from "./api.js";
import { getState, setState } from "./store.js";
import { submitJob, submitBatch } from "./jobs-actions.js";
import { toast, toastError } from "../components/toast.js";

export const VOICE_STATES = {
  queued: { label: "配音排隊中", tone: "info" },
  blank: { label: "沒有文字", tone: "neutral" },
  unassigned: { label: "未指定人物", tone: "neutral" },
  novoice: { label: "人物未設定聲音", tone: "warn" },
  none: { label: "未配音", tone: "neutral" },
  stale: { label: "需要重新生成", tone: "warn" },
  ready: { label: "已配音", tone: "ok" },
};

export const isVoai = (member) => !!member && member.engine === "voai";
const NO_VOAI = { configured: false, online: false, error: "", models: [] };

// Offline is a normal state shown in the cast editor, not an error toast. refresh re-reads the VoAI
// speaker list instead of the server's 10-minute cache.
export async function loadVoices(refresh = false) {
  setState((s) => ({ voice: { ...(s.voice || {}), loading: true } }));
  const voaiReq = api.get(`/api/voice/voai${refresh === true ? "?refresh=1" : ""}`)
    .catch((err) => ({ ...NO_VOAI, error: err.message }));
  const status = await api.get("/api/voice/status").catch((err) => ({ online: false, error: err.message }));
  let voices = [];
  let presets = [];
  let error = status.online ? "" : status.error || "連不到 Audio8Voice";
  if (status.online) {
    try {
      ({ voices, presets } = await api.get("/api/voice/voices"));
    } catch (err) {
      error = err.message;
    }
  }
  setState({ voice: { loading: false, status, voices, presets, error, voai: await voaiReq } });
}

export const castMember = (project, id) => (id ? (project.cast || []).find((m) => m.id === id) || null : null);
export const voiceClip = (project, capId) => (project.audio || []).find((a) => a.captionId === capId) || null;

// Where a caption should end to cover a voice-over `length` seconds long (AU-12, as voice.py voice_end does
// after a voice job): the voice's end, but never past the start of the next caption.
export function voiceEnd(project, cap, length) {
  const later = (project.captions || []).filter((c) => c.id !== cap.id && c.start >= cap.start).map((c) => c.start);
  return Math.round(Math.max(cap.end, Math.min(cap.start + length, ...later)) * 1000) / 1000;
}

const isActive = (j) => j.status === "queued" || j.status === "running";

// A voice's first line also waits for Audio8Voice to transcribe its reference clip (about 30 s, 180 s
// with the GPU busy). Picking a voice queues that on the ComfyUI lane right away instead, in the
// background; the server skips voices Audio8Voice already has ready.
export function prepareVoice(id) {
  const { pid, voice } = getState();
  const known = ((voice && voice.voices) || []).find((v) => v.id === id);
  if (!pid || !id || (known && known.prepared)) return;
  api.post("/api/voice/prepare", { pid, voice: id }).catch((err) => toastError(err, "無法先準備聲音"));
}

export const prepareJob = (jobs, voiceId) => (voiceId
  ? jobs.find((j) => j.kind === "voice" && isActive(j) && j.payload && j.payload.prepare === voiceId) || null
  : null);

// One of VOICE_STATES' keys. A voice-over is stale once the line, the speaker or the speaker's engine,
// voice, VoAI style and model, or speed differ from what it was generated from.
export function voiceState(project, cap, jobs = []) {
  if (jobs.some((j) => j.kind === "voice" && j.pid === project.id && isActive(j) && j.payload && j.payload.captionId === cap.id)) {
    return "queued";
  }
  const text = (cap.text || "").trim();
  if (!text) return "blank";
  const member = castMember(project, cap.speaker);
  if (!member) return "unassigned";
  if (!member.voice || (isVoai(member) && (!member.style || !member.model))) return "novoice";
  const clip = voiceClip(project, cap.id);
  if (!clip) return "none";
  const src = ((project.media || {})[clip.mediaId] || {}).source || {};
  const same = src.text === text && src.castId === member.id && src.voice === member.voice
    && Number(src.speed) === Number(member.speed ?? 1)
    && (src.backend || "audio8voice") === (isVoai(member) ? "voai" : "audio8voice")
    && (src.style || "") === (member.style || "") && (src.model || "") === (member.model || "");
  return same ? "ready" : "stale";
}

export function generateVoices(captionIds) {
  const { pid } = getState();
  if (!pid || !captionIds.length) return null;
  if (captionIds.length === 1) return submitJob("voice", null, { captionId: captionIds[0] });
  return submitBatch(captionIds.map((captionId) => ({ kind: "voice", pid, shotId: null, payload: { captionId } })));
}

// Every caption with a speaker whose voice-over is missing or stale.
export function generateMissing() {
  const { project, jobs } = getState();
  if (!project) return null;
  const captions = project.captions || [];
  const ids = captions.filter((c) => ["none", "stale"].includes(voiceState(project, c, jobs))).map((c) => c.id);
  if (ids.length) return generateVoices(ids);
  const unassigned = captions.filter((c) => voiceState(project, c, jobs) === "unassigned").length;
  toast(unassigned
    ? `沒有可以生成的配音：${unassigned} 句字幕還沒指定人物，請在字幕屬性選擇人物`
    : "沒有需要生成的配音，全部都是最新的", { duration: 3200 });
  return null;
}
