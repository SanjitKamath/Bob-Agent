// The microphone for one chat window: the controller, kept alive across renders.
import { useCallback, useEffect, useRef, useState } from "react";
import type { VoiceAvailability, VoicePrefs } from "../../../shared/voice";
import { bob } from "../bob";
import { IDLE, openMicrophone, type VoiceState, VoiceController } from "./voice";

export interface UseVoice {
  state: VoiceState;
  available: VoiceAvailability | null;
  /** Conversation mode: send on pause and listen again after each answer. */
  conversation: boolean;
  setConversation(on: boolean): void;
  /** Start listening, or stop and keep the words. */
  toggle(): void;
  /** Stop listening and return everything that was heard. */
  finish(): Promise<string>;
  clearError(): void;
}

export function useVoice(options: {
  prefs: VoicePrefs | undefined;
  /** The window is showing and in use; listening never continues in a hidden window. */
  active: boolean;
  /** Bob is not answering: in conversation mode the microphone reopens when this turns true. */
  idle: boolean;
  /** Called with the words each time they change while listening, and when listening ends. */
  onWords(text: string, final: boolean): void;
  /** A message ended by a pause: send it. */
  onUtterance(text: string): void;
}): UseVoice {
  const prefs = options.prefs;
  const [state, setState] = useState<VoiceState>(IDLE);
  const [available, setAvailable] = useState<VoiceAvailability | null>(null);
  const [conversation, setConversationState] = useState(prefs?.conversation ?? false);
  const latest = useRef({ ...options, conversation });
  latest.current = { ...options, conversation };
  const controller = useRef<VoiceController | null>(null);
  const listened = useRef(false);

  useEffect(() => {
    void bob.voice.availability().then(setAvailable, () => undefined);
  }, [prefs?.model, prefs?.language]);

  useEffect(() => {
    const instance = new VoiceController({
      bridge: bob.voice,
      capture: (onFrame) => openMicrophone(latest.current.prefs?.inputDevice ?? "", onFrame),
      onChange: (next) => {
        setState(next);
        if (next.phase === "listening" || next.phase === "stopping") {
          listened.current = true;
          latest.current.onWords(next.text, false);
        } else if (listened.current) {
          listened.current = false;
          latest.current.onWords(next.text, true);
        }
      },
      onUtterance: (text) => latest.current.onUtterance(text),
      pauseMs: () => latest.current.prefs?.pauseMs ?? 1200,
      autoEnd: () => latest.current.conversation,
    });
    controller.current = instance;
    return () => {
      controller.current = null;
      instance.dispose();
    };
  }, []);

  const toggle = useCallback(() => {
    const instance = controller.current;
    if (!instance) return;
    if (instance.current.phase === "off") {
      void instance.start();
    } else {
      // Stopping it yourself also ends conversation mode, so it does not start again.
      setConversationState(false);
      void instance.finish();
    }
  }, []);

  // Conversation mode: listen again once Bob has answered, while the window is in use.
  useEffect(() => {
    if (conversation && options.active && options.idle && state.phase === "off" && !state.error) {
      void controller.current?.start();
    }
  }, [conversation, options.active, options.idle, state.phase, state.error]);

  // Never listen in a window that is hidden, or when you have switched away from it.
  useEffect(() => {
    const away = (): void => {
      if (document.hidden) void controller.current?.finish();
    };
    document.addEventListener("visibilitychange", away);
    return () => document.removeEventListener("visibilitychange", away);
  }, []);
  useEffect(() => {
    if (!options.active) void controller.current?.finish();
  }, [options.active]);

  // The hotkey.
  useEffect(() => bob.voice.onToggle(toggle), [toggle]);

  return {
    state,
    available,
    conversation,
    setConversation: (on) => {
      setConversationState(on);
      if (!on && controller.current?.current.phase === "listening") void controller.current.finish();
    },
    toggle,
    finish: async () => (await controller.current?.finish()) ?? "",
    clearError: () => controller.current?.clearError(),
  };
}
