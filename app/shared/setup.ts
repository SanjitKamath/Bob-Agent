// First-run setup: what it does, in what order, and the pure pieces that need no Mac to test.

export const STEP_IDS = ["preflight", "home", "ollama", "runner", "models", "config", "bobd"] as const;
export type StepId = (typeof STEP_IDS)[number];

export const STEP_LABELS: Record<StepId, string> = {
  preflight: "Check this Mac",
  home: "Set up Bob's folder",
  ollama: "Install Ollama",
  runner: "Start the model runner",
  models: "Download the models",
  config: "Write Bob's settings",
  bobd: "Start Bob",
};

export type StepState = "waiting" | "running" | "done" | "skipped" | "failed";

export interface StepStatus {
  id: StepId;
  label: string;
  state: StepState;
  /** What it is doing, or why it failed, in plain words. */
  detail: string;
  /** Bytes or items done and total, when known. */
  progress: { done: number; total: number } | null;
}

export const ROLES = ["router", "main", "coder", "vision", "embeddings"] as const;
export type ModelRole = (typeof ROLES)[number];

export const ROLE_LABELS: Record<ModelRole, string> = {
  router: "Router (quick decisions)",
  main: "Main (answers and tools)",
  coder: "Coder (forms and rules)",
  vision: "Vision (images)",
  embeddings: "Embeddings (search)",
};

export type ModelChoices = Record<ModelRole, string>;

/** The models the shipped config uses; each role starts here. */
export const DEFAULT_MODELS: ModelChoices = {
  router: "gemma4:e4b",
  main: "gpt-oss:20b",
  coder: "gpt-oss:20b",
  vision: "gemma4:e4b",
  embeddings: "qwen3-embedding:0.6b",
};

export interface SetupChoices {
  models: ModelChoices;
  /** Open Bob when you log in (the app's own login item). */
  startAtLogin: boolean;
  /** An Ollama address you already run; when set, Bob installs no Ollama and downloads no models. */
  externalOllama: string | null;
}

export const DEFAULT_CHOICES: SetupChoices = {
  models: DEFAULT_MODELS,
  startAtLogin: true,
  externalOllama: null,
};

/** The address of the Ollama that Bob manages. Not 11434, so it never meets one you run yourself. */
export const MANAGED_OLLAMA_PORT = 11435;
export const managedOllamaUrl = `http://127.0.0.1:${MANAGED_OLLAMA_PORT}`;

export const LABEL_BOBD = "com.bob.bobd";
export const LABEL_OLLAMA = "com.bob.ollama";

export interface CatalogueModel {
  name: string;
  /** Letters: t tools, v vision, e embedding, k thinking, a audio, d decision. */
  caps: string;
  sizes: string[];
}

export interface Catalogue {
  source: string;
  models: CatalogueModel[];
}

export interface OllamaPin {
  version: string;
  url: string;
  sha256: string;
  bytes: number;
}

/** What setup remembers between launches. */
export interface SetupState {
  choices: SetupChoices | null;
  /** Steps finished, so a quit in the middle carries on from there. */
  steps: Partial<Record<StepId, "done" | "skipped">>;
  /** The choices the settings file was last written for. */
  configFor: string;
  finishedAt: string | null;
}

export const EMPTY_STATE: SetupState = { choices: null, steps: {}, configFor: "", finishedAt: null };

export function parseState(raw: unknown): SetupState {
  const input = (raw && typeof raw === "object" ? raw : {}) as Partial<SetupState>;
  const steps: SetupState["steps"] = {};
  for (const id of STEP_IDS) {
    const value = (input.steps as Record<string, unknown> | undefined)?.[id];
    if (value === "done" || value === "skipped") steps[id] = value;
  }
  const choices = input.choices && typeof input.choices === "object" ? mergeChoices(input.choices) : null;
  return {
    choices,
    steps,
    configFor: typeof input.configFor === "string" ? input.configFor : "",
    finishedAt: typeof input.finishedAt === "string" ? input.finishedAt : null,
  };
}

