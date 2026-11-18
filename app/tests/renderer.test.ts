import { describe, expect, it } from "vitest";
import { applyMessage, EMPTY, fromHistory, startTurn, withFiles } from "../renderer/src/lib/chat";
import { activate, forget, groupChats, keyFor, newChats, openStored, route, startNew, update, workingIds } from "../renderer/src/lib/chats";
import { addRoot, joinRoots, removeRoot, splitRoots } from "../renderer/src/lib/roots";
import { changedSettings, coerce, formFields } from "../renderer/src/lib/schema";
import type { ServerMessage } from "../shared/types";

const SCHEMA = {
  properties: {
    output_dir: { type: "string" as const, title: "Output dir", format: "path", default: "~/bob_output/forms" },
    default_client: { type: "string" as const, default: "acme" },
    repair_rounds: { type: "integer" as const, default: 3 },
    auto_draft: { type: "boolean" as const, default: true, description: "Draft replies" },
    mode: { type: "string" as const, enum: ["a", "b"], default: "a" },
    password: { type: "string" as const, writeOnly: true },
  },
};

describe("settings form from a plugin's schema", () => {
  const fields = formFields(SCHEMA, { default_client: "acme", repair_rounds: 5 });

  it("picks a control per type and never shows secrets", () => {
    expect(fields.map((f) => [f.key, f.kind, f.value])).toEqual([
      ["output_dir", "path", "~/bob_output/forms"],
      ["default_client", "text", "acme"],
      ["repair_rounds", "number", 5],
      ["auto_draft", "checkbox", true],
      ["mode", "select", "a"],
    ]);
    expect(fields[0].label).toBe("Output dir");
    expect(fields[3].help).toBe("Draft replies");
  });

  it("coerces what was typed, or says why not", () => {
    const [, , rounds, draft, mode] = fields;
    expect(coerce(rounds, "4")).toEqual({ ok: true, value: 4 });
    expect(coerce(rounds, "4.5")).toEqual({ ok: false, error: "repair_rounds needs a whole number" });
    expect(coerce(rounds, "")).toEqual({ ok: false, error: "repair_rounds needs a number" });
    expect(coerce(rounds, "abc").ok).toBe(false);
    expect(coerce(draft, false)).toEqual({ ok: true, value: false });
    expect(coerce(mode, "c").ok).toBe(false);
    expect(coerce(mode, "b")).toEqual({ ok: true, value: "b" });
  });

  it("sends only what changed", () => {
    expect(changedSettings(fields, { default_client: "acme", repair_rounds: "7", auto_draft: false })).toEqual({
      values: { repair_rounds: 7, auto_draft: false },
      errors: [],
    });
    expect(changedSettings(fields, { repair_rounds: "x" }).errors).toHaveLength(1);
    expect(changedSettings(fields, {})).toEqual({ values: {}, errors: [] });
  });

  it("edits a list setting one entry per line", () => {
    const schema = { properties: { skip_subjects: { type: "array" as const, title: "Skip mail whose subject contains" } } };
    const [field] = formFields(schema, { skip_subjects: ["newsletter", "[JIRA]"] });
    expect([field.kind, field.value]).toEqual(["lines", "newsletter\n[JIRA]"]);
    expect(coerce(field, "  newsletter \n\n[JIRA]\nYour daily digest")).toEqual({
      ok: true,
      value: ["newsletter", "[JIRA]", "Your daily digest"],
    });
    expect(changedSettings([field], { skip_subjects: "newsletter\n[JIRA]\n" })).toEqual({ values: {}, errors: [] });
    expect(changedSettings([field], { skip_subjects: "" }).values).toEqual({ skip_subjects: [] });
    const [empty] = formFields(schema, {});
    expect(empty.value).toBe("");
  });
});

