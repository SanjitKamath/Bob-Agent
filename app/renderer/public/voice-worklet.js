// Runs on the audio thread: collects the microphone's samples into 40 ms frames (640 samples at
// 16 kHz) and hands each to the page. Nothing else happens to the sound here.
class Framer extends AudioWorkletProcessor {
  constructor() {
    super();
    this.frame = new Float32Array(640);
    this.filled = 0;
  }

  process(inputs) {
    const channel = inputs[0] && inputs[0][0];
    if (channel) {
      for (let i = 0; i < channel.length; i += 1) {
        this.frame[this.filled++] = channel[i];
        if (this.filled === this.frame.length) {
          this.port.postMessage(this.frame.slice());
          this.filled = 0;
        }
      }
    }
    return true;
  }
}

registerProcessor("bob-framer", Framer);
