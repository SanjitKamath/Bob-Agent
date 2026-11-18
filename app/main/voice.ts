// Runs the speech helper and carries audio to it and words back. Nothing here knows about
// Electron, so it can be tested with a stand-in engine.
import { type ChildProcessWithoutNullStreams, spawn as nodeSpawn } from "node:child_process";
import { existsSync } from "node:fs";
import { join } from "node:path";
import {
  commandFrame,
  type EngineEvent,
  EventLines,
  frame,
  type VoiceAvailability,
  type VoiceEvent,
  type VoicePrefs,
  type VoiceStart,
  whisperModel,
} from "../shared/voice";

export interface EngineSpec {
  command: string;
  args: string[];
  /** Settings for a stand-in engine (BOB_STT_*); the real helper takes none. */
  env?: Record<string, string>;
  /** A stand-in engine used by tests. */
  custom: boolean;
}

/** What the helper runs: Bob's Python, importing the voice package. */
const HELPER = ["-c", "from bob_voice.cli import main; main()"];

/**
 * Where the speech helper is. The shared build carries its own Python (and the model); Bob from
 * source uses the Bob folder's. Tests set BOB_STT_BIN to a stand-in.
 */
export function engineSpec(options: {
  env: NodeJS.ProcessEnv;
  packaged: boolean;
  resourcesPath: string;
  /** The Bob folder, where `uv sync` made .venv. */
  bobHome: string;
  /** Where downloaded speech models are kept. */
  modelsDir: string;
  exists?: (path: string) => boolean;
}): EngineSpec | null {
  const exists = options.exists ?? existsSync;
  const override = options.env.BOB_STT_BIN;
  if (override) {
    let args: string[] = [];
    try {
      const parsed: unknown = JSON.parse(options.env.BOB_STT_ARGS ?? "[]");
      if (Array.isArray(parsed)) args = parsed.filter((item): item is string => typeof item === "string");
    } catch {
      // Not JSON: no extra arguments.
    }
    const env = Object.fromEntries(
      Object.entries(options.env).filter((entry): entry is [string, string] => entry[0].startsWith("BOB_STT_") && typeof entry[1] === "string"),
    );
    return { command: override, args, env, custom: true };
  }
  const bundled = join(options.resourcesPath, "runtime", "python", "bin", "python3");
  const venv = join(options.bobHome, ".venv", "bin", "python");
  const models = { BOB_VOICE_MODELS: options.modelsDir };
  if (options.packaged && exists(bundled)) {
    return {
      command: bundled,
      args: HELPER,
      env: { ...models, BOB_VOICE_BUNDLED: join(options.resourcesPath, "runtime", "voice") },
      custom: false,
    };
  }
  if (exists(venv)) return { command: venv, args: HELPER, env: models, custom: false };
  return null;
}

export interface Permission {
  /** macOS's answer for the microphone: granted, denied, restricted or not-determined. */
  status(): string;
  /** Show the system prompt; true when you allow it. */
  ask(): Promise<boolean>;
}

export interface VoiceOptions {
  spec: () => EngineSpec | null;
  platform: string;
  permission: Permission;
  /** Every event goes to the window that started listening. */
  emit: (owner: number, event: VoiceEvent) => void;
  /** The microphone opened or closed: the menu bar shows it. */
  onLive?: (live: boolean) => void;
  spawn?: (command: string, args: string[], env: Record<string, string>) => ChildProcessWithoutNullStreams;
  /** How long the engine may stay silent (ms) while starting, e.g. while it fetches a language. */
  startTimeoutMs?: number;
  /** No audio for this long means the window stopped sending. */
  audioTimeoutMs?: number;
  /** The longest one session may run. */
  maxSessionMs?: number;
}

const NO_ENGINE =
  "Bob's speech helper is not installed. In the Bob folder run `uv sync`, then start Bob again.";

interface Session {
  owner: number;
  child: ChildProcessWithoutNullStreams;
  engine: string;
  closed: boolean;
  ended: Promise<void>;
  lastAudio: number;
  timers: NodeJS.Timeout[];
  stderr: string;
}

export class VoiceManager {
  private session: Session | null = null;

  constructor(private readonly options: VoiceOptions) {}

  get live(): boolean {
    return this.session !== null && !this.session.closed;
  }

  async availability(prefs: VoicePrefs): Promise<VoiceAvailability> {
    const spec = this.options.spec();
    if (!spec) return { available: false, engine: "none", detail: NO_ENGINE, languages: [] };
    const model = whisperModel(prefs.language, prefs.model);
    const label = spec.custom ? "test engine" : `Whisper ${model.name}`;
    return {
      available: true,
      engine: label,
      detail: spec.custom ? label : `${label} (${model.megabytes} MB, kept on this Mac)`,
      languages: [],
    };
  }

