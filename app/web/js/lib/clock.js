// Playback clock shared by the preview player and the timeline playhead. Kept outside the store so
// 60 fps updates move DOM nodes directly instead of re-rendering components.

const subs = new Set();

// rate: 1 plays forwards, -1 backwards; J/L pressed again doubles it up to 8x. Stopping resets it to 1.
export const clock = { time: 0, playing: false, duration: 0, rate: 1 };

function emit() {
  for (const fn of [...subs]) fn(clock);
}

export function setTime(t) {
  const next = Math.max(0, Math.min(Number(t) || 0, clock.duration || 0));
  clock.time = next;
  emit();
}

export function setPlaying(playing) {
  if (clock.playing === playing) return;
  clock.playing = playing;
  if (!playing) clock.rate = 1;
  emit();
}

// Play/pause. At the end, play stays there (the playhead is not sent back to the beginning) unless looping.
export function togglePlay() {
  setPlaying(!clock.playing);
}

const MAX_SHUTTLE = 8;

// J/L shuttle: dir -1 plays backwards, 1 forwards; pressing the same way again while playing goes 2x, 4x, 8x.
export function shuttle(dir) {
  if (dir < 0 && clock.time <= 0) {
    setPlaying(false);
    return;
  }
  const same = clock.playing && Math.sign(clock.rate) === dir;
  const rate = same ? dir * Math.min(MAX_SHUTTLE, Math.abs(clock.rate) * 2) : dir;
  if (clock.playing && clock.rate === rate) return;
  clock.rate = rate;
  clock.playing = true;
  emit();
}

export function setDuration(duration) {
  clock.duration = Math.max(0, Number(duration) || 0);
  if (clock.time > clock.duration) clock.time = clock.duration;
  emit();
}

export function onClock(fn) {
  subs.add(fn);
  return () => subs.delete(fn);
}
