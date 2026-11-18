// Listening: the microphone in, words out. The controller has no React and no browser in it, so
// it can be tested with a stand-in microphone and a stand-in bridge.
import {
  type EngineEvent,
  Endpointer,
  rms,
  toPcm16,
  type VoiceErrorCode,
  type VoiceEvent,
  type VoiceStart,
  Words,
} from "../../../shared/voice";

export type VoicePhase = "off" | "starting" | "listening" | "stopping";

export interface VoiceState {
  phase: VoicePhase;
  /** Settled words followed by what is being heard. */
  text: string;
  /** 0 to 1, how loud it is. */
  level: number;
  error: string;
  errorCode: VoiceErrorCode | "";
  /** "Downloading the speech model… 40%" the first time. */
  progress: string;
}

export const IDLE: VoiceState = { phase: "off", text: "", level: 0, error: "", errorCode: "", progress: "" };

export interface Capture {
  stop(): void;
}

/** The part of the app bridge that listening uses. */
export interface VoiceBridge {
  start(): Promise<VoiceStart>;
  audio(frame: ArrayBuffer): void;
  stop(): Promise<void>;
  cancel(): Promise<void>;
  onEvent(listener: (event: VoiceEvent) => void): () => void;
}

export interface ControllerOptions {
  bridge: VoiceBridge;
  /** Opens the microphone; each call to ``onFrame`` carries 40 ms of 16 kHz samples. */
  capture: (onFrame: (samples: Float32Array) => void) => Promise<Capture>;
  onChange: (state: VoiceState) => void;
  /** A message ended by a pause (only when ``autoEnd``): the words, settled. */
  onUtterance?: (text: string) => void;
  pauseMs: () => number;
  /** Send on pause. Off means you stop it yourself. */
  autoEnd: () => boolean;
  now?: () => number;
  /** With nothing said for this long, listening stops by itself. */
  idleMs?: number;
}

export class VoiceController {
  private state: VoiceState = IDLE;
  private words = new Words();
  private endpoint: Endpointer | null = null;
  private capture: Capture | null = null;
  private closing: (() => void)[] = [];
  private token = 0;
  private frames = 0;
  private readonly unsubscribe: () => void;

  constructor(private readonly options: ControllerOptions) {
    this.unsubscribe = options.bridge.onEvent((event) => this.onEvent(event));
  }

  get current(): VoiceState {
    return this.state;
  }

  dispose(): void {
    this.unsubscribe();
    void this.cancel();
  }

  async start(): Promise<void> {
    if (this.state.phase !== "off") return;
    const token = ++this.token;
    this.words.reset();
    this.endpoint = new Endpointer({
      pauseMs: this.options.pauseMs(),
      idleMs: this.options.idleMs ?? 60_000,
    });
    this.frames = 0;
    this.set({ ...IDLE, phase: "starting" });
    const started = await this.options.bridge.start();
    if (token !== this.token) {
      if (started.ok) void this.options.bridge.cancel(); // cancelled while it was starting
      return;
    }
    if (!started.ok) {
      this.set({ ...IDLE, error: started.message, errorCode: started.code });
      return;
    }
    try {
      const capture = await this.options.capture((samples) => this.onFrame(samples));
      if (token !== this.token) {
        capture.stop();
        return;
      }
      this.capture = capture;
      this.set({ ...this.state, phase: "listening", progress: "" });
    } catch (error) {
      void this.options.bridge.cancel();
      this.set({
        ...IDLE,
        error: `The microphone could not be opened: ${error instanceof Error ? error.message : String(error)}`,
        errorCode: "failed",
      });
    }
  }

  /** Stop listening and settle the last words. Resolves with everything that was heard. */
  async finish(): Promise<string> {
    if (this.state.phase === "off") return this.state.text;
    if (this.state.phase === "starting") {
      await this.cancel();
      return "";
    }
    if (this.state.phase === "stopping") return this.waitClosed();
    this.set({ ...this.state, phase: "stopping", level: 0 });
    this.closeCapture();
    const closed = this.waitClosed();
    void this.options.bridge.stop();
    const text = await Promise.race([closed, new Promise<string>((resolve) => setTimeout(() => resolve(this.words.text), 6000))]);
    this.end(text);
    return text;
  }

