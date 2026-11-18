import { describe, expect, it } from "vitest";
import { type Capture, VoiceController, type VoiceBridge, type VoiceState } from "../renderer/src/lib/voice";
import type { VoiceEvent, VoiceStart } from "../shared/voice";

const WORDS = "what is on my list today".split(" ");

/** A bridge that hears one more word for every 5 frames, like the stand-in engine. */
function rig(options: { start?: VoiceStart; autoEnd?: boolean; pauseMs?: number; idleMs?: number } = {}) {
  const listeners = new Set<(event: VoiceEvent) => void>();
  const emit = (event: VoiceEvent): void => listeners.forEach((listener) => listener(event));
  let frames = 0;
  let heard = 0;
  const calls: string[] = [];
  let push: (samples: Float32Array) => void = () => undefined;
  let clock = 0;
  const bridge: VoiceBridge = {
    start: async () => {
      calls.push("start");
      return options.start ?? { ok: true, engine: "test" };
    },
    audio: () => {
      frames += 1;
      if (frames % 5 === 0 && heard < WORDS.length) {
        heard += 1;
        emit({ type: "partial", text: WORDS.slice(0, heard).join(" ") });
      }
    },
    stop: async () => {
      calls.push("stop");
      if (heard) emit({ type: "final", text: WORDS.slice(0, heard).join(" ") });
      emit({ type: "closed" });
    },
    cancel: async () => {
      calls.push("cancel");
    },
    onEvent: (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
  };
  const states: VoiceState[] = [];
  const utterances: string[] = [];
  let micClosed = 0;
  const controller = new VoiceController({
    bridge,
    capture: async (onFrame): Promise<Capture> => {
      push = onFrame;
      return { stop: () => (micClosed += 1) };
    },
    onChange: (state) => states.push(state),
    onUtterance: (text) => utterances.push(text),
    pauseMs: () => options.pauseMs ?? 1200,
    autoEnd: () => options.autoEnd ?? false,
    now: () => clock,
    idleMs: options.idleMs ?? 60_000,
  });
  /** Feed ``count`` frames of 40 ms at a loudness. */
  const talk = (count: number, amplitude: number): void => {
    for (let i = 0; i < count; i += 1) {
      clock += 40;
      push(new Float32Array(640).fill(amplitude));
    }
  };
  return { controller, bridge, calls, talk, states, utterances, emit, mic: () => micClosed };
}

describe("listening", () => {
  it("shows words as they are heard and keeps them when you stop", async () => {
    const { controller, talk, calls, mic } = rig();
    await controller.start();
    expect(controller.current.phase).toBe("listening");
    talk(25, 0.1);
    expect(controller.current.text).toBe("what is on my list");
    const text = await controller.finish();
    expect(text).toBe("what is on my list");
    expect(controller.current).toMatchObject({ phase: "off", text: "what is on my list" });
    expect(calls).toEqual(["start", "stop"]);
    expect(mic()).toBe(1);
  });

  it("sends a message when you pause, in conversation mode", async () => {
    const { controller, talk, utterances } = rig({ autoEnd: true, pauseMs: 1200 });
    await controller.start();
    talk(30, 0.1);
    talk(20, 0.0); // 800 ms of silence: not yet
    expect(utterances).toEqual([]);
    talk(15, 0.0); // past 1200 ms
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(utterances).toEqual(["what is on my list today"]);
    expect(controller.current.phase).toBe("off");
  });

  it("does not send by itself when you stop it yourself", async () => {
    const { controller, talk, utterances } = rig({ autoEnd: false });
    await controller.start();
    talk(30, 0.1);
    talk(60, 0.0);
    expect(utterances).toEqual([]);
    expect(controller.current.phase).toBe("listening");
    await controller.cancel();
  });

  it("stops by itself when nothing is said", async () => {
    const { controller, talk, calls } = rig({ idleMs: 2000 });
    await controller.start();
    talk(60, 0.0);
    expect(controller.current).toMatchObject({ phase: "off", error: "Stopped listening: nothing was said." });
    expect(calls).toContain("cancel");
  });

  it("explains why it cannot start", async () => {
    const { controller, mic } = rig({ start: { ok: false, code: "denied", message: "Allow the microphone." } });
    await controller.start();
    expect(controller.current).toMatchObject({ phase: "off", error: "Allow the microphone.", errorCode: "denied" });
    expect(mic()).toBe(0);
  });

  it("cancel drops what was heard and closes the microphone", async () => {
    const { controller, talk, mic, calls } = rig();
    await controller.start();
    talk(25, 0.1);
    await controller.cancel();
    expect(controller.current).toMatchObject({ phase: "off", text: "" });
    expect(mic()).toBe(1);
    expect(calls.at(-1)).toBe("cancel");
  });

  it("an engine error closes the microphone and keeps the message", async () => {
    const { controller, emit, mic, talk } = rig();
    await controller.start();
    talk(10, 0.1);
    emit({ type: "error", code: "failed", message: "the language went away" });
    expect(controller.current).toMatchObject({ phase: "off", error: "the language went away", text: "" });
    expect(mic()).toBe(1);
  });

  it("a helper that closes by itself keeps what was heard", async () => {
    const { controller, emit, talk } = rig();
    await controller.start();
    talk(25, 0.1);
    emit({ type: "closed" });
    expect(controller.current).toMatchObject({ phase: "off", text: "what is on my list" });
  });

  it("starting twice does nothing, and only listens once", async () => {
    const { controller, calls } = rig();
    await Promise.all([controller.start(), controller.start()]);
    expect(calls.filter((call) => call === "start")).toHaveLength(1);
    await controller.cancel();
  });
});
