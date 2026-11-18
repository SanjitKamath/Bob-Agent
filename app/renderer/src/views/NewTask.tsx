import { type FormEvent, useEffect, useRef, useState } from "react";
import { describeDue, parseDue } from "../../../shared/dates";
import { bob } from "../bob";
import { Dropdown } from "../components/Dropdown";
import { tasksTool } from "../lib/tasks";

type Priority = "high" | "normal" | "low";

/** The "New task" window, opened from the menu bar dropdown: everything about one task. */
export function NewTask() {
  const [title, setTitle] = useState("");
  const [notes, setNotes] = useState("");
  const [due, setDue] = useState("");
  const [priority, setPriority] = useState<Priority>("normal");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const titleBox = useRef<HTMLInputElement>(null);
  const day = parseDue(due);

  // Each time the window is shown again it starts empty with the cursor in the title.
  useEffect(() => {
    const reset = (): void => {
      setTitle("");
      setNotes("");
      setDue("");
      setPriority("normal");
      setError("");
      setBusy(false);
      titleBox.current?.focus();
    };
    const key = (event: KeyboardEvent): void => {
      if (event.key === "Escape") void bob.hideWindow();
    };
    window.addEventListener("focus", reset);
    window.addEventListener("keydown", key);
    titleBox.current?.focus();
    return () => {
      window.removeEventListener("focus", reset);
      window.removeEventListener("keydown", key);
    };
  }, []);

  async function submit(event: FormEvent): Promise<void> {
    event.preventDefault();
    if (!title.trim() || day === null || busy) return;
    setBusy(true);
    try {
      await tasksTool("create", {
        title: title.trim(),
        priority,
        ...(day ? { due: day } : {}),
        ...(notes.trim() ? { notes: notes.trim() } : {}),
      });
      void bob.refresh();
      await bob.hideWindow();
    } catch (problem) {
      setError(`${(problem as Error).message}. Is the Tasks plugin on?`);
      setBusy(false);
    }
  }

  return (
    <main className="newtask">
      <header className="newtask-head">
        <strong>New task</strong>
      </header>
      <form className="newtask-form" onSubmit={(event) => void submit(event)}>
        <label>
          <span>Task</span>
          <input ref={titleBox} value={title} placeholder="What needs doing?" onChange={(event) => setTitle(event.target.value)} />
        </label>
        <label>
          <span>Notes</span>
          <textarea value={notes} rows={3} placeholder="Optional" onChange={(event) => setNotes(event.target.value)} />
        </label>
        <div className="newtask-field">
          <span>Due</span>
          <div className="quick-due">
            {(["today", "tomorrow", "+1w"] as const).map((choice) => (
              <button
                type="button"
                key={choice}
                className={`chip-button ${due === choice ? "active" : ""}`}
                onClick={() => setDue(due === choice ? "" : choice)}
              >
                {choice === "+1w" ? "Next week" : choice === "today" ? "Today" : "Tomorrow"}
              </button>
            ))}
          </div>
          <input value={due} placeholder="or a date: fri, 20 nov, 1/12, 2026-12-01" aria-label="Due date" onChange={(event) => setDue(event.target.value)} />
          {day === null ? (
            <p className="quick-note error">I don't understand that date.</p>
          ) : (
            day && <p className="quick-note muted">Due {describeDue(day)}</p>
          )}
        </div>
        <div className="newtask-field">
          <span>Priority</span>
          <Dropdown
            label="Priority"
            value={priority}
            onChange={setPriority}
            choices={[
              { value: "high", label: "High" },
              { value: "normal", label: "Normal" },
              { value: "low", label: "Low" },
            ]}
          />
        </div>
        {error && <p className="error">{error}</p>}
        <footer className="newtask-actions">
          <button type="button" onClick={() => void bob.hideWindow()}>
            Cancel
          </button>
          <button className="primary" disabled={!title.trim() || day === null || busy}>
            Add task
          </button>
        </footer>
      </form>
    </main>
  );
}
