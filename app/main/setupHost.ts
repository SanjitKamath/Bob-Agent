// Connects first-run setup to the real Mac: the real commands, files and network, the window
// that shows progress, and the app settings that change once setup has finished.
import { app, BrowserWindow, dialog, ipcMain } from "electron";
import { execFile } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, renameSync, statfsSync, writeFileSync } from "node:fs";
import { homedir, release } from "node:os";
import { join } from "node:path";
import {
  DEFAULT_CHOICES,
  EMPTY_STATE,
  decideSetup,
  mergeChoices,
  parseState,
  type SetupChoices,
  type SetupSnapshot,
  type SetupState,
} from "../shared/setup";
import type { WheelPick } from "../shared/types";
import { SetupRunner, inspectMac, readCatalogue, readFacts, type SetupEnv } from "./setup";

const MAX_WHEEL = 25 * 1024 * 1024;

/** The shared build carries its own Python and files in Contents/Resources/runtime. */
export function runtimeFolder(): string | null {
  const folder = join(process.resourcesPath, "runtime");
  return app.isPackaged && existsSync(join(folder, "python")) ? folder : null;
}

export const sharedHome = (): string => join(homedir(), "Library", "Application Support", "Bob");

function realEnv(resources: string): SetupEnv {
  return {
    resources,
    home: sharedHome(),
    agents: join(homedir(), "Library", "LaunchAgents"),
    arch: process.arch,
    darwinMajor: Number.parseInt(release().split(".")[0] ?? "0", 10),
    uid: process.getuid?.() ?? 501,
    now: () => new Date(),
    sleep: (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
    fetch: (input, init) => fetch(input, init),
    freeBytes: async (path) => {
      let folder = path;
      while (!existsSync(folder) && folder !== "/") folder = join(folder, "..");
      const stats = statfsSync(folder);
      return stats.bavail * stats.bsize;
    },
    exec: (command, args, options) =>
      new Promise((resolve) => {
        execFile(
          command,
          args,
          { env: { ...process.env, ...options?.env }, maxBuffer: 8 * 1024 * 1024, timeout: 10 * 60_000 },
          (error, stdout, stderr) => {
            const code = error ? (typeof error.code === "number" ? error.code : 1) : 0;
            resolve({ code, stdout: String(stdout), stderr: String(stderr || (error && code !== 0 ? error.message : "")) });
          },
        );
      }),
  };
}

export interface SetupHostOptions {
  windows: {
    showSetup(onClosed: () => void): BrowserWindow;
    closeSetup(): void;
    showApp(): unknown;
  };
  /** Called when setup finishes: remember the folder, the login choice, and connect. */
  finished(choices: SetupChoices, home: string): void;
  /** Called at launch when nothing needs doing in the shared build: just make sure Bob runs. */
  ensureRunning(): Promise<void>;
}

export class SetupHost {
  private readonly resources = runtimeFolder();
  private runner: SetupRunner | null = null;
  private readonly stateFile = join(sharedHome(), "setup-state.json");

  constructor(private readonly options: SetupHostOptions) {}

  get bundled(): boolean {
    return this.resources !== null;
  }

  private loadState(): SetupState {
    try {
      return parseState(JSON.parse(readFileSync(this.stateFile, "utf8")));
    } catch {
      return { ...EMPTY_STATE, steps: {} };
    }
  }

  private saveState(state: SetupState): void {
    mkdirSync(sharedHome(), { recursive: true });
    const temp = `${this.stateFile}.tmp`;
    writeFileSync(temp, JSON.stringify(state, null, 2), "utf8");
    renameSync(temp, this.stateFile);
  }

  private async ensureRunner(): Promise<SetupRunner> {
    if (this.runner) return this.runner;
    const env = realEnv(this.resources!);
    const runner = new SetupRunner(
      env,
      {
        loadState: () => this.loadState(),
        saveState: (state) => this.saveState(state),
        catalogue: () => readCatalogue(env),
        facts: () => readFacts(env),
        onFinished: (choices) => {
          this.options.finished(choices, env.home);
          // Give the person a moment on the "done" screen, then open Bob.
        },
      },
      this.loadState().choices ?? DEFAULT_CHOICES,
      { catalogue: await readCatalogue(env), facts: await readFacts(env) },
    );
    runner.on("change", (snapshot: SetupSnapshot) => {
      for (const window of BrowserWindow.getAllWindows()) window.webContents.send("bob:setup-changed", snapshot);
    });
    this.runner = runner;
    return runner;
  }

  /** At launch: open setup if this is the shared build and something is missing. */
  async onLaunch(): Promise<"opened" | "running" | "personal"> {
    if (!this.resources) return "personal";
    const state = this.loadState();
    const env = realEnv(this.resources);
    const look = await inspectMac(env, state.choices);
    const decision = decideSetup({ bundled: true, state, ollamaUp: look.ollamaUp, missingModels: look.missingModels });
    if (decision === "none") {
      await this.options.ensureRunning();
      return "running";
    }
    await this.open();
    return "opened";
  }

  async open(): Promise<void> {
    if (!this.resources) return;
    const runner = await this.ensureRunner();
    const env = realEnv(this.resources);
    runner.reset({ catalogue: await readCatalogue(env), facts: await readFacts(env) });
    this.options.windows.showSetup(() => this.runner?.cancel());
  }

  register(): void {
    ipcMain.handle("bob:setup-bundled", () => this.bundled);
    ipcMain.handle("bob:setup-open", () => this.open());
    ipcMain.handle("bob:setup-snapshot", async () => (await this.ensureRunner()).snapshot());
    ipcMain.handle("bob:setup-start", async (_event, choices: unknown) => {
      const runner = await this.ensureRunner();
      // Whatever the window sends is cleaned: only known fields and valid tags get through.
      void runner.start(mergeChoices(choices, DEFAULT_CHOICES));
    });
    ipcMain.handle("bob:setup-cancel", () => this.runner?.cancel());
    ipcMain.handle("bob:pick-folder", async (event, message: unknown): Promise<string | null> => {
      const parent = BrowserWindow.fromWebContents(event.sender) ?? undefined;
      const options: Electron.OpenDialogOptions = {
        title: "Choose a folder",
        message: typeof message === "string" ? message.slice(0, 200) : "Choose a folder",
        properties: ["openDirectory"],
      };
      const chosen = parent ? await dialog.showOpenDialog(parent, options) : await dialog.showOpenDialog(options);
      const folder = chosen.filePaths[0];
      return chosen.canceled || !folder ? null : folder;
    });
    ipcMain.handle("bob:pick-wheel", async (event): Promise<WheelPick | null> => {
      const parent = BrowserWindow.fromWebContents(event.sender) ?? undefined;
      const options: Electron.OpenDialogOptions = {
        title: "Add a plugin",
        message: "Choose a plugin file (.whl)",
        properties: ["openFile"],
        filters: [{ name: "Python wheel", extensions: ["whl"] }],
      };
      const chosen = parent ? await dialog.showOpenDialog(parent, options) : await dialog.showOpenDialog(options);
      const file = chosen.filePaths[0];
      if (chosen.canceled || !file) return null;
      const data = readFileSync(file);
      if (data.length > MAX_WHEEL) throw new Error("That file is larger than a plugin may be (25 MB).");
      return { filename: file.split("/").at(-1) ?? "plugin.whl", data_b64: data.toString("base64") };
    });
  }
}
