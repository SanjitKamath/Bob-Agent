import { describe, expect, it } from "vitest";
import { bobHomeCandidates, summarise } from "../shared/bobd";
import { moveCard, visibleCards } from "../shared/cards";
import { menuPosition, shouldOpen } from "../main/placement";
import { parseDue } from "../shared/dates";
import { applyJobEvent, describeResult, jobFailed, jobsFor } from "../shared/jobs";
import { changesCards, noticeFor } from "../shared/notify";
import { DEFAULT_PREFS, mergePrefs } from "../shared/prefs";
import type { Card } from "../shared/types";
import { parseBobUrl } from "../shared/urls";
import { applyMessage, EMPTY, startTurn } from "../renderer/src/lib/chat";

const card = (plugin: string): Card => ({
  plugin, title: plugin, value: 1, status: "", error: "", refresh_seconds: null, buttons: [],
});
const CARDS = [card("mail"), card("tasks"), card("knowledge")];

describe("bob:// URLs", () => {
  it("accepts the fixed set of actions", () => {
    expect(parseBobUrl("bob://triage")).toEqual({ kind: "triage" });
    expect(parseBobUrl("bob://ask?q=what%27s%20new%3F")).toEqual({ kind: "ask", text: "what's new?" });
    expect(parseBobUrl("bob://ask")).toEqual({ kind: "panel" });
    expect(parseBobUrl("bob://ask?q=%20")).toEqual({ kind: "panel" });
    expect(parseBobUrl("bob://SETTINGS")).toEqual({ kind: "settings" });
    expect(parseBobUrl("bob://reindex")).toEqual({ kind: "reindex" });
    expect(parseBobUrl("bob://widget")).toEqual({ kind: "app", section: "today" }); // the widget is gone
    expect(parseBobUrl("bob:panel")).toEqual({ kind: "panel" });
    expect(parseBobUrl("bob://app")).toEqual({ kind: "app", section: "chat" });
    expect(parseBobUrl("bob://app?section=Tasks")).toEqual({ kind: "app", section: "tasks" });
    expect(parseBobUrl("bob://app?section=shell").kind).toBe("invalid");
  });

  it("never lets a link name a tool or another scheme", () => {
    for (const url of ["bob://tool/mail/send?to=x", "bob://mail.send", "bob://delete", "https://ask?q=x", "not a url"]) {
      expect(parseBobUrl(url).kind).toBe("invalid");
    }
    expect(parseBobUrl(`bob://ask?q=${"x".repeat(5000)}`).kind).toBe("invalid");
  });
});

describe("preferences", () => {
  it("merges valid values and ignores the rest", () => {
    const prefs = mergePrefs(DEFAULT_PREFS, {
      serverUrl: "http://localhost:9000/",
      cards: { hidden: ["mail", 7], order: "nope" },
      openAtLogin: "yes",
      extra: true,
    } as never);
    expect(prefs.serverUrl).toBe("http://localhost:9000");
    expect(prefs.cards).toEqual({ hidden: ["mail"], order: [] });
    expect(prefs.openAtLogin).toBe(true);
    expect(mergePrefs(DEFAULT_PREFS, "garbage")).toEqual(DEFAULT_PREFS);
    expect("widget" in mergePrefs(DEFAULT_PREFS, { widget: { visible: true } } as never)).toBe(false);
  });

  it("quits on Cmd+Q unless keep-running is switched on", () => {
    expect(DEFAULT_PREFS.keepRunning).toBe(false);
    expect(mergePrefs(DEFAULT_PREFS, { keepRunning: true }).keepRunning).toBe(true);
    expect(mergePrefs(DEFAULT_PREFS, { keepRunning: "yes" } as never).keepRunning).toBe(false);
  });

  it("keeps a valid theme and falls back to following macOS", () => {
    expect(DEFAULT_PREFS.theme).toBe("system");
    expect(mergePrefs(DEFAULT_PREFS, { theme: "dark" }).theme).toBe("dark");
    expect(mergePrefs(DEFAULT_PREFS, { theme: "sepia" } as never).theme).toBe("system");
    expect(mergePrefs({ ...DEFAULT_PREFS, theme: "light" }, {}).theme).toBe("light");
  });

  it("carries the card order and hidden cards over from the old widget settings", () => {
    const saved = { widget: { visible: true, size: "large", cards: ["tasks", "mail"], hidden: ["knowledge"], positions: {} } };
    expect(mergePrefs(DEFAULT_PREFS, saved).cards).toEqual({ order: ["tasks", "mail"], hidden: ["knowledge"] });
  });

  it("only accepts a bobd address on this Mac", () => {
    for (const url of ["http://192.168.1.5:8765", "https://example.com", "file:///etc", "nonsense"]) {
      expect(mergePrefs(DEFAULT_PREFS, { serverUrl: url }).serverUrl).toBe(DEFAULT_PREFS.serverUrl);
    }
  });
});

