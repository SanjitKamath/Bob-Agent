// Starting and stopping bobd from the app: which folder holds Bob, and what to show.

export type BobdAction = "start" | "restart" | "stop";

export interface BobdResult {
  ok: boolean;
  /** One line for the person: what happened, or what to do next. */
  message: string;
}

export const BOBD_ACTIONS: readonly BobdAction[] = ["start", "restart", "stop"];

/** The script, relative to the Bob folder. It is the only thing the app ever runs. */
export const BOBD_SCRIPT = "deploy/bobd.sh";

/** Folders to look for Bob in: the one set in Settings first, then the usual places. */
export function bobHomeCandidates(preferred: string, home: string): string[] {
  const list = [preferred.trim(), `${home}/Bob`, `${home}/dev/bob`, `${home}/dev/Bob`].filter(Boolean);
  return [...new Set(list.map((path) => path.replace(/\/+$/, "")))];
}

/** The script's summary is its last non-empty line; anything before it is detail. */
export function summarise(output: string, code: number | null): BobdResult {
  const lines = output
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean);
  const last = lines.at(-1) ?? "";
  if (code === 0) return { ok: true, message: last || "Done." };
  return { ok: false, message: last || "bobd could not be started. Run deploy/bobd.sh start in Terminal to see why." };
}
