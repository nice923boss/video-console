// Visible-range rendering (PF-02): a long sequence only builds the clips near the screen. The window covers
// the part on screen plus one screen on each side and moves a whole screen at a time, so scrolling re-renders
// the timeline once per screen instead of once per frame.
import { useEffect, useState } from "../lib/h.js";

// Null (draw everything) until the timeline knows its width.
export function useViewWindow(scrollRef, viewW, pps, anchorRef) {
  const [, setPage] = useState(0);
  useEffect(() => {
    const sc = scrollRef.current;
    if (!sc || !viewW) return undefined;
    const onScroll = () => setPage(Math.floor(sc.scrollLeft / viewW));
    sc.addEventListener("scroll", onScroll);
    return () => sc.removeEventListener("scroll", onScroll);
  }, [viewW]);
  const sc = scrollRef.current;
  if (!sc || !viewW) return null;
  // Right after a zoom the scroll position is only moved to its anchor once this render is on screen.
  const a = anchorRef.current;
  const left = a ? Math.max(0, a.time * pps - a.offset) : sc.scrollLeft;
  const page = Math.floor(left / viewW);
  return { from: ((page - 1) * viewW) / pps, to: ((page + 2) * viewW) / pps };
}

export const inView = (win, start, length) => !win || (start < win.to && start + Math.max(0, length) > win.from);