  async start(prefs: VoicePrefs, owner: number): Promise<VoiceStart> {
    if (this.session) this.cancel();
    const spec = this.options.spec();
    if (!spec) return { ok: false, code: "no-engine", message: NO_ENGINE };
    if (this.options.platform === "darwin") {
      const status = this.options.permission.status();
      if (status === "restricted") {
        return { ok: false, code: "restricted", message: "A restriction on this Mac blocks the microphone." };
      }
      const allowed =
        status === "granted" || (status === "not-determined" && (await this.options.permission.ask()));
      if (!allowed) {
        return {
          ok: false,
          code: "denied",
          message: "Bob has no access to the microphone. Allow it in System Settings → Privacy & Security → Microphone.",
        };
      }
    }

    const spawn =
      this.options.spawn ??
      ((command, args, env) => nodeSpawn(command, args, { stdio: "pipe", env: { ...process.env, ...env } }));
    let child: ChildProcessWithoutNullStreams;
    try {
      child = spawn(spec.command, spec.args, spec.env ?? {});
    } catch (error) {
      return { ok: false, code: "failed", message: `The speech helper would not start: ${String(error)}` };
    }
    const session: Session = {
      owner,
      child,
      engine: spec.custom ? "test" : "whisper",
      closed: false,
      ended: Promise.resolve(),
      lastAudio: Date.now(),
      timers: [],
      stderr: "",
    };
    this.session = session;

    let finish: () => void = () => undefined;
    session.ended = new Promise<void>((resolve) => (finish = resolve));
    const lines = new EventLines();

    const ready = new Promise<VoiceStart>((resolve) => {
      let settled = false;
      let startTimer: NodeJS.Timeout | null = null;
      const settle = (result: VoiceStart): void => {
        if (settled) return;
        settled = true;
        if (startTimer) clearTimeout(startTimer);
        resolve(result);
      };
      const arm = (): void => {
        if (startTimer) clearTimeout(startTimer);
        startTimer = setTimeout(
          () => settle({ ok: false, code: "failed", message: "The speech helper did not answer." }),
          this.options.startTimeoutMs ?? 20_000,
        );
      };
      arm();

      child.stdout.setEncoding("utf8");
      child.stdout.on("data", (chunk: string) => {
        for (const event of lines.push(chunk)) {
          if (!settled) arm();
          if (event.type === "ready") settle({ ok: true, engine: session.engine });
          if (event.type === "error" && !settled) {
            settle({ ok: false, code: "failed", message: event.message });
          }
          this.forward(session, event);
        }
      });
      child.stderr.setEncoding("utf8");
      child.stderr.on("data", (chunk: string) => {
        session.stderr = (session.stderr + chunk).slice(-2000);
      });
      child.on("error", (error) => {
        settle({ ok: false, code: "failed", message: `The speech helper failed: ${error.message}` });
        this.close(session, { type: "error", code: "failed", message: error.message });
      });
      child.on("exit", () => {
        settle({
          ok: false,
          code: "failed",
          message: session.stderr.trim().split("\n").at(-1) || "The speech helper stopped.",
        });
        this.close(session);
        finish();
      });
      child.stdin.on("error", () => undefined); // a helper that has gone away
      this.write(session, commandFrame({ cmd: "start", locale: prefs.language, model: prefs.model }));
    });

    const result = await ready;
    if (!result.ok) {
      this.cancel();
      return result;
    }
    this.options.onLive?.(true);
    session.timers.push(
      setInterval(() => {
        if (Date.now() - session.lastAudio > (this.options.audioTimeoutMs ?? 5000)) {
          this.options.emit(session.owner, {
            type: "error",
            code: "failed",
            message: "No sound is reaching the speech helper, so listening stopped.",
          });
          this.cancel();
        }
      }, 1000),
      setTimeout(() => this.stopSession(session), this.options.maxSessionMs ?? 15 * 60_000),
    );
    return result;
  }

  /** One 40 ms frame from the window. */
  audio(owner: number, bytes: Uint8Array): void {
    const session = this.session;
    if (!session || session.closed || session.owner !== owner) return;
    session.lastAudio = Date.now();
    this.write(session, frame("audio", bytes));
  }

  /** Let the engine settle what it heard, then it closes. */
  async stop(owner?: number): Promise<void> {
    const session = this.session;
    if (!session || (owner !== undefined && session.owner !== owner)) return;
    await this.stopSession(session);
  }

  /** End now; what was heard is dropped. */
  cancel(): void {
    const session = this.session;
    if (!session) return;
    this.write(session, commandFrame({ cmd: "cancel" }));
    this.close(session);
    setTimeout(() => session.child.kill("SIGKILL"), 300).unref();
  }

  private async stopSession(session: Session): Promise<void> {
    if (session.closed) return;
    this.write(session, commandFrame({ cmd: "stop" }));
    const kill = setTimeout(() => session.child.kill("SIGKILL"), 5000);
    await session.ended;
    clearTimeout(kill);
  }

  private write(session: Session, bytes: Uint8Array): void {
    if (session.closed || !session.child.stdin.writable) return;
    session.child.stdin.write(bytes);
  }

  private forward(session: Session, event: EngineEvent): void {
    if (event.type === "closed") {
      this.close(session); // says so once, whoever noticed first
      return;
    }
    if (session.closed) return;
    this.options.emit(session.owner, event);
  }

  private close(session: Session, last?: EngineEvent): void {
    if (session.closed) return;
    session.closed = true;
    for (const timer of session.timers) {
      clearInterval(timer);
      clearTimeout(timer);
    }
    if (this.session === session) this.session = null;
    this.options.onLive?.(false);
    if (last) this.options.emit(session.owner, last);
    this.options.emit(session.owner, { type: "closed" });
  }
}