describe("cards", () => {
  it("orders and hides cards as chosen in Settings", () => {
    const ids = (cards: Card[]): string[] => cards.map((c) => c.plugin);
    expect(ids(visibleCards(CARDS, { order: [], hidden: [] }))).toEqual(["mail", "tasks", "knowledge"]);
    expect(ids(visibleCards(CARDS, { order: ["knowledge"], hidden: ["mail"] }))).toEqual(["knowledge", "tasks"]);
    expect(ids(visibleCards(CARDS, { order: ["gone", "tasks"], hidden: [] }))).toEqual(["tasks", "mail", "knowledge"]);
  });

  it("moves a card and stops at the ends", () => {
    expect(moveCard(CARDS, [], "tasks", -1)).toEqual(["tasks", "mail", "knowledge"]);
    expect(moveCard(CARDS, ["tasks", "mail", "knowledge"], "tasks", 1)).toEqual(["mail", "tasks", "knowledge"]);
    expect(moveCard(CARDS, [], "mail", -1)).toEqual(["mail", "tasks", "knowledge"]);
    expect(moveCard(CARDS, [], "knowledge", 1)).toEqual(["mail", "tasks", "knowledge"]);
    expect(moveCard(CARDS, [], "missing", 1)).toEqual(["mail", "tasks", "knowledge"]);
  });
});

describe("notifications", () => {
  it("turns urgent mail into a notice with actions that are bob:// asks", () => {
    const notice = noticeFor("mail.urgent", { id: 12, from: "ana@example.com", subject: "Invoice overdue" })!;
    expect(notice.title).toBe("Urgent mail");
    expect(notice.body).toBe("ana@example.com: Invoice overdue");
    expect(notice.actions.map((a) => a.label)).toEqual(["Open thread", "Draft reply"]);
    expect(parseBobUrl(notice.actions[0].url)).toEqual({ kind: "ask", text: "Show me the thread for mail message 12" });
    expect(noticeFor("mail.urgent", { from: "x" })).toBeNull();
  });

  it("notifies about failures, not about routine successes", () => {
    expect(noticeFor("routine.finished", { routine: "mail-triage", outcome: "ok" })).toBeNull();
    expect(noticeFor("routine.finished", { routine: "mail-triage", outcome: "skipped" })).toBeNull();
    expect(noticeFor("routine.finished", { routine: "mail-triage", outcome: "error", detail: "no password" })!.body).toBe(
      "mail-triage: no password",
    );
    expect(noticeFor("conformance.finished", { ok: true })).toBeNull();
    expect(noticeFor("conformance.finished", { ok: false })!.title).toBe("Bob stopped");
    expect(noticeFor("card.created", { title: "Morning brief" })!.title).toBe("Morning brief");
    expect(noticeFor("tool.finished", {})).toBeNull();
  });

  it("knows which events change the cards", () => {
    for (const topic of ["tool.finished", "routine.finished", "plugin.enabled", "mail.urgent", "bobd.started"]) {
      expect(changesCards(topic)).toBe(true);
    }
    expect(changesCards("confirm.requested")).toBe(false);
  });
});

