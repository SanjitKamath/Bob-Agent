import { describe, expect, it } from "vitest";
import { fileURLToPath } from "node:url";
import { engineSpec, VoiceManager } from "../main/voice";
import { DEFAULT_PREFS, mergePrefs } from "../shared/prefs";
import {
  commandFrame,
  DEFAULT_VOICE,
  Endpointer,
  EventLines,
  frame,
  mergeVoice,
  rms,
  toPcm16,
  type VoiceEvent,
  whisperModel,
  Words,
} from "../shared/voice";

const FAKE = fileURLToPath(new URL("../scripts/fake-stt.mjs", import.meta.url));

function manager(env: Record<string, string> = {}, extra: Partial<ConstructorParameters<typeof VoiceManager>[0]> = {}) {
  const events: VoiceEvent[] = [];
  const live: boolean[] = [];
  const granted = { status: () => "granted", ask: async () => true };
  const voice = new VoiceManager({
    spec: () =>
      engineSpec({
        env: { BOB_STT_BIN: process.execPath, BOB_STT_ARGS: JSON.stringify([FAKE]), ...env },
        packaged: false,
        resourcesPath: "",
        bobHome: "",
        modelsDir: "",
      }),
    platform: "darwin",
    permission: granted,
    emit: (_owner, event) => events.push(event),
    onLive: (value) => live.push(value),
    ...extra,
  });
  return { voice, events, live };
}

const silence = new Uint8Array(1280);
const wait = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

describe("wire format", () => {
  it("frames carry a kind, a length and the payload", () => {
    const out = frame("audio", new Uint8Array([1, 2, 3]));
    expect([...out]).toEqual([2, 3, 0, 0, 0, 1, 2, 3]);
    expect(commandFrame({ cmd: "stop" })[0]).toBe(1);
  });

  it("events are read across chunks and stray lines are ignored", () => {
    const lines = new EventLines();
    expect(lines.push('{"type":"partial","te')).toEqual([]);
    expect(lines.push('xt":"hi"}\nnot json\n{"type":"nonsense"}\n{"type":"closed"}\n')).toEqual([
      { type: "partial", text: "hi" },
      { type: "closed" },
    ]);
  });

  it("converts samples to 16-bit and measures their level", () => {
    const pcm = toPcm16(new Float32Array([0, 1, -1, 2]));
    const view = new DataView(pcm.buffer);
    expect([0, 1, 2, 3].map((i) => view.getInt16(i * 2, true))).toEqual([0, 32767, -32768, 32767]);
    expect(rms(new Float32Array([0.5, -0.5]))).toBeCloseTo(0.5);
    expect(rms(new Float32Array())).toBe(0);
  });

  it("english uses its own model and other languages the multilingual one", () => {
    expect(whisperModel("en-GB", "base")).toEqual({ name: "base.en", megabytes: 148 });
    expect(whisperModel("en-US", "small")).toEqual({ name: "small.en", megabytes: 488 });
    expect(whisperModel("hi-IN", "small")).toEqual({ name: "small", megabytes: 488 });
  });
});

describe("words", () => {
  it("settled words stay and each partial replaces the last", () => {
    const words = new Words();
    words.partial("hello");
    words.partial("hello bob");
    expect(words.text).toBe("hello bob");
    words.final("hello bob");
    words.partial("what is");
    expect(words.text).toBe("hello bob what is");
    words.reset();
    expect(words.isEmpty).toBe(true);
  });
});

describe("endpointing", () => {
  const talk = (end: Endpointer, from: number, to: number, level: number, text = true) => {
    let result: string | null = null;
    for (let t = from; t <= to; t += 40) result = end.push(t, level, text) ?? result;
    return result;
  };

  it("ends a message after the pause that follows speech", () => {
    const end = new Endpointer({ pauseMs: 1200, idleMs: 60_000 });
    expect(talk(end, 40, 1000, 0.1)).toBeNull();
    expect(talk(end, 1040, 2000, 0.001)).toBeNull();
    expect(end.push(2300, 0.001, true)).toBe("end");
  });

  it("does not end without words, however quiet", () => {
    const end = new Endpointer({ pauseMs: 1200, idleMs: 60_000 });
    talk(end, 40, 1000, 0.1);
    expect(talk(end, 1040, 5000, 0.001, false)).toBeNull();
  });

  it("a cough is not speech, and long silence is idle", () => {
    const end = new Endpointer({ pauseMs: 1200, idleMs: 3000 });
    expect(end.push(40, 0.5, false)).toBeNull();
    expect(end.push(80, 0.0, false)).toBeNull();
    expect(end.spoken).toBe(false);
    expect(end.push(3100, 0.0, false)).toBe("idle");
  });
});

describe("voice preferences", () => {
  it("keep only valid values", () => {
    const merged = mergeVoice(DEFAULT_VOICE, { model: "huge", language: "en GB!", pauseMs: 99_999, hotkey: 7, conversation: true });
    expect(merged).toEqual({ ...DEFAULT_VOICE, conversation: true });
    expect(mergeVoice(DEFAULT_VOICE, { language: "hi-IN", pauseMs: 2000 })).toMatchObject({ language: "hi-IN", pauseMs: 2000 });
  });

  it("are part of the app's preferences", () => {
    expect(DEFAULT_PREFS.voice.conversation).toBe(false);
    expect(mergePrefs(DEFAULT_PREFS, { voice: { conversation: true } }).voice.conversation).toBe(true);
    expect(mergePrefs(DEFAULT_PREFS, {}).voice).toEqual(DEFAULT_VOICE);
  });
});

