// Preview player for the edit view: the stage (drawn by player-engine.js), the playback loop and the
// control bar. Playback time lives in lib/clock.js, so the timeline playhead and this player stay in
// step without re-rendering components on every frame.
import { html, useState, useEffect, useRef } from "../lib/h.js";
import { useStore, getState } from "../lib/store.js";
import { usePref } from "../lib/prefs.js";
import { useKeymap, keyHint } from "../lib/keymap.js";
import { clock, setTime, setPlaying, setDuration, onClock, togglePlay, shuttle } from "../lib/clock.js";
import { fmtTime, shotLabel } from "../lib/format.js";
import { sequenceDuration, clipIndexAt } from "../lib/sequence.js";
import { ops } from "../lib/actions.js";
import { legacySize, resolveStyle, usesStyle, dragScope, moveOps } from "../lib/caption-style.js";
import { createEngine } from "./player-engine.js";
import { Icon } from "./icons.js";
import { IconButton, Slider } from "./ui.js";
import { openMenu } from "./overlay.js";

const WAIT_LIMIT_MS = 4000;
const SEEK_JUMP = 0.05;
const DEFAULT_VIEW = { safe: false, thirds: false, captions: true };
const DEFAULT_VOLUME = { level: 1, muted: false };
const isBool = (v) => typeof v === "boolean";
const validView = (v) => !!v && isBool(v.safe) && isBool(v.thirds) && isBool(v.captions);
const validVolume = (v) => !!v && typeof v.level === "number" && v.level >= 0 && v.level <= 1 && isBool(v.muted);

// Advances the clock in real time while playing (backwards when clock.rate is -1). Seeks made
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

function viewMenu(e, view, setView) {
  const flip = (key) => () => setView({ ...view, [key]: !view[key] });
  openMenu(e, [
    { header: "預覽畫面" },
    { label: "顯示字幕", icon: "captions", checked: view.captions, onClick: flip("captions") },
    { label: "安全框（動作 93%、標題 90%）", icon: "frame", checked: view.safe, onClick: flip("safe") },
    { label: "三分構圖線", icon: "grid", checked: view.thirds, onClick: flip("thirds") },
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
};

// The engine plays media only forwards; backwards playback is drawn as a run of paused frames.
const rolling = (c) => c.playing && c.rate > 0;

function step(seconds) {
  setPlaying(false);
  setTime(clock.time + seconds);
}

export function Player({ project, clips, hotkeys = true }) {
  const legacy = useStore((s) => legacySize(s.settings));
  const gradeDraft = useStore((s) => s.gradeDraft);
  const [view, setView] = usePref("vc.player.view", DEFAULT_VIEW, validView);
  const [volume, setVolume] = usePref("vc.player.volume", DEFAULT_VOLUME, validVolume);
  const [loop, setLoop] = usePref("vc.player.loop", false, isBool);
  const [live, setLive] = useState({ playing: clock.playing, index: -1 });
  const [full, setFull] = useState(false);
  const rootRef = useRef(null);
  const hostRef = useRef(null);
  const timeRef = useRef(null);
  const engineRef = useRef(null);
  const clipsRef = useRef(clips);
  const liveRef = useRef(live);
  const loopRef = useRef(loop);
  clipsRef.current = clips;
  loopRef.current = loop;

  useEffect(() => {
    const engine = createEngine(hostRef.current, captionHooks);
    const loopCtl = createLoop(engine, loopRef);
    engineRef.current = engine;
    const off = onClock((c) => {
      loopCtl.onClock(c);
      engine.render(c.time, rolling(c));
      if (timeRef.current) timeRef.current.textContent = fmtTime(c.time);
      const index = clipIndexAt(clipsRef.current, c.time);
      if (liveRef.current.playing !== c.playing || liveRef.current.index !== index) {
        liveRef.current = { playing: c.playing, index };
        setLive(liveRef.current);
      }
    });
    const onFull = () => setFull(document.fullscreenElement === rootRef.current);
    document.addEventListener("fullscreenchange", onFull);
    return () => {
      off();
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
    engineRef.current.render(clock.time, rolling(clock));
    if (timeRef.current) timeRef.current.textContent = fmtTime(clock.time);
  }, [project, clips, legacy]);

  useEffect(() => {
    engineRef.current.setVolume(volume.level, volume.muted);
  }, [volume]);

  useEffect(() => {
    engineRef.current.setGradeDraft(gradeDraft);
  }, [gradeDraft]);

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
  const stageCls = ["pl-stage", view.safe && "show-safe", view.thirds && "show-thirds", view.captions && "show-caps"]
    .filter(Boolean).join(" ");
  const stageStyle = `--ar:${(width / height).toFixed(5)}`;
  const muted = volume.muted || volume.level === 0;

  return html`<div class=${`player ${full ? "is-full" : ""}`} ref=${rootRef}>
    <div class="pl-screen" onDblClick=${toggleFull}>
      <div class=${stageCls} style=${stageStyle} onClick=${() => clips.length && togglePlay()}>
        <div class="pl-host" ref=${hostRef}></div>
        <div class="pl-guides" aria-hidden="true">
          <i class="pl-safe-action"></i><i class="pl-safe-title"></i><i class="pl-thirds"></i>
        </div>
        ${!clips.length && html`<div class="pl-empty">
          <${Icon} name="film" size=${28} />
          <b>序列中還沒有鏡頭</b>
          <span>到分鏡頁新增鏡頭，或在鏡頭的「剪輯」分頁勾選「放入輸出序列」</span>
        </div>`}
      </div>
    </div>
    <div class="pl-bar">
      <${IconButton} icon="skip-back" size=${17} title=${`上一個鏡頭${keyHint("edit.prevEdit")}`} disabled=${!clips.length}
        onClick=${() => jumpClip(clips, -1)} />
      <${IconButton} icon=${live.playing ? "pause" : "play"} size=${20} kind="default" class="pl-play"
        title=${`${live.playing ? "暫停" : "播放"}${keyHint("edit.play")}`} disabled=${!clips.length} onClick=${togglePlay} />
      <${IconButton} icon="skip-forward" size=${17} title=${`下一個鏡頭${keyHint("edit.nextEdit")}`} disabled=${!clips.length}
        onClick=${() => jumpClip(clips, 1)} />
      <span class="pl-time mono"><span ref=${timeRef}>${fmtTime(clock.time)}</span><span class="dim"> / ${fmtTime(total)}</span></span>
      ${current && html`<span class="pl-clip dim" title=${shotLabel(current.shot, current.index)}>
        ${shotLabel(current.shot, current.index)}
      </span>`}
      <span class="grow"></span>
      <${IconButton} icon="refresh" size=${16} active=${loop} title=${loop ? "循環播放：開" : "循環播放：關"}
        onClick=${() => setLoop(!loop)} />
      <${IconButton} icon="frame" size=${16} active=${view.safe || view.thirds} title="預覽畫面選項"
        onClick=${(e) => viewMenu(e, view, setView)} />
      <div class="pl-vol">
        <${IconButton} icon=${muted ? "volume-x" : "volume"} size=${16} title=${muted ? "取消靜音" : "靜音"}
          onClick=${() => setVolume({ level: volume.level || 1, muted: !muted })} />
        <${Slider} value=${volume.muted ? 0 : volume.level} min=${0} max=${1} step=${0.05} class="pl-vol-range"
          onChange=${(level) => setVolume({ level, muted: false })} />
      </div>
      <${IconButton} icon="maximize" size=${16} title=${full ? "離開全螢幕（Esc）" : "全螢幕（雙擊畫面）"} onClick=${toggleFull} />
    </div>
  </div>`;
}
