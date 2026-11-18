import { useState } from "react";
import { visibleCards } from "../../../shared/cards";
import { bob, useAppState } from "../bob";
import { CardItems, CardView, ConnectionNote, Mark } from "../components/Cards";

/** Restart bobd, for example after updating Bob. */
function RestartBobd() {
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState("");
  return (
    <>
      <button
        disabled={busy}
        onClick={() => {
          setBusy(true);
          setNote("");
          void bob.bobd("restart").then((result) => {
            setBusy(false);
            setNote(result.message);
          });
        }}
      >
        {busy ? "Restarting bobd…" : "Restart bobd"}
      </button>
      {note && <p className="muted start-note">{note}</p>}
    </>
  );
}

/** The menu bar dropdown: status, quick actions and the plugin cards. */
export function Menu() {
  const state = useAppState();
  if (!state) return null;
  const cards = visibleCards(state.snapshot?.cards ?? [], state.prefs.cards);
  const dot = state.connection === "connected" ? "ok" : state.connection === "connecting" ? "wait" : "bad";
  return (
    <main className="menu">
      <header className="row">
        <Mark size={18} />
        <strong>Bob</strong>
        <span className={`dot ${dot}`} />
        <span className="muted">{state.connection === "connected" ? "running" : state.connection}</span>
        <span className="spacer" />
        <button className="plain" onClick={() => void bob.refresh()}>
          Refresh
        </button>
      </header>
      <ConnectionNote state={state.connection} detail={state.detail} />
      {state.connection === "connected" && (
        <button className="primary add-task" onClick={() => void bob.openView("newtask")}>
          + New task
        </button>
      )}
      <div className="menu-scroll">
      <div className="cards menu-cards">
        {cards.map((card) => (
          <CardView key={card.plugin} card={card} interactive />
        ))}
        {state.connection === "connected" && cards.length === 0 && (
          <p className="muted">No cards. Enable a plugin that has one, or unhide cards in Settings.</p>
        )}
      </div>
      {cards.map((card) => (
        <CardItems key={card.plugin} card={card} />
      ))}
      </div>
      <nav className="actions">
        <button onClick={() => void bob.openView("app", "today")}>Open Bob</button>
        <button onClick={() => void bob.openView("panel")}>Ask Bob… <kbd>⌥Space</kbd></button>
        <button onClick={() => void bob.openView("settings")}>Settings…</button>
        <RestartBobd />
        <button onClick={() => void bob.quit()}>Quit Bob</button>
      </nav>
    </main>
  );
}
