// Video scopes beside the preview (GR-07): a luma waveform and a vectorscope of the picture the player shows,
// grades included (lib/scope-frame.js), redrawn ten times a second while the panel is open.
import { html, useState, useEffect, useRef } from "../lib/h.js";
import { keyHint } from "../lib/keymap.js";
import { grabFrame } from "../lib/scope-frame.js";
import { LEVELS, TARGETS, SKIN_ANGLE, waveform, vectorscope, paint, vectorColors } from "../lib/scopes.js";
import { IconButton } from "./ui.js";

const GRAB_W = 320; // the picture is measured at this width
const CELLS = LEVELS; // waveform columns and vectorscope grid, as many as the luma levels
const INTERVAL = 100;
const BG = "#0b0c0e";
const GRID = "rgba(255,255,255,0.16)";
const LABEL = "rgba(255,255,255,0.5)";
const TRACE = [150, 235, 170];

// The canvas sized to its box at the screen's pixel density; drawing is in CSS pixels.
function fit(canvas) {
  const dpr = window.devicePixelRatio || 1;
  const [w, h] = [canvas.clientWidth, canvas.clientHeight];
  if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) {
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);
  }
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.fillStyle = BG;
  ctx.fillRect(0, 0, w, h);
  ctx.font = "10px sans-serif";
  ctx.lineWidth = 1;
  return [ctx, w, h];
}

// Painted cells (CELLS x CELLS) stretched over a rectangle of ctx.
function blit(ctx, scratch, pixels, x, y, w, h) {
  if (scratch.width !== CELLS) scratch.width = scratch.height = CELLS;
  scratch.getContext("2d").putImageData(new ImageData(pixels, CELLS, CELLS), 0, 0);
  ctx.drawImage(scratch, x, y, w, h);
}

function line(ctx, x0, y0, x1, y1) {
  ctx.beginPath();
  ctx.moveTo(x0, y0);
  ctx.lineTo(x1, y1);
  ctx.stroke();
}

function drawWave(canvas, scratch, frame) {
  const [ctx, w, h] = fit(canvas);
  const top = 12;
  const span = h - 4 - top;
  if (frame) {
    const counts = waveform(frame.data, frame.width, frame.height, CELLS);
    blit(ctx, scratch, paint(counts, (frame.width * frame.height) / CELLS / 24, TRACE), 0, top, w, span);
  }
  ctx.strokeStyle = GRID;
  ctx.fillStyle = LABEL;
  ctx.textBaseline = "bottom";
  for (const p of [0, 25, 50, 75, 100]) {
    const y = Math.round(top + span * (1 - p / 100)) + 0.5;
    line(ctx, 0, y, w, y);
    ctx.fillText(String(p), 3, y - 1);
  }
}

function drawVector(canvas, scratch, frame, colors) {
  const [ctx, w, h] = fit(canvas);
  const size = Math.min(w, h) - 8;
  const [cx, cy] = [w / 2, h / 2];
  if (frame) {
    const counts = vectorscope(frame.data, CELLS);
    blit(ctx, scratch, paint(counts, (frame.width * frame.height) / 2000, colors), cx - size / 2, cy - size / 2, size, size);
  }
  ctx.strokeStyle = GRID;
  ctx.beginPath();
  ctx.arc(cx, cy, size / 2, 0, 2 * Math.PI);
  ctx.stroke();
  line(ctx, cx - size / 2, cy, cx + size / 2, cy);
  line(ctx, cx, cy - size / 2, cx, cy + size / 2);
  const a = (SKIN_ANGLE * Math.PI) / 180;
  ctx.strokeStyle = "rgba(255,200,150,0.45)";
  line(ctx, cx, cy, cx + (Math.cos(a) * size) / 2, cy - (Math.sin(a) * size) / 2);
  ctx.strokeStyle = LABEL;
  ctx.fillStyle = LABEL;
  ctx.textBaseline = "middle";
  for (const [label, cb, cr] of TARGETS) {
    const [x, y] = [cx + cb * size, cy - cr * size];
    ctx.strokeRect(x - 4, y - 4, 8, 8);
    ctx.textAlign = cb < 0 ? "right" : "left";
    ctx.fillText(label, x + (cb < 0 ? -7 : 7), y);
  }
  ctx.textAlign = "left";
}

export function Scopes({ onClose }) {
  const rootRef = useRef(null);
  const waveRef = useRef(null);
  const vecRef = useRef(null);
  const [error, setError] = useState("");

  useEffect(() => {
    const [raw, out, scratch] = [0, 1, 2].map(() => document.createElement("canvas"));
    const colors = vectorColors(CELLS);
    let raf = 0;
    let last = 0;
    const tick = (now) => {
      raf = requestAnimationFrame(tick);
      if (now - last < INTERVAL) return;
      last = now;
      try {
        const graded = rootRef.current.parentElement.querySelector(".pl-graded");
        const frame = graded ? grabFrame(graded, raw, out, GRAB_W) : null;
        drawWave(waveRef.current, scratch, frame);
        drawVector(vecRef.current, scratch, frame, colors);
      } catch (err) {
        cancelAnimationFrame(raf);
        console.warn("無法更新示波器", err);
        setError(err.message || String(err));
      }
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, []);

  return html`<aside class="scopes" ref=${rootRef} aria-label="示波器">
    <div class="scopes-head">
      <b>示波器</b>
      <${IconButton} icon="x" size=${14} title=${`關閉示波器${keyHint("edit.scopes")}`} onClick=${onClose} />
    </div>
    <div class="scope-label">亮度波形<span>0 黑到 100 白</span></div>
    <canvas class="scope-wave" ref=${waveRef}></canvas>
    <div class="scope-label">向量儀<span>方塊為 75% 標準色，斜線為膚色線</span></div>
    <canvas class="scope-vector" ref=${vecRef}></canvas>
    ${error ? html`<p class="scope-note is-error">無法讀取預覽畫面：${error}</p>`
      : html`<p class="scope-note">量的是預覽畫面（含調色、轉場、子母畫面），不含字幕、遮罩與片頭片尾淡入淡出。</p>`}
  </aside>`;
}