// -- choices ----------------------------------------------------------------------------------

const TAG = /^[a-z0-9][a-z0-9._/-]*(:[A-Za-z0-9._-]+)?$/;

/** A model tag Bob accepts: plain Ollama syntax, and never a cloud tag (those leave the Mac). */
export function validTag(tag: string): boolean {
  if (!TAG.test(tag) || tag.length > 120) return false;
  const suffix = tag.includes(":") ? tag.split(":").pop()! : "";
  return !suffix.split("-").includes("cloud");
}

export function mergeChoices(input: unknown, base: SetupChoices = DEFAULT_CHOICES): SetupChoices {
  const patch = (input && typeof input === "object" ? input : {}) as Partial<SetupChoices>;
  const models = { ...base.models };
  for (const role of ROLES) {
    const tag = (patch.models as Record<string, unknown> | undefined)?.[role];
    if (typeof tag === "string" && validTag(tag.trim())) models[role] = tag.trim();
  }
  let external = base.externalOllama;
  if (patch.externalOllama === null) external = null;
  else if (typeof patch.externalOllama === "string" && isLoopbackUrl(patch.externalOllama)) {
    external = patch.externalOllama.replace(/\/+$/, "");
  }
  return {
    models,
    startAtLogin: typeof patch.startAtLogin === "boolean" ? patch.startAtLogin : base.startAtLogin,
    externalOllama: external,
  };
}