describe("starting bobd", () => {
  it("looks in the folder from Settings first, then the usual places", () => {
    expect(bobHomeCandidates("/x/MyBob/", "/Users/s")).toEqual(["/x/MyBob", "/Users/s/Bob", "/Users/s/dev/bob", "/Users/s/dev/Bob"]);
    expect(bobHomeCandidates("", "/Users/s")[0]).toBe("/Users/s/Bob");
  });

  it("shows the script's last line, and a hint when it failed silently", () => {
    expect(summarise("Ollama is not running.\nbobd started.\n", 0)).toEqual({ ok: true, message: "bobd started." });
    expect(summarise("bobd did not answer within 45 seconds. See log.\n", 1)).toEqual({
      ok: false,
      message: "bobd did not answer within 45 seconds. See log.",
    });
    expect(summarise("", 1).message).toContain("deploy/bobd.sh start");
  });
});

describe("background jobs", () => {
  const topic = "form-author.job.progress";
  const event = (data: Record<string, unknown>) => ({ type: "event" as const, topic, data, at: "" });

  it("collects progress per job and shows it only in its own chat", () => {
    let jobs = applyJobEvent([], topic, { job: "j1", session: "c1", stage: "draft", message: "Outline: 4 tabs" });
    jobs = applyJobEvent(jobs, topic, { job: "j1", message: "[1/14] Drafting A", done_sections: 0, total_sections: 14 });
    jobs = applyJobEvent(jobs, topic, { job: "j1", message: "[1/14] A: 11 field(s)", done_sections: 1, total_sections: 14 });
    jobs = applyJobEvent(jobs, "mail.urgent", { job: "j1", message: "not a job" });
    expect(jobs).toHaveLength(1);
    expect(jobs[0]).toMatchObject({ plugin: "form-author", session: "c1", stage: "draft", step: 1, total: 14, done: false });
    expect(jobs[0].lines.at(-1)).toBe("[1/14] A: 11 field(s)");
    expect(jobsFor(jobs, "c1")).toHaveLength(1);
    expect(jobsFor(jobs, "c2")).toHaveLength(0);
    expect(jobsFor(jobs, null)).toHaveLength(0);
    for (let n = 0; n < 10; n++) jobs = applyJobEvent(jobs, topic, { job: "j1", message: `line ${n}` });
    expect(jobs[0].lines).toHaveLength(6);
    jobs = applyJobEvent(jobs, topic, { job: "j1", status: "failed", message: "Stopped at draft: x", done: true });
    expect(jobFailed(jobs[0])).toBe(true);
  });

  it("keeps job progress in the chat after the answer has arrived", () => {
    let transcript = startTurn(EMPTY, "build my form");
    transcript = applyMessage(transcript, event({ job: "j1", session: "c1", message: "Outline" }));
    transcript = applyMessage(transcript, {
      type: "final", conversation_id: "c1", text: "Started.", specialist: "form-author",
      sources: [], warnings: [], steps: 2, seconds: 3,
    });
    transcript = applyMessage(transcript, event({ job: "j1", message: "Done: 185 fields", done: true, status: "done" }));
    const shown = jobsFor(transcript.jobs, transcript.conversationId);
    expect(shown).toHaveLength(1);
    expect(shown[0].done && !jobFailed(shown[0])).toBe(true);
    expect(transcript.turns[0].answer).toBe("Started.");
  });

  it("notifies when a job ends, not while it runs", () => {
    expect(noticeFor(topic, { job: "j1", message: "[2/14] Drafting" })).toBeNull();
    const done = noticeFor(topic, { job: "j1", done: true, status: "done", message: "Done: 185 fields" });
    expect(done?.title).toBe("A background job finished");
    expect(done?.actions.map((action) => action.url)).toEqual([
      "bob://app?section=chat",
      "bob://ask?q=What%20is%20the%20status%20of%20job%20j1%3F",
    ]);
    expect(noticeFor(topic, { job: "j1", done: true, status: "failed", message: "Stopped at draft" })?.title).toBe(
      "A background job stopped",
    );
    expect(changesCards(topic)).toBe(false);
  });
});

import { webAddress, webLabel } from "../shared/sources";

