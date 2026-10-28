// Edit timeline: ruler, then lanes from the top layer down (captions, adjustment layers, picture-in-picture
// tracks, the picture), the picture clips' own sound when any has some, then voice-overs and one lane per music track. A higher lane covers the ones below
// it, in the preview and in the export; an adjustment layer grades every picture lane below it. Zoom (px per second) and snapping are per-viewer preferences;
// the playhead follows the shared playback clock directly, without re-rendering.
import { html, useState, useEffect, useRef, useLayoutEffect } from "../lib/h.js";
import { useStore, setState, getState } from "../lib/store.js";
import { usePref } from "../lib/prefs.js";
import { useKeymap, keyHint } from "../lib/keymap.js";
import { clock, setTime, onClock } from "../lib/clock.js";
import { ops, undo, deleteShots, LINK_PREF } from "../lib/actions.js";
import { setWatch, watchRefuses } from "../lib/watch.js";
import { uploadFiles, importFiles } from "../lib/jobs-actions.js";
import { rippleOps } from "../lib/ripple.js";
import { copyItem, pasteItem } from "./tl-clipboard.js";
import { fmtTime, newId, round, clamp, kindOfName, ACCEPT_AUDIO } from "../lib/format.js";
import { sequenceDuration, audioLength, audioMedia, audioTrack, VOICE_TRACK, SOURCE_TRACK, musicTracks, mergeTracks } from "../lib/sequence.js";
import { generateMissing, generateVoices } from "../lib/voice.js";
import { rowRole, roleOp, duckDb, ROLE_LABELS, DUCK_LEVELS } from "../lib/duck.js";
import { decodeText, TEXT_ENCODINGS, encodingLabel, linesLabel } from "../lib/textfile.js";
import { openCastDialog } from "../dialogs/cast-dialog.js";
import { openTitleDialog } from "../dialogs/title-dialog.js";
import { openBilingualImport } from "../dialogs/bilingual-import.js";
import { captionRows } from "./tl-captions.js";
import { copyChapters } from "../lib/chapters.js";
import { upload } from "../lib/api.js";
import { MEDIA_MIME, placeMedia } from "../lib/media-actions.js";
import { toast, toastError } from "./toast.js";
import { splitAtPlayhead, splitAllAtPlayhead, trimAtPlayhead } from "./tl-edit.js";
import {
  MARKER_COLORS, KEYABLE, toggleEnabled, nudgeVolume, markIn, markOut, rangeFromClip, playRange, clearRange, markerAdd, markerColor,
  markerJump, freezeFrame, detachAudio, reverseClip, focusSpeed, addKeyframe, toggleKeyPanel, notYet,
} from "./tl-commands.js";
import { openMenu, openModal, Modal, promptDialog } from "./overlay.js";
import { Button, IconButton, Field, Select, Segmented, Toggle } from "./ui.js";
import { Icon } from "./icons.js";
import { TrackMeter } from "./audio-meter.js";
import {
  VideoTrack, AudioClip, OverlayClip, SourceAudioTrack, hasSourceAudio, dragPointer, makeSnap, VIDEO_H, multiAudio,
  AUDIO_H, PIP_H, ADD_H,
} from "./tl-tracks.js";
import { AdjustTrack, AdjustAddButton, ADJUST_H } from "./tl-adjust.js";
import { Minimap } from "./tl-minimap.js";
import { useViewWindow, inView } from "./tl-view.js";
import { ToolButtons } from "./tl-roll.js";

const LABEL_W = 176;
const RULER_H = 26;
const PPS_MIN = 10;
const PPS_MAX = 400;
const PPS_DEFAULT = 60;
const ZOOM_STEP = 1.25;
const ZOOM_WHEEL = 1.15;
const STEPS = [0.1, 0.2, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300];
const MINOR = { 0.2: 4, 2: 4, 15: 3, 120: 4 };
const MAJOR_PX = 80;
const MAX_TICKS = 1500;
const FOLLOW_PX = 24;
const NEW_CAPTION_LEN = 2;
const TRACK_SCALES = [0.75, 1, 1.25, 1.5, 2];

const isBool = (v) => typeof v === "boolean";
const validPps = (v) => typeof v === "number" && v >= PPS_MIN && v <= PPS_MAX;
const validScale = (v) => TRACK_SCALES.includes(v);

function addCaption() {
  const start = round(clock.time, 3);
  const id = newId("c");
  const item = { id, start, end: round(start + NEW_CAPTION_LEN, 3), text: "新字幕", position: "bottom" };
  ops([{ op: "caption.add", item }], { label: "新增字幕" }).then((res) => {
    if (res) setState({ editSel: { type: "caption", id } });
  });
}

function deleteSelection(sel) {
  if (!sel) return false;
  if (sel.type === "shot") deleteShots([sel.id]);
  else if (sel.type === "caption") ops([{ op: "caption.delete", id: sel.id }], { label: "刪除字幕" });
  else if (sel.type === "audio") ops([{ op: "audio.delete", id: sel.id }], { label: "移除音軌" });
  else if (sel.type === "overlay") ops([{ op: "overlay.delete", id: sel.id }], { label: "移除片段" });
  else if (sel.type === "adjust") ops([{ op: "adjust.delete", id: sel.id }], { label: "刪除調整圖層" });
  else return false;
  return true;
}

// Shift+Delete: what comes after the clip on its track moves back to close the gap.
function rippleDelete(project, sel) {
  if (!sel) return false;
  if (sel.type === "shot") return deleteShots([sel.id], { ripple: true }), true;
  const list = rippleOps(project, sel);
  if (!list) return false;
  ops(list, { label: "漣漪刪除" });
  return true;
}

const layerName = (layers, layer) => layer.name || `畫面 ${layers.indexOf(layer) + 2}`;
const isVisual = (f) => ["image", "video"].includes(kindOfName(f.name));
const isSrt = (f) => /\.srt$/i.test(f.name);
const fileStem = (name) => name.replace(/\.[a-z0-9]{2,5}$/i, "").slice(0, 120);

const addLayer = () => ops([{ op: "layer.add", layer: { id: newId("l") } }], { label: "新增畫面軌" });

async function removeLayer(layers, layer, count) {
  const res = await ops([{ op: "layer.delete", id: layer.id }], { label: "刪除畫面軌" });
  if (res) toast(`已刪除${layerName(layers, layer)}${count ? `與上面的 ${count} 段` : ""}`, { action: { label: "復原", fn: undo } });
}

