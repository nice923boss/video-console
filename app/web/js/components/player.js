// Preview player for the edit view: the stage (drawn by player-engine.js), the playback loop and the
// control bar. Playback time lives in lib/clock.js, so the timeline playhead and this player stay in
// step without re-rendering components on every frame.
import { html, useState, useEffect, useRef } from "../lib/h.js";
import { useStore, getState, setState } from "../lib/store.js";
import { usePref } from "../lib/prefs.js";
import { useKeymap, keyHint, keyLabel } from "../lib/keymap.js";
import { clock, setTime, setPlaying, setDuration, onClock, togglePlay, shuttle } from "../lib/clock.js";
import { fmtTime, shotLabel } from "../lib/format.js";
import { sequenceDuration, clipIndexAt } from "../lib/sequence.js";
import { ops, saveSettings, updateProject } from "../lib/actions.js";
import { api } from "../lib/api.js";
import { legacySize, resolveStyle, usesStyle, dragScope, moveOps } from "../lib/caption-style.js";
import { requestStems } from "../lib/voice-stems.js";
import { createEngine } from "./player-engine.js";
import { MiniMeter } from "./audio-meter.js";
import { Icon } from "./icons.js";
import { IconButton, Slider } from "./ui.js";
import { openMenu } from "./overlay.js";
import { toastError } from "./toast.js";

const WAIT_LIMIT_MS = 4000;
const SEEK_JUMP = 0.05;
const DEFAULT_VIEW = { safe: false, thirds: false, captions: true };
const DEFAULT_VOLUME = { level: 1, muted: false };
const isBool = (v) => typeof v === "boolean";
const validView = (v) => !!v && isBool(v.safe) && isBool(v.thirds) && isBool(v.captions);
const validVolume = (v) => !!v && typeof v.level === "number" && v.level >= 0 && v.level <= 1 && isBool(v.muted);

// Advances the clock at clock.rate times real time while playing (backwards when it is negative). Seeks made
// elsewhere (timeline, inspector) restart the timing from the new position; a buffering video holds
// the clock for a few seconds at most.
function createLoop(engine, loopRef) {
  let raf = 0;
  let t0 = 0;
  let wall0 = 0;
  let last = 0;
  let waitSince = 0;
  let rate = 1;
  const restart = (t) => {
    t0 = t;
    last = t;
    wall0 = performance.now();
    rate = clock.rate;
  };
  const tick = (now) => {
    raf = 0;
    if (!clock.playing) return;
    // Backwards playback seeks frame by frame (the engine renders it as paused), so nothing buffers.
    if (rate > 0 && engine.isWaiting()) {
      if (!waitSince) waitSince = now;
      if (now - waitSince < WAIT_LIMIT_MS) {
        t0 = last;
        wall0 = now;
        raf = requestAnimationFrame(tick);
        return;
      }
    } else {
      waitSince = 0;
    }
    let t = t0 + (rate * (now - wall0)) / 1000;
    if (rate < 0 && t <= 0) {
      setPlaying(false);
      setTime(0);
      return;
    }
    if (t >= clock.duration) {
      if (!loopRef.current || clock.duration <= 0) {
        setPlaying(false);
        setTime(clock.duration);
        return;
      }
      t = 0;
      t0 = 0;
      wall0 = now;
    }
    last = t;
    raf = requestAnimationFrame(tick);
    setTime(t);
  };
  return {
    onClock(c) {
      if (!c.playing) {
        if (raf) cancelAnimationFrame(raf);
        raf = 0;
        waitSince = 0;
      } else if (!raf) {
        restart(c.time);
        raf = requestAnimationFrame(tick);
      } else if (Math.abs(c.time - last) > SEEK_JUMP || c.rate !== rate) {
        restart(c.time);
      }
    },
    stop() {
      if (raf) cancelAnimationFrame(raf);
      raf = 0;
    },
  };
}

