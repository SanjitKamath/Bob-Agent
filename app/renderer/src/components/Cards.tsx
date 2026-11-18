import { useState } from "react";
import type { Card, CardButton, Confirmation } from "../../../shared/types";
import { bob } from "../bob";
import logoMark from "../assets/logo-mark.png";

/** Bob's logo, the same tile as the app icon. Decorative: "Bob" is always written beside it. */
export function Mark({ size = 22 }: { size?: number }) {
  return <img className="mark" src={logoMark} width={size} height={size} alt="" aria-hidden="true" draggable={false} />;
}

/** Shown while something loads: a shimmer, with words for screen readers. */
export function Shimmer({ label = "Loading" }: { label?: string }) {
  return (
    <div className="shimmer" role="status">
      <span />
      <span />
      <span />
      <b className="sr-only">{label}</b>
    </div>
  );
}

/** Gold marks the number that needs you: unanswered urgent mail. */
function needsYou(card: Card): boolean {
  return card.plugin === "mail" && typeof card.value === "number" && card.value > 0;
}

export function CardView({ card, interactive }: { card: Card; interactive: boolean }) {
  const [busy, setBusy] = useState("");
  const [note, setNote] = useState("");

  async function run(button: CardButton): Promise<void> {
    setBusy(button.label);
    setNote("");
    const result = await bob.runButton(button);
    setBusy("");
    if (!result.ok) setNote(result.error);
  }

  return (
    <section className={needsYou(card) ? "card attention" : "card"} aria-label={card.title}>
      <header className="card-title">{card.title}</header>
      <div className="card-value">{card.value ?? "–"}</div>
      <div className={card.error ? "card-status error" : "card-status"}>{card.error || note || card.status}</div>
      {interactive && card.buttons.length > 0 && (
        <div className="card-buttons">
          {card.buttons.map((button) => (
            <button key={button.label} disabled={busy !== ""} onClick={() => void run(button)}>
              {busy === button.label ? "Working…" : button.label}
              {button.asks_first ? " …" : ""}
            </button>
          ))}
        </div>
      )}
    </section>
  );
}

/** What a card counts, one line each: every open task, every mail that needs you. */
export function CardItems({ card }: { card: Card }) {
  const items = card.items ?? [];
  if (items.length === 0) return null;
  // Mail has no section of its own; its lines open Today.
  const section = card.plugin === "tasks" ? "tasks" : "today";
  return (
    <section className="lines" aria-label={`${card.title} list`}>
      <header className="lines-title">
        <span>{card.title}</span>
        <span className="muted">{items.length}</span>
      </header>
      <ul>
        {items.map((item, index) => (
          <li key={index}>
            <button className="line" onClick={() => void bob.openView("app", section)}>
              <span className={`flag ${item.flag}`} aria-hidden="true" />
              <span className="line-main">
                <span className="line-title">{item.title}</span>
                {item.detail && <span className="line-detail">{item.detail}</span>}
              </span>
            </button>
          </li>
        ))}
      </ul>
    </section>
  );
}

/** A confirm card: an external action waits here for your decision. */
export function ConfirmCard({ confirmation }: { confirmation: Confirmation }) {
  const entries = Object.entries(confirmation.arguments);
  const folder = confirmation.tool === "allow_folder";
  return (
    <section className="confirm" role="alertdialog" aria-label={folder ? "Allow a folder" : "Confirm action"}>
      <header>
        {folder ? (
          <>Bob wants to work in a folder you named</>
        ) : (
          <>
            Bob wants to run <strong>{confirmation.plugin}.{confirmation.tool}</strong>
          </>
        )}
      </header>
      <p className="muted">
        {folder
          ? "It can read and change files there for this chat only, never credentials, git repositories or system folders. Every change can be undone."
          : "This acts outside your Mac. It runs only if you approve."}
      </p>
      {entries.length > 0 && (
        <dl>
          {entries.map(([key, value]) => (
            <div key={key}>
              <dt>{key}</dt>
              <dd>{typeof value === "string" ? value : JSON.stringify(value)}</dd>
            </div>
          ))}
        </dl>
      )}
      <div className="row end">
        <button onClick={() => void bob.confirm(confirmation.id, false)}>Decline</button>
        <button className="primary" onClick={() => void bob.confirm(confirmation.id, true)}>
          Approve
        </button>
      </div>
    </section>
  );
}

/** Starts bobd with deploy/bobd.sh and says how it went. */
export function StartBobd({ label = "Start bobd", className = "primary" }: { label?: string; className?: string }) {
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState("");
  async function start(): Promise<void> {
    setBusy(true);
    setNote("");
    const result = await bob.bobd("start");
    setBusy(false);
    if (!result.ok) setNote(result.message);
  }
  return (
    <>
      <button className={className} disabled={busy} onClick={() => void start()}>
        {busy ? "Starting…" : label}
      </button>
      {note && <span className="start-note error">{note}</span>}
    </>
  );
}

export function ConnectionNote({ state, detail }: { state: string; detail: string }) {
  if (state === "connected") return null;
  const text = state === "connecting" ? "Connecting to bobd…" : detail || "bobd is not running.";
  return (
    <div className="banner start-banner">
      <span className="grow">{text}</span>
      {state === "offline" && <StartBobd />}
    </div>
  );
}
