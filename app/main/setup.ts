// First-run setup: the steps, and the runner that carries them out.
//
// Everything here is a fixed list written in code. The setup window can ask the runner to
// start, retry or cancel; it can never send it a command. Steps only write under Bob's own
// folder and ~/Library/LaunchAgents, never ask for an admin password, and never touch git.
import { createHash } from "node:crypto";
import { EventEmitter, once } from "node:events";
import { createWriteStream } from "node:fs";
import { access, chmod, copyFile, mkdir, readFile, rename, rm, writeFile } from "node:fs/promises";
import { join } from "node:path";
import {
  STEP_IDS,
  STEP_LABELS,
  type Catalogue,
  type OllamaPin,
  type SetupChoices,
  type SetupFacts,
  type SetupSnapshot,
  type SetupState,
  type StepId,
  type StepStatus,
  LABEL_BOBD,
  LABEL_OLLAMA,
  bobdPlist,
  choicesKey,
  formatBytes,
  managedOllamaUrl,
  missingTags,
  ollamaPlist,
  renderConfig,
  tagsToPull,
} from "../shared/setup";

export interface ExecResult {
  code: number;
  stdout: string;
  stderr: string;
}

export interface SetupEnv {
  /** Bob.app/Contents/Resources/runtime: Python, bobd.sh, the template, the catalogue, the pin. */
  resources: string;
  /** ~/Library/Application Support/Bob */
  home: string;
  /** ~/Library/LaunchAgents */
  agents: string;
  arch: string;
  /** The Darwin major version: 22 is macOS 13. */
  darwinMajor: number;
  uid: number;
  exec(command: string, args: string[], options?: { env?: Record<string, string> }): Promise<ExecResult>;
  fetch: typeof fetch;
  freeBytes(path: string): Promise<number>;
  sleep(ms: number): Promise<void>;
  now(): Date;
}

/** A failure with a message the person can act on. */
export class SetupError extends Error {}

const OFFLINE = "Bob needs the internet once, to fetch Ollama and the models. Check your connection and try again.";
const MIN_FREE = 2 * 1024 ** 3;
const MODELS_ADVICE = 30 * 1024 ** 3;

export const paths = (env: SetupEnv) => ({
  python: join(env.resources, "python", "bin", "python3"),
  bobdScript: join(env.resources, "bobd.sh"),
  template: join(env.resources, "config.template.yaml"),
  pin: join(env.resources, "ollama.json"),
  catalogue: join(env.resources, "models.json"),
  data: join(env.home, "data"),
  deploy: join(env.home, "deploy"),
  script: join(env.home, "deploy", "bobd.sh"),
  config: join(env.home, "config.yaml"),
  ollamaDir: join(env.home, "ollama"),
  ollamaBin: join(env.home, "ollama", "bin", "ollama"),
  ollamaVersion: join(env.home, "ollama", "bin", ".version"),
  ollamaModels: join(env.home, "ollama", "models"),
  bobdPlist: join(env.agents, `${LABEL_BOBD}.plist`),
  ollamaPlist: join(env.agents, `${LABEL_OLLAMA}.plist`),
  log: join(env.home, "data", "bobd.log"),
  ollamaLog: join(env.home, "data", "ollama.log"),
});

async function exists(path: string): Promise<boolean> {
  return access(path).then(
    () => true,
    () => false,
  );
}

async function sameContent(path: string, text: string): Promise<boolean> {
  return readFile(path, "utf8").then(
    (current) => current === text,
    () => false,
  );
}

async function json<T>(env: SetupEnv, url: string, init?: RequestInit, timeoutMs = 2000): Promise<T | null> {
  try {
    const response = await env.fetch(url, { ...init, signal: init?.signal ?? AbortSignal.timeout(timeoutMs) });
    return response.ok ? ((await response.json()) as T) : null;
  } catch {
    return null;
  }
}

/** The Ollama address this setup uses. */
export const ollamaUrl = (choices: SetupChoices): string => choices.externalOllama ?? managedOllamaUrl;

export async function installedTags(env: SetupEnv, url: string): Promise<string[] | null> {
  const found = await json<{ models?: { name?: string; model?: string }[] }>(env, `${url}/api/tags`);
  return found ? (found.models ?? []).map((m) => m.name ?? m.model ?? "").filter(Boolean) : null;
}

