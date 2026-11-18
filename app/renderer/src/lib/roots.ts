/** The folders a plugin may read are stored as one string, separated by colons. */

const SEPARATOR = ":";

export function splitRoots(value: unknown): string[] {
  return String(value ?? "")
    .split(SEPARATOR)
    .map((part) => part.trim())
    .filter(Boolean);
}

export function joinRoots(roots: string[]): string {
  return roots.join(SEPARATOR);
}

function same(a: string, b: string): boolean {
  const bare = (path: string): string => (path.length > 1 ? path.replace(/\/+$/, "") : path);
  return bare(a) === bare(b);
}

/**
 * Add a folder. A path with a colon cannot be stored (the colon is the separator), and a
 * folder already in the list is not added twice.
 */
export function addRoot(roots: string[], folder: string): string[] {
  const path = folder.trim();
  if (!path.startsWith("/") && !path.startsWith("~")) throw new Error("Choose a folder by its full path.");
  if (path.includes(SEPARATOR)) throw new Error("A folder name with a colon in it cannot be added.");
  return roots.some((root) => same(root, path)) ? roots : [...roots, path];
}

export function removeRoot(roots: string[], folder: string): string[] {
  return roots.filter((root) => !same(root, folder));
}
