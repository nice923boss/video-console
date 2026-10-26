// Clips on one track never overlap: a dragged clip lands in a free stretch of its track, and an edge stops
// at the clip beside it. `taken` is the other clips on that track as spans [{ s, e }] in timeline seconds.
const EPS = 1e-3;

// Where a clip `len` long, wanted at `want`, fits without overlapping any of `taken`: `want` itself when that
// stretch is free, else the nearest free stretch long enough, lined up with the clip it would have run into.
export function fitStart(want, len, taken) {
  const spans = [...taken].sort((a, b) => a.s - b.s);
  let best = Math.max(0, want);
  let dist = Infinity;
  let from = 0;
  for (const sp of [...spans, { s: Infinity, e: Infinity }]) {
    if (sp.s - from >= len - EPS) {
      const t = Math.min(Math.max(want, from), sp.s - len);
      if (Math.abs(t - want) < dist) {
        dist = Math.abs(t - want);
        best = t;
      }
    }
    from = Math.max(from, sp.e);
  }
  return best;
}

// How far the edges of the clip at start..end can go before they meet a neighbour: { lo, hi }.
// The overlays on one picture layer as spans, leaving out `skip` (the clip being dragged).
export function layerSpans(overlays, layer, skip) {
  return (overlays || []).filter((o) => o.id !== skip && o.layer === layer)
    .map((o) => ({ s: Number(o.start || 0), e: Number(o.start || 0) + Number(o.length || 0) }));
}

export function edgeRoom(start, end, taken) {
  const lo = Math.max(0, ...taken.filter((sp) => sp.e <= start + EPS).map((sp) => sp.e));
  const hi = Math.min(Infinity, ...taken.filter((sp) => sp.s >= end - EPS).map((sp) => sp.s));
  return { lo, hi };
}
