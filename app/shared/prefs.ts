import type { DeepPartial, Prefs } from "./types";
import { DEFAULT_VOICE, mergeVoice } from "./voice";

export const DEFAULT_PREFS: Prefs = {
  serverUrl: "http://127.0.0.1:8765",
  bobHome: "",
  cards: { order: [], hidden: [] },
  openAtLogin: true,
  keepRunning: false,
  notifications: true,
  theme: "system",
  voice: DEFAULT_VOICE,
};

const THEMES = ["system", "light", "dark"] as const;

function isLoopback(value: string): boolean {
  try {
    const url = new URL(value);
    return (
      (url.protocol === "http:" || url.protocol === "https:") &&
      ["127.0.0.1", "localhost", "[::1]"].includes(url.hostname)
    );
  } catch {
    return false;
  }
}

function strings(value: unknown, fallback: string[]): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : fallback;
}

/** Merge a patch (or whatever was read from disk) into valid preferences. */
export function mergePrefs(base: Prefs, patch: DeepPartial<Prefs> | unknown): Prefs {
  const input = (patch && typeof patch === "object" ? patch : {}) as DeepPartial<Prefs> & {
    /** Saved by versions with the desktop widget: its card order and hidden cards carry over. */
    widget?: { cards?: unknown; hidden?: unknown };
  };
  const cards = (input.cards ?? {}) as DeepPartial<Prefs["cards"]>;
  const legacy = input.cards ? {} : (input.widget ?? {});
  return {
    // The app only ever talks to bobd on this machine.
    serverUrl:
      typeof input.serverUrl === "string" && isLoopback(input.serverUrl)
        ? input.serverUrl.replace(/\/+$/, "")
        : base.serverUrl,
    bobHome: typeof input.bobHome === "string" ? input.bobHome : base.bobHome,
    cards: {
      order: strings(cards.order ?? legacy.cards, base.cards.order),
      hidden: strings(cards.hidden ?? legacy.hidden, base.cards.hidden),
    },
    openAtLogin: typeof input.openAtLogin === "boolean" ? input.openAtLogin : base.openAtLogin,
    keepRunning: typeof input.keepRunning === "boolean" ? input.keepRunning : base.keepRunning,
    notifications: typeof input.notifications === "boolean" ? input.notifications : base.notifications,
    theme: THEMES.includes(input.theme as never) ? (input.theme as Prefs["theme"]) : base.theme,
    voice: mergeVoice(base.voice, input.voice),
  };
}
