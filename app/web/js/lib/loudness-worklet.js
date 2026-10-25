// Runs the loudness meter (loudness.js) on the preview mix in the audio thread and posts its reading to the
// page ten times a second. The page sends "reset" to start a new measurement.
import { LoudnessMeter } from "./loudness.js";

class LoudnessProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.meter = new LoudnessMeter(sampleRate);
    this.every = Math.round(sampleRate / 10);
    this.count = 0;
    this.silence = null;
    this.port.onmessage = (e) => {
      if (e.data === "reset") this.meter.reset();
    };
  }

  // With nothing playing the input can arrive with no channels; that time still passes as silence.
  process(inputs) {
    const input = inputs[0];
    let n = 128;
    if (input && input.length) {
      this.meter.push(input);
      n = input[0].length;
    } else {
      if (!this.silence) this.silence = new Float32Array(128);
      this.meter.push([this.silence, this.silence]);
    }
    this.count += n;
    if (this.count >= this.every) {
      this.count = 0;
      this.port.postMessage(this.meter.read());
    }
    return true;
  }
}

registerProcessor("vc-loudness", LoudnessProcessor);
