// Several chats in one window: each keeps its own transcript and can be working at once.
import type { ChatSummary, ServerMessage } from "../../../shared/types";
import { applyMessage, EMPTY, owns, type Transcript } from "./chat";

export interface Chats {
  /** Open chats by key: the conversation id once bobd gave one, else "new-N". */
  byKey: Record<string, Transcript>;
  active: string;
  next: number;
}

export function newChats(): Chats {
  return { byKey: { "new-1": EMPTY }, active: "new-1", next: 2 };
}

export function activeTranscript(chats: Chats): Transcript {
  return chats.byKey[chats.active] ?? EMPTY;
}

/** The open chat showing a conversation, if any. */
export function keyFor(chats: Chats, conversationId: string): string | undefined {
  if (chats.byKey[conversationId]) return conversationId;
  return Object.keys(chats.byKey).find((key) => chats.byKey[key].conversationId === conversationId);
}

/** Route a streamed message to every chat it is about (job progress can touch several). */
export function route(chats: Chats, message: ServerMessage): { chats: Chats; finished: string | null } {
  let changed = false;
  let finished: string | null = null;
  const byKey: Record<string, Transcript> = {};
  for (const [key, transcript] of Object.entries(chats.byKey)) {
    const mine = owns(transcript, message);
    const next = applyMessage(transcript, message);
    if (next !== transcript) changed = true;
    if (mine && message.type === "final") finished = key;
    byKey[key] = next;
  }
  return { chats: changed ? { ...chats, byKey } : chats, finished };
}

export function update(chats: Chats, key: string, change: (transcript: Transcript) => Transcript): Chats {
  return { ...chats, byKey: { ...chats.byKey, [key]: change(chats.byKey[key] ?? EMPTY) } };
}

/** Show an empty chat: the current one if it is still empty, else a new one. */
export function startNew(chats: Chats): Chats {
  const empty = Object.keys(chats.byKey).find((key) => chats.byKey[key].turns.length === 0);
  if (empty) return { ...chats, active: empty };
  const key = `new-${chats.next}`;
  return { byKey: { ...chats.byKey, [key]: EMPTY }, active: key, next: chats.next + 1 };
}

/** Show a stored chat; if it is already open (perhaps still working) keep what is there. */
export function openStored(chats: Chats, conversationId: string, stored: Transcript): Chats {
  const key = keyFor(chats, conversationId);
  if (key) return { ...chats, active: key };
  return { ...chats, byKey: { ...chats.byKey, [conversationId]: stored }, active: conversationId };
}

export function activate(chats: Chats, key: string): Chats {
  return chats.byKey[key] ? { ...chats, active: key } : chats;
}

/** Forget a deleted chat; if it was showing, show an empty one. */
export function forget(chats: Chats, conversationId: string): Chats {
  const key = keyFor(chats, conversationId);
  if (!key) return chats;
  const byKey = { ...chats.byKey };
  delete byKey[key];
  const next = { ...chats, byKey };
  return chats.active === key ? startNew({ ...next, byKey: Object.keys(byKey).length ? byKey : { "new-1": EMPTY } }) : next;
}

/** Chats still working, by conversation id, from both the open transcripts and bobd. */
export function workingIds(chats: Chats, list: ChatSummary[]): Set<string> {
  const ids = new Set(list.filter((chat) => chat.running).map((chat) => chat.id));
  for (const transcript of Object.values(chats.byKey)) {
    if (transcript.busy && transcript.conversationId) ids.add(transcript.conversationId);
  }
  return ids;
}

export type Group = { label: string; chats: ChatSummary[] };

/** Pinned first, then by day: Today, Yesterday, Previous 7 days, Older. */
export function groupChats(list: ChatSummary[], now = new Date()): Group[] {
  const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  const day = 86_400_000;
  const groups: Group[] = [
    { label: "Pinned", chats: [] },
    { label: "Today", chats: [] },
    { label: "Yesterday", chats: [] },
    { label: "Previous 7 days", chats: [] },
    { label: "Older", chats: [] },
  ];
  for (const chat of list) {
    if (chat.pinned) {
      groups[0].chats.push(chat);
      continue;
    }
    const at = Date.parse(chat.updated_at);
    const index = at >= startOfToday ? 1 : at >= startOfToday - day ? 2 : at >= startOfToday - 7 * day ? 3 : 4;
    groups[index].chats.push(chat);
  }
  return groups.filter((group) => group.chats.length > 0);
}
