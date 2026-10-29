// Decoded sound for the preview (PB-01): the plain WAV copies (and the stems of a separated voice) are read by byte
// range in 5-second blocks, decoded to samples and kept as AudioBuffers, the most recently used up to a byte budget,
// so the scheduler can start any sound on an exact sample with nothing left to load.
export const RATE = 48000;
export const BLOCK = 5 * RATE; // frames per block
const BUDGET = 256 * 1024 * 1024; // bytes of decoded samples kept (32-bit floats)
const HEAD = 4096; // first read; a long LIST chunk before the samples is read again larger
const RETRY_MS = 10000; // a file that could not be read is tried again after this long

const files = new Map(); // url -> { info, promise, failedAt }
const blocks = new Map(); // `${i}|${url}` -> { buffer, promise, bytes }; insertion order is the use order
let held = 0;

// { channels, float, offset, frames } of a PCM WAV from its first bytes; total: the whole file's size. Walks the
// chunks, since ffmpeg may put a LIST chunk before the samples. null when the samples start past these bytes.
export function parseHead(buf, total) {
  const v = new DataView(buf);
  const tag = (p) => String.fromCharCode(v.getUint8(p), v.getUint8(p + 1), v.getUint8(p + 2), v.getUint8(p + 3));
  if (buf.byteLength < 12 || tag(0) !== "RIFF" || tag(8) !== "WAVE") throw new Error("不是 WAV 檔");
  let pos = 12, fmt = null;
  while (pos + 8 <= buf.byteLength) {
    const id = tag(pos), size = v.getUint32(pos + 4, true);
    if (id === "fmt ") {
      let format = v.getUint16(pos + 8, true);
      if (format === 0xfffe && size >= 40) format = v.getUint16(pos + 32, true); // WAVE_FORMAT_EXTENSIBLE
      fmt = { format, channels: v.getUint16(pos + 10, true), rate: v.getUint32(pos + 12, true),
              bits: v.getUint16(pos + 22, true) };
    } else if (id === "data") {
      if (!fmt) throw new Error("WAV 檔缺少格式資訊");
      const float = fmt.format === 3 && fmt.bits === 32;
      if (!float && !(fmt.format === 1 && fmt.bits === 16)) throw new Error(`不支援的 WAV 格式 ${fmt.format}/${fmt.bits}`);
      if (fmt.rate !== RATE) throw new Error(`WAV 取樣率 ${fmt.rate} 不是 ${RATE}`);
      const align = fmt.channels * (fmt.bits / 8), offset = pos + 8;
      // A file whose header was never finished says 0 or 0xFFFFFFFF; the bytes on disk are the truth then.
      const bytes = size && size !== 0xffffffff ? Math.min(size, total - offset) : total - offset;
      return { channels: fmt.channels, float, offset, frames: Math.max(0, Math.floor(bytes / align)) };
    }
    pos += 8 + size + (size & 1);
  }
  return null;
}

// One Float32Array per channel from interleaved little-endian samples.
export function deinterleave(buf, channels, float) {
  const src = float ? new Float32Array(buf) : new Int16Array(buf);
  const n = Math.floor(src.length / channels), scale = float ? 1 : 1 / 32768;
  const out = Array.from({ length: channels }, () => new Float32Array(n));
  for (let f = 0, k = 0; f < n; f++) for (let c = 0; c < channels; c++) out[c][f] = src[k++] * scale;
  return out;
}

async function readRange(url, start, end) {
  const res = await fetch(url, { headers: { Range: `bytes=${start}-${end}` } });
  if (res.status !== 206 && res.status !== 200) throw new Error(`讀取聲音檔失敗（HTTP ${res.status}）`);
  const total = Number((res.headers.get("Content-Range") || "").split("/")[1]) || Number(res.headers.get("Content-Length"));
  let buf = await res.arrayBuffer();
  if (res.status === 200) buf = buf.slice(start, end + 1); // the server ignored the range
  return { buf, total };
}

async function readInfo(url) {
  for (let size = HEAD; ; size *= 8) {
    const { buf, total } = await readRange(url, 0, size - 1);
    const info = parseHead(buf, total);
    if (info) return info;
    if (buf.byteLength < size) throw new Error("WAV 檔沒有聲音資料");
  }
}

// The file's { channels, float, offset, frames } once read; null while it is being read or could not be read
// (failed() tells which). Starts the read.
export function info(url) {
  let f = files.get(url);
  if (f?.failedAt && performance.now() - f.failedAt > RETRY_MS) f = null;
  if (!f) {
    f = { info: null, failedAt: 0, promise: null };
    files.set(url, f);
    f.promise = readInfo(url).then((got) => { f.info = got; return got; }, (err) => {
      f.failedAt = performance.now();
      console.warn("預覽聲音檔無法讀取", url, err);
      return null;
    });
  }
  return f.info;
}

export const failed = (url) => Boolean(files.get(url)?.failedAt);

function touch(key) {
  const b = blocks.get(key);
  if (b) { blocks.delete(key); blocks.set(key, b); }
  return b;
}

function trim() {
  for (const [key, b] of blocks) {
    if (held <= BUDGET) break;
    if (!b.buffer) continue; // still loading
    blocks.delete(key);
    held -= b.bytes;
  }
}

// Block i of the file as an AudioBuffer, or null while it loads (the load is started). A playing source keeps its
// buffer even after the cache lets it go.
export function peek(url, i) {
  const key = `${i}|${url}`;
  const b = touch(key);
  if (b) return b.buffer;
  const meta = info(url);
  if (!meta || i < 0 || i * BLOCK >= meta.frames) return null;
  const entry = { buffer: null, bytes: 0, promise: null };
  blocks.set(key, entry);
  const first = i * BLOCK, n = Math.min(BLOCK, meta.frames - first);
  const align = meta.channels * (meta.float ? 4 : 2);
  entry.promise = readRange(url, meta.offset + first * align, meta.offset + (first + n) * align - 1).then(({ buf }) => {
    const chans = deinterleave(buf, meta.channels, meta.float);
    const out = new AudioBuffer({ length: Math.max(1, chans[0].length), numberOfChannels: meta.channels, sampleRate: RATE });
    chans.forEach((data, c) => out.copyToChannel(data, c));
    if (blocks.get(key) === entry) {
      entry.buffer = out;
      entry.bytes = out.length * meta.channels * 4;
      held += entry.bytes;
      trim();
    }
    return out;
  }, (err) => {
    if (blocks.get(key) === entry) blocks.delete(key); // asked again on a later frame
    console.warn("預覽聲音區塊無法讀取", url, i, err);
    return null;
  });
  return null;
}

// Forget the files no longer played (a relinked file gets a new copy under the same name).
export function keepOnly(urls) {
  for (const url of files.keys()) if (!urls.has(url)) files.delete(url);
  for (const [key, b] of blocks) {
    if (urls.has(key.slice(key.indexOf("|") + 1))) continue;
    blocks.delete(key);
    held -= b.bytes;
  }
}
