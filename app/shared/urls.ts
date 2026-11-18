// The bob:// URL scheme: how Shortcuts, Raycast or Alfred trigger Bob with "Open URL".
//
// Only a fixed set of actions exists. A URL can never name an arbitrary tool, because
// any web page or document can ask the system to open a bob:// link.

import { type AppSection, isAppSection } from "./sections";

export type UrlAction =
  | { kind: "ask"; text: string }
  | { kind: "panel" }
  | { kind: "app"; section: AppSection }
  | { kind: "settings" }
  | { kind: "triage" }
  | { kind: "reindex" }
  | { kind: "invalid"; reason: string };

const MAX_QUESTION = 4000;

export function parseBobUrl(raw: string): UrlAction {
  let url: URL;
  try {
    url = new URL(raw);
  } catch {
    return { kind: "invalid", reason: "not a URL" };
  }
  if (url.protocol !== "bob:") return { kind: "invalid", reason: "not a bob:// URL" };
  // bob://ask?q=… parses "ask" as the host; bob:ask as the path.
  const action = (url.hostname || url.pathname.replace(/^\/+/, "")).toLowerCase();
  switch (action) {
    case "ask": {
      const text = (url.searchParams.get("q") ?? "").trim();
      if (!text) return { kind: "panel" };
      if (text.length > MAX_QUESTION) return { kind: "invalid", reason: "the question is too long" };
      return { kind: "ask", text };
    }
    case "":
    case "panel":
    case "chat":
      return { kind: "panel" };
    case "app": {
      // bob://app opens the main window; bob://app?section=tasks opens it on a section.
      const section = (url.searchParams.get("section") ?? "chat").toLowerCase();
      return isAppSection(section) ? { kind: "app", section } : { kind: "invalid", reason: `unknown section "${section}"` };
    }
    case "settings":
      return { kind: "settings" };
    case "widget": // the desktop widget is gone; old links open Today
      return { kind: "app", section: "today" };
    case "triage":
      return { kind: "triage" };
    case "reindex":
      return { kind: "reindex" };
    default:
      return { kind: "invalid", reason: `unknown action "${action}"` };
  }
}