/** The quick look at launch: is Ollama answering, and how many chosen models are missing. */
export async function inspectMac(
  env: SetupEnv,
  choices: SetupChoices | null,
): Promise<{ ollamaUp: boolean; missingModels: number }> {
  const url = choices ? ollamaUrl(choices) : managedOllamaUrl;
  const tags = await installedTags(env, url);
  if (tags === null) return { ollamaUp: false, missingModels: 0 };
  return { ollamaUp: true, missingModels: choices ? missingTags(tagsToPull(choices.models), tags).length : 0 };
}

export async function readCatalogue(env: SetupEnv): Promise<Catalogue> {
  try {
    const raw = JSON.parse(await readFile(paths(env).catalogue, "utf8")) as Catalogue;
    return { source: String(raw.source ?? ""), models: Array.isArray(raw.models) ? raw.models : [] };
  } catch {
    return { source: "", models: [] };
  }
}

export async function readFacts(env: SetupEnv): Promise<SetupFacts> {
  const found: string[] = [];
  if (await json(env, "http://127.0.0.1:11434/api/version", undefined, 800)) {
    found.push("An Ollama you run yourself is already here. Bob leaves it alone and keeps its own copy.");
  }
  const free = await env.freeBytes(env.home).catch(() => 0);
  return { freeBytes: free, arch: env.arch, found };
}

// -- the steps ----------------------------------------------------------------------------------

interface Context {
  env: SetupEnv;
  choices: SetupChoices;
  state: SetupState;
  signal: AbortSignal;
  report(detail: string, progress?: { done: number; total: number } | null): void;
  log(line: string): void;
}

interface Step {
  id: StepId;
  /** True when there is nothing to do; the step is then marked done without running. */
  check(ctx: Context): Promise<boolean>;
  run(ctx: Context): Promise<void>;
  /** Shown as the step's state when it was not needed. */
  skipWhen?(ctx: Context): boolean;
}

const external = (ctx: Context): boolean => ctx.choices.externalOllama !== null;

async function launchd(env: SetupEnv, label: string, plist: string): Promise<void> {
  const domain = `gui/${env.uid}`;
  const loaded = await env.exec("/bin/launchctl", ["print", `${domain}/${label}`]);
  if (loaded.code !== 0) {
    const boot = await env.exec("/bin/launchctl", ["bootstrap", domain, plist]);
    if (boot.code !== 0) throw new SetupError(`macOS would not load ${label}: ${firstLine(boot.stderr)}`);
  }
  const kick = await env.exec("/bin/launchctl", ["kickstart", `${domain}/${label}`]);
  if (kick.code !== 0) throw new SetupError(`macOS would not start ${label}: ${firstLine(kick.stderr)}`);
}

function firstLine(text: string): string {
  return text.trim().split("\n").filter(Boolean).at(-1) ?? "no details";
}

async function waitFor(env: SetupEnv, url: string, seconds: number, signal: AbortSignal): Promise<boolean> {
  for (let i = 0; i < seconds; i += 1) {
    if (signal.aborted) return false;
    if (await json(env, url, undefined, 1500)) return true;
    await env.sleep(1000);
  }
  return false;
}

async function download(ctx: Context, pin: OllamaPin, file: string): Promise<void> {
  const { env } = ctx;
  let response: Response;
  try {
    response = await env.fetch(pin.url, { signal: ctx.signal });
  } catch (error) {
    if (ctx.signal.aborted) throw new SetupError("Stopped.");
    throw new SetupError(`${OFFLINE} (${(error as Error).message})`);
  }
  if (!response.ok || !response.body) throw new SetupError(`The Ollama download failed (HTTP ${response.status}).`);
  const hash = createHash("sha256");
  const out = createWriteStream(file);
  let done = 0;
  try {
    for await (const chunk of response.body as unknown as AsyncIterable<Uint8Array>) {
      hash.update(chunk);
      if (!out.write(chunk)) await once(out, "drain");
      done += chunk.length;
      ctx.report(`Downloading Ollama ${pin.version}`, { done, total: pin.bytes });
    }
  } catch (error) {
    out.destroy();
    if (ctx.signal.aborted) throw new SetupError("Stopped.");
    throw new SetupError(`${OFFLINE} (${(error as Error).message})`);
  }
  out.end();
  await once(out, "close");
  if (hash.digest("hex") !== pin.sha256) {
    await rm(file, { force: true });
    throw new SetupError("The Ollama download did not match the checksum Bob expects, so it was not installed.");
  }
}

