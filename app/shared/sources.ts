import type { Source } from "./types";

/** The address of a source that came from the web, or null for a file. Only plain http(s) links. */
export function webAddress(source: Pick<Source, "collection" | "path">): string | null {
  if (source.collection !== "web") return null;
  try {
    const url = new URL(source.path);
    if (url.protocol !== "https:" && url.protocol !== "http:") return null;
    if (url.username || url.password) return null;
    return url.toString();
  } catch {
    return null;
  }
}

/** What to show for a web source: its site, then its title. */
export function webLabel(source: Pick<Source, "path" | "title">): string {
  try {
    const host = new URL(source.path).hostname.replace(/^www\./, "");
    return source.title && source.title !== source.path ? `${host}: ${source.title}` : host;
  } catch {
    return source.title || source.path;
  }
}
