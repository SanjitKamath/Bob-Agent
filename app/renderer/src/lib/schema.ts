// A plugin's settings form is generated from the JSON schema bobd serves, so a new
// plugin gets a form with no app changes.

export interface SchemaProperty {
  type: "string" | "integer" | "number" | "boolean" | "array";
  title?: string;
  description?: string;
  default?: unknown;
  format?: string;
  enum?: string[];
  writeOnly?: boolean;
}

export interface SettingsSchema {
  properties?: Record<string, SchemaProperty>;
}

export type FieldKind = "text" | "path" | "number" | "checkbox" | "select" | "lines";

export interface FormField {
  key: string;
  label: string;
  help: string;
  kind: FieldKind;
  options: string[];
  integer: boolean;
  value: string | number | boolean;
}

export function formFields(schema: SettingsSchema, values: Record<string, unknown>): FormField[] {
  return Object.entries(schema.properties ?? {})
    .filter(([, property]) => !property.writeOnly) // secrets live in the Keychain, not in forms
    .map(([key, property]) => {
      const current = values[key] ?? property.default;
      let kind: FieldKind = "text";
      if (property.type === "array") kind = "lines"; // a list of text, one entry per line
      else if (property.enum?.length) kind = "select";
      else if (property.type === "boolean") kind = "checkbox";
      else if (property.type === "integer" || property.type === "number") kind = "number";
      else if (property.format === "path") kind = "path";
      const value =
        kind === "checkbox"
          ? current === true
          : kind === "number"
            ? typeof current === "number"
              ? current
              : ""
            : kind === "lines"
              ? (Array.isArray(current) ? current : []).map(String).join("\n")
              : String(current ?? "");
      return {
        key,
        label: property.title ?? key,
        help: property.description ?? "",
        kind,
        options: property.enum ?? [],
        integer: property.type === "integer",
        value,
      };
    });
}

/** Turn what was typed into the value bobd expects, or say why it cannot be. */
export function coerce(field: FormField, raw: string | boolean): { ok: true; value: unknown } | { ok: false; error: string } {
  if (field.kind === "checkbox") return { ok: true, value: raw === true };
  if (field.kind === "lines") return { ok: true, value: splitLines(String(raw)) };
  const text = String(raw).trim();
  if (field.kind === "number") {
    if (text === "") return { ok: false, error: `${field.label} needs a number` };
    const number = Number(text);
    if (!Number.isFinite(number)) return { ok: false, error: `${field.label} needs a number` };
    if (field.integer && !Number.isInteger(number)) return { ok: false, error: `${field.label} needs a whole number` };
    return { ok: true, value: number };
  }
  if (field.kind === "select" && !field.options.includes(text)) {
    return { ok: false, error: `${field.label} must be one of ${field.options.join(", ")}` };
  }
  return { ok: true, value: text };
}

/** One entry per non-empty line, trimmed. */
export function splitLines(text: string): string[] {
  return text
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean);
}

/** Only the settings that differ from what bobd has. */
export function changedSettings(fields: FormField[], edits: Record<string, string | boolean>): { values: Record<string, unknown>; errors: string[] } {
  const values: Record<string, unknown> = {};
  const errors: string[] = [];
  for (const field of fields) {
    if (!(field.key in edits)) continue;
    const result = coerce(field, edits[field.key]);
    if (!result.ok) errors.push(result.error);
    else if (field.kind === "lines") {
      if (JSON.stringify(result.value) !== JSON.stringify(splitLines(String(field.value)))) values[field.key] = result.value;
    } else if (result.value !== field.value) values[field.key] = result.value;
  }
  return { values, errors };
}