async function pullModel(ctx: Context, tag: string, index: number, count: number): Promise<void> {
  const { env } = ctx;
  let response: Response;
  try {
    response = await env.fetch(`${ollamaUrl(ctx.choices)}/api/pull`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ model: tag, stream: true }),
      signal: ctx.signal,
    });
  } catch (error) {
    if (ctx.signal.aborted) throw new SetupError("Stopped. Starting again carries on from here.");
    throw new SetupError(`${OFFLINE} (${(error as Error).message})`);
  }
  if (!response.ok || !response.body) {
    throw new SetupError(`Ollama would not download ${tag} (HTTP ${response.status}).`);
  }
  const totals = new Map<string, { total: number; done: number }>();
  const decoder = new TextDecoder();
  let pending = "";
  let finished = false;
  const read = (line: string): void => {
    if (!line.trim()) return;
    let event: { status?: string; digest?: string; total?: number; completed?: number; error?: string };
    try {
      event = JSON.parse(line);
    } catch {
      return;
    }
    if (event.error) throw new SetupError(`Ollama could not download ${tag}: ${event.error}`);
    if (event.digest && event.total) totals.set(event.digest, { total: event.total, done: event.completed ?? 0 });
    if (event.status === "success") finished = true;
    const all = [...totals.values()];
    const total = all.reduce((sum, item) => sum + item.total, 0);
    const done = all.reduce((sum, item) => sum + item.done, 0);
    const name = count > 1 ? `${tag} (${index + 1} of ${count})` : tag;
    ctx.report(`Downloading ${name}${total ? `, ${formatBytes(done)} of ${formatBytes(total)}` : ""}`, total ? { done, total } : null);
  };
  try {
    for await (const chunk of response.body as unknown as AsyncIterable<Uint8Array>) {
      pending += decoder.decode(chunk, { stream: true });
      const lines = pending.split("\n");
      pending = lines.pop() ?? "";
      lines.forEach(read);
    }
    read(pending);
  } catch (error) {
    if (error instanceof SetupError) throw error;
    if (ctx.signal.aborted) throw new SetupError("Stopped. Starting again carries on from here.");
    throw new SetupError(`The download was cut off (${(error as Error).message}). Try again; it carries on from where it stopped.`);
  }
  if (!finished) throw new SetupError(`The download of ${tag} stopped early. Try again; it carries on from where it stopped.`);
}

