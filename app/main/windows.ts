// The app's windows: the main Bob window, hotkey panel and menu bar dropdown.
import { app, BrowserWindow, screen, shell, type Tray } from "electron";
import { join } from "node:path";
import type { AppSection, ViewName } from "../shared/types";
import { BLUR_GUARD_MS, menuPosition, shouldOpen } from "./placement";

const PRELOAD = join(__dirname, "preload.js");
const PAGE = join(__dirname, "..", "renderer", "index.html");

function create(
  view: ViewName,
  options: Electron.BrowserWindowConstructorOptions,
  mode = "",
  query: Record<string, string> = {},
): BrowserWindow {
  const window = new BrowserWindow({
    show: false,
    skipTaskbar: true,
    fullscreenable: false,
    ...options,
    webPreferences: {
      preload: PRELOAD,
      contextIsolation: true,
      sandbox: true,
      nodeIntegration: false,
      webSecurity: true,
    },
  });
  // Windows only ever show the app's own page; links open in the browser.
  window.webContents.setWindowOpenHandler(({ url }) => {
    if (/^https?:\/\//.test(url)) void shell.openExternal(url);
    return { action: "deny" };
  });
  window.webContents.on("will-navigate", (event) => event.preventDefault());
  void window.loadFile(PAGE, { query: { view, ...(mode ? { mode } : {}), ...query } });
  return window;
}

/**
 * Let a panel window appear over other apps' full-screen spaces and on every desktop.
 *
 * ``skipTransformProcessType`` matters: by default Electron turns Bob into a background-only
 * process for this, which makes the Dock icon vanish and come back while the Bob window is
 * open, and loses the window's focus. Bob decides for itself when the Dock icon shows.
 */
function floatOverEverything(window: BrowserWindow): void {
  window.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true, skipTransformProcessType: true });
  window.setAlwaysOnTop(true, "pop-up-menu");
}

/** Bob's Dock icon follows its windows; switching it when nothing changes makes macOS flicker. */
function showDock(): void {
  if (app.dock && !app.dock.isVisible()) void app.dock.show();
}
function hideDock(): void {
  if (app.dock?.isVisible()) app.dock.hide();
}

const shownAt = new WeakMap<BrowserWindow, number>();
const SMOKE = process.argv.includes("--smoke");

/** Show a floating window and give it the keyboard, noting when, for ``hideWhenAway``. */
function reveal(window: BrowserWindow): void {
  shownAt.set(window, Date.now());
  window.show();
  window.focus();
}

/**
 * A floating window that goes away when you click or switch to something else, as a launcher
 * does. Otherwise it would stay on top of whatever app or desktop you moved to.
 */
function hideWhenAway(window: BrowserWindow): void {
  window.on("blur", () => {
    if (SMOKE || Date.now() - (shownAt.get(window) ?? 0) < BLUR_GUARD_MS) return;
    if (window.isVisible()) window.hide();
  });
}

/**
 * Bring a normal window to the front on the desktop you are on, with the keyboard.
 *
 * Without ``app.focus({ steal: true })`` a window opened from the menu bar or the hotkey can
 * appear behind the app you were using. Marking it as on every desktop for a moment, then
 * back, moves it here instead of sending you to the desktop it was last on.
 */
function bringHere(window: BrowserWindow): void {
  if (window.isMinimized()) window.restore();
  app.focus({ steal: true });
  window.setVisibleOnAllWorkspaces(true, { skipTransformProcessType: true });
  window.show();
  window.focus();
  setTimeout(() => {
    if (!window.isDestroyed()) window.setVisibleOnAllWorkspaces(false, { skipTransformProcessType: true });
  }, 300);
}

export class Windows {
  private panel: BrowserWindow | null = null;
  private menu: BrowserWindow | null = null;
  private main: BrowserWindow | null = null;
  private setup: BrowserWindow | null = null;
  private newTask: BrowserWindow | null = null;
  private menuShownAt = 0;
  private menuHiddenAt = 0;

