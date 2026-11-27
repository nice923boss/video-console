// AudioWorklet of a clip's 壓縮器: compressor.js run on the clip's sound in the preview. The page posts the clip's
// `comp` settings (null passes the sound through, "end" lets the node go) and hears back the reduction in dB.
import { createCompressor } from "./compressor.js";

class CompressorProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.comp = null;
    this.tick = 0;
    this.alive = true;
    this.port.onmessage = (e) => {
      if (e.data === "end") this.alive = false;
      else this.comp = e.data ? createCompressor(e.data) : null;
    };
  }

  process(inputs, outputs) {
    if (!this.alive) return false;
    const inp = inputs[0], out = outputs[0];
    if (!inp.length) return true;
    const L = inp[0], R = inp[1] || inp[0];
    if (!this.comp) {
      out[0].set(L);
      out[1].set(R);
      return true;
    }
    this.comp.process(L, R, out[0], out[1]);
    if ((this.tick += 1) % 16 === 0) this.port.postMessage(this.comp.reduction);
    return true;
  }
}

registerProcessor("vc-compressor", CompressorProcessor);
