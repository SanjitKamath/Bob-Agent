// Which bobd events become notifications, and what their action buttons do.

export interface Notice {
  title: string;
  body: string;
  /** Button labels with the bob:// URL each one opens. A click on the body opens the first. */
  actions: { label: string; url: string }[];
}

const ask = (question: string): string => `bob://ask?q=${encodeURIComponent(question)}`;

export function noticeFor(topic: string, data: Record<string, unknown>): Notice | null {
  const text = (key: string): string => (typeof data[key] === "string" ? (data[key] as string) : "");
  if (topic.endsWith(".job.progress")) {
    // Only the end of a background job is worth a notification.
    if (data.done !== true) return null;
    const failed = data.status === "failed" || /FAILED|Stopped at/.test(text("message"));
    return {
      title: failed ? "A background job stopped" : "A background job finished",
      body: text("message").slice(0, 240),
      actions: [
        { label: "Open chat", url: "bob://app?section=chat" },
        { label: "Status", url: ask(`What is the status of job ${text("job")}?`) },
      ],
    };
  }
  switch (topic) {
    case "mail.urgent": {
      const id = typeof data.id === "number" ? data.id : null;
      if (id === null) return null;
      return {
        title: "Urgent mail",
        body: `${text("from")}: ${text("subject")}`.slice(0, 240),
        actions: [
          { label: "Open thread", url: ask(`Show me the thread for mail message ${id}`) },
          { label: "Draft reply", url: ask(`Draft a reply to mail message ${id}`) },
        ],
      };
    }
    case "routine.finished": {
      if (data.outcome === "ok" || data.outcome === "skipped") return null;
      return {
        title: "A routine failed",
        body: `${text("routine")}: ${text("detail") || String(data.outcome)}`.slice(0, 240),
        actions: [{ label: "Open Bob", url: "bob://settings" }],
      };
    }
    case "card.created":
      return {
        title: text("title") || "Bob has something for you",
        body: "Ready to read.",
        actions: [{ label: "Open", url: "bob://app?section=today" }],
      };
    case "conformance.finished":
      if (data.ok !== false) return null;
      return {
        title: "Bob stopped",
        body: "The read-only check failed, so bobd shut itself down.",
        actions: [{ label: "Details", url: "bob://settings" }],
      };
    default:
      return null;
  }
}

/** Events after which the cards may show different numbers. */
export function changesCards(topic: string): boolean {
  return (
    topic === "tool.finished" ||
    topic === "routine.finished" ||
    topic === "card.created" ||
    topic === "bobd.started" ||
    topic.startsWith("plugin.") ||
    topic.startsWith("mail.")
  );
}
