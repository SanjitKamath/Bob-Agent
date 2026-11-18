// Types shared by the main process and the renderer. They mirror bobd's API.
import type { BobdAction, BobdResult } from "./bobd";
import type { AppSection } from "./sections";
import type { SetupChoices, SetupSnapshot } from "./setup";
import type { VoiceAvailability, VoiceEvent, VoicePrefs, VoiceStart } from "./voice";

export type { AppSection } from "./sections";

export type ViewName = "app" | "panel" | "menu" | "settings" | "setup" | "newtask";
export type ConnectionState = "connecting" | "connected" | "offline" | "unauthorized";

export interface CardButton {
  label: string;
  plugin: string;
  tool: string;
  args: Record<string, unknown>;
  asks_first: boolean;
}

/** One line a card lists under its number: a task, a mail that needs you. */
export interface CardItem {
  title: string;
  detail: string;
  flag: string;
}

export interface Card {
  plugin: string;
  title: string;
  value: number | string | null;
  status: string;
  error: string;
  refresh_seconds: number | null;
  /** Absent when bobd is older than the lists. */
  items?: CardItem[];
  buttons: CardButton[];
}

export interface Brief {
  id: number;
  created_at: string;
  kind: string;
  title: string;
  body: string;
}

export interface CardSnapshot {
  generated_at: string;
  cards: Card[];
  brief: Brief | null;
}

export interface Source {
  n: number;
  citation: string;
  collection: string;
  source: string;
  path: string;
  line: number;
  version: string;
  title: string;
}

export interface Confirmation {
  id: string;
  plugin: string;
  tool: string;
  arguments: Record<string, unknown>;
  actor: string;
  session: string | null;
}

/** Which chat and which question a streamed message belongs to. */
export interface ChatTag {
  conversation_id?: string;
  /** The id the app gave the question, so each window knows its own answers. */
  request_id?: string;
}

/** Everything bobd sends on the app's one WebSocket. */
export type ServerMessage = (
  | { type: "started" }
  | { type: "queued" }
  | { type: "route"; domain: string; risk: string; routed_by: string }
  | { type: "token"; text: string }
  | { type: "tool_call"; plugin: string; tool: string; arguments: Record<string, unknown> }
  | { type: "tool_result"; plugin: string; tool: string; outcome: string }
  | ({ type: "confirm_request" } & Confirmation)
  | { type: "confirm_ack"; id: string; ok: boolean }
  | {
      type: "final";
      conversation_id: string;
      text: string;
      specialist: string;
      sources: Source[];
      warnings: string[];
      steps: number;
      seconds: number;
      /** Reused from the answer cache; when it was first worked out. */
      cached?: boolean;
      cached_at?: string | null;
      /** Where the time went, in seconds: cache, route, model, total. */
      timings?: Record<string, number>;
    }
  | { type: "error"; message: string }
  | { type: "cancelled" }
  | { type: "event"; topic: string; data: Record<string, unknown>; at: string }
) &
  ChatTag;

/** One chat in the history list. */
export interface ChatSummary {
  id: string;
  title: string;
  pinned: boolean;
  created_at: string;
  updated_at: string;
  messages: number;
  running: boolean;
  /** With a search: the text that matched. */
  match?: string;
}

/** One stored message of a chat. */
export interface StoredTurn {
  id: number;
  at: string;
  role: "user" | "assistant";
  content: string;
  specialist: string | null;
}

export type Theme = "system" | "light" | "dark";

export interface Prefs {
  serverUrl: string;
  /** The Bob repo folder; used only to find the token file when no Keychain entry exists. */
  bobHome: string;
  /** Plugin cards on Today and in the menu bar. */
  cards: {
    /** Plugin ids in display order. Empty means every card, in bobd's order. */
    order: string[];
    /** Plugin ids kept out of the menu bar. */
    hidden: string[];
  };
  openAtLogin: boolean;
  /** Cmd+Q hides Bob to the menu bar instead of quitting; Quit Bob in the dropdown still quits. */
  keepRunning: boolean;
  /** "system" follows macOS. */
  theme: Theme;
  notifications: boolean;
  voice: VoicePrefs;
}