export const STEPS: Step[] = [
  {
    id: "preflight",
    check: async () => false, // quick, and worth doing every time
    async run({ env, report, log }) {
      if (env.arch !== "arm64") throw new SetupError("Bob needs a Mac with Apple Silicon (M1 or later).");
      if (env.darwinMajor < 22) throw new SetupError("Bob needs macOS 13 or later.");
      const free = await env.freeBytes(env.home);
      if (free < MIN_FREE) {
        throw new SetupError(`Only ${formatBytes(free)} is free on this Mac. Free up some space and try again.`);
      }
      if (free < MODELS_ADVICE) log(`${formatBytes(free)} is free. The models are large; if the download stops for lack of space, free some up.`);
      if (!(await exists(paths(env).python))) {
        throw new SetupError("This copy of Bob is incomplete (its runtime is missing). Download Bob again.");
      }
      report(`${formatBytes(free)} free`);
    },
  },
  {
    id: "home",
    async check({ env }) {
      const p = paths(env);
      const wanted = await readFile(p.bobdScript, "utf8").catch(() => null);
      return wanted !== null && (await sameContent(p.script, wanted)) && (await exists(p.data));
    },
    async run({ env, report }) {
      const p = paths(env);
      for (const folder of [p.data, p.deploy, p.ollamaDir, join(env.home, "user_site"), env.agents]) {
        await mkdir(folder, { recursive: true });
      }
      await copyFile(p.bobdScript, p.script);
      await chmod(p.script, 0o755);
      // Bob's own runtime came inside the app you already chose to open; macOS must not stop
      // it again when the background service starts it.
      await env.exec("/usr/bin/xattr", ["-dr", "com.apple.quarantine", env.resources]);
      const probe = await env.exec(p.python, ["-c", "import bobd.service, bob_tasks.server"]);
      if (probe.code !== 0) {
        throw new SetupError(`Bob's own runtime would not start on this Mac: ${firstLine(probe.stderr)}`);
      }
      report("Bob's folder is ready");
    },
  },
  {
    id: "ollama",
    skipWhen: external,
    async check({ env }) {
      const p = paths(env);
      const pin = JSON.parse(await readFile(p.pin, "utf8")) as OllamaPin;
      const have = await readFile(p.ollamaVersion, "utf8").catch(() => "");
      return have.trim() === pin.version && (await exists(p.ollamaBin));
    },
    async run(ctx) {
      const { env, report } = ctx;
      const p = paths(env);
      const pin = JSON.parse(await readFile(p.pin, "utf8")) as OllamaPin;
      const archive = join(p.ollamaDir, "ollama-download.tgz");
      await mkdir(p.ollamaDir, { recursive: true });
      await download(ctx, pin, archive);
      report("Unpacking Ollama");
      const staging = join(p.ollamaDir, "bin.new");
      await rm(staging, { recursive: true, force: true });
      await mkdir(staging, { recursive: true });
      const unpacked = await env.exec("/usr/bin/tar", ["-xzf", archive, "-C", staging]);
      if (unpacked.code !== 0) throw new SetupError(`Ollama would not unpack: ${firstLine(unpacked.stderr)}`);
      const signed = await env.exec("/usr/bin/codesign", ["--verify", "--strict", join(staging, "ollama")]);
      if (signed.code !== 0) {
        await rm(staging, { recursive: true, force: true });
        throw new SetupError(`macOS did not accept the downloaded program's signature, so it was not installed: ${firstLine(signed.stderr)}`);
      }
      await rm(join(p.ollamaDir, "bin"), { recursive: true, force: true });
      await rename(staging, join(p.ollamaDir, "bin"));
      await writeFile(p.ollamaVersion, pin.version, "utf8");
      await rm(archive, { force: true });
      const version = await env.exec(p.ollamaBin, ["--version"]);
      if (version.code !== 0) throw new SetupError(`The new Ollama would not run: ${firstLine(version.stderr)}`);
      report(`Ollama ${pin.version} installed`);
    },
  },
  {
    id: "runner",
    skipWhen: external,
    async check({ env, choices }) {
      return (await installedTags(env, ollamaUrl(choices))) !== null;
    },
    async run(ctx) {
      const { env, choices, report } = ctx;
      const p = paths(env);
      await writeFile(
        p.ollamaPlist,
        ollamaPlist({ binary: p.ollamaBin, models: p.ollamaModels, home: p.ollamaDir, log: p.ollamaLog }),
        "utf8",
      );
      await mkdir(p.ollamaModels, { recursive: true });
      report("Starting the model runner");
      await launchd(env, LABEL_OLLAMA, p.ollamaPlist);
      if (!(await waitFor(env, `${ollamaUrl(choices)}/api/version`, 30, ctx.signal))) {
        throw new SetupError(`The model runner did not answer within 30 seconds. Its log is ${p.ollamaLog}.`);
      }
      report("The model runner is running");
    },
  },
  {
    id: "models",
    async check({ env, choices }) {
      const have = await installedTags(env, ollamaUrl(choices));
      return have !== null && missingTags(tagsToPull(choices.models), have).length === 0;
    },
    async run(ctx) {
      const { env, choices } = ctx;
      const have = (await installedTags(env, ollamaUrl(choices))) ?? [];
      const missing = missingTags(tagsToPull(choices.models), have);
      for (const [index, tag] of missing.entries()) await pullModel(ctx, tag, index, missing.length);
      const after = (await installedTags(env, ollamaUrl(choices))) ?? [];
      const still = missingTags(tagsToPull(choices.models), after);
      if (still.length) throw new SetupError(`These models are still missing: ${still.join(", ")}.`);
      ctx.report("All models are here");
    },
  },
  {
    id: "config",
    async check({ env, choices, state }) {
      return state.configFor === choicesKey(choices) && (await exists(paths(env).config));
    },
    async run({ env, choices, state, report }) {
      const p = paths(env);
      const text = renderConfig(await readFile(p.template, "utf8"), choices.models, ollamaUrl(choices));
      if ((await exists(p.config)) && !(await sameContent(p.config, text))) {
        // Settings you edited are never lost: the old file is kept beside the new one.
        const stamp = env.now().toISOString().replace(/[-:T]/g, "").slice(0, 14);
        await rename(p.config, `${p.config}.bak-${stamp}`);
      }
      await writeFile(p.config, text, "utf8");
      state.configFor = choicesKey(choices);
      report("Settings written");
    },
  },
  {
    id: "bobd",
    async check({ env }) {
      const p = paths(env);
      const wanted = bobdPlist({ python: p.python, config: p.config, home: env.home, log: p.log });
      return (await sameContent(p.bobdPlist, wanted)) && (await json(env, "http://127.0.0.1:8765/health")) !== null;
    },
    async run({ env, choices, report }) {
      const p = paths(env);
      await writeFile(p.bobdPlist, bobdPlist({ python: p.python, config: p.config, home: env.home, log: p.log }), "utf8");
      report("Starting Bob");
      const started = await env.exec("/bin/bash", [p.script, "restart"], {
        env: { BOB_URL: "http://127.0.0.1:8765", BOB_OLLAMA_URL: ollamaUrl(choices) },
      });
      if (started.code !== 0) throw new SetupError(firstLine(started.stdout) || firstLine(started.stderr));
      report("Bob is running");
    },
  },
];

