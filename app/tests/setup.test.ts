import { createHash } from "node:crypto";
import { existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { SetupRunner, type ExecResult, type RunnerHooks, type SetupEnv, inspectMac } from "../main/setup";
import {
  DEFAULT_CHOICES,
  DEFAULT_MODELS,
  EMPTY_STATE,
  bobdPlist,
  decideSetup,
  mergeChoices,
  missingTags,
  modelLines,
  optionsFor,
  ollamaPlist,
  parseState,
  renderConfig,
  tagsToPull,
  validTag,
  type Catalogue,
  type SetupState,
} from "../shared/setup";
import { readFileSync as read } from "node:fs";

// The template lives in the Bob folder's deploy/share; this file is in app/tests.
const TEMPLATE_AT = [join(__dirname, "..", "..", "deploy", "share"), join(__dirname, "..", "..", "bobnew", "deploy", "share")]
  .map((folder) => join(folder, "config.template.yaml"))
  .find((file) => existsSync(file))!;
const TEMPLATE = read(TEMPLATE_AT, "utf8");
const ARCHIVE = Buffer.from("pretend this is ollama-darwin.tgz");
const PIN = { version: "v9.9.9", url: "https://example.test/ollama.tgz", sha256: createHash("sha256").update(ARCHIVE).digest("hex"), bytes: ARCHIVE.length };

interface World {
  env: SetupEnv;
  home: string;
  installed: Set<string>;
  calls: string[];
  hooks: RunnerHooks;
  saved: () => SetupState;
  fetches: string[];
  options: { offline?: boolean; badArchive?: boolean; pullError?: string; arch?: string; free?: number };
}

function world(options: World["options"] = {}): World {
  const root = mkdtempSync(join(tmpdir(), "bob-setup-"));
  const resources = join(root, "res");
  const home = join(root, "support", "Bob");
  const agents = join(root, "agents");
  mkdirSync(join(resources, "python", "bin"), { recursive: true });
  writeFileSync(join(resources, "python", "bin", "python3"), "");
  writeFileSync(join(resources, "bobd.sh"), "#!/bin/bash\necho hi\n");
  writeFileSync(join(resources, "config.template.yaml"), TEMPLATE);
  writeFileSync(join(resources, "ollama.json"), JSON.stringify(PIN));
  writeFileSync(join(resources, "models.json"), JSON.stringify({ source: "t", models: [{ name: "gpt-oss", caps: "tk", sizes: ["20b"] }] }));
  const installed = new Set<string>();
  const calls: string[] = [];
  const fetches: string[] = [];
  let runnerUp = false;
  let bobdUp = false;
  const w: World = { env: null as never, home, installed, calls, hooks: null as never, saved: null as never, fetches, options };
  const ok = (stdout = ""): ExecResult => ({ code: 0, stdout, stderr: "" });
  const env: SetupEnv = {
    resources,
    home,
    agents,
    arch: options.arch ?? "arm64",
    darwinMajor: 24,
    uid: 501,
    now: () => new Date("2026-11-17T10:00:00Z"),
    sleep: async () => undefined,
    freeBytes: async () => options.free ?? 200 * 1024 ** 3,
    async exec(command, args) {
      calls.push([command.split("/").at(-1), ...args].join(" "));
      if (command === "/usr/bin/tar") {
        const dir = args[args.indexOf("-C") + 1];
        writeFileSync(join(dir, "ollama"), "binary");
        return ok();
      }
      if (command === "/bin/launchctl" && args[0] === "kickstart" && args[1].endsWith("com.bob.ollama")) runnerUp = true;
      if (command === "/bin/launchctl" && args[0] === "print") return { code: 1, stdout: "", stderr: "" };
      if (command === "/bin/bash") {
        bobdUp = true;
        return ok("bobd restarted.");
      }
      return ok();
    },
    async fetch(input, init) {
      const url = String(input);
      fetches.push(url);
      if (options.offline && !url.includes("127.0.0.1")) throw new Error("getaddrinfo ENOTFOUND");
      if (url === PIN.url) {
        const bytes = options.badArchive ? Buffer.from("tampered") : ARCHIVE;
        return new Response(bytes);
      }
      if (url.endsWith("/api/version")) return runnerUp ? Response.json({ version: "x" }) : new Response("", { status: 503 });
      if (url.endsWith("/api/tags")) {
        return runnerUp ? Response.json({ models: [...installed].map((name) => ({ name })) }) : new Response("", { status: 503 });
      }
      if (url.endsWith("/health")) return bobdUp ? Response.json({ ok: true }) : new Response("", { status: 503 });
      if (url.endsWith("/api/pull")) {
        const tag = JSON.parse(String(init?.body)).model as string;
        if (options.pullError) return new Response(JSON.stringify({ error: options.pullError }) + "\n");
        installed.add(tag);
        const lines = [{ status: "pulling manifest" }, { status: "pulling aa", digest: "aa", total: 100, completed: 40 }, { status: "pulling aa", digest: "aa", total: 100, completed: 100 }, { status: "success" }];
        return new Response(lines.map((l) => JSON.stringify(l)).join("\n") + "\n");
      }
      throw new Error(`unexpected fetch ${url}`);
    },
  };
  let state: SetupState = { ...EMPTY_STATE, steps: {} };
  w.env = env;
  w.hooks = {
    loadState: () => structuredClone(state),
    saveState: (next) => void (state = structuredClone(next)),
    catalogue: async () => ({ source: "", models: [] }),
    facts: async () => ({ freeBytes: 0, arch: "arm64", found: [] }),
    onFinished: () => undefined,
  };
  w.saved = () => state;
  return w;
}

const runner = (w: World) =>
  new SetupRunner(w.env, w.hooks, DEFAULT_CHOICES, { catalogue: { source: "", models: [] }, facts: { freeBytes: 0, arch: "arm64", found: [] } });

describe("setup runs every step in order and leaves Bob running", () => {
  it("does it all on a clean Mac, verifies the download, and writes the files", async () => {
    const w = world();
    const r = runner(w);
    await r.start(DEFAULT_CHOICES);
    const snap = r.snapshot();
    expect(snap.error).toBe("");
    expect(snap.phase).toBe("done");
    expect(snap.steps.map((s) => s.state)).toEqual(Array(7).fill("done"));
    expect(readFileSync(join(w.home, "deploy", "bobd.sh"), "utf8")).toContain("echo hi");
    expect(readFileSync(join(w.home, "ollama", "bin", "ollama"), "utf8")).toBe("binary");
    expect(readFileSync(join(w.home, "ollama", "bin", ".version"), "utf8")).toBe("v9.9.9");
    expect([...w.installed].sort()).toEqual(["gemma4:e4b", "gpt-oss:20b", "qwen3-embedding:0.6b"]);
    const config = readFileSync(join(w.home, "config.yaml"), "utf8");
    expect(config).toContain('router: {model: "gemma4:e4b", num_ctx: 8192, temperature: 0.0}');
    expect(config).toContain("base_url: http://127.0.0.1:11435");
    expect(config).not.toContain("{{");
    expect(w.saved().finishedAt).toBe("2026-11-17T10:00:00.000Z");
    const agents = readdirSync(join(w.home, "..", "..", "agents")).sort();
    expect(agents).toEqual(["com.bob.bobd.plist", "com.bob.ollama.plist"]);
    // The signature of what was downloaded is checked before it is put in place.
    expect(w.calls.some((c) => c.startsWith("codesign --verify"))).toBe(true);
    expect(w.calls.some((c) => c.startsWith("xattr -dr com.apple.quarantine"))).toBe(true);
  });

  it("is safe to run again: everything already done is skipped", async () => {
    const w = world();
    await runner(w).start(DEFAULT_CHOICES);
    const again = runner(w);
    w.calls.length = 0;
    w.fetches.length = 0;
    await again.start(DEFAULT_CHOICES);
    expect(again.snapshot().steps.slice(1).map((s) => s.detail)).toEqual(Array(6).fill("Already done"));
    expect(w.fetches.some((u) => u === PIN.url || u.endsWith("/api/pull"))).toBe(false);
    expect(w.calls.some((c) => c.startsWith("tar"))).toBe(false);
  });

  it("refuses a download that does not match the pinned checksum and installs nothing", async () => {
    const w = world({ badArchive: true });
    const r = runner(w);
    await r.start(DEFAULT_CHOICES);
    const snap = r.snapshot();
    expect(snap.phase).toBe("failed");
    expect(snap.steps.find((s) => s.id === "ollama")!.state).toBe("failed");
    expect(snap.error).toContain("did not match the checksum");
    expect(readdirSync(join(w.home, "ollama"))).not.toContain("bin");
    expect(w.calls.some((c) => c.startsWith("tar"))).toBe(false);
  });

  it("says plainly when there is no internet, and carries on after a retry", async () => {
    const w = world({ offline: true });
    const r = runner(w);
    await r.start(DEFAULT_CHOICES);
    expect(r.snapshot().error).toContain("needs the internet once");
    w.options.offline = false;
    await r.start(DEFAULT_CHOICES);
    expect(r.snapshot().phase).toBe("done");
  });

  it("reports a failed model download in Ollama's words and stops there", async () => {
    const w = world({ pullError: "no space left on device" });
    const r = runner(w);
    await r.start(DEFAULT_CHOICES);
    const snap = r.snapshot();
    expect(snap.phase).toBe("failed");
    expect(snap.error).toContain("no space left on device");
    expect(snap.steps.map((s) => s.state).slice(-2)).toEqual(["waiting", "waiting"]);
  });

  it("stops on an Intel Mac before changing anything", async () => {
    const w = world({ arch: "x64" });
    const r = runner(w);
    await r.start(DEFAULT_CHOICES);
    expect(r.snapshot().error).toContain("Apple Silicon");
    expect(w.calls).toEqual([]);
  });

  it("with an Ollama of your own it installs none but still fetches the models there", async () => {
    const w = world();
    const choices = mergeChoices({ externalOllama: "http://127.0.0.1:11434" });
    // Their Ollama is already answering.
    const base = w.env.fetch;
    w.env.fetch = async (input, init) => {
      const url = String(input);
      if (url.startsWith("http://127.0.0.1:11434/api/tags")) return Response.json({ models: [...w.installed].map((name) => ({ name })) });
      if (url.startsWith("http://127.0.0.1:11434/api/pull")) {
        const tag = JSON.parse(String(init?.body)).model as string;
        w.installed.add(tag);
        return new Response(JSON.stringify({ status: "success" }) + "\n");
      }
      return base(input, init);
    };
    const r = runner(w);
    await r.start(choices);
    const states = Object.fromEntries(r.snapshot().steps.map((s) => [s.id, s.state]));
    expect(r.snapshot().error).toBe("");
    expect(states.ollama).toBe("skipped");
    expect(states.runner).toBe("skipped");
    expect(w.calls.some((c) => c.startsWith("tar"))).toBe(false);
    expect(readFileSync(join(w.home, "config.yaml"), "utf8")).toContain("base_url: http://127.0.0.1:11434");
  });

  it("keeps settings you edited: the old file is set aside when the choices change", async () => {
    const w = world();
    await runner(w).start(DEFAULT_CHOICES);
    writeFileSync(join(w.home, "config.yaml"), "# my edits\n");
    await runner(w).start(mergeChoices({ models: { main: "qwen3:8b" } }));
    const files = readdirSync(w.home);
    expect(files.some((f) => f.startsWith("config.yaml.bak-20261117"))).toBe(true);
    expect(readFileSync(join(w.home, "config.yaml"), "utf8")).toContain('main: {model: "qwen3:8b"');
  });

  it("looks at a Mac quickly: Ollama answering and how many models are missing", async () => {
    const w = world();
    expect(await inspectMac(w.env, DEFAULT_CHOICES)).toEqual({ ollamaUp: false, missingModels: 0 });
    await runner(w).start(DEFAULT_CHOICES);
    w.installed.delete("gpt-oss:20b");
    expect(await inspectMac(w.env, DEFAULT_CHOICES)).toEqual({ ollamaUp: true, missingModels: 1 });
  });
});

describe("what setup decides without a Mac", () => {
  const done: SetupState = { ...EMPTY_STATE, finishedAt: "x", steps: { bobd: "done" } };
  it("only the shared build ever opens setup", () => {
    expect(decideSetup({ bundled: false, state: EMPTY_STATE, ollamaUp: false, missingModels: 5 })).toBe("none");
  });
  it("opens fully the first time, resumes after a quit, and goes to models when only those are gone", () => {
    expect(decideSetup({ bundled: true, state: EMPTY_STATE, ollamaUp: false, missingModels: 0 })).toBe("full");
    expect(decideSetup({ bundled: true, state: { ...EMPTY_STATE, steps: { home: "done" } }, ollamaUp: false, missingModels: 0 })).toBe("resume");
    expect(decideSetup({ bundled: true, state: done, ollamaUp: true, missingModels: 2 })).toBe("models");
    expect(decideSetup({ bundled: true, state: done, ollamaUp: true, missingModels: 0 })).toBe("none");
    expect(decideSetup({ bundled: true, state: done, ollamaUp: false, missingModels: 0 })).toBe("full");
  });
});

describe("model choices", () => {
  const catalogue: Catalogue = {
    source: "t",
    models: [
      { name: "llama3.2", caps: "t", sizes: ["1b", "3b"] },
      { name: "llava", caps: "v", sizes: ["7b"] },
      { name: "nomic-embed-text", caps: "e", sizes: [] },
      { name: "gemma4", caps: "vtka", sizes: ["e4b"] },
    ],
  };
  it("offers each role the models that can do it, the shipped one first", () => {
    expect(optionsFor("main", catalogue)).toEqual(["gpt-oss:20b", "llama3.2:1b", "llama3.2:3b", "gemma4:e4b"]);
    expect(optionsFor("vision", catalogue)).toEqual(["gemma4:e4b", "llava:7b"]);
    expect(optionsFor("embeddings", catalogue)).toEqual(["qwen3-embedding:0.6b", "nomic-embed-text"]);
    expect(optionsFor("router", catalogue)).not.toContain("nomic-embed-text");
  });
  it("accepts typed tags that are plain Ollama tags and refuses cloud ones", () => {
    expect(validTag("qwen3:8b")).toBe(true);
    expect(validTag("hf.co/someone/model:Q4_K_M")).toBe(true);
    expect(validTag("gpt-oss:120b-cloud")).toBe(false);
    expect(validTag("rm -rf /")).toBe(false);
    expect(validTag("")).toBe(false);
    expect(mergeChoices({ models: { main: "bad tag!" } }).models.main).toBe(DEFAULT_MODELS.main);
  });
  it("only ever talks to Ollama on this Mac", () => {
    expect(mergeChoices({ externalOllama: "http://example.com:11434" }).externalOllama).toBeNull();
    expect(mergeChoices({ externalOllama: "http://localhost:11434/" }).externalOllama).toBe("http://localhost:11434");
  });
  it("pulls each model once and treats a bare name as :latest", () => {
    expect(tagsToPull(DEFAULT_MODELS)).toEqual(["gemma4:e4b", "gpt-oss:20b", "qwen3-embedding:0.6b"]);
    expect(missingTags(["nomic-embed-text", "gpt-oss:20b"], ["nomic-embed-text:latest"])).toEqual(["gpt-oss:20b"]);
  });
  it("writes dimensions only for the embedding model whose size Bob knows", () => {
    expect(modelLines(DEFAULT_MODELS)).toContain("dimensions: 1024");
    expect(modelLines({ ...DEFAULT_MODELS, embeddings: "nomic-embed-text" })).not.toContain("dimensions");
    expect(renderConfig("{{OLLAMA_URL}}|{{MODELS}}", DEFAULT_MODELS, "http://x")).toMatch(/^http:\/\/x\|  router:/);
  });
  it("remembers state across launches and ignores junk", () => {
    expect(parseState("junk")).toEqual(EMPTY_STATE);
    const state = parseState({ steps: { home: "done", ollama: "weird" }, finishedAt: "t", choices: { models: { main: "qwen3:8b" } } });
    expect(state.steps).toEqual({ home: "done" });
    expect(state.choices?.models.main).toBe("qwen3:8b");
  });
});

describe("the background services", () => {
  it("start only when Bob asks, and come back after a crash", () => {
    const text = bobdPlist({ python: "/App/Bob.app/python3", config: "/S/config.yaml", home: "/S", log: "/S/bobd.log" });
    expect(text).toContain("<key>RunAtLoad</key><false/>");
    expect(text).toContain("<key>Crashed</key><true/>");
    expect(text).toContain("from bobd.service import main; main()");
  });
  it("run Ollama on Bob's own port with the models in Bob's folder", () => {
    const text = ollamaPlist({ binary: "/S/ollama/bin/ollama", models: "/S/ollama/models", home: "/S/ollama", log: "/S/o.log" });
    expect(text).toContain("<string>127.0.0.1:11435</string>");
    expect(text).toContain("<key>OLLAMA_MODELS</key><string>/S/ollama/models</string>");
  });
  it("escape paths with special characters", () => {
    expect(bobdPlist({ python: "/A & B/<x>/python3", config: "c", home: "h", log: "l" })).toContain("/A &amp; B/&lt;x&gt;/python3");
  });
});
