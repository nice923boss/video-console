// Edit timeline: ruler, then lanes from the top layer down (captions, adjustment layers, picture-in-picture
// tracks, the picture), the picture clips' own sound when any has some, then voice-overs and one lane per soundtrack item. A higher lane covers the ones below
// it, in the preview and in the export; an adjustment layer grades every picture lane below it. Zoom (px per second) and snapping are per-viewer preferences;
// the playhead follows the shared playback clock directly, without re-rendering.
import { html, useState, useEffect, useRef, useLayoutEffect } from "../lib/h.js";
import { useStore, setState } from "../lib/store.js";
import { usePref } from "../lib/prefs.js";
import { useKeymap, keyHint } from "../lib/keymap.js";
import { clock, setTime, onClock } from "../lib/clock.js";
import { ops, undo, deleteShots, LINK_PREF } from "../lib/actions.js";
import { uploadFiles } from "../lib/jobs-actions.js";
import { fmtTime, newId, round, clamp, kindOfName, ACCEPT_AUDIO } from "../lib/format.js";
import { sequenceDuration, audioLength } from "../lib/sequence.js";
import { generateMissing } from "../lib/voice.js";
import { openCastDialog } from "../dialogs/cast-dialog.js";
import { toast } from "./toast.js";
import { splitAtPlayhead, trimAtPlayhead } from "./tl-edit.js";
import { Button, IconButton } from "./ui.js";
import { Icon } from "./icons.js";
import {
  VideoTrack, CaptionTrack, AudioClip, OverlayClip, SourceAudioTrack, hasSourceAudio, dragPointer, makeSnap, VIDEO_H,
  CAPTION_H, AUDIO_H, PIP_H, ADD_H,
} from "./tl-tracks.js";
import { AdjustTrack, AdjustAddButton, ADJUST_H } from "./tl-adjust.js";

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

const isBool = (v) => typeof v === "boolean";
const validPps = (v) => typeof v === "number" && v >= PPS_MIN && v <= PPS_MAX;

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

const layerName = (layers, layer) => layer.name || `畫面 ${layers.indexOf(layer) + 2}`;
const isVisual = (f) => ["image", "video"].includes(kindOfName(f.name));
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

function Ruler({ width, pps, onScrub }) {
  let step = STEPS.find((s) => s * pps >= MAJOR_PX) || STEPS[STEPS.length - 1];
  const span = width / pps;
  while (span / step > MAX_TICKS) step *= 2;
  const minor = (step * pps) / (MINOR[step] || 5);
  const ticks = [];
  for (let k = 0; k * step <= span; k += 1) {
    const t = Math.round(k * step * 1000) / 1000;
    ticks.push(html`<span key=${k} class="tl-tick mono" style=${`left:${t * pps}px`}>${fmtTime(t, { tenths: step < 1 })}</span>`);
  }
  return html`<div class="tl-ruler" style=${`width:${width}px;--minor:${minor.toFixed(2)}px;--major:${(step * pps).toFixed(2)}px`}
    onPointerDown=${onScrub} title="拖曳移動播放頭">${ticks}</div>`;
}