  all(): BrowserWindow[] {
    return [this.main, this.panel, this.menu, this.setup, this.newTask].filter(
      (window): window is BrowserWindow => window !== null && !window.isDestroyed(),
    );
  }

  // -- hotkey panel ---------------------------------------------------------------

  private ensurePanel(): BrowserWindow {
    if (this.panel && !this.panel.isDestroyed()) return this.panel;
    const panel = create("panel", {
      type: "panel", // floats, even over full-screen apps
      width: 720,
      height: 540,
      minWidth: 480,
      minHeight: 320,
      frame: false,
      transparent: true,
      vibrancy: "under-window",
      visualEffectState: "active",
      alwaysOnTop: true,
      hiddenInMissionControl: true,
    });
    floatOverEverything(panel);
    hideWhenAway(panel);
    panel.on("closed", () => (this.panel = null));
    this.panel = panel;
    return panel;
  }

  showPanel(): BrowserWindow {
    const panel = this.ensurePanel();
    if (!panel.isVisible()) {
      // Open on the display the pointer is on, a third of the way down.
      const area = screen.getDisplayNearestPoint(screen.getCursorScreenPoint()).workArea;
      const [width, height] = panel.getSize();
      panel.setPosition(
        Math.round(area.x + (area.width - width) / 2),
        Math.round(area.y + (area.height - height) / 3),
      );
    }
    reveal(panel);
    return panel;
  }

  togglePanel(): void {
    const panel = this.ensurePanel();
    if (panel.isVisible() && panel.isFocused()) panel.hide();
    else this.showPanel();
  }

  /**
   * The voice hotkey: start or stop listening in the chat you are using. In the Bob window that
   * is the Chat section; anywhere else it is the hotkey panel.
   */
  voiceToggle(): void {
    const focused = this.main && !this.main.isDestroyed() && this.main.isFocused() ? this.main : null;
    const target = focused ? this.showApp("chat") : this.showPanel();
    const send = (): void => target.webContents.send("bob:voice-toggle");
    if (target.webContents.isLoading()) target.webContents.once("did-finish-load", send);
    else send();
  }

  /** Send text to the panel as if you had typed and sent it. */
  ask(text: string): void {
    const panel = this.showPanel();
    const send = (): void => panel.webContents.send("bob:ask-text", text);
    if (panel.webContents.isLoading()) panel.webContents.once("did-finish-load", send);
    else send();
  }

  // -- menu bar dropdown --------------------------------------------------------------

  toggleMenu(tray: Tray): void {
    if (!this.menu || this.menu.isDestroyed()) {
      this.menu = create("menu", {
        type: "panel", // can open over another app's full-screen space
        width: 340,
        height: 600,
        frame: false,
        resizable: false,
        movable: false,
        transparent: true,
        vibrancy: "menu",
        visualEffectState: "active",
        alwaysOnTop: true,
        hiddenInMissionControl: true,
      });
      floatOverEverything(this.menu);
      this.menu.on("blur", () => {
        // A blur that arrives while the dropdown is still opening would close it at once.
        if (Date.now() - this.menuShownAt < BLUR_GUARD_MS) return;
        this.hideMenu();
      });
      this.menu.on("hide", () => (this.menuHiddenAt = Date.now()));
      this.menu.on("closed", () => (this.menu = null));
    }
    const menu = this.menu;
    if (!shouldOpen(menu.isVisible(), this.menuHiddenAt, Date.now())) {
      this.hideMenu();
      return;
    }
    const icon = tray.getBounds();
    const pointer = screen.getCursorScreenPoint();
    const area = screen.getDisplayMatching(icon.width > 0 ? icon : { ...pointer, width: 1, height: 1 }).workArea;
    const [width] = menu.getSize();
    const at = menuPosition(icon, area, pointer, width);
    menu.setPosition(at.x, at.y);
    this.menuShownAt = Date.now();
    menu.show();
    menu.focus();
  }

  private hideMenu(): void {
    if (this.menu && !this.menu.isDestroyed() && this.menu.isVisible()) this.menu.hide();
  }

  // -- the "New task" window ---------------------------------------------------------------

