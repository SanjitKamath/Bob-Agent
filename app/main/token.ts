// Finding the API token. bobd creates it on first start and keeps it in the Keychain.
import { execFile } from "node:child_process";
import { readFile } from "node:fs/promises";
import { join } from "node:path";

const SERVICE = "bob";
const ACCOUNT = "api-token";

let keychainRead: Promise<string | null> | null = null;

/**
 * Read the token from the Keychain. The first time, macOS asks whether to allow it; the
 * read waits for your answer, however long that takes, and only one read runs at a time.
 * Choose "Always Allow" and it never asks again.
 */
function fromKeychain(): Promise<string | null> {
  keychainRead ??= new Promise<string | null>((resolve) => {
    execFile("/usr/bin/security", ["find-generic-password", "-s", SERVICE, "-a", ACCOUNT, "-w"], (error, stdout) =>
      resolve(error ? null : stdout.trim() || null),
    );
  }).finally(() => {
    keychainRead = null;
  });
  return keychainRead;
}

async function fromFile(bobHome: string): Promise<string | null> {
  if (!bobHome) return null;
  try {
    return (await readFile(join(bobHome, "data", "api-token"), "utf8")).trim() || null;
  } catch {
    return null;
  }
}

/**
 * Order: $BOB_API_TOKEN, the token file in the Bob folder, then the Keychain.
 * `askKeychain: false` skips the Keychain, so a retry never re-opens a dialog you declined.
 */
export async function readToken(bobHome: string, askKeychain = true): Promise<string | null> {
  return process.env.BOB_API_TOKEN || (await fromFile(bobHome)) || (askKeychain ? await fromKeychain() : null);
}