export interface AppState {
  connection: ConnectionState;
  detail: string;
  snapshot: CardSnapshot | null;
  confirmations: Confirmation[];
  prefs: Prefs;
  /** macOS "Reduce transparency" is on: frosted surfaces turn solid. */
  reduceTransparency: boolean;
}

/** A request the renderer asks the main process to make to bobd. */
export interface ApiRequest {
  method: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
  path: string;
  body?: unknown;
}

export type ApiResult = { ok: true; data: unknown } | { ok: false; status: number; error: string; data?: unknown };

/** What the preload script exposes as `window.bob`. */
export interface BobBridge {
  view: ViewName;
  state(): Promise<AppState>;
  onState(listener: (state: AppState) => void): () => void;
  onChat(listener: (message: ServerMessage) => void): () => void;
  onAsk(listener: (text: string) => void): () => void;
  /** The main window is asked to show a section. */
  onNavigate(listener: (section: AppSection) => void): () => void;
  api(request: ApiRequest): Promise<ApiResult>;
  /** fresh: skip the answer cache and work the answer out again. */
  ask(
    text: string,
    conversationId: string | null,
    fresh?: boolean,
    requestId?: string,
    /** "voice" when you said it: spoken words are never used to name a folder. */
    input?: "voice",
  ): Promise<void>;
  /** Open the main window on Chat and send this question there. */
  askInApp(text: string): Promise<void>;
  /** Stop one chat, or with no id everything this app started. */
  cancel(conversationId?: string | null): Promise<void>;
  confirm(id: string, approved: boolean): Promise<void>;
  runButton(button: CardButton): Promise<ApiResult>;
  setPrefs(patch: DeepPartial<Prefs>): Promise<Prefs>;
  openView(view: ViewName, section?: AppSection): Promise<void>;
  hideWindow(): Promise<void>;
  revealSource(source: Source): Promise<boolean>;
  pathForFile(file: File): string;
  refresh(): Promise<void>;
  /** Start, restart or stop bobd with deploy/bobd.sh. */
  bobd(action: BobdAction): Promise<BobdResult>;
  quit(): Promise<void>;
  /** First-run setup (the shared build only). The window can ask; it cannot send commands. */
  setup: {
    snapshot(): Promise<SetupSnapshot>;
    onChange(listener: (snapshot: SetupSnapshot) => void): () => void;
    start(choices: SetupChoices): Promise<void>;
    cancel(): Promise<void>;
    /** Open the setup window again from Settings. */
    open(): Promise<void>;
    /** Whether this copy of Bob came with its own runtime (the shared build). */
    bundled(): Promise<boolean>;
  };
  /** Speech to text. The window captures the audio; the main process runs the engine. */
  voice: {
    availability(): Promise<VoiceAvailability>;
    /** Opens the microphone permission (asking for it the first time) and starts the engine. */
    start(): Promise<VoiceStart>;
    /** 16 kHz mono 16-bit audio, 40 ms at a time. */
    audio(frame: ArrayBuffer): void;
    /** Finish: the engine settles the last words, then sends "closed". */
    stop(): Promise<void>;
    /** Stop at once and drop what was heard. */
    cancel(): Promise<void>;
    onEvent(listener: (event: VoiceEvent) => void): () => void;
    /** The hotkey was pressed while this window had the chat. */
    onToggle(listener: () => void): () => void;
    /** Open System Settings on the microphone page. */
    openMicrophoneSettings(): Promise<void>;
  };
  /** Plugins added from a wheel file, chosen in a file dialog. */
  pickWheel(): Promise<WheelPick | null>;
  /** A folder chosen in a file dialog, as a full path; null when the dialog is cancelled. */
  pickFolder(message?: string): Promise<string | null>;
}

export interface WheelPick {
  filename: string;
  data_b64: string;
}

export type DeepPartial<T> = { [K in keyof T]?: T[K] extends object ? DeepPartial<T[K]> : T[K] };
