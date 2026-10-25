// Global app state: one plain object replaced on every change, plus a selector hook.
import { useState, useEffect, useRef } from "./h.js";

const INITIAL = {
  boot: "loading",          // loading | ready | error | closed
  bootError: null,
  connected: false,
  instance: null,
  version: "",
  settings: null,
  secrets: {},
  projects: [],
  jobs: [],
  queue: { paused: false, counts: {}, active: 0 },
  comfy: [],
  workflows: [],
  providers: [],
  presets: {},
  system: {},
  pid: null,                // open project id
  project: null,            // open project document
  undo: { undo: 0, redo: 0, undoLabel: "", redoLabel: "" },
  view: "home",
  selected: [],             // selected shot ids (board / inspector)
  focus: null,              // shot id the inspector shows
  editSel: null,            // timeline selection: {type: "shot"|"audio"|"caption", id}
  inspectorTab: "content",
  libraryTab: "media",      // media | exports
  enginesTab: "routes",
  providerSel: null,        // provider id shown in the engines page cloud API tab
  wfCheck: null,            // {sid, at}: ask the workflows tab to run a compatibility check
  voice: null,              // Audio8Voice for the cast editor: {loading, status, voices, presets, error}
  captionDragScope: null,   // "one" | "style" once picked; null: the viewer's saved choice
  gradeDraft: null,         // {id, grade}: an adjustment layer's or a shot's grade while its panel control is dragged
  gradeCompare: false,      // the preview shows the picture before grading on its left half (GR-06)
  maskDraft: null,          // {id, masks}: a shot's blur and mosaic boxes while a panel slider is dragged (GR-12)
  editRange: null,          // {in, out}: the timeline range marked with I and O (seconds)
  clipMarks: null,          // {pid, times}: where the preview mix went over 0 dB while playing (audio-meter.js)
  solo: null,               // {pid, keys}: timeline rows heard alone in the preview; never saved or exported (MX-03)
  audioSel: null,           // audio clip ids Ctrl+clicked together; counts only while it holds editSel (tl-tracks.js)
  editTool: "select",       // timeline click tool: "select" | "trim" | "blade"
  keyPanel: false,          // the keyframe list in the edit properties panel is open
  watch: false,             // watch mode (lib/watch.js): this window only shows the open project
};

let state = { ...INITIAL };
const listeners = new Set();

export function getState() {
  return state;
}

export function setState(patch) {
  const next = typeof patch === "function" ? patch(state) : patch;
  if (!next) return;
  state = { ...state, ...next };
  for (const fn of [...listeners]) fn(state);
}

export function subscribe(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

export function shallowEqual(a, b) {
  if (Object.is(a, b)) return true;
  if (!a || !b || typeof a !== "object" || typeof b !== "object") return false;
  if (Array.isArray(a) !== Array.isArray(b)) return false;
  const ka = Object.keys(a);
  const kb = Object.keys(b);
  if (ka.length !== kb.length) return false;
  for (const k of ka) {
    if (!Object.prototype.hasOwnProperty.call(b, k) || !Object.is(a[k], b[k])) return false;
  }
  return true;
}

// Re-render only when the selected slice changes (shallow compare by default).
export function useStore(selector, eq = shallowEqual) {
  const [, force] = useState(0);
  const selRef = useRef(selector);
  selRef.current = selector;
  const valRef = useRef();
  valRef.current = selector(state);
  useEffect(() => {
    const check = (s) => {
      const next = selRef.current(s);
      if (!eq(valRef.current, next)) {
        valRef.current = next;
        force((n) => n + 1);
      }
    };
    const off = subscribe(check);
    check(state); // catch a change between render and subscribe
    return off;
  }, []);
  return valRef.current;
}

// Convenience selectors used across views.
export const selectProject = (s) => s.project;
export const selectSettings = (s) => s.settings;