// Video files the preview plays, as lowres.py's wanted() lists them (PV-06): the sequence's active videos and the
// picture-in-picture clips. See-through WebM files have no smaller copy.
function playedVideos(project) {
  const ids = [...(project.shots || []).filter((s) => !s.disabled).map((s) => s.activeVideo),
    ...(project.overlays || []).filter((o) => !o.disabled).map((o) => o.mediaId)];
  const out = [];
  for (const id of ids) {
    const item = id && ((project.takes || {})[id] || (project.media || {})[id]);
    if (item && item.kind === "video" && item.file && !item.alphaCodec && !out.includes(item)) out.push(item);
  }
  return out;
}

const RES_LABEL = { 1: "完整", 2: "1/2", 4: "1/4" };
const PROGRAM_CHANNEL = "vc-program"; // program.js listens on the same name
const validRes = (v) => [1, 2, 4].includes(v);

function viewMenu(e, view, setView, cover, res, setRes, compare) {
  const flip = (key) => () => setView({ ...view, [key]: !view[key] });
  openMenu(e, [
    { header: "預覽畫面" },
    { label: "顯示字幕", icon: "captions", checked: view.captions, onClick: flip("captions") },
    { label: "安全框（動作 93%、標題 90%）", icon: "frame", checked: view.safe, onClick: flip("safe") },
    { label: "三分構圖線", icon: "grid", checked: view.thirds, onClick: flip("thirds") },
    { label: "調色前後對照（左半調色前、右半調色後）", icon: "palette", checked: compare,
      onClick: () => setState({ gradeCompare: !compare }) },
    { divider: true },
    { label: "填滿畫面（比例不同時裁切，輸出相同）", icon: "maximize", checked: cover,
      onClick: () => updateProject({ settings: { fit: cover ? "contain" : "cover" } }, { label: "切換畫面填滿" }) },
    { divider: true },
    { header: "預覽解析度（輸出一律用原檔）" },
    ...[1, 2, 4].map((n) => ({ label: RES_LABEL[n], checked: res === n, onClick: () => setRes(n),
      hint: n > 1 ? "先做較小的預覽檔，長片、4K 較順" : "" })),
  ]);
}

function jumpClip(clips, dir) {
  if (!clips.length) return;
  const t = clock.time;
  const i = Math.max(0, clipIndexAt(clips, t));
  if (dir < 0) {
    const target = t - clips[i].start > 0.25 || i === 0 ? clips[i].start : clips[i - 1].start;
    setTime(target);
  } else if (i + 1 < clips.length) {
    setTime(clips[i + 1].start);
  } else {
    setTime(clock.duration);
  }
}

// Captions dragged in the preview: just that caption, or every caption of its style (the scope picked in
// the caption panel).
const captionHooks = {
  onCaptionMove(cap, point) {
    const st = getState();
    if (!st.project) return null;
    const scope = dragScope(st);
    return ops(moveOps(st.project, cap, point, scope, legacySize(st.settings)),
      { label: scope === "style" ? "移動同樣式字幕位置" : "移動字幕位置" });
  },
  captionDragLabel(cap) {
    const st = getState();
    if (!st.project || dragScope(st) !== "style") return "只移動這句";
    const legacy = legacySize(st.settings);
    const id = resolveStyle(st.project, cap, legacy).id;
    const n = (st.project.captions || []).filter((c) => usesStyle(st.project, c, id, legacy)).length;
    return `同樣式全部一起移動（${n} 句）`;
  },
  dragDisabled: () => getState().watch,
};

// The engine plays media only forwards; backwards playback is drawn as a run of paused frames.
const rolling = (c) => c.playing && c.rate > 0;

function step(seconds) {
  setPlaying(false);
  setTime(clock.time + seconds);
}