// -- the runner ----------------------------------------------------------------------------------

export interface RunnerHooks {
  loadState(): SetupState;
  saveState(state: SetupState): void;
  catalogue(): Promise<Catalogue>;
  facts(): Promise<SetupFacts>;
  onFinished(choices: SetupChoices): void;
}

export class SetupRunner extends EventEmitter {
  private statuses: StepStatus[] = this.fresh();
  private phase: SetupSnapshot["phase"] = "welcome";
  private errorText = "";
  private lines: string[] = [];
  private abort: AbortController | null = null;
  private busy = false;

  constructor(
    private readonly env: SetupEnv,
    private readonly hooks: RunnerHooks,
    private choices: SetupChoices,
    private cache: { catalogue: Catalogue; facts: SetupFacts },
  ) {
    super();
    const saved = hooks.loadState();
    if (saved.choices) this.choices = saved.choices;
  }

  private fresh(): StepStatus[] {
    return STEP_IDS.map((id) => ({ id, label: STEP_LABELS[id], state: "waiting", detail: "", progress: null }));
  }

  snapshot(): SetupSnapshot {
    return {
      phase: this.phase,
      steps: this.statuses.map((s) => ({ ...s })),
      choices: this.choices,
      catalogue: this.cache.catalogue,
      facts: this.cache.facts,
      log: this.lines.slice(-200),
      error: this.errorText,
    };
  }

  private changed(): void {
    this.emit("change", this.snapshot());
  }

  private status(id: StepId): StepStatus {
    return this.statuses.find((s) => s.id === id)!;
  }

  /** Back to the first screen with fresh facts, unless a run is going on. */
  reset(cache: { catalogue: Catalogue; facts: SetupFacts }): void {
    if (this.busy) return;
    this.cache = cache;
    this.phase = "welcome";
    this.statuses = this.fresh();
    this.errorText = "";
    this.lines = [];
    const saved = this.hooks.loadState();
    if (saved.choices) this.choices = saved.choices;
    this.changed();
  }

  cancel(): void {
    this.abort?.abort();
  }

  /** Run every step that is not already done. Safe to call again after a failure. */
  async start(choices: SetupChoices): Promise<void> {
    if (this.busy) return;
    this.busy = true;
    this.choices = choices;
    this.errorText = "";
    this.phase = "running";
    this.statuses = this.fresh();
    const state = this.hooks.loadState();
    state.choices = choices;
    this.abort = new AbortController();
    try {
      for (const step of STEPS) {
        const status = this.status(step.id);
        const ctx: Context = {
          env: this.env,
          choices,
          state,
          signal: this.abort.signal,
          report: (detail, progress = null) => {
            status.detail = detail;
            status.progress = progress;
            this.changed();
          },
          log: (line) => {
            this.lines.push(line);
            this.changed();
          },
        };
        status.state = "running";
        this.changed();
        try {
          if (step.skipWhen?.(ctx)) {
            status.state = "skipped";
            status.detail = "Not needed";
          } else if (await step.check(ctx)) {
            status.state = "done";
            status.detail = "Already done";
          } else {
            await step.run(ctx);
            status.state = "done";
            status.progress = null;
          }
          state.steps[step.id] = status.state === "skipped" ? "skipped" : "done";
          this.hooks.saveState(state);
        } catch (error) {
          status.state = "failed";
          status.progress = null;
          status.detail = error instanceof SetupError ? error.message : `Something unexpected went wrong: ${(error as Error).message}`;
          this.errorText = status.detail;
          this.lines.push(`${step.id}: ${status.detail}`);
          this.phase = "failed";
          this.hooks.saveState(state);
          this.changed();
          return;
        }
        this.changed();
      }
      state.finishedAt = this.env.now().toISOString();
      this.hooks.saveState(state);
      this.phase = "done";
      this.changed();
      this.hooks.onFinished(choices);
    } finally {
      this.busy = false;
      this.abort = null;
    }
  }
}
