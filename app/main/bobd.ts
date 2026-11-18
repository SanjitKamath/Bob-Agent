// Runs deploy/bobd.sh from the Bob folder. Nothing else can be run this way: the action is
// one of three fixed words and the script path is fixed relative to the Bob folder.
import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import { BOBD_ACTIONS, BOBD_SCRIPT, type BobdAction, type BobdResult, bobHomeCandidates, summarise } from "../shared/bobd";

const TIMEOUT_MS = 120_000;
const MAX_OUTPUT = 8000;

export function findBobHome(preferred: string): string | null {
  return bobHomeCandidates(preferred, homedir()).find((folder) => existsSync(join(folder, BOBD_SCRIPT))) ?? null;
}

let running: Promise<BobdResult> | null = null;

export function runBobd(action: BobdAction, preferredHome: string, serverUrl: string): Promise<BobdResult> {
  if (!BOBD_ACTIONS.includes(action)) return Promise.resolve({ ok: false, message: `unknown action ${String(action)}` });
  // One at a time: a second click while starting waits for the same result.
  if (running) return running;
  const home = findBobHome(preferredHome);
  if (!home) {
    return Promise.resolve({
      ok: false,
      message: "Can't find the Bob folder. Set it under Bob → General → Bob folder.",
    });
  }
  running = new Promise<BobdResult>((resolve) => {
    let output = "";
    const child = spawn("/bin/bash", [join(home, BOBD_SCRIPT), action], {
      cwd: home,
      env: {
        ...process.env,
        BOB_URL: serverUrl,
        // Apps opened from Finder get a minimal PATH.
        PATH: `/opt/homebrew/bin:/usr/local/bin:${process.env.PATH ?? "/usr/bin:/bin"}`,
      },
      stdio: ["ignore", "pipe", "pipe"],
    });
    const collect = (chunk: Buffer): void => {
      output = (output + chunk.toString()).slice(-MAX_OUTPUT);
    };
    child.stdout.on("data", collect);
    child.stderr.on("data", collect);
    const timer = setTimeout(() => child.kill("SIGTERM"), TIMEOUT_MS);
    child.on("error", (error) => {
      clearTimeout(timer);
      resolve({ ok: false, message: `Could not run ${BOBD_SCRIPT}: ${error.message}` });
    });
    child.on("close", (code) => {
      clearTimeout(timer);
      resolve(summarise(output, code));
    });
  }).finally(() => {
    running = null;
  });
  return running;
}
