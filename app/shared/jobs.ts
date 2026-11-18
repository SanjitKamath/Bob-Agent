// Background jobs that report progress into a chat, e.g. building a form.
// A plugin publishes "<plugin>.job.progress" events; data.session is the chat it belongs to.

export interface JobProgress {
  job: string;
  plugin: string;
  /** The conversation the job reports to; empty when it was started outside a chat. */
  session: string;
  stage: string;
  status: string;
  message: string;
  /** Recent progress lines, oldest first. */
  lines: string[];
  done: boolean;
  /** Sections drafted, or validated, out of the total. */
  step: number | null;
  total: number | null;
}

const MAX_LINES = 6;

export function isJobProgress(topic: string): boolean {
  return topic.endsWith(".job.progress");
}

function text(data: Record<string, unknown>, key: string): string {
  return typeof data[key] === "string" ? (data[key] as string) : "";
}

function number(data: Record<string, unknown>, key: string): number | null {
  return typeof data[key] === "number" ? (data[key] as number) : null;
}

/** Add one progress event to the list of jobs, newest job last. */
export function applyJobEvent(jobs: JobProgress[], topic: string, data: Record<string, unknown>): JobProgress[] {
  const id = text(data, "job");
  if (!isJobProgress(topic) || !id) return jobs;
  const existing = jobs.find((item) => item.job === id);
  const message = text(data, "message");
  const step = number(data, "done_sections") ?? number(data, "validated");
  const total = number(data, "total_sections") ?? number(data, "total_validated");
  const next: JobProgress = {
    job: id,
    plugin: topic.slice(0, -".job.progress".length),
    session: text(data, "session") || existing?.session || "",
    stage: text(data, "stage") || existing?.stage || "",
    status: text(data, "status") || existing?.status || "running",
    message: message || existing?.message || "",
    lines: message ? [...(existing?.lines ?? []), message].slice(-MAX_LINES) : (existing?.lines ?? []),
    done: data.done === true || existing?.done === true,
    step: step ?? existing?.step ?? null,
    total: total ?? existing?.total ?? null,
  };
  return existing ? jobs.map((item) => (item.job === id ? next : item)) : [...jobs, next];
}

/** The jobs to show in a chat. */
export function jobsFor(jobs: JobProgress[], conversationId: string | null): JobProgress[] {
  return conversationId ? jobs.filter((item) => item.session === conversationId) : [];
}

/** True when a finished job did not end well. */
export function jobFailed(item: JobProgress): boolean {
  return item.done && (item.status === "failed" || /FAILED|Stopped at/.test(item.message));
}

/** A finished job's last message, split so it can be laid out: a summary, a folder, a list. */
export interface JobResult {
  summary: string;
  /** Where the files went, when the message names a folder. */
  folder: string;
  blocks: { kind: "heading" | "item" | "text"; text: string }[];
}

export function describeResult(message: string): JobResult {
  const lines = message
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean);
  const first = lines.shift() ?? "";
  const folder = /\s*Folder:\s*(\S.*)$/.exec(first);
  return {
    summary: folder ? first.slice(0, folder.index).trim() : first,
    folder: folder ? folder[1].trim() : "",
    blocks: lines.map((line) =>
      line.startsWith("- ")
        ? { kind: "item" as const, text: line.slice(2).trim() }
        : { kind: line.endsWith(":") ? ("heading" as const) : ("text" as const), text: line },
    ),
  };
}