// Where a shot dropped at time t goes: before the clip whose middle is past t (an index into project.shots).
function shotIndexAt(project, clips, t) {
  const k = clips.filter((c) => c.start + c.length / 2 < t).length;
  if (k < clips.length) return { index: clips[k].index, at: clips[k].start };
  return { index: clips.length ? clips[clips.length - 1].index + 1 : project.shots.length, at: sequenceDuration(clips) };
}

// Each picture or clip becomes a new shot at `index`; the file is imported straight into it.
async function insertShots(files, index) {
  const shots = files.map((f) => ({ id: newId("s"), name: fileStem(f.name) }));
  const res = await ops(shots.map((shot, i) => ({ op: "shot.add", shot, index: index + i })), { label: "穿插新鏡頭" });
  if (!res) return;
  for (let i = 0; i < files.length; i += 1) await uploadFiles([files[i]], { target: "shot", shot: shots[i].id });
}

// Each picture or clip gets its own new track above the picture lane, starting at `start`.
async function addLayersWith(files, start) {
  const ids = files.map(() => newId("l"));
  const res = await ops(ids.map((id) => ({ op: "layer.add", layer: { id } })), { label: "新增畫面軌" });
  if (!res) return;
  for (let i = 0; i < files.length; i += 1) await uploadFiles([files[i]], { layer: ids[i], start });
}

// Timeline keys that only move the playhead, the I/O range, the zoom or the track height (or copy a clip).
const WATCH_KEYS = new Set(["edit.deselect", "edit.copy", "edit.markerNext", "edit.markerPrev", "edit.markIn", "edit.markOut",
  "edit.playRange", "edit.clearRange", "edit.zoomIn", "edit.zoomOut", "edit.zoomFit", "edit.trackTaller", "edit.trackShorter"]);

const markerHex = (color) => (MARKER_COLORS.find((c) => c.value === color) || MARKER_COLORS[0]).hex;

async function nameMarker(m) {
  const next = await promptDialog({
    title: "命名標記", label: "名稱", value: m.name || "", placeholder: "例如：開場、產品介紹",
    hint: "複製 YouTube 章節時當作章節標題", confirmLabel: "儲存", validate: () => "",
  });
  if (next != null && next.trim() !== (m.name || "")) {
    ops([{ op: "marker.update", id: m.id, patch: { name: next.trim().slice(0, 80) } }], { label: "命名標記" });
  }
}

// Right-click on a marker: its name, colour, the chapter list, or delete it.
function markerMenu(e, m) {
  e.stopPropagation();
  openMenu(e, [
    { header: `標記 ${fmtTime(m.start)}${m.name ? `「${m.name}」` : ""}` },
    { label: "命名…", icon: "pencil", onClick: () => nameMarker(m) },
    { label: "複製 YouTube 章節", icon: "copy", onClick: () => copyChapters(getState().project) },
    { divider: true },
    ...MARKER_COLORS.map((c) => ({ label: c.label, swatch: c.hex, checked: m.color === c.value,
      onClick: () => m.color !== c.value && ops([{ op: "marker.update", id: m.id, patch: { color: c.value } }], { label: "修改標記顏色" }) })),
    { divider: true },
    { label: "刪除標記", icon: "trash", danger: true, onClick: () => ops([{ op: "marker.delete", id: m.id }], { label: "刪除標記" }) },
  ]);
}

function overMenu(e, t) {
  e.stopPropagation();
  openMenu(e, [
    { header: `爆音 ${fmtTime(t)}` },
    { label: "清除所有爆音標記", icon: "trash", onClick: () => setState({ clipMarks: null }) },
  ]);
}

function Ruler({ width, pps, onScrub, markers, range }) {
  const overs = useStore((s) => (s.clipMarks && s.clipMarks.pid === s.pid ? s.clipMarks.times : null));
  let step = STEPS.find((s) => s * pps >= MAJOR_PX) || STEPS[STEPS.length - 1];
  const span = width / pps;
  while (span / step > MAX_TICKS) step *= 2;
  const minor = (step * pps) / (MINOR[step] || 5);
  const ticks = [];
  for (let k = 0; k * step <= span; k += 1) {
    const t = Math.round(k * step * 1000) / 1000;
    ticks.push(html`<span key=${k} class="tl-tick mono" style=${`left:${t * pps}px`}>${fmtTime(t, { tenths: step < 1 })}</span>`);
  }
  const jump = (e, m) => {
    if (e.button !== 0) return;
    e.stopPropagation();
    setTime(Number(m.start));
  };
  return html`<div class="tl-ruler" style=${`width:${width}px;--minor:${minor.toFixed(2)}px;--major:${(step * pps).toFixed(2)}px`}
    onPointerDown=${onScrub} title="拖曳移動播放頭">${ticks}
    ${range && html`<i class="tl-range-bar" style=${`left:${range.in * pps}px;width:${(range.out - range.in) * pps}px`}></i>`}
    ${markers.map((m) => html`<i key=${m.id} class="tl-marker" style=${`left:${m.start * pps}px;--mk:${markerHex(m.color)}`}
      title=${`標記 ${fmtTime(m.start)}${m.name ? `「${m.name}」` : ""}：點一下跳到這裡，右鍵命名、換顏色或刪除`} onPointerDown=${(e) => jump(e, m)}
      onContextMenu=${(e) => markerMenu(e, m)}></i>`)}
    ${overs && overs.map((t) => html`<i key=${`over${t}`} class="tl-over" style=${`left:${t * pps}px`}
      title=${`爆音 ${fmtTime(t)}：播放時超過 0dB。點一下跳到這裡，右鍵清除`} onPointerDown=${(e) => jump(e, { start: t })}
      onContextMenu=${(e) => overMenu(e, t)}></i>`)}
  </div>`;
}

// Caption voice-overs share rows: each goes into the first row where it does not overlap.
function packVoiceRows(project, items) {
  const rows = [];
  const ends = [];
  const sorted = [...items].sort((a, b) => Number(a.start || 0) - Number(b.start || 0));
  for (const item of sorted) {
    const start = Number(item.start || 0);
    const end = start + audioLength(item, audioMedia(project, item));
    const i = ends.findIndex((e) => e <= start + 1e-3);
    if (i < 0) {
      rows.push([item]);
      ends.push(end);
    } else {
      rows[i].push(item);
      ends[i] = end;
    }
  }
  return rows;
}

function mergeAudioTracks(project) {
  const merged = mergeTracks(project);
  if (!merged.ops.length) {
    toast("沒有可以合併的音軌：每條音軌都和其他音軌有同時出聲的地方", { level: "warn" });
    return;
  }
  ops(merged.ops, { label: "合併音軌" }).then((res) => {
    if (res) toast(`已合併成 ${merged.tracks} 條音軌`, { duration: 2200 });
  });
}