// Caption voice-overs share rows: each goes into the first row where it does not overlap.
function packVoiceRows(project, items) {
  const rows = [];
  const ends = [];
  const sorted = [...items].sort((a, b) => Number(a.start || 0) - Number(b.start || 0));
  for (const item of sorted) {
    const start = Number(item.start || 0);
    const end = start + audioLength(item, (project.media || {})[item.mediaId]);
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

function contentSpan(project, clips) {
  let end = sequenceDuration(clips);
  for (const c of project.captions || []) end = Math.max(end, Number(c.end) || 0);
  for (const a of project.audio || []) {
    end = Math.max(end, Number(a.start || 0) + audioLength(a, (project.media || {})[a.mediaId]));
  }
  for (const o of project.overlays || []) end = Math.max(end, Number(o.start || 0) + Number(o.length || 0));
  for (const j of project.adjustments || []) end = Math.max(end, Number(j.start || 0) + Number(j.length || 0));
  return Math.max(1, end);
}

export function Timeline({ project, clips, hotkeys = true }) {
  const { editSel, focus } = useStore((s) => ({ editSel: s.editSel, focus: s.focus }));
  const [pps, setPps] = usePref("vc.timeline.pps", PPS_DEFAULT, validPps);
  const [snapOn, setSnapOn] = usePref("vc.timeline.snap", true, isBool);
  const [linkOn, setLinkOn] = usePref(LINK_PREF, true, isBool);
  const [viewW, setViewW] = useState(0);
  const [drop, setDrop] = useState(null);
  const [adjGhost, setAdjGhost] = useState(null);
  const scrollRef = useRef(null);
  const headRef = useRef(null);
  const fileRef = useRef(null);
  const anchorRef = useRef(null);
  const ppsRef = useRef(pps);
  const dataRef = useRef(null);
  ppsRef.current = pps;
  dataRef.current = { project, clips, snapOn, linkOn };

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
    if (e.ctrlKey || e.metaKey) {
      e.preventDefault();
      const offset = Math.max(0, e.clientX - sc.getBoundingClientRect().left - LABEL_W);
      zoomTo(pps * (e.deltaY < 0 ? ZOOM_WHEEL : 1 / ZOOM_WHEEL), { time: (sc.scrollLeft + offset) / pps, offset });
    } else if (!e.shiftKey && Math.abs(e.deltaY) > Math.abs(e.deltaX) && sc.scrollHeight <= sc.clientHeight + 1) {
      e.preventDefault();
      sc.scrollLeft += e.deltaY;
    }
  };

  const scrub = (e) => {
    if (e.button !== 0) return;
    const lane = e.currentTarget;
    const at = (ev) => setTime(Math.max(0, (ev.clientX - lane.getBoundingClientRect().left) / ppsRef.current));
    at(e);
    dragPointer(e, { threshold: 0, onMove: (dx, ev) => at(ev) });
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
      pts.push(start, start + audioLength(a, (d.project.media || {})[a.mediaId]));
    }
    for (const o of d.project.overlays || []) if (o.id !== exclude.overlay) pts.push(o.start, o.start + o.length);
    for (const j of d.project.adjustments || []) if (j.id !== exclude.adjust) pts.push(j.start, j.start + j.length);
    return makeSnap(pts, ppsRef.current);
  };

  const selRef = useRef(editSel);
  selRef.current = editSel;
  useKeymap({
    "edit.delete": () => deleteSelection(selRef.current),
    "edit.deselect": () => (selRef.current ? setState({ editSel: null }) : false),
    "edit.split": () => splitAtPlayhead(dataRef.current.project, dataRef.current.clips, selRef.current),
    "edit.trimStart": () => trimAtPlayhead(dataRef.current.project, dataRef.current.clips, selRef.current, "start"),
    "edit.trimEnd": () => trimAtPlayhead(dataRef.current.project, dataRef.current.clips, selRef.current, "end"),
    "edit.addCaption": () => addCaption(),
    "edit.zoomIn": () => zoomTo(ppsRef.current * ZOOM_STEP),
    "edit.zoomOut": () => zoomTo(ppsRef.current / ZOOM_STEP),
    "edit.zoomFit": () => fit(),
    "edit.snap": () => setSnapOn(!dataRef.current.snapOn),
    "edit.link": () => setLinkOn(!dataRef.current.linkOn),
  }, { active: hotkeys });

  const hasFiles = (e) => e.dataTransfer && [...e.dataTransfer.types].includes("Files");
  const dropTime = (e) => {
    const sc = scrollRef.current;
    return Math.max(0, (e.clientX - sc.getBoundingClientRect().left - LABEL_W + sc.scrollLeft) / pps);
  };
  const dropRow = (e) => {
    const row = e.target.closest && e.target.closest("[data-drop]");
    return row ? row.dataset.drop : "";
  };
  const onDragOver = (e) => {
    if (!hasFiles(e)) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "copy";
    const row = dropRow(e);
    const at = row === "video" ? shotIndexAt(project, clips, dropTime(e)).at : null;
    if (!drop || drop.row !== row || drop.at !== at) setDrop({ row, at });
  };
  const onDragLeave = (e) => {
    if (!e.currentTarget.contains(e.relatedTarget)) setDrop(null);
  };
  // Sound always goes on the soundtrack. Pictures and clips dropped on the picture lane become new shots
  // in between, on an upper track they go on that track, on the new-track row each gets a new
  // track; anywhere else they only go to the library.
  const onDrop = (e) => {
    if (!hasFiles(e)) return;
    e.preventDefault();
    e.stopPropagation();
    setDrop(null);
    const t = dropTime(e);
    const start = round(t, 2);
    const row = dropRow(e);
    const files = [...e.dataTransfer.files];
    const visual = files.filter(isVisual);
    const rest = files.filter((f) => !isVisual(f));
    if (rest.length) uploadFiles(rest, { track: true, start });
    if (!visual.length) return;
    if (row === "video") insertShots(visual, shotIndexAt(project, clips, t).index);
    else if (row === "new-layer") addLayersWith(visual, start);
    else if (row.startsWith("layer:")) uploadFiles(visual, { layer: row.slice(6), start });
    else {
      uploadFiles(visual, { track: true, start });
      toast("圖片與影片已加入素材庫；拖到「畫面」軌可穿插成新鏡頭，拖到上層畫面軌可疊在上面", { level: "info" });
    }
  };
  const pickAudio = (e) => {
    const files = [...e.currentTarget.files];
    e.currentTarget.value = "";
    if (files.length) uploadFiles(files, { track: true, start: round(clock.time, 2) });
  };

  const ctx = {
    pid: project.id, pps, ar: project.settings.width / project.settings.height, snapper,
    insertAt: (t) => shotIndexAt(project, clips, t),
  };
  const tracks = project.tracks || {};
  const layers = project.layers || [];
  const overlays = project.overlays || [];
  const adjustments = project.adjustments || [];
  const audio = project.audio || [];
  const music = audio.filter((a) => !a.captionId);
  const voiceRows = packVoiceRows(project, audio.filter((a) => a.captionId));
  const total = sequenceDuration(clips);
  const lane = (cls, body) => html`<div class=${`tl-lane ${cls}`} style=${`width:${contentW}px`} onPointerDown=${onLaneDown}>${body}</div>`;
  const label = (icon, text, extra = null) => html`<div class="tl-label"><${Icon} name=${icon} size=${14} /><span>${text}</span>${extra}</div>`;
  const row = (key, height, body, { drop: target = null, hidden = false, muted = false } = {}) => html`<div key=${key}
    class=${["tl-row", hidden && "is-hidden", muted && "is-muted", target && drop && drop.row === target && "is-drop-target"].filter(Boolean).join(" ")}
    style=${`height:${height}px`} data-drop=${target}>${body}</div>`;
  const setTrack = (key, label) => ops([{ op: "project.update", patch: { tracks: { [key]: !tracks[key] } } }], { label });
  const eyeBtn = (hidden, onClick) => html`<${IconButton} icon=${hidden ? "eye-off" : "eye"} size=${14} active=${hidden}
    title=${hidden ? "顯示這條軌道" : "隱藏這條軌道（預覽與輸出都不顯示）"} onClick=${onClick} />`;
  const muteBtn = (muted, onClick, what = "這條軌道") => html`<${IconButton} icon=${muted ? "volume-x" : "volume"} size=${14}
    active=${muted} title=${muted ? "取消靜音" : `${what}靜音`} onClick=${onClick} />`;

  return html`<div class=${`timeline ${drop ? "is-drop" : ""}`} onDragOver=${onDragOver} onDragLeave=${onDragLeave} onDrop=${onDrop}>
    <div class="tl-toolbar">
      <${Button} size="sm" kind="ghost" icon="captions" onClick=${addCaption} title=${`在播放頭位置新增 2 秒字幕${keyHint("edit.addCaption")}`}>新增字幕<//>
      <${Button} size="sm" kind="ghost" icon="music" onClick=${() => fileRef.current && fileRef.current.click()}
        title="在播放頭位置加入音樂或旁白">加入音訊<//>
      <input ref=${fileRef} type="file" accept=${ACCEPT_AUDIO} multiple hidden onChange=${pickAudio} />
      <${Button} size="sm" kind="ghost" icon="users" onClick=${openCastDialog} title="人物與 Audio8Voice 配音聲音">人物表<//>
      <${Button} size="sm" kind="ghost" icon="mic" onClick=${generateMissing}
        title="替已指定人物、還沒配音或內容已改的字幕生成配音">生成配音<//>
      <${IconButton} icon="scissors" size=${16} title=${`在播放頭分割選取的軌道，沒有選取時分割畫面軌${keyHint("edit.split")}`}
        disabled=${!clips.length && !(editSel && editSel.type !== "shot")} onClick=${() => splitAtPlayhead(project, clips, editSel)} />
      <${IconButton} icon="magnet" size=${16} active=${snapOn}
        title=${`${snapOn ? "磁吸對齊：開，拖曳時按住 Alt 暫停" : "磁吸對齊：關"}${keyHint("edit.snap")}`} onClick=${() => setSnapOn(!snapOn)} />
      <${IconButton} icon="link" size=${16} active=${linkOn}
        title=${`${linkOn ? "連動上下軌：開，鏡頭移動或改長度時，字幕、上層畫面、音樂、音效跟著移動" : "連動上下軌：關"}${keyHint("edit.link")}`}
        onClick=${() => setLinkOn(!linkOn)} />
      <span class="grow"></span>
      <span class="tl-total dim">序列 ${fmtTime(total, { tenths: false })}，${clips.length} 個片段</span>
      <${IconButton} icon="zoom-out" size=${16} title=${`縮小${keyHint("edit.zoomOut")}`} disabled=${pps <= PPS_MIN} onClick=${() => zoomTo(pps / ZOOM_STEP)} />
      <span class="tl-zoom mono" title="Ctrl 加滑鼠滾輪也可以縮放">${Math.round(pps)} px/秒</span>
      <${IconButton} icon="zoom-in" size=${16} title=${`放大${keyHint("edit.zoomIn")}`} disabled=${pps >= PPS_MAX} onClick=${() => zoomTo(pps * ZOOM_STEP)} />
      <${IconButton} icon="expand" size=${16} title=${`符合視窗寬度${keyHint("edit.zoomFit")}`} onClick=${fit} />
    </div>
    <div class="tl-scroll" ref=${scrollRef} onWheel=${onWheel} onScroll=${placeHead}>
      <div class="tl-canvas" style=${`width:${LABEL_W + contentW}px;--label-w:${LABEL_W}px`}>
        <div class="tl-row tl-row-ruler" style=${`height:${RULER_H}px`}>
          <div class="tl-label tl-corner"></div>
          <${Ruler} width=${contentW} pps=${pps} onScrub=${scrub} />
        </div>
        ${row("captions", CAPTION_H, html`
          ${label("captions", "字幕", eyeBtn(!!tracks.captionsHidden,
            () => setTrack("captionsHidden", tracks.captionsHidden ? "顯示字幕軌" : "隱藏字幕軌")))}
          ${lane("tl-lane-caption", html`<${CaptionTrack} project=${project} ctx=${ctx} editSel=${editSel} />`)}`,
          { hidden: !!tracks.captionsHidden })}
        ${(adjustments.length > 0 || adjGhost) && row("adjust", ADJUST_H, html`
          ${label("palette", "調整圖層", eyeBtn(!!tracks.adjustHidden,
            () => setTrack("adjustHidden", tracks.adjustHidden ? "顯示調整圖層軌" : "隱藏調整圖層軌")))}
          ${lane("tl-lane-adjust", html`<${AdjustTrack} project=${project} ctx=${ctx} editSel=${editSel} />
            ${adjGhost && html`<div class="tl-adj-ghost" style=${`left:${adjGhost.start * pps}px;width:${Math.max(4, adjGhost.length * pps)}px`}>
              <${Icon} name="plus" size=${12} /><span>放開加入調整圖層</span></div>`}`)}`,
          { hidden: !!tracks.adjustHidden })}
        ${row("add-layer", ADD_H, html`
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
                onClick=${() => removeLayer(layers, layer, items.length)} />`)}
            ${lane("tl-lane-pip", items.map((item) => html`<${OverlayClip} key=${item.id} project=${project} item=${item} ctx=${ctx}
              selected=${!!editSel && editSel.type === "overlay" && editSel.id === item.id} />`))}`,
            { drop: `layer:${layer.id}`, hidden: !!layer.hidden, muted: !!layer.muted });
        })}
        ${row("video", VIDEO_H, html`
          ${label("film", "畫面", html`
            <${AdjustAddButton} clips=${clips} pps=${pps} onGhost=${setAdjGhost} />
            ${eyeBtn(!!tracks.videoHidden, () => setTrack("videoHidden", tracks.videoHidden ? "顯示畫面軌" : "隱藏畫面軌"))}
            ${muteBtn(!!tracks.videoMuted, () => setTrack("videoMuted", tracks.videoMuted ? "取消靜音" : "畫面軌靜音"), "影片原聲")}`)}
          ${lane("tl-lane-video", html`<${VideoTrack} project=${project} clips=${clips} ctx=${ctx} editSel=${editSel} focus=${focus} />
            ${drop && drop.row === "video" && drop.at != null && html`<i class="tl-insert" style=${`left:${drop.at * pps}px`}></i>`}`)}`,
          { drop: "video", hidden: !!tracks.videoHidden, muted: !!tracks.videoMuted })}
        ${hasSourceAudio(clips) && row("source-audio", AUDIO_H, html`
          ${label("volume", "原聲", muteBtn(!!tracks.videoMuted,
            () => setTrack("videoMuted", tracks.videoMuted ? "取消靜音" : "畫面軌靜音"), "影片原聲"))}
          ${lane("tl-lane-audio", html`<${SourceAudioTrack} project=${project} clips=${clips} ctx=${ctx} editSel=${editSel} />`)}`,
          { muted: !!tracks.videoMuted })}
        ${voiceRows.map((items, i) => row(`voice-${i}`, AUDIO_H, html`
          ${label("mic", voiceRows.length > 1 ? `配音 ${i + 1}` : "配音", muteBtn(!!tracks.voiceMuted,
            () => setTrack("voiceMuted", tracks.voiceMuted ? "取消配音靜音" : "配音全部靜音"), "所有配音"))}
          ${lane("tl-lane-audio", items.map((item) => html`<${AudioClip} key=${item.id} project=${project} item=${item} ctx=${ctx}
            selected=${!!editSel && editSel.type === "audio" && editSel.id === item.id} />`))}`,
          { muted: !!tracks.voiceMuted }))}
        ${music.map((item, i) => html`<div key=${item.id} class="tl-row" style=${`height:${AUDIO_H}px`}>
          ${label("music", `音訊 ${i + 1}`, html`<${IconButton} icon=${item.muted ? "volume-x" : "volume"} size=${14}
            active=${item.muted} title=${item.muted ? "取消靜音" : "這條音軌靜音"}
            onClick=${() => ops([{ op: "audio.update", id: item.id, patch: { muted: !item.muted } }],
              { label: item.muted ? "取消靜音" : "音軌靜音" })} />`)}
          ${lane("tl-lane-audio", html`<${AudioClip} project=${project} item=${item} ctx=${ctx}
            selected=${!!editSel && editSel.type === "audio" && editSel.id === item.id} />`)}
        </div>`)}
        ${!audio.length && html`<div class="tl-row" style=${`height:${AUDIO_H}px`}>
          ${label("music", "音訊")}
          ${lane("tl-lane-audio", html`<span class="tl-lane-hint dim">把音樂或旁白檔案拖到這裡，或按「加入音訊」</span>`)}
        </div>`}
        <div class="tl-playhead" ref=${headRef} style=${`left:${LABEL_W}px`}></div>
      </div>
    </div>
  </div>`;
}