  /** Stop at once and drop what was heard. */
  async cancel(): Promise<void> {
    this.token += 1;
    const wasOff = this.state.phase === "off";
    this.closeCapture();
    this.words.reset();
    this.set({ ...IDLE, error: this.state.error, errorCode: this.state.errorCode });
    if (!wasOff) await this.options.bridge.cancel();
  }

  clearError(): void {
    if (this.state.error) this.set({ ...this.state, error: "", errorCode: "" });
  }

  // -- internals ---------------------------------------------------------------------------------

  private onFrame(samples: Float32Array): void {
    if (this.state.phase !== "listening") return;
    const bytes = toPcm16(samples);
    this.options.bridge.audio(bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength) as ArrayBuffer);
    const level = Math.min(1, rms(samples) * 6);
    this.frames += 1;
    const result = this.endpoint?.push((this.options.now ?? Date.now)(), rms(samples), !this.words.isEmpty) ?? null;
    if (this.frames % 3 === 0) this.set({ ...this.state, level });
    if (result === "idle") {
      void this.cancel();
      this.set({ ...IDLE, error: "Stopped listening: nothing was said." });
    } else if (result === "end" && this.options.autoEnd()) {
      void this.finish().then((text) => text.trim() && this.options.onUtterance?.(text.trim()));
    }
  }

  private onEvent(event: VoiceEvent): void {
    if (this.state.phase === "off") return;
    switch (event.type) {
      case "partial":
        this.words.partial(event.text);
        this.set({ ...this.state, text: this.words.text });
        break;
      case "final":
        this.words.final(event.text);
        this.set({ ...this.state, text: this.words.text });
        break;
      case "progress":
        this.set({
          ...this.state,
          progress:
            event.what === "downloading"
              ? `Downloading the speech model… ${Math.round(event.fraction * 100)}%`
              : "Loading the speech model…",
        });
        break;
      case "error": {
        const code = (event as Extract<EngineEvent, { type: "error" }>).code;
        this.closeCapture();
        this.words.reset();
        this.set({ ...IDLE, error: event.message, errorCode: code === "denied" ? "denied" : "failed" });
        for (const done of this.closing.splice(0)) done();
        break;
      }
      case "closed":
        if (this.state.phase === "listening") {
          // The helper went away on its own: keep what was heard.
          this.closeCapture();
          this.end(this.words.text);
        }
        for (const done of this.closing.splice(0)) done();
        break;
      default:
        break;
    }
  }

  private waitClosed(): Promise<string> {
    return new Promise((resolve) => this.closing.push(() => resolve(this.words.text)));
  }

  private end(text: string): void {
    this.set({ ...IDLE, text });
  }

  private closeCapture(): void {
    this.capture?.stop();
    this.capture = null;
  }

  private set(next: VoiceState): void {
    this.state = next;
    this.options.onChange(next);
  }
}

/** The real microphone: 16 kHz mono through the audio worklet. */
export async function openMicrophone(deviceId: string, onFrame: (samples: Float32Array) => void): Promise<Capture> {
  const stream = await navigator.mediaDevices.getUserMedia({
    audio: {
      ...(deviceId ? { deviceId: { exact: deviceId } } : {}),
      channelCount: 1,
      echoCancellation: true,
      noiseSuppression: true,
    },
  });
  const context = new AudioContext({ sampleRate: 16000 });
  try {
    await context.audioWorklet.addModule(new URL("voice-worklet.js", document.baseURI).href);
  } catch (error) {
    stream.getTracks().forEach((track) => track.stop());
    void context.close();
    throw error;
  }
  const source = context.createMediaStreamSource(stream);
  const node = new AudioWorkletNode(context, "bob-framer", { numberOfInputs: 1, numberOfOutputs: 0 });
  node.port.onmessage = (message: MessageEvent<Float32Array>) => onFrame(message.data);
  source.connect(node);
  return {
    stop() {
      node.port.onmessage = null;
      source.disconnect();
      node.disconnect();
      stream.getTracks().forEach((track) => track.stop());
      void context.close();
    },
  };
}