  /** A small window with the whole task form, opened from the dropdown. */
  showNewTask(): BrowserWindow {
    if (!this.newTask || this.newTask.isDestroyed()) {
      const window = create("newtask", {
        type: "panel", // can open over another app's full-screen space
        width: 440,
        height: 560,
        resizable: false,
        frame: false,
        transparent: true,
        vibrancy: "under-window",
        visualEffectState: "active",
        hiddenInMissionControl: true,
      });
      floatOverEverything(window);
      hideWhenAway(window);
      window.on("closed", () => (this.newTask = null));
      this.newTask = window;
    }
    const window = this.newTask;
    this.hideMenu();
    const area = screen.getDisplayNearestPoint(screen.getCursorScreenPoint()).workArea;
    const [width, height] = window.getSize();
    window.setPosition(
      Math.round(area.x + (area.width - width) / 2),
      Math.round(area.y + (area.height - height) / 3),
    );
    reveal(window);
    return window;
  }

  // -- the main Bob window ---------------------------------------------------------------
  //
  // A normal Mac window with everything in one place. While it is open Bob shows a Dock
  // icon and app menus; when it closes Bob goes back to being a menu bar app.

  showApp(section?: AppSection): BrowserWindow {
    let main = this.main;
    if (!main || main.isDestroyed()) {
      main = create(
        "app",
        {
          width: 1080,
          height: 720,
          minWidth: 820,
          minHeight: 520,
          title: "Bob",
          titleBarStyle: "hiddenInset",
          skipTaskbar: false,
          // Native full screen hides the menu bar, and with it Bob's icon and the clock; the
          // green button zooms the window to fill the screen instead.
          fullscreenable: false,
          // The sidebar is translucent: macOS draws the desktop through the window and
          // each area paints its own surface over it.
          vibrancy: "sidebar",
          visualEffectState: "followWindow",
          backgroundColor: "#00000000",
        },
        "",
        section ? { section } : {},
      );
      main.on("closed", () => {
        this.main = null;
        if (!this.setup || this.setup.isDestroyed()) hideDock();
      });
      this.main = main;
    } else if (section) {
      const target = main;
      const send = (): void => target.webContents.send("bob:navigate", section);
      if (target.webContents.isLoading()) target.webContents.once("did-finish-load", send);
      else send();
    }
    showDock();
    bringHere(main);
    return main;
  }

  /** The first-run setup window: small, centred, one task. Closing it stops a run in progress. */
  showSetup(onClosed: () => void): BrowserWindow {
    if (this.setup && !this.setup.isDestroyed()) {
      showDock();
      bringHere(this.setup);
      return this.setup;
    }
    const setup = create("setup", {
      width: 640,
      height: 700,
      minWidth: 560,
      minHeight: 560,
      title: "Set up Bob",
      titleBarStyle: "hiddenInset",
      skipTaskbar: false,
      vibrancy: "sidebar",
      visualEffectState: "followWindow",
      backgroundColor: "#00000000",
      show: true,
    });
    setup.once("ready-to-show", () => bringHere(setup));
    showDock();
    setup.on("closed", () => {
      this.setup = null;
      if (!this.main || this.main.isDestroyed()) hideDock();
      onClosed();
    });
    this.setup = setup;
    return setup;
  }

  closeSetup(): void {
    this.setup?.destroy();
  }

  showSettings(section: AppSection = "plugins"): void {
    this.showApp(section);
  }

  /** Open the main window on Chat and send text there as if you had typed it. */
  askInApp(text: string): void {
    const main = this.showApp("chat");
    const send = (): void => main.webContents.send("bob:ask-text", text);
    if (main.webContents.isLoading()) main.webContents.once("did-finish-load", send);
    else send();
  }

  /** Close the Bob window and tuck the panel and dropdown away; the menu bar item stays. */
  closeAll(): void {
    this.main?.close();
    this.panel?.hide();
    this.newTask?.hide();
    this.hideMenu();
  }

  hideSender(sender: Electron.WebContents): void {
    const window = BrowserWindow.fromWebContents(sender);
    window?.hide();
  }
}
