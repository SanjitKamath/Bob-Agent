// The main Bob window's sections, in sidebar order.

export const APP_SECTIONS = [
  "chat",
  "today",
  "tasks",
  "knowledge",
  "plugins",
  "models",
  "routines",
  "general",
  "privacy",
] as const;

export type AppSection = (typeof APP_SECTIONS)[number];

export const SECTION_LABELS: Record<AppSection, string> = {
  chat: "Chat",
  today: "Today",
  tasks: "Tasks",
  knowledge: "Knowledge",
  plugins: "Plugins",
  models: "Models",
  routines: "Routines",
  general: "General",
  privacy: "Privacy",
};

export function isAppSection(value: unknown): value is AppSection {
  return typeof value === "string" && (APP_SECTIONS as readonly string[]).includes(value);
}