/** Bob only ever talks to Ollama on this Mac. */
export function isLoopbackUrl(value: string): boolean {
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

/** Every tag to take from Ollama, once each. */
export function tagsToPull(models: ModelChoices): string[] {
  return [...new Set(ROLES.map((role) => models[role]))];
}

/** "gemma4" means "gemma4:latest" to Ollama. */
export function sameTag(a: string, b: string): boolean {
  const full = (tag: string): string => (tag.includes(":") ? tag : `${tag}:latest`);
  return full(a) === full(b);
}

export function missingTags(wanted: string[], installed: string[]): string[] {
  return wanted.filter((tag) => !installed.some((have) => sameTag(have, tag)));
}

// -- the model drop-downs ---------------------------------------------------------------------

/** The tags a catalogue entry can be pulled as. */
export function modelTags(model: CatalogueModel): string[] {
  return model.sizes.length ? model.sizes.map((size) => `${model.name}:${size}`) : [model.name];
}

const NEEDS: Record<ModelRole, (caps: string) => boolean> = {
  router: (caps) => !caps.includes("e") && !caps.includes("d"),
  main: (caps) => caps.includes("t"),
  coder: (caps) => caps.includes("t"),
  vision: (caps) => caps.includes("v"),
  embeddings: (caps) => caps.includes("e"),
};

/** Every model that fits a role, in the catalogue's order, with the shipped default first. */
export function optionsFor(role: ModelRole, catalogue: Catalogue): string[] {
  const tags = catalogue.models.filter((model) => NEEDS[role](model.caps)).flatMap(modelTags);
  const first = DEFAULT_MODELS[role];
  return [first, ...tags.filter((tag) => tag !== first)];
}

// -- the settings file ------------------------------------------------------------------------

const CONTEXT: Record<ModelRole, number> = { router: 8192, main: 32768, coder: 32768, vision: 8192, embeddings: 16384 };

/** The one model whose embedding size is known to Bob; other models use their own. */
const KNOWN_DIMENSIONS: Record<string, number> = { "qwen3-embedding:0.6b": 1024 };

export function modelLines(models: ModelChoices): string {
  return ROLES.map((role) => {
    const tag = models[role];
    const parts = [`model: "${tag}"`, `num_ctx: ${CONTEXT[role]}`];
    if (role === "router") parts.push("temperature: 0.0");
    if (role === "embeddings" && KNOWN_DIMENSIONS[tag]) parts.push(`dimensions: ${KNOWN_DIMENSIONS[tag]}`);
    return `  ${role}: {${parts.join(", ")}}`;
  }).join("\n");
}

export function renderConfig(template: string, models: ModelChoices, ollamaUrl: string): string {
  return template.replace("{{OLLAMA_URL}}", ollamaUrl).replace("{{MODELS}}", modelLines(models));
}

export function choicesKey(choices: SetupChoices): string {
  return JSON.stringify([choices.models, choices.externalOllama ?? ""]);
}

// -- launchd ----------------------------------------------------------------------------------

function esc(value: string): string {
  return value.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function plist(label: string, args: string[], env: Record<string, string>, cwd: string, log: string): string {
  const entries = Object.entries(env)
    .map(([key, value]) => `        <key>${esc(key)}</key><string>${esc(value)}</string>`)
    .join("\n");
  return `<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>${esc(label)}</string>
    <key>ProgramArguments</key>
    <array>
${args.map((arg) => `        <string>${esc(arg)}</string>`).join("\n")}
    </array>
    <key>WorkingDirectory</key><string>${esc(cwd)}</string>
    <key>EnvironmentVariables</key>
    <dict>
${entries}
    </dict>
    <!-- Bob starts this itself (Bob's bobd.sh); launchd only brings it back after a crash. -->
    <key>RunAtLoad</key><false/>
    <key>KeepAlive</key><dict><key>Crashed</key><true/></dict>
    <key>StandardOutPath</key><string>${esc(log)}</string>
    <key>StandardErrorPath</key><string>${esc(log)}</string>
</dict>
</plist>
`;
}

export function bobdPlist(o: { python: string; config: string; home: string; log: string }): string {
  return plist(
    LABEL_BOBD,
    [o.python, "-c", "from bobd.service import main; main()", o.config],
    { PATH: "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin", PYDANTIC_AI_NO_BANNER: "1", PYTHONDONTWRITEBYTECODE: "1" },
    o.home,
    o.log,
  );
}

export function ollamaPlist(o: { binary: string; models: string; home: string; log: string }): string {
  return plist(
    LABEL_OLLAMA,
    [o.binary, "serve"],
    {
      OLLAMA_HOST: `127.0.0.1:${MANAGED_OLLAMA_PORT}`,
      OLLAMA_MODELS: o.models,
      PATH: "/usr/bin:/bin",
    },
    o.home,
    o.log,
  );
}

// -- deciding whether to open setup ---------------------------------------------------------------

export type SetupDecision = "none" | "full" | "resume" | "models";

/**
 * Whether this launch opens the setup window.
 * Only the shared build ever does. A Mac that has finished setup and whose models are all there
 * sees nothing; one that lost its models goes straight to downloading them.
 */
export function decideSetup(o: {
  bundled: boolean;
  state: SetupState;
  ollamaUp: boolean;
  missingModels: number;
}): SetupDecision {
  if (!o.bundled) return "none";
  if (!o.state.finishedAt) return Object.keys(o.state.steps).length > 0 ? "resume" : "full";
  if (!o.ollamaUp) return "full";
  return o.missingModels > 0 ? "models" : "none";
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024 * 1024) return `${Math.max(0, Math.round(bytes / 1024))} KB`;
  if (bytes < 1024 ** 3) return `${(bytes / 1024 ** 2).toFixed(0)} MB`;
  return `${(bytes / 1024 ** 3).toFixed(1)} GB`;
}

// -- what the window shows ------------------------------------------------------------------------

export type SetupPhase = "welcome" | "running" | "failed" | "done";

export interface SetupFacts {
  freeBytes: number;
  arch: string;
  /** Whether Ollama and models are already on this Mac, said in a line each. */
  found: string[];
}

export interface SetupSnapshot {
  phase: SetupPhase;
  steps: StepStatus[];
  choices: SetupChoices;
  catalogue: Catalogue;
  facts: SetupFacts;
  log: string[];
  error: string;
}
