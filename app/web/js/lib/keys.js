// Keyboard shortcuts as a stack of layers: the most recently mounted layer wins, and a modal layer
// (dialogs, the command palette) hides everything below it.
import { useEffect, useRef } from "./h.js";

const layers = [];

function isTyping(el) {
  if (!el || el === document.body) return false;
  if (el.isContentEditable) return true;
  return el.tagName === "INPUT" || el.tagName === "TEXTAREA" || el.tagName === "SELECT";
}

export function comboOf(e) {
  let key = e.key === " " ? "space" : (e.key || "").toLowerCase();
  if (key === "esc") key = "escape";
  // Option on a Mac turns letters into symbols (Option+C types "ç"), and Shift turns digits into symbols
  // (Ctrl+Shift+8 arrives as "*"), so read the physical key instead.
  const shiftedMod = e.shiftKey && (e.ctrlKey || e.metaKey);
  if ((e.altKey || shiftedMod) && /^(Key[A-Z]|Digit[0-9])$/.test(e.code || "")) key = e.code.slice(-1).toLowerCase();
  const parts = [];
  if (e.ctrlKey || e.metaKey) parts.push("mod");
  if (e.altKey) parts.push("alt");
  // Shift is part of printable symbols like "?", so only count it for named keys and letters.
  if (e.shiftKey && (key.length > 1 || /[a-z0-9]/.test(key))) parts.push("shift");
  parts.push(key);
  return parts.join("+");
}

// "mod++" is Ctrl with the "+" key, so a trailing "+" is the key itself, not a separator.
export function comboParts(combo) {
  if (!combo.endsWith("+")) return combo.split("+");
  return [...combo.slice(0, -1).split("+").filter(Boolean), "+"];
}

function onKeyDown(e) {
  if (e.isComposing || e.keyCode === 229) return; // IME composition (Chinese input) owns the keys
  const combo = comboOf(e);
  const typing = isTyping(e.target);
  const onButton = e.target && e.target.tagName === "BUTTON";
  for (let i = layers.length - 1; i >= 0; i -= 1) {
    const layer = layers[i];
    const entry = layer.map.current[combo];
    if (entry) {
      const fn = typeof entry === "function" ? entry : entry.fn;
      const allowInInput = typeof entry === "object" && entry.allowInInput;
      if ((typing && !allowInInput) || (onButton && (combo === "space" || combo === "enter"))) {
        if (layer.modal) return;
        continue;
      }
      const handled = fn(e);
      if (handled !== false) {
        e.preventDefault();
        e.stopPropagation();
        return;
      }
    }
    if (layer.modal) return;
  }
}

document.addEventListener("keydown", onKeyDown);

// A pointer press that calls preventDefault keeps focus where it was, so a button clicked earlier would
// take the next Space or Enter instead of the shortcuts. Drop that focus the way a plain click would.
export function releaseFocus() {
  const el = document.activeElement;
  if (el && el !== document.body && typeof el.blur === "function") el.blur();
}

// map: {"mod+z": fn, "escape": {fn, allowInInput: true}}; a handler returning false lets the key through.
export function useHotkeys(map, { active = true, modal = false } = {}) {
  const ref = useRef(map);
  ref.current = map;
  useEffect(() => {
    if (!active) return undefined;
    const layer = { map: ref, modal };
    layers.push(layer);
    return () => {
      const i = layers.indexOf(layer);
      if (i >= 0) layers.splice(i, 1);
    };
  }, [active, modal]);
}

export const MOD_LABEL = navigator.platform && /Mac/.test(navigator.platform) ? "⌘" : "Ctrl";