describe("chat transcript", () => {
  it("marks an answer reused from the cache", () => {
    const reused: ServerMessage = { ...(final as Extract<ServerMessage, { type: "final" }>), cached: true, cached_at: "2026-11-15T10:00:00+00:00" };
    const done = play([reused]);
    expect(done.turns[0].cached).toBe(true);
  });

  it("ignores answers to a question another window asked", () => {
    const idle = { ...EMPTY };
    const answer: ServerMessage = {
      type: "final", conversation_id: "other", text: "Hi", specialist: "general", warnings: [], steps: 1, seconds: 1, sources: [],
    };
    expect(applyMessage(idle, answer)).toBe(idle);
    expect(applyMessage(idle, { type: "error", message: "x" })).toBe(idle);
  });

  const play = (messages: ServerMessage[]) => messages.reduce(applyMessage, startTurn(EMPTY, "send my notes"));
  const final: ServerMessage = {
    type: "final", conversation_id: "c1", text: "Sent [1].", specialist: "mail", warnings: ["check"], steps: 2, seconds: 3.2,
    sources: [{ n: 1, citation: "a:b:1@c", collection: "a", source: "s", path: "b", line: 1, version: "c", title: "t" }],
  };

  it("streams tokens, tracks tools and ends with the verified answer", () => {
    const done = play([
      { type: "route", domain: "mail", risk: "high", routed_by: "model" },
      { type: "token", text: "Let me " },
      { type: "token", text: "check." },
      { type: "tool_call", plugin: "mail", tool: "send", arguments: {} },
      { type: "tool_result", plugin: "mail", tool: "send", outcome: "ok" },
      { type: "token", text: "Sent" },
      final,
    ]);
    const turn = done.turns[0];
    expect(turn).toMatchObject({ answer: "Sent [1].", specialist: "mail", done: true, seconds: 3.2, warnings: ["check"] });
    expect(turn.cached).toBe(false);
    expect(turn.tools).toEqual([{ plugin: "mail", tool: "send", outcome: "ok" }]);
    expect(turn.sources).toHaveLength(1);
    expect(done).toMatchObject({ busy: false, conversationId: "c1" });
  });

  it("drops thinking-aloud text when a tool call starts, and marks denied tools", () => {
    const mid = play([
      { type: "token", text: "I will send it" },
      { type: "tool_call", plugin: "mail", tool: "send", arguments: {} },
    ]);
    expect(mid.turns[0].answer).toBe("");
    expect(mid.turns[0].tools[0].outcome).toBe("running");
    const denied = applyMessage(mid, { type: "tool_result", plugin: "mail", tool: "send", outcome: "denied" });
    expect(denied.turns[0].tools[0].outcome).toBe("denied");
  });

  it("ends a turn on error or stop, and ignores messages with no open turn", () => {
    expect(play([{ type: "error", message: "model failed" }])).toMatchObject({ busy: false, turns: [{ error: "model failed", done: true }] });
    expect(play([{ type: "cancelled" }]).turns[0].error).toBe("Stopped.");
    expect(applyMessage(EMPTY, { type: "token", text: "x" })).toBe(EMPTY);
    const done = play([final]);
    expect(applyMessage(done, { type: "token", text: "late" })).toBe(done);
    expect(applyMessage(done, { type: "event", topic: "tool.finished", data: {}, at: "" })).toBe(done);
    expect(startTurn(done, "next").turns.map((t) => t.id)).toEqual([1, 2]);
  });

  it("routes tagged messages to the question with their request id", () => {
    let chat = startTurn(EMPTY, "first", "r1");
    chat = applyMessage(chat, { type: "started", conversation_id: "c9", request_id: "r1" });
    expect(chat.conversationId).toBe("c9");
    chat = startTurn(chat, "second", "r2");
    chat = applyMessage(chat, { type: "queued", conversation_id: "c9", request_id: "r2" });
    expect(chat.turns.map((t) => t.queued)).toEqual([false, true]);
    chat = applyMessage(chat, { type: "token", text: "one", request_id: "r1", conversation_id: "c9" });
    expect(chat.turns.map((t) => t.answer)).toEqual(["one", ""]);
    // Another window's question in the same chat is not ours.
    expect(applyMessage(chat, { type: "token", text: "x", request_id: "elsewhere" })).toBe(chat);
    chat = applyMessage(chat, { ...(final as object), request_id: "r1", conversation_id: "c9" } as ServerMessage);
    expect(chat.busy).toBe(true); // the second question is still waiting
    chat = applyMessage(chat, { type: "started", request_id: "r2", conversation_id: "c9" });
    chat = applyMessage(chat, { type: "cancelled", request_id: "r2", conversation_id: "c9" });
    expect(chat.turns.map((t) => [t.done, t.queued, t.error])).toEqual([
      [true, false, ""],
      [true, false, "Stopped."],
    ]);
    expect(chat.busy).toBe(false);
  });

  it("rebuilds a stored chat from its messages", () => {
    const chat = fromHistory("c3", [
      { id: 1, at: "", role: "user", content: "hello", specialist: null },
      { id: 2, at: "", role: "assistant", content: "hi there", specialist: "general" },
      { id: 3, at: "", role: "user", content: "and?", specialist: null },
    ]);
    expect(chat.conversationId).toBe("c3");
    expect(chat.turns.map((t) => [t.question, t.answer, t.done])).toEqual([
      ["hello", "hi there", true],
      ["and?", "", true],
    ]);
    expect(chat.busy).toBe(false);
  });

  it("appends dropped file paths to the question", () => {
    expect(withFiles("summarise", [])).toBe("summarise");
    expect(withFiles("summarise", ["/a/x.pdf", "/b/y.docx"])).toBe("summarise\n\nFiles:\n- /a/x.pdf\n- /b/y.docx");
  });
});