const SRT_VOICES = [
  { value: "delete", label: "刪除配音", hint: "舊字幕的配音和生成的音檔一起刪掉" },
  { value: "keep", label: "保留配音", hint: "配音留在配音軌上變成一般音訊，不再跟著字幕改" },
  { value: "regen", label: "重新配音", hint: "新字幕沿用時間重疊最多的舊字幕的說話者，匯入後重新生成配音" },
];

function SrtImportBody({ project, file, buf, first, close }) {
  const count = (project.captions || []).length;
  const voiced = (project.audio || []).filter((a) => a.captionId).length;
  const at = round(clock.time, 3);
  const [decoded, setDecoded] = useState(first);
  const [mode, setMode] = useState("add");
  const [fromPlayhead, setFromPlayhead] = useState(false);
  const [voices, setVoices] = useState("delete");
  const replace = mode === "replace";
  const go = () => close({ decoded, replace, offset: fromPlayhead ? at : 0, voices: replace ? voices : "delete" });
  return html`<${Modal} title="匯入 SRT" width=${500} onClose=${() => close(null)}
    footer=${html`<${Button} onClick=${() => close(null)}>取消<//><${Button} kind="primary" onClick=${go}>匯入<//>`}>
    <${Field} label="檔案編碼" error=${decoded.badLines.length ? `${linesLabel(decoded.badLines)}有無法辨識的字元，請換一種編碼` : ""}
      hint=${`「${file.name}」目前用 ${encodingLabel(decoded.encoding)} 讀取`}>
      <${Select} value=${decoded.encoding} options=${TEXT_ENCODINGS.filter((o) => o.value !== "auto")}
        onChange=${(enc) => setDecoded(decodeText(buf, enc))} />
    <//>
    ${count > 0 && html`<${Field} label=${`專案已有 ${count} 則字幕`}
      hint=${replace ? "先刪掉現有字幕再匯入" : "兩邊都留著，時間重疊的字幕會疊成兩行"}>
      <${Segmented} value=${mode} onChange=${setMode} size="sm"
        options=${[{ value: "add", label: "保留並加入" }, { value: "replace", label: "取代現有字幕" }]} />
    <//>`}
    ${replace && voiced > 0 && html`<${Field} label=${`現有字幕綁定的配音（${voiced} 段）`}
      hint=${SRT_VOICES.find((v) => v.value === voices).hint}>
      <${Segmented} value=${voices} onChange=${setVoices} size="sm" options=${SRT_VOICES} />
    <//>`}
    ${at > 0.1 && html`<${Toggle} checked=${fromPlayhead} onChange=${setFromPlayhead} label=${`從播放頭 ${fmtTime(at)} 開始`}
      hint=${fromPlayhead ? "SRT 的 0 秒對齊播放頭，適合插入一段局部字幕" : "照 SRT 裡寫的時間放"} />`}
    <p class="modal-text dim">匯入是一個步驟，按復原${keyHint("app.undo")}可整批取消。</p>
  <//>`;
}

// An .srt's subtitles at the times it gives (or from the playhead). Asks first when there is something to decide:
// captions already there, the playhead away from 0, or characters the detected encoding could not read.
async function importSrt(project, file) {
  let buf;
  try {
    buf = await file.arrayBuffer();
  } catch (err) {
    toastError(err, "無法讀取檔案");
    return;
  }
  const first = decodeText(buf);
  const ask = (project.captions || []).length > 0 || clock.time > 0.1 || first.badLines.length > 0;
  const choice = ask
    ? await openModal((close) => html`<${SrtImportBody} project=${project} file=${file} buf=${buf} first=${first} close=${close} />`)
    : { decoded: first, replace: false, offset: 0, voices: "delete" };
  if (!choice) return;
  const query = new URLSearchParams({ voices: choice.voices });
  if (choice.replace) query.set("replace", "1");
  if (choice.offset) query.set("offset", String(choice.offset));
  try {
    // sent as UTF-8 whatever the file was, so the server never has to guess the encoding again
    const body = new Blob([choice.decoded.text], { type: "text/plain" });
    const res = await upload(`/api/projects/${encodeURIComponent(project.id)}/captions.srt?${query}`, body);
    const bad = choice.decoded.badLines;
    toast(`已匯入 ${res.added} 則字幕${bad.length ? `，${linesLabel(bad)}有無法辨識的字元` : ""}`, { level: bad.length ? "warn" : "success" });
    if (choice.voices === "regen") {
      if (res.voiced.length) generateVoices(res.voiced);
      else toast("沒有新字幕和原本有說話者的字幕時間重疊，沒有重新配音", { level: "warn" });
    }
  } catch (err) {
    toastError(err, "無法匯入 SRT");
  }
}

function contentSpan(project, clips) {
  let end = sequenceDuration(clips);
  for (const c of project.captions || []) end = Math.max(end, Number(c.end) || 0);
  for (const a of project.audio || []) {
    end = Math.max(end, Number(a.start || 0) + audioLength(a, audioMedia(project, a)));
  }
  for (const o of project.overlays || []) end = Math.max(end, Number(o.start || 0) + Number(o.length || 0));
  for (const j of project.adjustments || []) end = Math.max(end, Number(j.start || 0) + Number(j.length || 0));
  return Math.max(1, end);
}

