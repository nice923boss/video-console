// Store-only ZIP with UTF-8 names for 打包回傳 (page.js). No ZIP64: the page keeps the total under 4 GB.
// makeZip(entries: [{ name, blob }], onProgress(0-1)) -> Blob
const makeZip = (() => {
  "use strict";
  const CRC = new Uint32Array(256).map((_, n) => {
    let c = n;
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    return c >>> 0;
  });
  async function crcOf(blob, onBytes) {
    let c = 0xffffffff;
    for (let pos = 0; pos < blob.size; pos += 8 * 1024 * 1024) {
      const buf = new Uint8Array(await blob.slice(pos, pos + 8 * 1024 * 1024).arrayBuffer());
      for (let i = 0; i < buf.length; i++) c = CRC[(c ^ buf[i]) & 0xff] ^ (c >>> 8);
      onBytes(buf.length);
    }
    return (c ^ 0xffffffff) >>> 0;
  }
  return async function makeZip(entries, onProgress) {
    const enc = new TextEncoder();
    const d = new Date();
    const time = (d.getHours() << 11) | (d.getMinutes() << 5) | (d.getSeconds() >> 1);
    const date = ((d.getFullYear() - 1980) << 9) | ((d.getMonth() + 1) << 5) | d.getDate();
    const total = entries.reduce((n, e) => n + e.blob.size, 0) || 1;
    const parts = [];
    const central = [];
    let offset = 0;
    let done = 0;
    for (const e of entries) {
      const name = enc.encode(e.name);
      const size = e.blob.size;
      const crc = await crcOf(e.blob, (n) => { done += n; onProgress(done / total); });
      const local = new DataView(new ArrayBuffer(30));
      [[0, 0x04034b50, 4], [4, 20, 2], [6, 0x0800, 2], [8, 0, 2], [10, time, 2], [12, date, 2], [14, crc, 4],
        [18, size, 4], [22, size, 4], [26, name.length, 2], [28, 0, 2]]
        .forEach(([at, v, n]) => (n === 4 ? local.setUint32(at, v, true) : local.setUint16(at, v, true)));
      const cen = new DataView(new ArrayBuffer(46));
      [[0, 0x02014b50, 4], [4, 20, 2], [6, 20, 2], [8, 0x0800, 2], [10, 0, 2], [12, time, 2], [14, date, 2],
        [16, crc, 4], [20, size, 4], [24, size, 4], [28, name.length, 2], [42, offset, 4]]
        .forEach(([at, v, n]) => (n === 4 ? cen.setUint32(at, v, true) : cen.setUint16(at, v, true)));
      parts.push(local, name, e.blob);
      central.push(cen, name);
      offset += 30 + name.length + size;
    }
    const cdSize = central.reduce((n, p) => n + p.byteLength, 0);
    const end = new DataView(new ArrayBuffer(22));
    end.setUint32(0, 0x06054b50, true);
    end.setUint16(8, entries.length, true);
    end.setUint16(10, entries.length, true);
    end.setUint32(12, cdSize, true);
    end.setUint32(16, offset, true);
    return new Blob([...parts, ...central, end], { type: "application/zip" });
  };
})();