import { parseInline, parseMarkdown } from "../renderer/src/lib/markdown";

describe("markdown in answers", () => {
  it("reads bold and inline code, and leaves stray markers alone", () => {
    expect(parseInline("Use **FIRST_OF** or `CONCAT(a, b)` now")).toEqual([
      { kind: "text", text: "Use " },
      { kind: "bold", text: "FIRST_OF" },
      { kind: "text", text: " or " },
      { kind: "code", text: "CONCAT(a, b)" },
      { kind: "text", text: " now" },
    ]);
    expect(parseInline("2 ** 3 and a ` tick")).toEqual([{ kind: "text", text: "2 ** 3 and a ` tick" }]);
    expect(parseInline("<img src=x onerror=alert(1)>")).toEqual([{ kind: "text", text: "<img src=x onerror=alert(1)>" }]);
  });

  it("reads paragraphs, lists, headings and code blocks", () => {
    const blocks = parseMarkdown("## Tasks\nYou have:\n\n- **one**\n- two\n1. first\n2) second\n\n```py\nx = 1\n# not a heading\n```\nDone.");
    expect(blocks.map((b) => b.kind)).toEqual(["heading", "paragraph", "list", "list", "code", "paragraph"]);
    expect(blocks[2]).toMatchObject({ ordered: false, items: [[{ kind: "bold", text: "one" }], [{ kind: "text", text: "two" }]] });
    expect(blocks[3]).toMatchObject({ ordered: true });
    expect(blocks[4]).toEqual({ kind: "code", text: "x = 1\n# not a heading", lang: "py" });
  });

  it("keeps the language of a fenced block only when it is a plain name", () => {
    expect(parseMarkdown("```python\nx = 1\n```")).toEqual([{ kind: "code", text: "x = 1", lang: "python" }]);
    expect(parseMarkdown("```\nx = 1\n```")).toEqual([{ kind: "code", text: "x = 1" }]);
    expect(parseMarkdown("```<img onerror=alert(1)>\nx\n```")).toEqual([{ kind: "code", text: "x" }]);
  });

  it("copes with an answer that is still streaming", () => {
    expect(parseMarkdown("Here:\n```\npartial")).toEqual([
      { kind: "paragraph", inline: [{ kind: "text", text: "Here:" }] },
      { kind: "code", text: "partial" },
    ]);
    expect(parseMarkdown("")).toEqual([]);
  });
});

