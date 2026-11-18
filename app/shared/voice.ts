// Speech to text: what the renderer, the main process and the speech helper agree on.
//
// Audio is captured in the window (so the microphone permission belongs to Bob.app), sent to the
// main process, and from there to a small helper (voice/, Python) that runs Whisper on this Mac.
// Nothing is written to disk and nothing leaves the machine.

/** What you can set in Settings → General → Voice. */
export interface VoicePrefs {
  /** Whisper's size: base is quick and small is more accurate (and larger to store). */
  model: "base" | "small";
  /** A BCP 47 language tag, for example en-US. */
  language: string;
  /** An Electron accelerator; empty means no hotkey. */
  hotkey: string;
  /** Silence that ends a spoken message in conversation mode. */
  pauseMs: number;
  /** Send each message when you pause, and listen again once Bob has answered. */
  conversation: boolean;
  /** An input device id from the browser; empty means the Mac's default microphone. */
  inputDevice: string;
}

export const DEFAULT_VOICE: VoicePrefs = {
  model: "base",
  language: "en-US",
  hotkey: "Alt+Shift+Space",
  pauseMs: 1200,
  conversation: false,
  inputDevice: "",
};

export const PAUSE_CHOICES = [800, 1200, 2000] as const;

export const MODEL_CHOICES = [
  { value: "base", label: "Quick (base)" },
  { value: "small", label: "More accurate (small)" },
] as const;

/** The Whisper model a language and size use, and how big it is: English has its own models. */
export function whisperModel(language: string, size: VoicePrefs["model"]): { name: string; megabytes: number } {
  const english = language.toLowerCase().startsWith("en");
  return { name: english ? `${size}.en` : size, megabytes: size === "small" ? 488 : 148 };
}

const LANGUAGE = /^[a-z]{2,3}(-[A-Za-z0-9]{2,8}){0,2}$/;

export function mergeVoice(base: VoicePrefs, patch: unknown): VoicePrefs {
  const input = (patch && typeof patch === "object" ? patch : {}) as Partial<VoicePrefs>;
  return {
    model: input.model === "base" || input.model === "small" ? input.model : base.model,
    language: typeof input.language === "string" && LANGUAGE.test(input.language) ? input.language : base.language,
    hotkey: typeof input.hotkey === "string" && input.hotkey.length <= 40 ? input.hotkey.trim() : base.hotkey,
    pauseMs:
      typeof input.pauseMs === "number" && input.pauseMs >= 400 && input.pauseMs <= 5000
        ? Math.round(input.pauseMs)
        : base.pauseMs,
    conversation: typeof input.conversation === "boolean" ? input.conversation : base.conversation,
    inputDevice: typeof input.inputDevice === "string" ? input.inputDevice.slice(0, 200) : base.inputDevice,
  };
}

// -- the wire format ---------------------------------------------------------------------------

/** Audio sent to the helper: 16 kHz, mono, signed 16-bit little-endian. */
export const SAMPLE_RATE = 16000;
/** One frame is 40 ms of audio. */
export const FRAME_SAMPLES = 640;

const COMMAND = 1;
const AUDIO = 2;
/** The largest payload either side will accept; anything bigger is a broken stream. */
export const MAX_PAYLOAD = 1 << 20;

export type EngineCommand =
  | { cmd: "start"; locale: string; model: VoicePrefs["model"]; vocabulary?: string[] }
  | { cmd: "stop" }
  | { cmd: "cancel" };

/** A frame on the helper's standard input: one byte of kind, four of length, then the payload. */
export function frame(kind: "command" | "audio", payload: Uint8Array): Uint8Array {
  if (payload.byteLength > MAX_PAYLOAD) throw new Error("frame is too large");
  const out = new Uint8Array(5 + payload.byteLength);
  out[0] = kind === "command" ? COMMAND : AUDIO;
  new DataView(out.buffer).setUint32(1, payload.byteLength, true);
  out.set(payload, 5);
  return out;
}

export function commandFrame(command: EngineCommand): Uint8Array {
  return frame("command", new TextEncoder().encode(JSON.stringify(command)));
}

/** What the helper writes on its standard output, one JSON object per line. */
export type EngineEvent =
  | { type: "ready"; engine: string; locale: string }
  | { type: "progress"; what: string; fraction: number }
  /** The words being heard now; each partial replaces the one before it. */
  | { type: "partial"; text: string }
  /** Words that are settled; each final is added after the ones before it. */
  | { type: "final"; text: string }
  | { type: "error"; code: string; message: string }
  | { type: "closed" };

