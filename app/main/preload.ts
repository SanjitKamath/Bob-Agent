// The only bridge between a window and the main process. Windows get no Node access.
import { contextBridge, ipcRenderer, webUtils } from "electron";
import type { AppSection, AppState, BobBridge, ServerMessage, ViewName } from "../shared/types";
import type { VoiceEvent } from "../shared/voice";

const VIEWS: ViewName[] = ["app", "panel", "menu", "settings", "setup", "newtask"];
const requested = new URLSearchParams(location.search).get("view") as ViewName;

function subscribe<T>(channel: string, listener: (value: T) => void): () => void {
  const handler = (_event: unknown, value: T): void => listener(value);
  ipcRenderer.on(channel, handler);
  return () => ipcRenderer.removeListener(channel, handler);
}

const bridge: BobBridge = {
  view: VIEWS.includes(requested) ? requested : "panel",
  state: () => ipcRenderer.invoke("bob:state"),
  onState: (listener) => subscribe<AppState>("bob:state-changed", listener),
  onChat: (listener) => subscribe<ServerMessage>("bob:chat", listener),
  onAsk: (listener) => subscribe<string>("bob:ask-text", listener),
  onNavigate: (listener) => subscribe<AppSection>("bob:navigate", listener),
  api: (request) => ipcRenderer.invoke("bob:api", request),
  ask: (text, conversationId, fresh, requestId, input) =>
    ipcRenderer.invoke("bob:ask", text, conversationId, fresh === true, requestId, input),
  askInApp: (text) => ipcRenderer.invoke("bob:ask-in-app", text),
  cancel: (conversationId) => ipcRenderer.invoke("bob:cancel", conversationId ?? null),
  confirm: (id, approved) => ipcRenderer.invoke("bob:confirm", id, approved),
  runButton: (button) => ipcRenderer.invoke("bob:button", button),
  setPrefs: (patch) => ipcRenderer.invoke("bob:prefs", patch),
  openView: (view, section) => ipcRenderer.invoke("bob:open", view, section),
  hideWindow: () => ipcRenderer.invoke("bob:hide"),
  revealSource: (source) => ipcRenderer.invoke("bob:reveal", source),
  pathForFile: (file) => webUtils.getPathForFile(file),
  refresh: () => ipcRenderer.invoke("bob:refresh"),
  bobd: (action) => ipcRenderer.invoke("bob:bobd", action),
  quit: () => ipcRenderer.invoke("bob:quit"),
  setup: {
    snapshot: () => ipcRenderer.invoke("bob:setup-snapshot"),
    onChange: (listener) => subscribe("bob:setup-changed", listener),
    start: (choices) => ipcRenderer.invoke("bob:setup-start", choices),
    cancel: () => ipcRenderer.invoke("bob:setup-cancel"),
    open: () => ipcRenderer.invoke("bob:setup-open"),
    bundled: () => ipcRenderer.invoke("bob:setup-bundled"),
  },
  voice: {
    availability: () => ipcRenderer.invoke("bob:voice-availability"),
    start: () => ipcRenderer.invoke("bob:voice-start"),
    audio: (frame) => ipcRenderer.send("bob:voice-audio", frame),
    stop: () => ipcRenderer.invoke("bob:voice-stop"),
    cancel: () => ipcRenderer.invoke("bob:voice-cancel"),
    onEvent: (listener) => subscribe<VoiceEvent>("bob:voice-event", listener),
    onToggle: (listener) => subscribe<void>("bob:voice-toggle", () => listener()),
    openMicrophoneSettings: () => ipcRenderer.invoke("bob:voice-mic-settings"),
  },
  pickWheel: () => ipcRenderer.invoke("bob:pick-wheel"),
  pickFolder: (message) => ipcRenderer.invoke("bob:pick-folder", message),
};

contextBridge.exposeInMainWorld("bob", bridge);