describe("several chats", () => {
  const ask = (chats: ReturnType<typeof newChats>, key: string, text: string, request: string) =>
    update(chats, key, (t) => startTurn(t, text, request));
  const final = (request: string, conversation: string, text: string): ServerMessage => ({
    type: "final", conversation_id: conversation, text, specialist: "general", warnings: [], steps: 1, seconds: 1, sources: [],
    request_id: request,
  });

  it("keeps each chat's answers apart and says which chat finished", () => {
    let chats = ask(newChats(), "new-1", "slow one", "a");
    chats = startNew(chats);
    expect(chats.active).toBe("new-2");
    chats = ask(chats, "new-2", "quick one", "b");
    let routed = route(chats, { type: "started", request_id: "a", conversation_id: "ca" });
    routed = route(routed.chats, final("b", "cb", "quick answer"));
    expect(routed.finished).toBe("new-2");
    chats = routed.chats;
    expect(chats.byKey["new-1"]).toMatchObject({ busy: true, conversationId: "ca" });
    expect(chats.byKey["new-2"].turns[0].answer).toBe("quick answer");
    expect(keyFor(chats, "cb")).toBe("new-2");
    expect([...workingIds(chats, [])]).toEqual(["ca"]);
  });

  it("opens stored chats once, reuses an empty one for New chat, and forgets deleted ones", () => {
    let chats = newChats();
    expect(startNew(chats)).toEqual(chats); // still empty: no second blank chat
    chats = openStored(chats, "c1", { ...EMPTY, conversationId: "c1", turns: startTurn(EMPTY, "q").turns });
    expect(chats.active).toBe("c1");
    const again = openStored(activate(chats, "new-1"), "c1", EMPTY);
    expect(again.active).toBe("c1");
    expect(again.byKey.c1.turns).toHaveLength(1); // kept what was open
    const gone = forget(again, "c1");
    expect(gone.byKey.c1).toBeUndefined();
    expect(gone.active).toBe("new-1");
  });

  it("groups the list: pinned, today, yesterday, this week, older", () => {
    const now = new Date(2026, 10, 16, 15, 0);
    const at = (days: number) => new Date(2026, 10, 16 - days, 9, 0).toISOString();
    const chat = (id: string, days: number, pinned = false) => ({
      id, title: id, pinned, created_at: at(days), updated_at: at(days), messages: 2, running: false,
    });
    const groups = groupChats([chat("p", 30, true), chat("t", 0), chat("y", 1), chat("w", 4), chat("o", 20)], now);
    expect(groups.map((g) => [g.label, g.chats.map((c) => c.id)])).toEqual([
      ["Pinned", ["p"]],
      ["Today", ["t"]],
      ["Yesterday", ["y"]],
      ["Previous 7 days", ["w"]],
      ["Older", ["o"]],
    ]);
  });
});

describe("folders Bob may read", () => {
  it("adds, removes and joins folders", () => {
    const roots = splitRoots("~/Documents: ~/Desktop::");
    expect(roots).toEqual(["~/Documents", "~/Desktop"]);
    const added = addRoot(roots, "/Users/me/dev/customer");
    expect(joinRoots(added)).toBe("~/Documents:~/Desktop:/Users/me/dev/customer");
    expect(addRoot(added, "/Users/me/dev/customer/")).toBe(added);
    expect(removeRoot(added, "~/Desktop")).toEqual(["~/Documents", "/Users/me/dev/customer"]);
    expect(splitRoots(undefined)).toEqual([]);
  });

  it("refuses a path that cannot be stored", () => {
    expect(() => addRoot([], "/Users/me/a:b")).toThrow(/colon/);
    expect(() => addRoot([], "relative/folder")).toThrow(/full path/);
  });
});
