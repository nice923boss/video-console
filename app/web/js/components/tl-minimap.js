// Timeline overview (UI-12): the whole sequence as one strip of shot pictures under the tracks, shown once
// the zoom makes it wider than the window. The box marks the part on screen; pressing or dragging on the
// strip moves the playhead there and scrolls that spot to the middle.
import { html, useEffect, useRef } from "../lib/h.js";
import { clock, setTime, onClock } from "../lib/clock.js";
import { fileUrl, clamp } from "../lib/format.js";
import { dragPointer } from "./tl-tracks.js";

const pct = (v) => `${(clamp(v, 0, 1) * 100).toFixed(3)}%`;

export function Minimap({ pid, clips, span, pps, viewW, scrollRef, labelW }) {
  const stripRef = useRef(null);
  const boxRef = useRef(null);
  const headRef = useRef(null);
  const live = useRef(null);
  live.current = { span, pps, viewW };

  // The box and the playhead follow the scroll and the clock directly, without a render per frame.
  useEffect(() => {
    const sc = scrollRef.current;
    const place = () => {
      const box = boxRef.current;
      if (!box) return;
      box.style.left = pct(sc.scrollLeft / pps / span);
      box.style.width = pct(viewW / pps / span);
    };
    place();
    sc.addEventListener("scroll", place);
    return () => sc.removeEventListener("scroll", place);
  }, [span, pps, viewW]);
  useEffect(() => onClock((c) => {
    if (headRef.current) headRef.current.style.left = pct(c.time / live.current.span);
  }), []);

  const jump = (ev) => {
    const now = live.current;
    const r = stripRef.current.getBoundingClientRect();
    const t = clamp((ev.clientX - r.left) / r.width, 0, 1) * now.span;
    setTime(t);
    scrollRef.current.scrollLeft = Math.max(0, t * now.pps - now.viewW / 2);
  };
  const onDown = (e) => {
    if (e.button !== 0) return;
    jump(e);
    dragPointer(e, { threshold: 0, onMove: (dx, ev) => jump(ev) });
  };

  return html`<div class="tl-mini">
    <span class="tl-mini-label dim" style=${`width:${labelW}px`}>總覽</span>
    <div class="tl-mini-strip" ref=${stripRef} onPointerDown=${onDown} title="整段序列：按一下或拖曳跳到該處">
      ${clips.map((c) => {
        const path = c.take && (c.take.thumb || (c.source === "image" ? c.take.file : ""));
        return html`<i key=${c.shot.id} class="tl-mini-clip"
          style=${`left:${pct(c.start / span)};width:${pct(c.length / span)}${path ? `;background-image:url("${fileUrl(pid, path)}")` : ""}`}></i>`;
      })}
      <div class="tl-mini-box" ref=${boxRef}></div>
      <div class="tl-mini-head" ref=${headRef} style=${`left:${pct(clock.time / span)}`}></div>
    </div>
  </div>`;
}