describe("sources from the web", () => {
  it("open only plain web links", () => {
    expect(webAddress({ collection: "web", path: "https://example.com/a?b=1" })).toBe("https://example.com/a?b=1");
    for (const path of ["file:///etc/passwd", "javascript:alert(1)", "https://u:p@example.com/", "not a url", "x-apple.systempreferences:"]) {
      expect(webAddress({ collection: "web", path })).toBeNull();
    }
    expect(webAddress({ collection: "forms", path: "https://example.com/" })).toBeNull();
  });
  it("are labelled by site and title", () => {
    expect(webLabel({ path: "https://www.python.org/doc/", title: "Docs" })).toBe("python.org: Docs");
    expect(webLabel({ path: "https://example.com/", title: "https://example.com/" })).toBe("example.com");
  });
});

describe("a finished job's message", () => {
  it("is split into a summary, a folder and a list", () => {
    const text =
      "Done: 9 fields in 2 sections; fac_cli passed. Folder: /Users/me/out/Form_1\n\nWhat I could not do:\n- Address: not filled\n- PAN: guessed";
    expect(describeResult(text)).toEqual({
      summary: "Done: 9 fields in 2 sections; fac_cli passed.",
      folder: "/Users/me/out/Form_1",
      blocks: [
        { kind: "heading", text: "What I could not do:" },
        { kind: "item", text: "Address: not filled" },
        { kind: "item", text: "PAN: guessed" },
      ],
    });
  });

  it("keeps a plain message as the summary", () => {
    expect(describeResult("Stopped at validate")).toEqual({ summary: "Stopped at validate", folder: "", blocks: [] });
  });
});

describe("the menu bar dropdown", () => {
  const area = { x: 0, y: 25, width: 1440, height: 875 };
  const icon = { x: 1200, y: 0, width: 40, height: 24 };

  it("opens under the icon, centred", () => {
    expect(menuPosition(icon, area, { x: 0, y: 0 }, 340)).toEqual({ x: 1050, y: 29 });
  });

  it("stays inside the screen at the right edge", () => {
    const edge = { x: 1410, y: 0, width: 30, height: 24 };
    expect(menuPosition(edge, area, { x: 0, y: 0 }, 340).x).toBe(1440 - 340 - 8);
  });

  it("opens under the pointer when the icon's place is unknown (full-screen space)", () => {
    const none = { x: 0, y: 0, width: 0, height: 0 };
    expect(menuPosition(none, area, { x: 900, y: 3 }, 340)).toEqual({ x: 730, y: 29 });
  });

  it("does not reopen on the click that closed it", () => {
    expect(shouldOpen(true, 0, 1000)).toBe(false);
    expect(shouldOpen(false, 900, 1000)).toBe(false); // it hid itself a moment ago
    expect(shouldOpen(false, 0, 1000)).toBe(true);
  });
});

describe("a typed due date", () => {
  const now = new Date(2026, 10, 18); // Wednesday 18 November 2026
  it("understands the words people use", () => {
    expect(parseDue("", now)).toBe("");
    expect(parseDue("today", now)).toBe("2026-11-18");
    expect(parseDue("Tomorrow", now)).toBe("2026-11-19");
    expect(parseDue("fri", now)).toBe("2026-11-20");
    expect(parseDue("wednesday", now)).toBe("2026-11-25"); // today is "today"
    expect(parseDue("+3", now)).toBe("2026-11-21");
    expect(parseDue("+1w", now)).toBe("2026-11-25");
    expect(parseDue("in 2 weeks", now)).toBe("2026-12-02");
  });
  it("reads dates, day first, and rolls a past day into next year", () => {
    expect(parseDue("2026-12-01", now)).toBe("2026-12-01");
    expect(parseDue("1/12", now)).toBe("2026-12-01");
    expect(parseDue("20 nov", now)).toBe("2026-11-20");
    expect(parseDue("Nov 20th", now)).toBe("2026-11-20");
    expect(parseDue("5 nov", now)).toBe("2027-11-05");
    expect(parseDue("3/4/27", now)).toBe("2027-04-03");
  });
  it("refuses what it cannot read or what is not a day", () => {
    expect(parseDue("soonish", now)).toBeNull();
    expect(parseDue("2026-02-30", now)).toBeNull();
    expect(parseDue("31/2", now)).toBeNull();
    expect(parseDue("12 foo", now)).toBeNull();
  });
});