export function Timeline({ project, clips, hotkeys = true }) {
  const { editSel, focus, picked, editRange, editTool, watch, audioSel, solo } = useStore((s) => ({
    editSel: s.editSel, focus: s.focus, picked: s.selected, editRange: s.editRange, editTool: s.editTool, watch: s.watch, audioSel: multiAudio(s),
    solo: s.solo && s.solo.pid === s.pid ? s.solo.keys : null,
  }));
  const audioOn = (id) => (audioSel ? audioSel.includes(id) : !!editSel && editSel.type === "audio" && editSel.id === id);
  const [pps, setPps] = usePref("vc.timeline.pps", PPS_DEFAULT, validPps);
  const [scale, setScale] = usePref("vc.timeline.scale", 1, validScale);
  const [snapOn, setSnapOn] = usePref("vc.timeline.snap", true, isBool);
  const [linkOn, setLinkOn] = usePref(LINK_PREF, true, isBool);
  const [viewW, setViewW] = useState(0);
  const [drop, setDrop] = useState(null);
  const [adjGhost, setAdjGhost] = useState(null);
  const [audioDrag, setAudioDrag] = useState(false);
  const [pipGap, setPipGap] = useState(null); // {at, length}: an upper clip dragged into the picture lane
  const scrollRef = useRef(null);
  const headRef = useRef(null);
  const fileRef = useRef(null);
  const srtRef = useRef(null);
  const anchorRef = useRef(null);
  const ppsRef = useRef(pps);
  const dataRef = useRef(null);
  ppsRef.current = pps;
  dataRef.current = { project, clips, snapOn, linkOn, scale };

  const placeHead = () => {
    const el = headRef.current;
    const sc = scrollRef.current;
    if (!el || !sc) return;
    const x = clock.time * ppsRef.current;
    el.style.transform = `translateX(${x}px)`;
    el.style.visibility = x < sc.scrollLeft - 1 ? "hidden" : "";
  };

  useEffect(() => onClock((c) => {
    const sc = scrollRef.current;
    if (c.playing && sc) {
      const x = c.time * ppsRef.current;
      if (x > sc.scrollLeft + sc.clientWidth - LABEL_W - FOLLOW_PX || x < sc.scrollLeft) {
        sc.scrollLeft = Math.max(0, x - FOLLOW_PX);
      }
    }
    placeHead();
  }), []);

  useEffect(() => {
    const sc = scrollRef.current;
    const ro = new ResizeObserver(() => setViewW(Math.max(0, sc.clientWidth - LABEL_W)));
    ro.observe(sc);
    return () => ro.disconnect();
  }, []);

  useLayoutEffect(() => {
    const sc = scrollRef.current;
    const a = anchorRef.current;
    if (sc && a) sc.scrollLeft = Math.max(0, a.time * pps - a.offset);
    anchorRef.current = null;
    placeHead();
  }, [pps]);

  const span = contentSpan(project, clips);
  const contentW = Math.ceil(Math.max(viewW, span * pps + viewW * 0.6));
  const win = useViewWindow(scrollRef, viewW, pps, anchorRef);

  // Keep the playhead in place while zooming when it is on screen, otherwise the left edge.
  const zoomTo = (raw, anchor = null) => {
    const next = round(clamp(raw, PPS_MIN, PPS_MAX), 2);
    if (next === pps) return;
    const sc = scrollRef.current;
    if (anchor) {
      anchorRef.current = anchor;
    } else if (sc) {
      const x = clock.time * pps;
      const onScreen = x >= sc.scrollLeft && x <= sc.scrollLeft + sc.clientWidth - LABEL_W;
      anchorRef.current = onScreen ? { time: clock.time, offset: x - sc.scrollLeft } : { time: sc.scrollLeft / pps, offset: 0 };
    }
    setPps(next);
  };
  const fit = () => {
    const sc = scrollRef.current;
    if (!sc) return;
    anchorRef.current = { time: 0, offset: 0 };
    setPps(round(clamp((sc.clientWidth - LABEL_W - 32) / span, PPS_MIN, PPS_MAX), 2));
  };

  const onWheel = (e) => {
    const sc = scrollRef.current;
    if (e.altKey || e.ctrlKey || e.metaKey) {
      e.preventDefault();
      const offset = Math.max(0, e.clientX - sc.getBoundingClientRect().left - LABEL_W);
      zoomTo(pps * (e.deltaY < 0 ? ZOOM_WHEEL : 1 / ZOOM_WHEEL), { time: (sc.scrollLeft + offset) / pps, offset });
    } else if (!e.shiftKey && Math.abs(e.deltaY) > Math.abs(e.deltaX) && sc.scrollHeight <= sc.clientHeight + 1) {
      e.preventDefault();
      sc.scrollLeft += e.deltaY;
    }
  };

  const scrubLane = (lane, e) => {
    const at = (ev) => setTime(Math.max(0, (ev.clientX - lane.getBoundingClientRect().left) / ppsRef.current));
    at(e);
    dragPointer(e, { threshold: 0, onMove: (dx, ev) => at(ev) });
  };
  const scrub = (e) => {
    if (e.button !== 0) return;
    scrubLane(e.currentTarget, e);
  };
  const onLaneDown = (e) => {
    if (e.target !== e.currentTarget) return;
    setState({ editSel: null });
    scrub(e);
  };

  // Snap points for a drag; `exclude` names the item being dragged. Clips after the dragged shot move
  // with it, so only the clips before it count.
  const snapper = (exclude) => {
    const d = dataRef.current;
    if (!d.snapOn) return (t) => t;
    const pts = [0, clock.time];
    const ci = exclude.shot ? d.clips.findIndex((c) => c.shot.id === exclude.shot) : -1;
    d.clips.forEach((c, i) => {
      if (ci >= 0 && i >= ci) return;
      pts.push(c.start, c.start + c.length);
    });
    for (const c of d.project.captions || []) if (c.id !== exclude.cap) pts.push(c.start, c.end);
    for (const a of d.project.audio || []) {
      if (a.id === exclude.audio) continue;
      const start = Number(a.start || 0);
      pts.push(start, start + audioLength(a, audioMedia(d.project, a)));
    }
    for (const o of d.project.overlays || []) if (o.id !== exclude.overlay) pts.push(o.start, o.start + o.length);
    for (const j of d.project.adjustments || []) if (j.id !== exclude.adjust) pts.push(j.start, j.start + j.length);
    return makeSnap(pts, ppsRef.current);
  };

  const selRef = useRef(editSel);
  selRef.current = editSel;
  // Commands that act on the project, the sequence and the selection as they are when the key is pressed.
  const on = (fn) => () => fn(dataRef.current.project, dataRef.current.clips, selRef.current);
  const stepScale = (dir) => {
    const i = TRACK_SCALES.indexOf(dataRef.current.scale) + dir;
    if (i < 0 || i >= TRACK_SCALES.length) return toast(dir > 0 ? "軌道已經最高" : "軌道已經最低", { id: "tl-scale", duration: 1500 });
    setScale(TRACK_SCALES[i]);
  };
  const keys = {
    "edit.delete": () => deleteSelection(selRef.current),
    "edit.rippleDelete": () => rippleDelete(dataRef.current.project, selRef.current),
    "edit.deselect": () => (selRef.current ? setState({ editSel: null }) : false),
    "edit.copy": on(copyItem),
    "edit.paste": () => pasteItem(dataRef.current.project, dataRef.current.clips),
    "edit.split": on(splitAtPlayhead),
    "edit.splitAll": on(splitAllAtPlayhead),
    "edit.trimStart": () => trimAtPlayhead(dataRef.current.project, dataRef.current.clips, selRef.current, "start"),
    "edit.trimEnd": () => trimAtPlayhead(dataRef.current.project, dataRef.current.clips, selRef.current, "end"),
    "edit.toolSelect": () => setState({ editTool: "select" }),
    "edit.toolBlade": () => setState({ editTool: "blade" }),
    "edit.toolTrim": () => setState({ editTool: "trim" }),
    "edit.toggleEnabled": on(toggleEnabled),
    "edit.addCaption": () => addCaption(),
    "edit.markerAdd": on(markerAdd),
    "edit.markerColor": on(markerColor),
    "edit.markerNext": () => markerJump(dataRef.current.project, 1),
    "edit.markerPrev": () => markerJump(dataRef.current.project, -1),
    "edit.volumeUp": on((p, c, sel) => nudgeVolume(p, c, sel, 1, getState().editRange)),
    "edit.volumeDown": on((p, c, sel) => nudgeVolume(p, c, sel, -1, getState().editRange)),
    "edit.markIn": () => markIn(contentSpan(dataRef.current.project, dataRef.current.clips)),
    "edit.markOut": () => markOut(),
    "edit.rangeFromClip": on(rangeFromClip),
    "edit.playRange": () => playRange(),
    "edit.clearRange": () => (getState().editRange ? clearRange() : false),
    "edit.freeze": on(freezeFrame),
    "edit.detachAudio": on(detachAudio),
    "edit.reverseClip": on(reverseClip),
    "edit.speed": on(focusSpeed),
    "edit.speedCurve": () => notYet("曲線變速"),
    "edit.keyframePanel": () => toggleKeyPanel(selRef.current),
    "edit.keyframeAdd": on(addKeyframe),
    "edit.group": () => notYet("組合"),
    "edit.compound": () => notYet("新建複合片段"),
    "edit.uncompound": () => notYet("解除複合片段"),
    "edit.precompose": () => notYet("預合成"),
    "edit.zoomIn": () => zoomTo(ppsRef.current * ZOOM_STEP),
    "edit.zoomOut": () => zoomTo(ppsRef.current / ZOOM_STEP),
    "edit.zoomFit": () => fit(),
    "edit.trackTaller": () => stepScale(1),
    "edit.trackShorter": () => stepScale(-1),
    "edit.snap": () => setSnapOn(!dataRef.current.snapOn),
    "edit.link": () => setLinkOn(!dataRef.current.linkOn),
  };
  // Watch mode keeps only the keys that look around; the rest say why nothing happens.
  useKeymap(watch ? Object.fromEntries(Object.entries(keys).map(([id, fn]) => [id, WATCH_KEYS.has(id) ? fn : () => { watchRefuses(); }]))
    : keys, { active: hotkeys });

  // Clicks on a clip in the blade tool cut it there; Shift+click on a clip that takes keyframes adds one there.
  const onCanvasDown = (e) => {
    if (e.button !== 0) return;
    const el = e.target.closest && e.target.closest("[data-sel]");
    const blade = editTool === "blade";
    if (!el || !(blade || e.shiftKey)) return;
    const [type, id] = el.dataset.sel.split(":");
    if (!blade && !KEYABLE.includes(type)) return;
    e.preventDefault();
    e.stopPropagation();
    const sc = scrollRef.current;
    const t = Math.max(0, (e.clientX - sc.getBoundingClientRect().left - LABEL_W + sc.scrollLeft) / pps);
    const sel = { type, id };
    setState({ editSel: sel });
    if (!blade) return addKeyframe(project, clips, sel, t);
    setTime(t);
    splitAtPlayhead(project, clips, sel);
  };

  // Watch mode: a press on a lane, clip included, only moves the playhead; the ruler works as usual and
  // the track buttons and clip menus stay inert. Double-clicks still jump to an item's start.
  const watchDown = (e) => {
    if (e.target.closest(".tl-row-ruler")) return;
    e.stopPropagation();
    const lane = e.target.closest(".tl-lane");
    if (lane && e.button === 0) scrubLane(lane, e);
  };
  const watchBlock = (e) => {
    e.stopPropagation();
    if (e.type === "contextmenu") e.preventDefault();
  };

  const hasFiles = (e) => e.dataTransfer && [...e.dataTransfer.types].includes("Files");
  const hasMedia = (e) => e.dataTransfer && [...e.dataTransfer.types].includes(MEDIA_MIME);
  const dropTime = (e) => {
    const sc = scrollRef.current;
    return Math.max(0, (e.clientX - sc.getBoundingClientRect().left - LABEL_W + sc.scrollLeft) / pps);
  };
  const dropRow = (e) => {
    const row = e.target.closest && e.target.closest("[data-drop]");
    return row ? row.dataset.drop : "";
  };
  const onDragOver = (e) => {
    if (!hasFiles(e) && !hasMedia(e)) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = watch ? "none" : "copy";
    if (watch) return;
    const row = dropRow(e);
    const at = row === "video" ? shotIndexAt(project, clips, dropTime(e)).at : null;
    if (!drop || drop.row !== row || drop.at !== at) setDrop({ row, at });
  };
  const onDragLeave = (e) => {
    if (!e.currentTarget.contains(e.relatedTarget)) setDrop(null);
  };
  // Sound always goes on the soundtrack: on the voice rows or the music track it is dropped on, anywhere
  // else (or on the new-track row) each file on a track of its own. Pictures and clips dropped on the
  // picture lane (or on any row that is not a picture track) become new shots in between at the drop time,
  // on an upper track they go on that track, on the new-track row each gets a new track. Cards dragged from
  // the media bin follow the same rules without uploading anything.
  const onDrop = (e) => {
    if (!hasFiles(e) && !hasMedia(e)) return;
    e.preventDefault();
    e.stopPropagation();
    setDrop(null);
    if (watchRefuses()) return;
    const t = dropTime(e);
    const start = round(t, 2);
    const row = dropRow(e);
    const sound = row.startsWith("audio:") && row !== "audio:new" ? row.slice(6) : null;
    if (hasMedia(e)) {
      let ids = [];
      try {
        ids = JSON.parse(e.dataTransfer.getData(MEDIA_MIME) || "[]");
      } catch {
        return;
      }
      const list = ids.map((id) => project.media[id]).filter(Boolean);
      const layer = row === "new-layer" ? "new" : row.startsWith("layer:") ? row.slice(6) : null;
      placeMedia(project, list, { index: shotIndexAt(project, clips, t).index, start, layer, track: sound });
      return;
    }
    const files = [...e.dataTransfer.files];
    const srt = files.filter(isSrt);
    if (srt.length) importSrt(project, srt[0]);
    const visual = files.filter(isVisual);
    const rest = files.filter((f) => !isVisual(f) && !isSrt(f));
    if (rest.length) uploadFiles(rest, { track: true, start, audioTrack: sound });
    if (!visual.length) return;
    if (row === "new-layer") addLayersWith(visual, start);
    else if (row.startsWith("layer:")) uploadFiles(visual, { layer: row.slice(6), start });
    else insertShots(visual, shotIndexAt(project, clips, t).index);
  };
  const pickAudio = (e) => {
    const files = [...e.currentTarget.files];
    e.currentTarget.value = "";
    if (files.length) uploadFiles(files, { track: true, start: round(clock.time, 2) });
  };
  const pickSrt = (e) => {
    const file = e.currentTarget.files[0];
    e.currentTarget.value = "";
    if (file) importSrt(project, file);
  };

  const ctx = {
    pid: project.id, pps, scale, ar: project.settings.width / project.settings.height, snapper, win,
    insertAt: (t) => shotIndexAt(project, clips, t), onAudioDrag: setAudioDrag, tool: watch ? "watch" : editTool,
    onPipGap: (g) => setPipGap((old) => (g && old && g.at === old.at && g.length === old.length ? old : g)),
  };
  const tracks = project.tracks || {};
  const layers = project.layers || [];
  const overlays = project.overlays || [];
  const adjustments = project.adjustments || [];
  const audio = project.audio || [];
  const voiceRows = packVoiceRows(project, audio.filter((a) => audioTrack(a) === VOICE_TRACK));
  const music = musicTracks(audio, tracks.musicOrder);
  // While an upper clip hovers over the picture lane, the shots from the insert point on move back to show the room.
  const laneClips = pipGap ? clips.map((c) => (c.start >= pipGap.at - 1e-6 ? { ...c, start: c.start + pipGap.length } : c)) : clips;
  const total = sequenceDuration(clips);
  const nearAudio = (a) => audioOn(a.id) || inView(win, Number(a.start || 0), audioLength(a, audioMedia(project, a)));
  const lane = (cls, body) => html`<div class=${`tl-lane ${cls}`} style=${`width:${contentW}px`} onPointerDown=${onLaneDown}>${body}</div>`;
  const label = (icon, text, extra = null) => html`<div class="tl-label">${typeof icon === "string"
    ? html`<${Icon} name=${icon} size=${14} />` : icon}<span>${text}</span>${extra}</div>`;
  const row = (key, height, body, { drop: target = null, hidden = false, muted = false } = {}) => html`<div key=${key}
    class=${["tl-row", hidden && "is-hidden", muted && "is-muted", target && drop && drop.row === target && "is-drop-target"].filter(Boolean).join(" ")}
    style=${`height:${Math.round(height * scale)}px`} data-drop=${target}>${body}</div>`;
  const setTrack = (key, label) => ops([{ op: "project.update", patch: { tracks: { [key]: !tracks[key] } } }], { label });
  // Swaps music track i with the one above (d = -1) or below (d = 1); the whole order is saved.
  const moveTrack = (i, d) => {
    const keys = music.map((m) => m.key);
    [keys[i], keys[i + d]] = [keys[i + d], keys[i]];
    ops([{ op: "project.update", patch: { tracks: { musicOrder: keys } } }], { label: d < 0 ? "音軌上移" : "音軌下移" });
  };
  const orderBtns = (i) => music.length > 1 && html`<div class="tl-order">
    <button type="button" title="這條音軌上移" aria-label="這條音軌上移" disabled=${i === 0} onClick=${() => moveTrack(i, -1)}>
      <${Icon} name="chevron-up" size=${12} /></button>
    <button type="button" title="這條音軌下移" aria-label="這條音軌下移" disabled=${i === music.length - 1}
      onClick=${() => moveTrack(i, 1)}><${Icon} name="chevron-down" size=${12} /></button></div>`;
  const eyeBtn = (hidden, onClick) => html`<${IconButton} icon=${hidden ? "eye-off" : "eye"} size=${14} active=${hidden}
    title=${hidden ? "顯示這條軌道" : "隱藏這條軌道（預覽與輸出都不顯示）"} onClick=${onClick} />`;
  const muteBtn = (muted, onClick, what = "這條軌道") => html`<${IconButton} icon=${muted ? "volume-x" : "volume"} size=${14}
    active=${muted} title=${muted ? "取消靜音" : `${what}靜音`} onClick=${onClick} />`;
  // A sound row's leading icon sets what the row is for (AU-02): music on the 背景音樂 rows dips while a 人聲 row
  // (or a clip switched to 人聲) plays. Only music tracks can be 背景音樂, so sound effects keep their level.
  const db = duckDb(project.settings);
  const roleBtn = (key, icon, music = false) => {
    const role = rowRole(tracks, key);
    const set = (r) => r !== role && ops([roleOp(tracks, key, r)], { label: `音軌用途：${ROLE_LABELS[r]}` });
    const setDb = (d) => d !== db && ops([{ op: "project.update", patch: { settings: { duckDb: d } } }],
      { label: `背景音樂降低 ${d} dB` });
    return html`<button type="button" class=${`tl-role${role ? ` is-${role}` : ""}`}
      title=${`音軌用途：${ROLE_LABELS[role]}${role === "bgm" ? `，遇到人聲降低 ${db} dB` : ""}（點一下更改）`}
      onClick=${(e) => openMenu(e, [
        { header: "音軌用途" },
        { label: "一般", checked: !role, onClick: () => set("") },
        { label: "人聲", icon: "mic", hint: "背景音樂軌在這條出聲時自動降低", checked: role === "speech", onClick: () => set("speech") },
        music && { label: "背景音樂", icon: "music", hint: "遇到人聲自動降低，說完再拉回", checked: role === "bgm", onClick: () => set("bgm") },
        music && role === "bgm" && { divider: true },
        music && role === "bgm" && { header: "遇到人聲降低（整個專案共用）" },
        ...(music && role === "bgm" ? DUCK_LEVELS.map((d) => ({ label: `${d} dB`, checked: db === d, onClick: () => setDb(d) })) : []),
      ])}><${Icon} name=${role === "speech" ? "mic" : icon} size=${14} /></button>`;
  };
  // Solo (MX-03): only the soloed rows sound in the preview; several can be on. Not saved, not exported.
  const soloKeys = solo || [];
  const soloBtn = (track, what = "這條軌道") => {
    const on = soloKeys.includes(track);
    const keys = on ? soloKeys.filter((k) => k !== track) : [...soloKeys, track];
    return html`<${IconButton} icon="headphones" size=${14} class=${on ? "is-solo" : ""}
      title=${on ? "取消獨奏" : `獨奏：預覽時只聽${what}（可同時獨奏多條，不影響輸出）`}
      onClick=${() => setState({ solo: keys.length ? { pid: project.id, keys } : null })} />`;
  };

  return html`<div class=${`timeline ${drop ? "is-drop" : ""} ${!watch && editTool !== "select" ? `is-${editTool}` : ""} ${watch ? "is-watch" : ""}`}
    onDragOver=${onDragOver} onDragLeave=${onDragLeave} onDrop=${onDrop}>
    <div class="tl-toolbar">
      ${watch ? html`<span class="tl-watch-note"><${Icon} name="eye" size=${14} />觀看模式：只能看，不會修改專案</span>
        <${Button} size="sm" icon="x" onClick=${() => setWatch(false)}>結束觀看<//>` : html`
      <${Button} size="sm" kind="ghost" icon="caption-plus" onClick=${addCaption} title=${`在播放頭位置新增 2 秒字幕${keyHint("edit.addCaption")}`}>新增字幕<//>
      <${Button} size="sm" kind="ghost" icon="title-card" onClick=${() => openTitleDialog()}
        title="片頭標題、下標字卡等範本，可改字與顏色，加在播放頭位置">標題字卡<//>
      <${Button} size="sm" kind="ghost" icon="file-import" onClick=${() => srtRef.current && srtRef.current.click()}
        title="匯入 SRT 字幕檔，字幕放在檔案記載的時間；也可以把 .srt 拖進時間軸">匯入 SRT<//>
      <input ref=${srtRef} type="file" accept=".srt" hidden onChange=${pickSrt} />
      <${Button} size="sm" kind="ghost" icon="languages" onClick=${() => openBilingualImport(project)}
        title="匯入中英對照的 Word 或文字檔，中文、英文分成上下兩條字幕軌，可用語音對齊到影片">雙語字幕<//>
      <${Button} size="sm" kind="ghost" icon="music" onClick=${() => importFiles(fileRef, { track: true, start: round(clock.time, 2) }, true)}
        title="在播放頭位置加入音樂或旁白">加入音訊<//>
      <input ref=${fileRef} type="file" accept=${ACCEPT_AUDIO} multiple hidden onChange=${pickAudio} />
      <${Button} size="sm" kind="ghost" icon="users" onClick=${openCastDialog} title="人物與 Audio8Voice 配音聲音">人物表<//>
      <${Button} size="sm" kind="ghost" icon="mic" onClick=${generateMissing}
        title="替已指定人物、還沒配音或內容已改的字幕生成配音">生成配音<//>
      <${Button} size="sm" kind="ghost" icon="rows" onClick=${() => mergeAudioTracks(project)} disabled=${music.length < 2}
        title="把不會同時出聲的音軌併成同一條，同時出聲的才分開放">合併音軌<//>
      <${ToolButtons} tool=${editTool} />
      <${IconButton} icon="scissors" size=${16} title=${`在播放頭分割選取的軌道，沒有選取時分割畫面軌${keyHint("edit.split")}`}
        disabled=${!clips.length && !(editSel && editSel.type !== "shot")} onClick=${() => splitAtPlayhead(project, clips, editSel)} />
      <${IconButton} icon="magnet" size=${16} active=${snapOn}
        title=${`${snapOn ? "磁吸對齊：開，拖曳時按住 Alt 暫停" : "磁吸對齊：關"}${keyHint("edit.snap")}`} onClick=${() => setSnapOn(!snapOn)} />
      <${IconButton} icon="link" size=${16} active=${linkOn}
        title=${`${linkOn ? "連動上下軌：開，鏡頭移動或改長度時，字幕、上層畫面、音樂、音效跟著移動" : "連動上下軌：關"}${keyHint("edit.link")}`}
        onClick=${() => setLinkOn(!linkOn)} />
      <${IconButton} icon="range-x" size=${16} title=${`清除區域（入點、出點）${keyHint("edit.clearRange")}`}
        disabled=${!editRange} onClick=${clearRange} />`}
      <span class="grow"></span>
      ${!watch && html`<${Button} size="sm" kind="ghost" icon="eye" onClick=${() => setWatch(true)}
        title="只看不改：即時看 Claude 透過 API 剪輯，這個視窗不會修改專案">觀看模式<//>`}
      <span class="tl-total dim">序列 ${fmtTime(total, { tenths: false })}，${clips.length} 個片段</span>
      <${IconButton} icon="zoom-out" size=${16} title=${`縮小${keyHint("edit.zoomOut")}`} disabled=${pps <= PPS_MIN} onClick=${() => zoomTo(pps / ZOOM_STEP)} />
      <span class="tl-zoom mono" title="Alt 或 Ctrl 加滑鼠滾輪也可以縮放">${Math.round(pps)} px/秒</span>
      <${IconButton} icon="zoom-in" size=${16} title=${`放大${keyHint("edit.zoomIn")}`} disabled=${pps >= PPS_MAX} onClick=${() => zoomTo(pps * ZOOM_STEP)} />
      <${IconButton} icon="expand" size=${16} title=${`符合視窗寬度${keyHint("edit.zoomFit")}`} onClick=${fit} />
    </div>
    <div class="tl-scroll" ref=${scrollRef} onWheel=${onWheel} onScroll=${placeHead}>
      <div class="tl-canvas" style=${`width:${LABEL_W + contentW}px;--label-w:${LABEL_W}px`} onPointerDownCapture=${watch ? watchDown : onCanvasDown}
        onClickCapture=${watch ? watchBlock : null} onContextMenuCapture=${watch ? watchBlock : null}>
        <div class="tl-row tl-row-ruler" style=${`height:${RULER_H}px`}>
          <div class="tl-label tl-corner"></div>
          <${Ruler} width=${contentW} pps=${pps} onScrub=${scrub} markers=${project.markers || []} range=${editRange} />
        </div>
        ${captionRows({ project, ctx, editSel, tracks, row, label, lane, eyeBtn, setTrack })}
        ${(adjustments.length > 0 || adjGhost) && row("adjust", ADJUST_H, html`
          ${label("palette", "調整圖層", eyeBtn(!!tracks.adjustHidden,
            () => setTrack("adjustHidden", tracks.adjustHidden ? "顯示調整圖層軌" : "隱藏調整圖層軌")))}
          ${lane("tl-lane-adjust", html`<${AdjustTrack} project=${project} ctx=${ctx} editSel=${editSel} />
            ${adjGhost && html`<div class="tl-adj-ghost" style=${`left:${adjGhost.start * pps}px;width:${Math.max(4, adjGhost.length * pps)}px`}>
              <${Icon} name="plus" size=${12} /><span>放開加入調整圖層</span></div>`}`)}`,
          { hidden: !!tracks.adjustHidden })}
        ${!watch && row("add-layer", ADD_H, html`
          <div class="tl-label"><button type="button" class="tl-add-track" title="在最上層新增一條畫面軌" onClick=${addLayer}>
            <${Icon} name="plus" size=${14} /><span>新增軌道</span></button></div>
          ${lane("tl-lane-add", html`<span class="tl-lane-hint dim">把圖片或影片拖到這裡，放到新的畫面軌</span>`)}`,
          { drop: "new-layer" })}
        ${[...layers].reverse().map((layer) => {
          const name = layerName(layers, layer);
          const items = overlays.filter((o) => o.layer === layer.id);
          return row(layer.id, PIP_H, html`
            ${label("layers", name, html`
              ${eyeBtn(!!layer.hidden, () => ops([{ op: "layer.update", id: layer.id, patch: { hidden: !layer.hidden } }],
                { label: layer.hidden ? `顯示${name}` : `隱藏${name}` }))}
              ${muteBtn(!!layer.muted, () => ops([{ op: "layer.update", id: layer.id, patch: { muted: !layer.muted } }],
                { label: layer.muted ? "取消靜音" : `${name} 靜音` }))}
              <${IconButton} icon="trash" size=${14} title=${`刪除${name}${items.length ? `與上面的 ${items.length} 段` : ""}`}
                onClick=${() => removeLayer(layers, layer, items.length)} />
              <${TrackMeter} track=${layer.id} />`)}
            ${lane("tl-lane-pip", items.filter((o) => (editSel && editSel.id === o.id)
              || inView(win, Number(o.start || 0), Number(o.length || 1))).map((item) => html`<${OverlayClip} key=${item.id} project=${project} item=${item} ctx=${ctx}
              selected=${!!editSel && editSel.type === "overlay" && editSel.id === item.id} />`))}`,
            { drop: `layer:${layer.id}`, hidden: !!layer.hidden, muted: !!layer.muted });
        })}
        ${row("video", VIDEO_H, html`
          ${label("film", "畫面", html`
            <${AdjustAddButton} clips=${clips} pps=${pps} onGhost=${setAdjGhost} />
            ${eyeBtn(!!tracks.videoHidden, () => setTrack("videoHidden", tracks.videoHidden ? "顯示畫面軌" : "隱藏畫面軌"))}
            ${muteBtn(!!tracks.videoMuted, () => setTrack("videoMuted", tracks.videoMuted ? "取消靜音" : "畫面軌靜音"), "影片原聲")}`)}
          ${lane("tl-lane-video", html`<${VideoTrack} project=${project} clips=${laneClips} ctx=${ctx} editSel=${editSel} focus=${focus} picked=${picked} />
            ${drop && drop.row === "video" && drop.at != null && html`<i class="tl-insert" style=${`left:${drop.at * pps}px`}></i>`}`)}`,
          { drop: "video", hidden: !!tracks.videoHidden, muted: !!tracks.videoMuted })}
        ${hasSourceAudio(clips) && row("source-audio", AUDIO_H, html`
          ${label(roleBtn(SOURCE_TRACK, "volume"), "原聲", html`${soloBtn(SOURCE_TRACK, "影片原聲")}${muteBtn(!!tracks.videoMuted,
            () => setTrack("videoMuted", tracks.videoMuted ? "取消靜音" : "畫面軌靜音"), "影片原聲")}
            <${TrackMeter} track=${SOURCE_TRACK} />`)}
          ${lane("tl-lane-audio", html`<${SourceAudioTrack} project=${project} clips=${laneClips} ctx=${ctx} editSel=${editSel} picked=${picked} />`)}`,
          { muted: !!tracks.videoMuted })}
        ${voiceRows.map((items, i) => row(`voice-${i}`, AUDIO_H, html`
          ${label(roleBtn(VOICE_TRACK, "mic"), voiceRows.length > 1 ? `配音 ${i + 1}` : "配音", html`${soloBtn(VOICE_TRACK, "所有配音")}${muteBtn(!!tracks.voiceMuted,
            () => setTrack("voiceMuted", tracks.voiceMuted ? "取消配音靜音" : "配音全部靜音"), "所有配音")}
            ${i === 0 && html`<${TrackMeter} track=${VOICE_TRACK} />`}`)}
          ${lane("tl-lane-audio", items.filter(nearAudio).map((item) => html`<${AudioClip} key=${item.id} project=${project} item=${item} ctx=${ctx}
            selected=${audioOn(item.id)} />`))}`,
          { drop: `audio:${VOICE_TRACK}`, muted: !!tracks.voiceMuted }))}
        ${music.map(({ key, items }, i) => {
          // The track counts as muted when every clip on it is; the switch sets them all alike.
          const muted = items.every((a) => a.muted);
          return row(key, AUDIO_H, html`
            ${label(roleBtn(key, "music", true), `音訊 ${i + 1}`, html`${orderBtns(i)}${soloBtn(key, "這條音軌")}<${IconButton} icon=${muted ? "volume-x" : "volume"} size=${14}
              active=${muted} title=${muted ? "取消靜音" : "這條音軌靜音"}
              onClick=${() => ops(items.filter((a) => !!a.muted === muted).map((a) => ({ op: "audio.update", id: a.id, patch: { muted: !muted } })),
                { label: muted ? "取消靜音" : "音軌靜音" })} />
              <${TrackMeter} track=${key} />`)}
            ${lane("tl-lane-audio", items.filter(nearAudio).map((item) => html`<${AudioClip} key=${item.id} project=${project} item=${item} ctx=${ctx}
              selected=${audioOn(item.id)} />`))}`,
            { drop: `audio:${key}` });
        })}
        ${!watch && audio.length > 0 && (audioDrag || drop) && row("add-audio", ADD_H, html`
          ${label("plus", "新增音軌")}
          ${lane("tl-lane-add", html`<span class="tl-lane-hint dim">放到這裡，開一條新的音軌</span>`)}`,
          { drop: "audio:new" })}
        ${!audio.length && html`<div class="tl-row" style=${`height:${Math.round(AUDIO_H * scale)}px`}>
          ${label("music", "音訊")}
          ${lane("tl-lane-audio", html`<span class="tl-lane-hint dim">${watch ? "還沒有音訊" : "把音樂或旁白檔案拖到這裡，或按「加入音訊」"}</span>`)}
        </div>`}
        ${editRange && html`<div class="tl-range" style=${`left:${LABEL_W + editRange.in * pps}px;width:${(editRange.out - editRange.in) * pps}px`}></div>`}
        <div class="tl-playhead" ref=${headRef} style=${`left:${LABEL_W}px`}></div>
      </div>
    </div>
    ${viewW > 0 && span * pps > viewW && html`<${Minimap} pid=${project.id} clips=${clips} span=${span} pps=${pps} viewW=${viewW}
      scrollRef=${scrollRef} labelW=${LABEL_W} />`}
  </div>`;
}