describe("finding the helper", () => {
  const base = { env: {}, packaged: false, resourcesPath: "/app/Resources", bobHome: "/home/Bob", modelsDir: "/data/voice-models" };
  it("uses the shared build's own Python, or the Bob folder's, or none", () => {
    const packaged = engineSpec({ ...base, packaged: true, exists: () => true });
    expect(packaged?.command).toBe("/app/Resources/runtime/python/bin/python3");
    expect(packaged?.args).toEqual(["-c", "from bob_voice.cli import main; main()"]);
    expect(packaged?.env).toEqual({ BOB_VOICE_MODELS: "/data/voice-models", BOB_VOICE_BUNDLED: "/app/Resources/runtime/voice" });

    const source = engineSpec({ ...base, exists: (p) => p === "/home/Bob/.venv/bin/python" });
    expect(source?.command).toBe("/home/Bob/.venv/bin/python");
    expect(source?.env).toEqual({ BOB_VOICE_MODELS: "/data/voice-models" });

    // A packaged app without its own Python (the personal build) falls back to the Bob folder.
    expect(engineSpec({ ...base, packaged: true, exists: (p) => p.startsWith("/home/Bob") })?.command).toBe("/home/Bob/.venv/bin/python");
    expect(engineSpec({ ...base, exists: () => false })).toBeNull();
  });

  it("takes a stand-in engine from the environment", () => {
    const spec = engineSpec({ ...base, env: { BOB_STT_BIN: "node", BOB_STT_ARGS: '["x.mjs"]', BOB_STT_FAIL: "start" }, exists: () => false });
    expect(spec).toMatchObject({ command: "node", args: ["x.mjs"], env: { BOB_STT_FAIL: "start" }, custom: true });
  });
});

describe("the voice manager, with a stand-in engine", () => {
  it("starts, hears words as audio arrives, and settles them on stop", async () => {
    const { voice, events, live } = manager({ BOB_STT_SCRIPT: "hello bob how are you" });
    const started = await voice.start(DEFAULT_VOICE, 1);
    expect(started).toEqual({ ok: true, engine: "test" });
    expect(voice.live).toBe(true);
    for (let i = 0; i < 25; i += 1) voice.audio(1, silence); // 1 second: five words
    await wait(150);
    await voice.stop(1);
    await wait(50);
    const partials = events.filter((e) => e.type === "partial").map((e) => (e as { text: string }).text);
    expect(partials.at(-1)).toBe("hello bob how are you");
    expect(events.filter((e) => e.type === "final")).toEqual([{ type: "final", text: "hello bob how are you" }]);
    expect(events.filter((e) => e.type === "closed")).toHaveLength(1);
    expect(live).toEqual([true, false]);
    expect(voice.live).toBe(false);
  });

  it("cancel drops what was heard", async () => {
    const { voice, events } = manager();
    await voice.start(DEFAULT_VOICE, 1);
    for (let i = 0; i < 10; i += 1) voice.audio(1, silence);
    voice.cancel();
    await wait(400);
    expect(events.some((e) => e.type === "final")).toBe(false);
    expect(events.filter((e) => e.type === "closed")).toHaveLength(1);
  });

  it("ignores audio from a window that did not start listening", async () => {
    const { voice, events } = manager();
    await voice.start(DEFAULT_VOICE, 1);
    for (let i = 0; i < 25; i += 1) voice.audio(2, silence);
    await wait(100);
    expect(events.some((e) => e.type === "partial")).toBe(false);
    voice.cancel();
  });

  it("explains a missing helper and a refused microphone", async () => {
    const none = new VoiceManager({
      spec: () => null, platform: "darwin", emit: () => undefined,
      permission: { status: () => "granted", ask: async () => true },
    });
    expect(await none.start(DEFAULT_VOICE, 1)).toMatchObject({ ok: false, code: "no-engine" });
    expect((await none.availability(DEFAULT_VOICE)).available).toBe(false);

    const denied = manager({}, { permission: { status: () => "denied", ask: async () => false } });
    expect(await denied.voice.start(DEFAULT_VOICE, 1)).toMatchObject({ ok: false, code: "denied" });
    const restricted = manager({}, { permission: { status: () => "restricted", ask: async () => false } });
    expect(await restricted.voice.start(DEFAULT_VOICE, 1)).toMatchObject({ ok: false, code: "restricted" });
  });

  it("asks the first time, and starts when you allow it", async () => {
    let asked = 0;
    const { voice } = manager({}, { permission: { status: () => "not-determined", ask: async () => (asked += 1) > 0 } });
    expect((await voice.start(DEFAULT_VOICE, 1)).ok).toBe(true);
    expect(asked).toBe(1);
    voice.cancel();
  });

  it("reports an engine that fails to start", async () => {
    const { voice, live } = manager({ BOB_STT_FAIL: "start" });
    const result = await voice.start(DEFAULT_VOICE, 1);
    expect(result).toMatchObject({ ok: false, message: "the language is not installed" });
    expect(live).not.toContain(true);
    expect(voice.live).toBe(false);
  });

  it("stops listening when no audio arrives", async () => {
    const { voice, events } = manager({}, { audioTimeoutMs: 1100 });
    await voice.start(DEFAULT_VOICE, 1);
    await wait(2500);
    expect(events).toContainEqual(expect.objectContaining({ type: "error", code: "failed" }));
    expect(voice.live).toBe(false);
  });
});