export function Player({ project, clips, hotkeys = true, meters = false, onToggleMeters = null, bin = false, onToggleBin = null,
  scopes = false, onToggleScopes = null, onLayout = null, side = null, chat = false, onToggleChat = null }) {
  const legacy = useStore((s) => legacySize(s.settings));
  const gradeDraft = useStore((s) => s.gradeDraft);
  const maskDraft = useStore((s) => s.maskDraft);
  const compare = useStore((s) => s.gradeCompare);
  const solo = useStore((s) => (s.solo && s.solo.pid === s.pid ? s.solo.keys : null));
  const voiceStems = useStore((s) => s.voiceStems);
  const voiceMonitor = useStore((s) => s.voiceMonitor);
  const [shown, setShown] = usePref("vc.player.view", DEFAULT_VIEW, validView);
  // Safe area and thirds live in settings (ui.safeArea / ui.thirds); only the captions switch is per browser.
  const safe = useStore((s) => !!(s.settings && s.settings.ui && s.settings.ui.safeArea));
  const thirds = useStore((s) => !!(s.settings && s.settings.ui && s.settings.ui.thirds));
  const view = { ...shown, safe, thirds };
  const setView = (next) => {
    setShown(next);
    if (next.safe !== safe || next.thirds !== thirds) saveSettings({ ui: { safeArea: next.safe, thirds: next.thirds } });
  };
  const [volume, setVolume] = usePref("vc.player.volume", DEFAULT_VOLUME, validVolume);
  const [loop, setLoop] = usePref("vc.player.loop", false, isBool);
  const [res, setRes] = usePref("vc.player.res", 1, validRes);
  const [live, setLive] = useState({ playing: clock.playing, index: -1, rate: 1 });
  const [full, setFull] = useState(false);
  const rootRef = useRef(null);
  const hostRef = useRef(null);
  const timeRef = useRef(null);
  const engineRef = useRef(null);
  const clipsRef = useRef(clips);
  const liveRef = useRef(live);
  const loopRef = useRef(loop);
  const programRef = useRef(null);
  clipsRef.current = clips;
  loopRef.current = loop;

  useEffect(() => {
    const engine = createEngine(hostRef.current, captionHooks);
    const loopCtl = createLoop(engine, loopRef);
    engineRef.current = engine;
    // Program window (PV-07): it says hello when it opens and bye when it closes; it gets every clock tick.
    const program = { channel: new BroadcastChannel(PROGRAM_CHANNEL), open: false, send: null };
    program.channel.onmessage = ({ data }) => {
      program.open = data.type === "hello" || (program.open && data.type !== "bye");
      if (data.type === "hello" && program.send) program.send();
    };
    programRef.current = program;
    const off = onClock((c) => {
      loopCtl.onClock(c);
      engine.render(c.time, rolling(c), c.rate);
      if (program.open) program.channel.postMessage({ type: "clock", t: c.time, playing: rolling(c), rate: c.rate });
      if (timeRef.current) timeRef.current.textContent = fmtTime(c.time);
      const index = clipIndexAt(clipsRef.current, c.time);
      const rate = c.playing ? c.rate : 1;
      const was = liveRef.current;
      if (was.playing !== c.playing || was.index !== index || was.rate !== rate) {
        liveRef.current = { playing: c.playing, index, rate };
        setLive(liveRef.current);
      }
    });
    const onFull = () => setFull(document.fullscreenElement === rootRef.current);
    document.addEventListener("fullscreenchange", onFull);
    return () => {
      off();
      program.channel.postMessage({ type: "closed", t: clock.time });
      program.channel.close();
      loopCtl.stop();
      setPlaying(false);
      engine.destroy();
      engineRef.current = null;
      document.removeEventListener("fullscreenchange", onFull);
    };
  }, []);

  useEffect(() => {
    engineRef.current.setProject(project, clips, legacy);
    setDuration(sequenceDuration(clips));
    engineRef.current.render(clock.time, rolling(clock), clock.rate);
    if (timeRef.current) timeRef.current.textContent = fmtTime(clock.time);
  }, [project, clips, legacy]);

  useEffect(() => {
    engineRef.current.setVolume(volume.level, volume.muted);
  }, [volume]);

  useEffect(() => {
    engineRef.current.setGradeDraft(gradeDraft);
  }, [gradeDraft]);

  useEffect(() => {
    engineRef.current.setMaskDraft(maskDraft);
  }, [maskDraft]);

  useEffect(() => {
    engineRef.current.setCompare(compare);
  }, [compare]);

  useEffect(() => {
    engineRef.current.setSolo(solo);
  }, [solo]);

  // The stems the audio clips' 降噪 / 去回音 / 聲道 play from, made again whenever their settings or trims change.
  useEffect(() => {
    requestStems(project);
  }, [project]);
  useEffect(() => {
    engineRef.current.setVoice(voiceStems, voiceMonitor);
  }, [voiceStems, voiceMonitor]);

  // Smaller preview copies: the engine plays each one as it arrives; the server makes the missing ones once.
  const played = res > 1 ? playedVideos(project) : [];
  const missing = played.filter((v) => !v[`preview${res}`]).map((v) => v.id).join(",");
  useEffect(() => {
    engineRef.current.setPreviewScale(res);
  }, [res]);
  useEffect(() => {
    if (missing) api.post(`/api/projects/${project.id}/preview-files`, { scale: res }).catch((err) => toastError(err, "無法製作預覽檔"));
  }, [project.id, res, missing]);
  const ready = played.length - (missing ? missing.split(",").length : 0);

  // What the program window draws: sent when it opens and whenever any of it changes.
  useEffect(() => {
    const program = programRef.current;
    program.send = () => {
      program.channel.postMessage({ type: "state", project, legacy, res, captions: view.captions });
      program.channel.postMessage({ type: "clock", t: clock.time, playing: rolling(clock), rate: clock.rate });
    };
    if (program.open) program.send();
  }, [project, legacy, res, view.captions]);
  const openProgram = () => {
    const win = window.open("/program.html", "vc-program", "popup,width=960,height=540");
    if (win) win.focus();
    else toastError(new Error("瀏覽器擋下了新視窗，請允許這個網站開啟彈出式視窗"), "無法開啟節目畫面");
  };

  const fps = Math.max(1, Number(project.settings.fps) || 24);
  const toggleFull = () => {
    if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
    else if (rootRef.current) rootRef.current.requestFullscreen().catch(() => {});
  };
  useKeymap({
    "edit.play": () => togglePlay(),
    "edit.reverse": () => shuttle(-1),
    "edit.stop": () => setPlaying(false),
    "edit.forward": () => shuttle(1),
    "edit.prevFrame": () => step(-1 / fps),
    "edit.nextFrame": () => step(1 / fps),
    "edit.back1s": () => step(-1),
    "edit.fwd1s": () => step(1),
    "edit.back10f": () => step(-10 / fps),
    "edit.fwd10f": () => step(10 / fps),
    "edit.prevEdit": () => jumpClip(clipsRef.current, -1),
    "edit.nextEdit": () => jumpClip(clipsRef.current, 1),
    "edit.start": () => setTime(0),
    "edit.end": () => setTime(clock.duration),
  }, { active: hotkeys });

  const { width, height } = project.settings;
  const total = sequenceDuration(clips);
  const current = live.index >= 0 ? clips[live.index] : null;
  const cover = project.settings.fit === "cover";
  const stageCls = ["pl-stage", view.safe && "show-safe", view.thirds && "show-thirds", view.captions && "show-caps",
    cover && "fit-cover", compare && "show-compare"]
    .filter(Boolean).join(" ");
  const stageStyle = `--ar:${(width / height).toFixed(5)}`;
  const muted = volume.muted || volume.level === 0;

  return html`<div class=${`player ${full ? "is-full" : ""}`} ref=${rootRef}>
    <div class="pl-view">
    <div class="pl-screen" onDblClick=${toggleFull}>
      <div class=${stageCls} style=${stageStyle} onClick=${() => clips.length && togglePlay()}>
        <div class="pl-host" ref=${hostRef}></div>
        <div class="pl-guides" aria-hidden="true">
          <i class="pl-safe-action"></i><i class="pl-safe-title"></i><i class="pl-thirds"></i>
          <i class="pl-compare"><b>調色前</b><b>調色後</b></i>
        </div>
        ${!clips.length && html`<div class="pl-empty">
          <${Icon} name="film" size=${28} />
          <b>序列中還沒有鏡頭</b>
          <span>到分鏡頁新增鏡頭，或在鏡頭的「剪輯」分頁勾選「放入輸出序列」</span>
        </div>`}
      </div>
    </div>
    ${side}
    </div>
    <div class="pl-bar">
      ${onToggleBin && html`<${IconButton} icon="images" size=${16} active=${bin} onClick=${onToggleBin}
        title=${`${bin ? "隱藏" : "顯示"}素材清單，可拖到時間軸${keyHint("edit.bin")}`} />`}
      ${onLayout && html`<${IconButton} icon="panel" size=${16} onClick=${onLayout} title="工作區版面：切換或儲存面板配置" />`}
      <${IconButton} icon="skip-back" size=${17} title=${`上一個鏡頭${keyHint("edit.prevEdit")}`} disabled=${!clips.length}
        onClick=${() => jumpClip(clips, -1)} />
      <${IconButton} icon=${live.playing ? "pause" : "play"} size=${20} kind="default" class="pl-play"
        title=${`${live.playing ? "暫停" : "播放"}${keyHint("edit.play")}`} disabled=${!clips.length} onClick=${togglePlay} />
      <${IconButton} icon="skip-forward" size=${17} title=${`下一個鏡頭${keyHint("edit.nextEdit")}`} disabled=${!clips.length}
        onClick=${() => jumpClip(clips, 1)} />
      <span class="pl-time mono"><span ref=${timeRef}>${fmtTime(clock.time)}</span><span class="dim"> / ${fmtTime(total)}</span></span>
      ${onToggleMeters && html`<${MiniMeter} active=${meters} onClick=${onToggleMeters}
        title=${`${meters ? "隱藏" : "顯示"}音量表${keyHint("edit.meters")}`} />`}
      ${live.rate !== 1 && html`<span class="pl-rate mono" title=${`${[keyLabel("edit.reverse"), keyLabel("edit.forward")].filter(Boolean).join("、")} 再按一次加速，${keyLabel("edit.play") || "播放鈕"} 恢復正常`}>${live.rate}x</span>`}
      ${current && html`<span class="pl-clip dim" title=${shotLabel(current.shot, current.index)}>
        ${shotLabel(current.shot, current.index)}
      </span>`}
      <span class="grow"></span>
      ${res > 1 && html`<span class="pl-res mono" title=${`預覽解析度 ${RES_LABEL[res]}：${ready}／${played.length} 個影片已有較小的預覽檔${ready < played.length ? "，其餘製作中或無法製作，先播原檔" : ""}；輸出一律用原檔`}>${RES_LABEL[res]}</span>`}
      <${IconButton} icon="refresh" size=${16} active=${loop}
        title=${`${loop ? "循環播放：開" : "循環播放：關"}，播放區域${keyHint("edit.playRange")}時只在入點與出點之間循環`}
        onClick=${() => setLoop(!loop)} />
      ${onToggleScopes && html`<${IconButton} icon="activity" size=${16} active=${scopes} onClick=${onToggleScopes}
        title=${`${scopes ? "隱藏" : "顯示"}示波器（亮度波形、向量儀）${keyHint("edit.scopes")}`} />`}
      ${onToggleChat && html`<${IconButton} icon="bot" size=${16} active=${chat} onClick=${onToggleChat}
        title=${`${chat ? "隱藏" : "顯示"} AI 助理：用文字模型剪輯與上字幕${keyHint("edit.chat")}`} />`}
      <${IconButton} icon="frame" size=${16} active=${view.safe || view.thirds || compare} title="預覽畫面選項"
        onClick=${(e) => viewMenu(e, view, setView, cover, res, setRes, compare)} />
      <div class="pl-vol">
        <${IconButton} icon=${muted ? "volume-x" : "volume"} size=${16} title=${muted ? "取消靜音" : "靜音"}
          onClick=${() => setVolume({ level: volume.level || 1, muted: !muted })} />
        <${Slider} value=${volume.muted ? 0 : volume.level} min=${0} max=${1} step=${0.05} class="pl-vol-range"
          onChange=${(level) => setVolume({ level, muted: false })} />
      </div>
      <${IconButton} icon="external" size=${16} title="節目畫面：在新視窗只放預覽畫面，拖到第二螢幕後雙擊全螢幕" onClick=${openProgram} />
      <${IconButton} icon="maximize" size=${16} title=${full ? "離開全螢幕（Esc）" : "全螢幕（雙擊畫面）"} onClick=${toggleFull} />
    </div>
  </div>`;
}