const EVENT_TYPES = new Set(["ready", "progress", "partial", "final", "error", "closed"]);

/** Reads the helper's output a chunk at a time and returns the events it completes. */
export class EventLines {
  private rest = "";

  push(chunk: string): EngineEvent[] {
    this.rest += chunk;
    if (this.rest.length > MAX_PAYLOAD) this.rest = "";
    const lines = this.rest.split("\n");
    this.rest = lines.pop() ?? "";
    const events: EngineEvent[] = [];
    for (const line of lines) {
      if (!line.trim()) continue;
      try {
        const value = JSON.parse(line) as { type?: unknown };
        if (typeof value.type === "string" && EVENT_TYPES.has(value.type)) events.push(value as EngineEvent);
      } catch {
        // A stray log line is not an event.
      }
    }
    return events;
  }
}

// -- what the window sees ----------------------------------------------------------------------

/** What the main process tells the window while it listens. */
export type VoiceEvent = EngineEvent | { type: "level"; level: number };

export type VoiceErrorCode =
  | "denied" // the microphone is switched off for Bob in System Settings
  | "restricted"
  | "no-engine" // the speech helper is not part of this build
  | "unsupported" // this Mac cannot run it (needs macOS 26)
  | "busy"
  | "failed";

export type VoiceStart = { ok: true; engine: string } | { ok: false; code: VoiceErrorCode; message: string };

/** Whether voice can work on this Mac, for Settings. */
export interface VoiceAvailability {
  available: boolean;
  engine: string;
  detail: string;
  /** Languages the engine has (or can fetch); empty when unknown. */
  languages: string[];
}

/** The words shown while listening: what is settled, then what is being heard. */
export class Words {
  private settled: string[] = [];
  private heard = "";

  partial(text: string): void {
    this.heard = text.trim();
  }

  final(text: string): void {
    const clean = text.trim();
    if (clean) this.settled.push(clean);
    this.heard = "";
  }

  get text(): string {
    return [...this.settled, this.heard].filter(Boolean).join(" ");
  }

  get isEmpty(): boolean {
    return this.text === "";
  }

  reset(): void {
    this.settled = [];
    this.heard = "";
  }
}

// -- deciding when a spoken message is over ----------------------------------------------------

export interface EndpointOptions {
  /** Silence after speech that ends the message. */
  pauseMs: number;
  /** With nothing said at all for this long, listening stops by itself. */
  idleMs: number;
  /** An RMS level (0 to 1) above which something is being said. */
  threshold?: number;
  /** Speech shorter than this is a cough, not a message. */
  minSpeechMs?: number;
}

export type EndpointResult = "end" | "idle" | null;

export class Endpointer {
  private heard = false;
  private spoke = 0;
  private lastVoice = 0;
  private since = 0;
  private lastSeen = 0;

  constructor(private readonly options: EndpointOptions) {}

  /** Feed one audio level with the time in ms; true text means words have been recognised. */
  push(now: number, level: number, hasText: boolean): EndpointResult {
    if (!this.since) this.since = now;
    const step = this.lastSeen ? now - this.lastSeen : 0;
    this.lastSeen = now;
    if (level >= (this.options.threshold ?? 0.015)) {
      this.spoke += step;
      this.lastVoice = now;
      if (this.spoke >= (this.options.minSpeechMs ?? 200)) this.heard = true;
    }
    if (this.heard && hasText && now - this.lastVoice >= this.options.pauseMs) return "end";
    if (!this.heard && now - this.since >= this.options.idleMs) return "idle";
    return null;
  }

  /** Whether any speech has been heard since the last reset. */
  get spoken(): boolean {
    return this.heard;
  }

  reset(): void {
    this.heard = false;
    this.spoke = 0;
    this.lastVoice = 0;
    this.since = 0;
    this.lastSeen = 0;
  }
}

/** Convert floating-point samples (-1 to 1) to 16-bit little-endian bytes. */
export function toPcm16(samples: Float32Array): Uint8Array {
  const out = new Uint8Array(samples.length * 2);
  const view = new DataView(out.buffer);
  for (let i = 0; i < samples.length; i += 1) {
    const clamped = Math.max(-1, Math.min(1, samples[i]));
    view.setInt16(i * 2, Math.round(clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff), true);
  }
  return out;
}

export function rms(samples: Float32Array): number {
  if (samples.length === 0) return 0;
  let sum = 0;
  for (const value of samples) sum += value * value;
  return Math.sqrt(sum / samples.length);
}

