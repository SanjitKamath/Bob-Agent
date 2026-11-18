// Bob's Mac app: a menu bar app that opens a full Bob window when you want one.
// Everything it shows comes from bobd.
import {
  app,
  globalShortcut,
  ipcMain,
  Menu,
  type MenuItemConstructorOptions,
  nativeImage,
  nativeTheme,
  Notification,
  session,
  shell,
  systemPreferences,
  Tray,
  powerMonitor,
  webContents,
} from "electron";
import { existsSync, mkdirSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { join, resolve, sep } from "node:path";
import { isJobProgress } from "../shared/jobs";
import { changesCards, noticeFor } from "../shared/notify";
import type {
  ApiRequest,
  AppState,
  CardButton,
  Confirmation,
  DeepPartial,
  Prefs,
  ServerMessage,
  AppSection,
  Source,
  ViewName,
  CardSnapshot,
} from "../shared/types";
import { APP_SECTIONS, isAppSection, SECTION_LABELS } from "../shared/sections";
import type { BobdAction, BobdResult } from "../shared/bobd";
import { parseBobUrl } from "../shared/urls";
import { Backend } from "./backend";
import { runBobd } from "./bobd";
import { PrefsStore } from "./store";
import { webAddress } from "../shared/sources";
import { SetupHost, sharedHome } from "./setupHost";
import { Windows } from "./windows";
import { engineSpec, VoiceManager } from "./voice";
import type { VoicePrefs } from "../shared/voice";

const SMOKE = process.argv.includes("--smoke");
const PANEL_SHORTCUT = "Alt+Space";
const KNOWLEDGE_ROOT = join(homedir(), "knowledge_base");

if (!app.requestSingleInstanceLock()) app.quit();

let store: PrefsStore;
let backend: Backend;
let windows: Windows;
let setupHost: SetupHost;
let voice: VoiceManager;
let voiceHotkey = "";
let tray: Tray | null = null;
let snapshot: CardSnapshot | null = null;
let confirmations: Confirmation[] = [];
let refreshTimer: NodeJS.Timeout | null = null;
let intervalTimer: NodeJS.Timeout | null = null;
let pendingUrl: string | null = null;
/** Set once startup is done; macOS also sends "activate" while launching. */
let started = false;

function state(): AppState {
  return {
    connection: backend.state,
    detail: backend.detail,
    snapshot,
    confirmations,
    prefs: store.get(),
    reduceTransparency: nativeTheme.prefersReducedTransparency,
  };
}

function broadcast(): void {
  const current = state();
  for (const window of windows.all()) window.webContents.send("bob:state-changed", current);
  // The menu bar always reads just "Bob"; counts live in the app and the dropdown.
}

// -- cards ----------------------------------------------------------------------------

async function refreshCards(): Promise<void> {
  const result = await backend.request({ method: "GET", path: "/v1/widget" }, 30000);
  if (result.ok) snapshot = result.data as CardSnapshot;
  broadcast();
  // Cards that asked for "every 5m" are refreshed on the shortest such interval.
  const seconds = (snapshot?.cards ?? [])
    .map((card) => card.refresh_seconds)
    .filter((value): value is number => typeof value === "number" && value > 0);
  if (intervalTimer) clearInterval(intervalTimer);
  intervalTimer = seconds.length
    ? setInterval(() => void refreshCards(), Math.max(15, Math.min(...seconds)) * 1000)
    : null;
}

/** Refresh soon; several events in a row cause one request. */
function refreshSoon(): void {
  if (refreshTimer) return;
  refreshTimer = setTimeout(() => {
    refreshTimer = null;
    void refreshCards();
  }, 300);
}

// -- messages from bobd -----------------------------------------------------------------

function notify(topic: string, data: Record<string, unknown>): void {
  if (!store.get().notifications || !Notification.isSupported()) return;
  const notice = noticeFor(topic, data);
  if (!notice) return;
  const notification = new Notification({
    title: notice.title,
    body: notice.body,
    actions: notice.actions.map((action) => ({ type: "button" as const, text: action.label })),
  });
  notification.on("click", () => handleUrl(notice.actions[0]?.url ?? "bob://panel"));
  notification.on("action", (_event, index) => handleUrl(notice.actions[index]?.url ?? "bob://panel"));
  notification.show();
}

function onMessage(message: ServerMessage): void {
  switch (message.type) {
    case "confirm_request": {
      const { type: _type, ...confirmation } = message;
      confirmations = [...confirmations.filter((item) => item.id !== confirmation.id), confirmation];
      // An external action never runs directly: its confirm card opens in the panel.
      windows.showPanel();
      broadcast();
      return;
    }
    case "confirm_ack":
      confirmations = confirmations.filter((item) => item.id !== message.id);
      broadcast();
      break;
    case "event":
      if (message.topic === "confirm.closed") {
        confirmations = confirmations.filter((item) => item.id !== message.data.id);
        broadcast();
      }
      if (changesCards(message.topic)) refreshSoon();
      notify(message.topic, message.data);
      // Background jobs report progress into the chat that started them.
      if (isJobProgress(message.topic)) {
        for (const window of windows.all()) window.webContents.send("bob:chat", message);
      }
      return;
    default:
      break;
  }
  for (const window of windows.all()) window.webContents.send("bob:chat", message);
}

// -- bob:// ---------------------------------------------------------------------------------

async function runTool(plugin: string, tool: string, args: Record<string, unknown> = {}) {
  const result = await backend.request({
    method: "POST",
    path: "/v1/tools/call",
    body: { plugin, tool, arguments: args },
  });
  refreshSoon();
  return result;
}

function handleUrl(raw: string): void {
  if (!app.isReady() || !windows) {
    pendingUrl = raw;
    return;
  }
  const action = parseBobUrl(raw);
  switch (action.kind) {
    case "ask":
      windows.ask(action.text);
      break;
    case "panel":
      windows.showPanel();
      break;
    case "app":
      windows.showApp(action.section);
      break;
    case "settings":
      windows.showSettings();
      break;
    case "triage":
      void runTool("mail", "triage");
      break;
    case "reindex":
      void runTool("knowledge", "reindex");
      break;
    case "invalid":
      console.warn(`ignored ${raw}: ${action.reason}`);
      break;
  }
}

app.on("open-url", (event, url) => {
  event.preventDefault();
  handleUrl(url);
});
app.on("second-instance", (_event, argv) => {
  const url = argv.find((argument) => argument.startsWith("bob://"));
  if (url) handleUrl(url);
  else windows?.showApp();
});
// Opening Bob.app again (Finder, Spotlight, the Dock) brings up the main window.
app.on("activate", () => {
  if (started) windows?.showApp();
});

// -- quitting ------------------------------------------------------------------------------------

/** Set when Bob is really leaving: Quit Bob in the dropdown, or the Mac shutting down. */
let quitting = false;

function quitCompletely(): void {
  quitting = true;
  app.quit();
}

/** Cmd+Q and the Dock's Quit: with "keep running" on, Bob only closes its windows. */
function quitFromApp(): void {
  if (store?.get().keepRunning) windows.closeAll();
  else quitCompletely();
}

// Dock-menu Quit and other routes land here. A shutdown or log out is never held up.
app.on("before-quit", (event) => {
  if (quitting || !started || !store?.get().keepRunning) return;
  event.preventDefault();
  windows.closeAll();
});
app.whenReady().then(() => powerMonitor.on("shutdown", () => (quitting = true)));

/** True when macOS started Bob as a login item; then it starts quietly in the menu bar. */
function openedAtLogin(): boolean {
  try {
    return app.getLoginItemSettings().wasOpenedAtLogin === true;
  } catch {
    return false;
  }
}

// -- IPC: what windows may ask for ---------------------------------------------------------------

function openView(view: ViewName, section?: AppSection): void {
  if (view === "app") windows.showApp(section ?? "today");
  else if (view === "settings") windows.showSettings(section);
  else if (view === "newtask") windows.showNewTask();
  else windows.showPanel();
}

function applyPrefs(patch: DeepPartial<Prefs>): Prefs {
  const before = store.get();
  const after = store.update(patch);
  if (after.serverUrl !== before.serverUrl || after.bobHome !== before.bobHome) void backend.reconnect();
  if (after.openAtLogin !== before.openAtLogin && app.isPackaged) {
    app.setLoginItemSettings({ openAtLogin: after.openAtLogin });
  }
  // The theme is macOS's own appearance for Bob's windows, so vibrancy, menus and every
  // window (the menu bar dropdown too) change together.
  nativeTheme.themeSource = after.theme;
  // Cmd+Q's meaning is shown in the app menu.
  if (after.keepRunning !== before.keepRunning) Menu.setApplicationMenu(buildMenu());
  if (after.voice.hotkey !== before.voice.hotkey) registerVoiceHotkey(after.voice);
  broadcast();
  return after;
}

function revealSource(source: Source): boolean {
  // A page from the web opens in the browser; only plain http(s) links ever do.
  const address = webAddress(source);
  if (address) {
    void shell.openExternal(address);
    return true;
  }
  // Sources cite files in Bob's own copy of the knowledge base.
  const folder = source.source === "files" ? "files" : source.source;
  const file = resolve(KNOWLEDGE_ROOT, source.collection, folder, source.path);
  if (!file.startsWith(KNOWLEDGE_ROOT + sep) || !existsSync(file)) return false;
  shell.showItemInFolder(file);
  return true;
}

function registerIpc(): void {
  ipcMain.handle("bob:state", () => state());
  ipcMain.handle("bob:api", (_event, request: ApiRequest) => backend.request(request));
  ipcMain.handle("bob:ask", (_event, text: string, conversationId: string | null, fresh?: boolean, requestId?: string, input?: string) => {
    const request_id = typeof requestId === "string" ? requestId.slice(0, 64) : "";
    const sent = backend.send({
      type: "ask",
      text: String(text),
      conversation_id: conversationId,
      fresh: fresh === true,
      request_id,
      ...(input === "voice" ? { input: "voice" } : {}),
    });
    if (!sent) onMessage({ type: "error", message: backend.detail || "bobd is not connected.", request_id });
  });
  ipcMain.handle("bob:ask-in-app", (_event, text: string) => {
    const question = String(text ?? "").trim();
    if (question) windows.askInApp(question.slice(0, 4000));
  });
  ipcMain.handle("bob:cancel", (_event, conversationId?: string | null) =>
    void backend.send(typeof conversationId === "string" && conversationId ? { type: "cancel", conversation_id: conversationId } : { type: "cancel" }),
  );
  ipcMain.handle("bob:confirm", (_event, id: string, approved: boolean) => {
    backend.send({ type: "confirm", id: String(id), approved: approved === true });
  });
  ipcMain.handle("bob:button", (_event, button: CardButton) => {
    // Only buttons bobd itself put on a card can be run.
    const known = snapshot?.cards.some((card) =>
      card.buttons.some((b) => b.plugin === button.plugin && b.tool === button.tool),
    );
    if (!known) return { ok: false, status: 400, error: "unknown button" };
    const declared = snapshot!.cards
      .flatMap((card) => card.buttons)
      .find((b) => b.plugin === button.plugin && b.tool === button.tool)!;
    return runTool(declared.plugin, declared.tool, declared.args);
  });
  ipcMain.handle("bob:prefs", (_event, patch: DeepPartial<Prefs>) => applyPrefs(patch));
  ipcMain.handle("bob:open", (_event, view: ViewName, section?: unknown) =>
    openView(view, isAppSection(section) ? section : undefined),
  );
  ipcMain.handle("bob:hide", (event) => windows.hideSender(event.sender));
  ipcMain.handle("bob:reveal", (_event, source: Source) => revealSource(source));
  ipcMain.handle("bob:refresh", async () => {
    // Refresh also retries a connection that is down, including the Keychain read.
    if (backend.state === "connected") await refreshCards();
    else await backend.reconnect();
  });
  ipcMain.handle("bob:bobd", (_event, action: BobdAction) => controlBobd(action));
  ipcMain.handle("bob:quit", () => quitCompletely());

  // Speech to text. Only the window that started listening can send audio or stop it.
  ipcMain.handle("bob:voice-availability", () => voice.availability(store.get().voice));
  ipcMain.handle("bob:voice-start", (event) => voice.start(store.get().voice, event.sender.id));
  ipcMain.on("bob:voice-audio", (event, bytes: unknown) => {
    if (bytes instanceof ArrayBuffer && bytes.byteLength <= 8192) voice.audio(event.sender.id, new Uint8Array(bytes));
  });
  ipcMain.handle("bob:voice-stop", (event) => voice.stop(event.sender.id));
  ipcMain.handle("bob:voice-cancel", () => voice.cancel());
  ipcMain.handle("bob:voice-mic-settings", () =>
    shell.openExternal("x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone"),
  );
}

/** The microphone is Bob's alone: only Bob's own windows may ask for it, and for nothing else. */
function setUpVoice(): void {
  voice = new VoiceManager({
    spec: () =>
      engineSpec({
        env: process.env,
        packaged: app.isPackaged,
        resourcesPath: process.resourcesPath,
        bobHome: store.get().bobHome || join(homedir(), "Bob"),
        modelsDir: join(app.getPath("userData"), "voice-models"),
      }),
    platform: process.platform,
    permission: {
      status: () => systemPreferences.getMediaAccessStatus("microphone"),
      ask: () => systemPreferences.askForMediaAccess("microphone"),
    },
    emit: (owner, event) => webContents.fromId(owner)?.send("bob:voice-event", event),
    onLive: (live) => tray?.setTitle(live ? "Bob ●" : "Bob"),
  });
  const ours = (contents: Electron.WebContents | null): boolean =>
    contents !== null && windows.all().some((window) => window.webContents === contents);
  session.defaultSession.setPermissionRequestHandler((contents, permission, callback, details) => {
    const audioOnly = "mediaTypes" in details && (details.mediaTypes ?? []).every((type) => type === "audio");
    callback(ours(contents) && (permission === "notifications" || (permission === "media" && audioOnly)));
  });
  session.defaultSession.setPermissionCheckHandler((contents, permission) =>
    ours(contents) && (permission === "notifications" || permission === "media"),
  );
  // The microphone closes when the screen locks or the Mac sleeps, and with its window.
  powerMonitor.on("lock-screen", () => voice.cancel());
  powerMonitor.on("suspend", () => voice.cancel());
  app.on("web-contents-created", (_event, contents) => {
    const id = contents.id;
    contents.on("destroyed", () => void voice.stop(id));
  });
}

/** (Re)register the hotkey that starts and stops listening; empty or taken means none. */
function registerVoiceHotkey(prefs: VoicePrefs): void {
  if (voiceHotkey) globalShortcut.unregister(voiceHotkey);
  voiceHotkey = "";
  if (!prefs.hotkey || prefs.hotkey === PANEL_SHORTCUT) return;
  try {
    if (globalShortcut.register(prefs.hotkey, () => windows.voiceToggle())) voiceHotkey = prefs.hotkey;
    else console.warn(`${prefs.hotkey} is taken by another app; use the microphone button.`);
  } catch {
    console.warn(`${prefs.hotkey} is not a valid shortcut.`);
  }
}

// -- starting bobd ---------------------------------------------------------------------------------

async function controlBobd(action: BobdAction): Promise<BobdResult> {
  const prefs = store.get();
  const result = await runBobd(action, prefs.bobHome, prefs.serverUrl);
  // Connect (or reconnect) straight away rather than waiting for the next retry.
  if (action !== "stop") await backend.reconnect();
  broadcast();
  if (!result.ok && Notification.isSupported()) {
    new Notification({ title: "Bob", body: result.message }).show();
  }
  return result;
}

// -- app menu (shown while the Bob window is open) ------------------------------------------------

function buildMenu(): Menu {
  const goTo = (section: AppSection, key: string): MenuItemConstructorOptions => ({
    label: SECTION_LABELS[section],
    accelerator: key,
    click: () => windows.showApp(section),
  });
  const template: MenuItemConstructorOptions[] = [
    {
      label: "Bob",
      submenu: [
        { role: "about" },
        { type: "separator" },
        { label: "Settings…", accelerator: "CommandOrControl+,", click: () => windows.showSettings() },
        { type: "separator" },
        { label: "Start bobd", click: () => void controlBobd("start") },
        { label: "Restart bobd", click: () => void controlBobd("restart") },
        { type: "separator" },
        { role: "hide" },
        { role: "hideOthers" },
        { role: "unhide" },
        { type: "separator" },
        ...(store?.get().keepRunning
          ? [
              { label: "Close Bob (keeps running)", accelerator: "CommandOrControl+Q", click: () => quitFromApp() },
              { label: "Quit Bob Completely", accelerator: "Alt+CommandOrControl+Q", click: () => quitCompletely() },
            ]
          : [{ label: "Quit Bob", accelerator: "CommandOrControl+Q", click: () => quitFromApp() }]),
      ],
    },
    { role: "editMenu" },
    {
      label: "Go",
      submenu: [
        ...APP_SECTIONS.slice(0, 9).map((section, index) => goTo(section, `CommandOrControl+${index + 1}`)),
        { type: "separator" },
        { label: "Ask Bob (panel)", accelerator: "Alt+Space", registerAccelerator: false, click: () => windows.showPanel() },
      ],
    },
    {
      label: "View",
      submenu: [
        { label: "Refresh", accelerator: "CommandOrControl+R", click: () => void refreshCards() },
        ...(app.isPackaged ? [] : [{ role: "toggleDevTools" } as MenuItemConstructorOptions]),
      ],
    },
    { role: "windowMenu" },
  ];
  return Menu.buildFromTemplate(template);
}

// -- start ----------------------------------------------------------------------------------------

async function smoke(): Promise<void> {
  // Renders every window once, saves a picture of each, and quits. Used to check a build.
  const out = process.env.BOB_SMOKE_DIR ?? join(app.getPath("temp"), "bob-smoke");
  mkdirSync(out, { recursive: true });
  windows.showPanel();
  windows.showApp("today");
  windows.toggleMenu(tray!);
  const pause = (ms: number): Promise<void> => new Promise((done) => setTimeout(done, ms));
  await pause(Number(process.env.BOB_SMOKE_WAIT_MS ?? 4000));
  const question = process.env.BOB_SMOKE_ASK;
  if (question) {
    // A real question through the panel, as bob://ask would send it.
    const panel = windows.showPanel();
    handleUrl(`bob://ask?q=${encodeURIComponent(question)}`);
    for (let waited = 0; waited < 180000; waited += 1000) {
      await pause(1000);
      const finished = await panel.webContents.executeJavaScript("document.querySelector('.meta, .turn .error') !== null");
      if (finished) break;
    }
  }
  await pause(1500);
  const report: Record<string, unknown> = { connection: backend.state, detail: backend.detail, snapshot };
  for (const window of windows.all()) {
    const view = new URL(window.webContents.getURL()).searchParams;
    const name = `${view.get("view")}${view.get("mode") ? "-" + view.get("mode") : ""}`;
    report[name] = await window.webContents.executeJavaScript("document.body.innerText.slice(0, 600)");
    if (!window.isVisible()) continue; // a hidden window has nothing to capture
    try {
      writeFileSync(join(out, `${name}.png`), (await window.webContents.capturePage()).toPNG());
    } catch (error) {
      report[`${name}-capture`] = String(error);
    }
  }
  report.shortcuts = {
    panel: globalShortcut.isRegistered(PANEL_SHORTCUT),
  };
  writeFileSync(join(out, "report.json"), JSON.stringify(report, null, 2));
  quitCompletely();
}

app.whenReady().then(async () => {
  app.dock?.hide(); // a menu bar app
  store = new PrefsStore(join(app.getPath("userData"), "prefs.json"));
  backend = new Backend({ serverUrl: () => store.get().serverUrl, bobHome: () => store.get().bobHome });
  windows = new Windows();
  setupHost = new SetupHost({
    windows: {
      showSetup: (onClosed) => windows.showSetup(onClosed),
      closeSetup: () => windows.closeSetup(),
      showApp: () => windows.showApp(),
    },
    finished: (choices, home) => {
      applyPrefs({ bobHome: home, openAtLogin: choices.startAtLogin });
      void backend.reconnect().then(() => refreshCards());
    },
    ensureRunning: async () => {
      applyPrefs({ bobHome: sharedHome() });
      await controlBobd("start");
    },
  });
  registerIpc();
  setupHost.register();
  Menu.setApplicationMenu(buildMenu());
  // From source, give the Dock the app's icon; a packaged Bob.app has its own.
  const devIcon = join(__dirname, "..", "..", "build", "icon.png");
  if (!app.isPackaged && existsSync(devIcon)) app.dock?.setIcon(devIcon);

  if (!app.isDefaultProtocolClient("bob")) app.setAsDefaultProtocolClient("bob");
  if (app.isPackaged) app.setLoginItemSettings({ openAtLogin: store.get().openAtLogin });
  nativeTheme.themeSource = store.get().theme;
  // macOS appearance and "Reduce transparency" changes reach every window.
  nativeTheme.on("updated", () => broadcast());

  tray = new Tray(nativeImage.createEmpty());
  tray.setTitle("Bob");
  tray.setToolTip("Bob");
  tray.on("click", () => windows.toggleMenu(tray!));
  tray.on("right-click", () => windows.showApp());

  // If another app already holds a shortcut, registration fails and Bob says so.
  if (!globalShortcut.register(PANEL_SHORTCUT, () => windows.togglePanel())) {
    console.warn(`${PANEL_SHORTCUT} is taken by another app; open the panel from the menu bar.`);
  }

  setUpVoice();
  registerVoiceHotkey(store.get().voice);

  backend.on("message", onMessage);
  backend.on("state", (connection) => {
    if (connection === "connected") void refreshCards();
    else broadcast();
  });
  await backend.start();

  // The shared build checks that everything it needs is in place; the personal build never does.
  const setupOpened = !SMOKE && (await setupHost.onLaunch()) === "opened";

  if (setupOpened) {
    // The setup window is already showing.
  } else if (pendingUrl) {
    handleUrl(pendingUrl);
    pendingUrl = null;
  } else if (!SMOKE && !openedAtLogin()) {
    // Opened from Finder, Spotlight or the Dock: show the Bob window.
    windows.showApp();
  }
  started = true;
  if (SMOKE) await smoke();
});

// Closing every window must not quit a menu bar app.
app.on("window-all-closed", () => undefined);
app.on("will-quit", () => {
  globalShortcut.unregisterAll();
  backend?.stop();
});
