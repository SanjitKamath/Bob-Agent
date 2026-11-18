// One chat's transcript, built from the messages bobd streams.
import { applyJobEvent, type JobProgress } from "../../../shared/jobs";
import type { ServerMessage, Source, StoredTurn } from "../../../shared/types";

export interface ToolStep {
  plugin: string;
  tool: string;
  outcome: "running" | "ok" | "denied" | "error";
}

export interface Turn {
  id: number;
  question: string;
  answer: string;
  specialist: string;
  tools: ToolStep[];
  sources: Source[];
  warnings: string[];
  error: string;
  done: boolean;
  seconds: number | null;
  /** The answer was reused from the answer cache. */
  cached: boolean;
  /** The id this question was sent with; bobd tags every message about it. */
  requestId: string;
  /** Waiting for an earlier question in the same chat. */
  queued: boolean;
}

export interface Transcript {
  turns: Turn[];
  conversationId: string | null;
  busy: boolean;
  /** Background jobs (a form being built, say); shown in the chat that started them. */
  jobs: JobProgress[];
}

export const EMPTY: Transcript = { turns: [], conversationId: null, busy: false, jobs: [] };

let counter = 0;
/** A fresh request id. */
export function requestId(): string {
  counter += 1;
  const random = globalThis.crypto?.randomUUID?.() ?? Math.random().toString(36).slice(2);
  return `${random.slice(0, 12)}-${counter}`;
}

export function startTurn(transcript: Transcript, question: string, request = ""): Transcript {
  const turn: Turn = {
    id: (transcript.turns.at(-1)?.id ?? 0) + 1,
    question,
    answer: "",
    specialist: "",
    tools: [],
    sources: [],
    warnings: [],
    error: "",
    done: false,
    seconds: null,
    cached: false,
    requestId: request,
    queued: false,
  };
  return { ...transcript, turns: [...transcript.turns, turn], busy: true };
}

/** The turn a message is about: the one with its request id, or (older bobd) the last one. */
function target(transcript: Transcript, message: ServerMessage): number {
  if (message.request_id) return transcript.turns.findIndex((turn) => turn.requestId === message.request_id && !turn.done);
  const last = transcript.turns.length - 1;
  return last >= 0 && !transcript.turns[last].done && transcript.busy ? last : -1;
}

function patch(transcript: Transcript, index: number, change: (turn: Turn) => Turn): Transcript {
  const turns = transcript.turns.map((turn, i) => (i === index ? change(turn) : turn));
  return { ...transcript, turns, busy: turns.some((turn) => !turn.done) };
}

/** True when this transcript asked the question the message is about. */
export function owns(transcript: Transcript, message: ServerMessage): boolean {
  return message.type !== "event" && target(transcript, message) !== -1;
}

/**
 * Apply one streamed message. Messages about other chats, or asked from another window,
 * leave the transcript unchanged.
 */
export function applyMessage(transcript: Transcript, message: ServerMessage): Transcript {
  // Job progress keeps coming after the answer; keep it whether or not this chat is waiting.
  if (message.type === "event") {
    const jobs = applyJobEvent(transcript.jobs ?? [], message.topic, message.data);
    return jobs === transcript.jobs ? transcript : { ...transcript, jobs };
  }
  const index = target(transcript, message);
  if (index === -1) return transcript;
  const adopt = (next: Transcript): Transcript =>
    message.conversation_id && !next.conversationId ? { ...next, conversationId: message.conversation_id } : next;
  switch (message.type) {
    case "queued":
      return adopt(patch(transcript, index, (turn) => ({ ...turn, queued: true })));
    case "started":
      return adopt(patch(transcript, index, (turn) => ({ ...turn, queued: false })));
    case "route":
      return patch(transcript, index, (turn) => ({ ...turn, specialist: message.domain }));
    case "token":
      return patch(transcript, index, (turn) => ({ ...turn, answer: turn.answer + message.text }));
    case "tool_call":
      return patch(transcript, index, (turn) => ({
        ...turn,
        // Text written before a tool call was the model thinking aloud, not the answer.
        answer: "",
        tools: [...turn.tools, { plugin: message.plugin, tool: message.tool, outcome: "running" }],
      }));
    case "tool_result":
      return patch(transcript, index, (turn) => {
        const at = turn.tools.findLastIndex(
          (step) => step.plugin === message.plugin && step.tool === message.tool && step.outcome === "running",
        );
        if (at === -1) return turn;
        const outcome = (["ok", "denied", "error"].includes(message.outcome) ? message.outcome : "error") as ToolStep["outcome"];
        return { ...turn, tools: turn.tools.map((step, i) => (i === at ? { ...step, outcome } : step)) };
      });
    case "final": {
      const next = patch(transcript, index, (turn) => ({
        ...turn,
        answer: message.text,
        specialist: message.specialist,
        sources: message.sources,
        warnings: message.warnings,
        seconds: message.seconds,
        cached: message.cached === true,
        queued: false,
        done: true,
      }));
      return { ...next, conversationId: message.conversation_id };
    }
    case "error":
      return adopt(patch(transcript, index, (turn) => ({ ...turn, error: message.message, queued: false, done: true })));
    case "cancelled":
      return adopt(patch(transcript, index, (turn) => ({ ...turn, error: "Stopped.", queued: false, done: true })));
    default:
      return transcript;
  }
}

/** A stored chat, opened from the history list. */
export function fromHistory(conversationId: string, stored: StoredTurn[]): Transcript {
  const turns: Turn[] = [];
  for (const entry of stored) {
    if (entry.role === "user") {
      turns.push({ ...startTurn(EMPTY, entry.content).turns[0], id: turns.length + 1, done: true });
    } else if (turns.length) {
      const last = turns[turns.length - 1];
      turns[turns.length - 1] = { ...last, answer: entry.content, specialist: entry.specialist ?? "" };
    }
  }
  return { turns, conversationId, busy: false, jobs: [] };
}

/** The text sent to bobd: the question, plus the paths of any files dropped on the panel. */
export function withFiles(question: string, files: string[]): string {
  if (!files.length) return question;
  return `${question}\n\nFiles:\n${files.map((file) => `- ${file}`).join("\n")}`;
}
