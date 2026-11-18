// Preferences, kept as JSON in the app's user-data folder.
import { mkdirSync, readFileSync, renameSync, writeFileSync } from "node:fs";
import { dirname } from "node:path";
import { DEFAULT_PREFS, mergePrefs } from "../shared/prefs";
import type { DeepPartial, Prefs } from "../shared/types";

export class PrefsStore {
  private prefs: Prefs;

  constructor(private readonly file: string) {
    let saved: unknown = {};
    try {
      saved = JSON.parse(readFileSync(file, "utf8"));
    } catch {
      // First run, or an unreadable file: start from the defaults.
    }
    this.prefs = mergePrefs(DEFAULT_PREFS, saved);
  }

  get(): Prefs {
    return this.prefs;
  }

  update(patch: DeepPartial<Prefs>): Prefs {
    this.prefs = mergePrefs(this.prefs, patch);
    mkdirSync(dirname(this.file), { recursive: true });
    // Write to a temp file first so a crash never leaves half a file.
    const temp = `${this.file}.tmp`;
    writeFileSync(temp, JSON.stringify(this.prefs, null, 2), "utf8");
    renameSync(temp, this.file);
    return this.prefs;
  }
}
