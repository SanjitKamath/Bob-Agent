import { Chat } from "../components/Chat";

/** The hotkey panel (⌥Space): a floating chat. */
export function Panel() {
  return (
    <main className="panel">
      <Chat variant="panel" />
    </main>
  );
}
