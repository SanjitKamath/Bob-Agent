// The main Bob window: chat, today, tasks and every setting in one place.
import { type CSSProperties, type FormEvent, type ReactNode, useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { visibleCards } from "../../../shared/cards";
import { APP_SECTIONS, type AppSection, isAppSection, SECTION_LABELS } from "../../../shared/sections";
import type { AppState } from "../../../shared/types";
import { bob, useAppState } from "../bob";
import { CardView, ConnectionNote, Mark, StartBobd } from "../components/Cards";
import { Chat } from "../components/Chat";
import { Markdown } from "../components/Markdown";
import { tasksTool } from "../lib/tasks";
import { GeneralTab, Knowledge, Models, Plugins, Privacy, Routines } from "./Settings";

const GROUPS: { label: string; sections: AppSection[] }[] = [
  { label: "", sections: ["chat", "today", "tasks"] },
  { label: "Settings", sections: ["knowledge", "plugins", "models", "routines", "general", "privacy"] },
];

/** Sections that read their data from bobd, so they wait until it is running. */
const NEEDS_BOBD: AppSection[] = ["today", "tasks", "knowledge", "plugins", "models", "routines", "privacy"];

// -- tasks ----------------------------------------------------------------------------------

interface Task {
  id: number;
  title: string;
  due: string | null;
  priority: "high" | "normal" | "low";
  notes: string;
  completed_at: string | null;
}

function today(): string {
  const now = new Date();
  const pad = (n: number): string => String(n).padStart(2, "0");
  return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
}

function useTasks(status: "open" | "done" | "all", limit: number, stamp: string) {
  const [tasks, setTasks] = useState<Task[]>([]);
  const [error, setError] = useState("");
  const [tick, setTick] = useState(0);
  useEffect(() => {
    let live = true;
    tasksTool<{ tasks: Task[] }>("list", { status, limit })
      .then((result) => {
        if (!live) return;
        setTasks(result.tasks);
        setError("");
      })
      .catch((problem: Error) => {
        if (!live) return;
        setTasks([]);
        setError(problem.message);
      });
    return () => {
      live = false;
    };
  }, [status, limit, stamp, tick]);
  const reload = useCallback(() => setTick((value) => value + 1), []);
  return { tasks, setTasks, error, reload };
}

function TaskRow({ task, onChange }: { task: Task; onChange: () => void }) {
  const [error, setError] = useState("");
  const done = task.completed_at !== null;
  const overdue = !done && task.due !== null && task.due < today();
  async function toggle(): Promise<void> {
    try {
      await tasksTool("complete", { task_id: task.id, done: !done });
      onChange();
    } catch (problem) {
      setError((problem as Error).message);
    }
  }
  return (
    <li className={done ? "item task-row done" : "item task-row"}>
      <input type="checkbox" checked={done} aria-label={`${done ? "Reopen" : "Complete"} ${task.title}`} onChange={() => void toggle()} />
      <span className="grow title">{task.title}</span>
      {task.priority !== "normal" && <span className={`badge priority ${task.priority}`}>{task.priority}</span>}
      {task.due && <span className={overdue ? "due warning" : "due muted"}>{task.due === today() ? "today" : task.due}</span>}
      {error && <span className="error">{error}</span>}
    </li>
  );
}

function NewTask({ onCreated }: { onCreated: () => void }) {
  const [title, setTitle] = useState("");
  const [due, setDue] = useState("");
  const [priority, setPriority] = useState<Task["priority"]>("normal");
  const [error, setError] = useState("");
  async function submit(event: FormEvent): Promise<void> {
    event.preventDefault();
    if (!title.trim()) return;
    try {
      await tasksTool("create", { title: title.trim(), priority, ...(due.trim() ? { due: due.trim() } : {}) });
      setTitle("");
      setDue("");
      setPriority("normal");
      setError("");
      onCreated();
    } catch (problem) {
      setError((problem as Error).message);
    }
  }
  return (
    <form className="task-form" onSubmit={(event) => void submit(event)}>
      <input className="grow" value={title} placeholder="New task" onChange={(event) => setTitle(event.target.value)} />
      <input className="due-input" value={due} placeholder="Due: today or 2026-12-01" onChange={(event) => setDue(event.target.value)} />
      <select value={priority} onChange={(event) => setPriority(event.target.value as Task["priority"])}>
        <option value="high">High</option>
        <option value="normal">Normal</option>
        <option value="low">Low</option>
      </select>
      <button className="primary" disabled={!title.trim()}>
        Add
      </button>
      {error && <div className="error full">{error}</div>}
    </form>
  );
}

function Tasks({ state }: { state: AppState }) {
  const [status, setStatus] = useState<"open" | "done" | "all">("open");
  const { tasks, error, reload } = useTasks(status, 200, state.snapshot?.generated_at ?? "");
  return (
    <section>
      <h2>Tasks</h2>
      <NewTask onCreated={reload} />
      <div className="segmented">
        {(["open", "done", "all"] as const).map((value) => (
          <button key={value} className={value === status ? "active" : ""} onClick={() => setStatus(value)}>
            {value === "open" ? "Open" : value === "done" ? "Done" : "All"}
          </button>
        ))}
      </div>
      {error ? (
        <p className="error">{error.includes("tasks") ? error : `${error}. Is the Tasks plugin on?`}</p>
      ) : (
        <ul className="items">
          {tasks.map((task) => (
            <TaskRow key={task.id} task={task} onChange={reload} />
          ))}
          {tasks.length === 0 && <li className="muted">{status === "done" ? "Nothing finished yet." : "No open tasks."}</li>}
        </ul>
      )}
    </section>
  );
}

// -- today ----------------------------------------------------------------------------------

function Today({ state, go }: { state: AppState; go: (section: AppSection) => void }) {
  const cards = visibleCards(state.snapshot?.cards ?? [], { order: state.prefs.cards.order, hidden: [] });
  const brief = state.snapshot?.brief ?? null;
  const { tasks, error, reload } = useTasks("open", 8, state.snapshot?.generated_at ?? "");
  const date = new Date().toLocaleDateString(undefined, { weekday: "long", day: "numeric", month: "long" });
  return (
    <section>
      <p className="eyebrow">{date}</p>
      <h2>Today</h2>
      <div className="cards today-cards">
        {cards.map((card) => (
          <CardView key={card.plugin} card={card} interactive />
        ))}
      </div>
      {cards.length === 0 && <p className="muted">No cards yet. Plugins with a card show it here.</p>}

      <div className="row">
        <h3 className="grow">Next tasks</h3>
        <button className="plain" onClick={() => go("tasks")}>
          All tasks
        </button>
      </div>
      {error ? (
        <p className="muted">The Tasks plugin is off or not answering.</p>
      ) : (
        <ul className="items">
          {tasks.map((task) => (
            <TaskRow key={task.id} task={task} onChange={reload} />
          ))}
          {tasks.length === 0 && <li className="muted">Nothing open.</li>}
        </ul>
      )}

      {brief && (
        <>
          <h3>{brief.title}</h3>
          <div className="brief-full">
            <Markdown text={brief.body} />
          </div>
        </>
      )}
    </section>
  );
}

// -- the window -----------------------------------------------------------------------------

function initialSection(): AppSection {
  const requested = new URLSearchParams(location.search).get("section");
  return isAppSection(requested) ? requested : "today";
}

export function App() {
  const state = useAppState();
  const [section, setSection] = useState<AppSection>(initialSection);
  useEffect(() => bob.onNavigate(setSection), []);
  // Questions sent from the menu bar or bob:// show the chat.
  useEffect(() => bob.onAsk(() => setSection("chat")), []);
  useEffect(() => {
    document.title = `Bob · ${SECTION_LABELS[section]}`;
  }, [section]);

  // The selected section's pill slides to it.
  const nav = useRef<HTMLElement>(null);
  const [pill, setPill] = useState<CSSProperties>({ opacity: 0 });
  useLayoutEffect(() => {
    const place = (): void => {
      const active = nav.current?.querySelector<HTMLElement>(".tab.active");
      if (active) setPill({ transform: `translateY(${active.offsetTop}px)`, height: active.offsetHeight, opacity: 1 });
    };
    place();
    window.addEventListener("resize", place);
    return () => window.removeEventListener("resize", place);
  }, [section, state !== null]);

  if (!state) return null;
  const connected = state.connection === "connected";
  const dot = connected ? "ok" : state.connection === "connecting" ? "wait" : "bad";

  let body: ReactNode = null;
  if (section === "chat") body = null;
  else if (!connected && NEEDS_BOBD.includes(section)) {
    body = <p className="muted">This appears once bobd is running.</p>;
  } else if (section === "today") body = <Today state={state} go={setSection} />;
  else if (section === "tasks") body = <Tasks state={state} />;
  else if (section === "knowledge") body = <Knowledge />;
  else if (section === "plugins") body = <Plugins />;
  else if (section === "models") body = <Models />;
  else if (section === "routines") body = <Routines />;
  else if (section === "general") body = <GeneralTab state={state} />;
  else body = <Privacy />;

  return (
    <main className="app">
      <nav className="sidebar drag" ref={nav}>
        <span className="tab-pill" aria-hidden="true" style={pill} />
        <div className="brand">
          <Mark />
          Bob
        </div>
        {GROUPS.map((group) => (
          <div key={group.label || "main"} className="group">
            {group.label && <h4>{group.label}</h4>}
            {group.sections.map((name) => (
              <button
                key={name}
                className={name === section ? "tab active nodrag" : "tab nodrag"}
                onClick={() => setSection(name)}
              >
                {SECTION_LABELS[name]}
                <kbd>⌘{APP_SECTIONS.indexOf(name) + 1}</kbd>
                {name === "chat" && state.confirmations.length > 0 && <span className="count">{state.confirmations.length}</span>}
              </button>
            ))}
          </div>
        ))}
        <div className="status nodrag">
          <span className={`dot ${dot}`} />
          <span>{connected ? "bobd running" : state.connection === "connecting" ? "Connecting…" : "bobd offline"}</span>
          {state.connection === "offline" ? (
            <StartBobd label="Start" className="plain" />
          ) : (
            !connected && (
              <button className="plain" onClick={() => void bob.refresh()}>
                Retry
              </button>
            )
          )}
        </div>
      </nav>
      {/* The chat stays mounted so a conversation survives a look at another section. */}
      <div className="content chat-host" hidden={section !== "chat"}>
        <Chat variant="app" active={section === "chat"} />
      </div>
      {section !== "chat" && (
        <div className="content">
          <ConnectionNote state={state.connection} detail={state.detail} />
          {body}
        </div>
      )}
    </main>
  );
}
