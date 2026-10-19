// Playback clock shared by the preview player and the timeline playhead. Kept outside the store so
// 60 fps updates move DOM nodes directly instead of re-rendering components.

const subs = new Set();

// rate: 1 plays forwards, -1 backwards (J/K/L); stopping resets it to forwards.
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

// Play/pause; pressing play at the end starts again from the beginning.
export function togglePlay() {
  if (!clock.playing && clock.duration > 0 && clock.time >= clock.duration - 0.05) setTime(0);
  setPlaying(!clock.playing);
}

// J/K/L shuttle at normal speed: dir -1 plays backwards, 1 forwards.
export function shuttle(dir) {
  if (dir < 0 && clock.time <= 0) {
    setPlaying(false);
    return;
  }
  if (dir > 0 && !clock.playing && clock.duration > 0 && clock.time >= clock.duration - 0.05) clock.time = 0;
  if (clock.playing && clock.rate === dir) return;
  clock.rate = dir;
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
