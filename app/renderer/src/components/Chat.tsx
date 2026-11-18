// Chats with Bob. The main window keeps several, listed beside the transcript, and any
// number can be working at once; the hotkey panel shows one.
import { type DragEvent, type KeyboardEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { describeResult, jobFailed, type JobProgress, jobsFor } from "../../../shared/jobs";
import { webAddress, webLabel } from "../../../shared/sources";
import type { ChatSummary, ServerMessage, Source, StoredTurn } from "../../../shared/types";
import { bob, call, useAppState } from "../bob";
import { EMPTY, fromHistory, requestId, startTurn, type Turn, withFiles } from "../lib/chat";
import {
  activate,
  activeTranscript,
  type Chats,
  forget,
  groupChats,
  keyFor,
  newChats,
  openStored,
  route,
  startNew,
  update,
  workingIds,
} from "../lib/chats";
import { useVoice } from "../lib/useVoice";
import { ConfirmCard, ConnectionNote, Mark, Shimmer } from "./Cards";
import { Markdown } from "./Markdown";

function SourceLink({ source }: { source: Source }) {
  const [missing, setMissing] = useState(false);
  const web = webAddress(source) !== null;
  return (
    <button
      className="source"
      title={web ? `Open ${source.path} in your browser` : missing ? "This file is not in Bob's knowledge base folder" : "Show in Finder"}
      onClick={() => void bob.revealSource(source).then((found) => setMissing(!found))}
    >
      [{source.n}] {web ? webLabel(source) : `${source.path}:${source.line}`}
      {missing ? " (not found)" : ""}
    </button>
  );
}

function TurnView({ turn, onAskAgain }: { turn: Turn; onAskAgain: () => void }) {
  return (
    <article className="turn">
      <div className="question">{turn.question}</div>
      {turn.tools.length > 0 && (
        <div className="tools">
          {turn.tools.map((step, index) => (
            <span key={index} className={`chip ${step.outcome}`}>
              {step.plugin}.{step.tool}
            </span>
          ))}
        </div>
      )}
      {turn.answer && <Markdown text={turn.answer} />}
      {!turn.done && !turn.answer &&
        (turn.queued ? <div className="muted working">Waiting for the previous question…</div> : <Shimmer label="Working" />)}
      {turn.error && <div className="error">{turn.error}</div>}
      {turn.warnings.map((warning) => (
        <div key={warning} className="warning">
          {warning}
        </div>
      ))}
      {turn.sources.length > 0 && (
        <div className="sources">
          {turn.sources.map((source) => (
            <SourceLink key={source.n} source={source} />
          ))}
        </div>
      )}
      {turn.done && turn.seconds !== null && (
        <div className="meta">
          {turn.specialist} · {turn.seconds}s
          {turn.cached && (
            <>
              {" · cached answer · "}
              <button className="plain link" title="Work the answer out again, ignoring the cache" onClick={onAskAgain}>
                Ask again
              </button>
            </>
          )}
        </div>
      )}
    </article>
  );
}

function JobView({ job }: { job: JobProgress }) {
  const failed = jobFailed(job);
  const state = job.done ? (failed ? "failed" : "done") : "running";
  return (
    <article className={`job job-${state}`} aria-live="polite">
      <div className="job-head">
        <span className={`chip ${job.done ? (failed ? "error" : "ok") : "running"}`}>
          {job.done ? (failed ? "Stopped" : "Done") : job.stage || "Working"}
        </span>
        <span className="job-title">Background job {job.job}</span>
        {!job.done && job.step !== null && job.total !== null && (
          <span className="job-count">
            {job.step} / {job.total}
          </span>
        )}
      </div>
      {!job.done && job.step !== null && job.total ? (
        <progress max={job.total} value={job.step} />
      ) : null}
      {job.done && job.lines.length > 0 ? (
        <JobResultView lines={job.lines} />
      ) : (
        <ul className="job-lines">
          {job.lines.map((line, index) => (
            <li key={index} className={index === job.lines.length - 1 ? "" : "muted"}>
              {line}
            </li>
          ))}
        </ul>
      )}
    </article>
  );
}

/** A finished job: what happened so far in small type, then the result laid out. */
function JobResultView({ lines }: { lines: string[] }) {
  const [copied, setCopied] = useState(false);
  const result = describeResult(lines[lines.length - 1]);
  const earlier = lines.slice(0, -1);
  return (
    <div className="job-result">
      {earlier.length > 0 && (
        <details className="job-steps">
          <summary>Earlier steps</summary>
          <ul className="job-lines">
            {earlier.map((line, index) => (
              <li key={index} className="muted">
                {line}
              </li>
            ))}
          </ul>
        </details>
      )}
      <p className="job-summary">{result.summary}</p>
      {result.folder && (
        <div className="job-folder">
          <code title={result.folder}>{result.folder}</code>
          <button
            className="plain link"
            onClick={() => void navigator.clipboard.writeText(result.folder).then(() => setCopied(true))}
          >
            {copied ? "Copied" : "Copy path"}
          </button>
        </div>
      )}
      {result.blocks.map((block, index) =>
        block.kind === "heading" ? (
          <h4 key={index} className="job-heading">
            {block.text.replace(/:$/, "")}
          </h4>
        ) : block.kind === "item" ? (
          <p key={index} className="job-item">
            {block.text}
          </p>
        ) : (
          <p key={index} className="job-note">
            {block.text}
          </p>
        ),
      )}
    </div>
  );
}

// -- state: several chats, routed by request ------------------------------------------------

/** Topics that change the chat list. */
const LIST_TOPICS = new Set(["chat.updated", "chat.deleted"]);

function useChats(notifyWhenAway: boolean) {
  const state = useAppState();
  const [chats, setChats] = useState<Chats>(newChats);
  const latest = useRef(chats);
  const notifications = useRef(true);
  notifications.current = state?.prefs.notifications ?? true;
  const [listTick, setListTick] = useState(0);

  const set = useCallback((next: Chats) => {
    latest.current = next;
    setChats(next);
  }, []);

  useEffect(
    () =>
      bob.onChat((message: ServerMessage) => {
        const { chats: next, finished } = route(latest.current, message);
        if (next !== latest.current) set(next);
        if (message.type === "event" ? LIST_TOPICS.has(message.topic) : ["started", "final", "error", "cancelled"].includes(message.type)) {
          setListTick((tick) => tick + 1);
        }
        if (message.type === "event" && message.topic === "chat.deleted") {
          set(forget(latest.current, String(message.data.id)));
        }
        // A chat you are not looking at finished: say so.
        if (finished && notifyWhenAway && notifications.current && (finished !== latest.current.active || !document.hasFocus())) {
          const transcript = latest.current.byKey[finished];
          const question = transcript?.turns.at(-1)?.question ?? "";
          const note = new Notification("Bob answered", { body: question.slice(0, 120), silent: false });
          note.onclick = () => {
            window.focus();
            set(activate(latest.current, finished));
          };
        }
      }),
    [notifyWhenAway, set],
  );

  function send(text: string, attached: string[] = [], fresh = false, key = latest.current.active, spoken = false): void {
    const question = text.trim();
    const transcript = latest.current.byKey[key] ?? EMPTY;
    // A new chat gets its id with the first answer; until then it takes one question.
    if (!question || (transcript.busy && !transcript.conversationId)) return;
    const request = requestId();
    set(update(latest.current, key, (current) => startTurn(current, question, request)));
    void bob.ask(withFiles(question, attached), transcript.conversationId, fresh, request, spoken ? "voice" : undefined);
  }

  async function open(conversationId: string): Promise<void> {
    const key = keyFor(latest.current, conversationId);
    if (key) {
      set(activate(latest.current, key));
      return;
    }
    const chat = await call<{ turns: StoredTurn[] }>("GET", `/v1/conversations/${encodeURIComponent(conversationId)}`);
    set(openStored(latest.current, conversationId, fromHistory(conversationId, chat.turns)));
  }

  /** Reload a stored chat that finished while it was not open here. */
  async function reload(conversationId: string): Promise<void> {
    const key = keyFor(latest.current, conversationId);
    if (!key || latest.current.byKey[key].busy) return;
    const chat = await call<{ turns: StoredTurn[] }>("GET", `/v1/conversations/${encodeURIComponent(conversationId)}`);
    set(update(latest.current, key, (current) => ({ ...fromHistory(conversationId, chat.turns), jobs: current.jobs })));
  }

  return {
    chats,
    transcript: activeTranscript(chats),
    listTick,
    send,
    open,
    reload,
    startNew: () => set(startNew(latest.current)),
    forget: (conversationId: string) => set(forget(latest.current, conversationId)),
    sendToNew: (text: string) => {
      const next = startNew(latest.current);
      set(next);
      send(text, [], false, next.active);
    },
  };
}

// -- the chat list ----------------------------------------------------------------------------

function ChatRow({
  chat,
  active,
  working,
  onOpen,
  onChanged,
}: {
  chat: ChatSummary;
  active: boolean;
  working: boolean;
  onOpen: () => void;
  onChanged: () => void;
}) {
  const [renaming, setRenaming] = useState(false);
  const [title, setTitle] = useState(chat.title);
  const [confirming, setConfirming] = useState(false);
  const path = `/v1/conversations/${encodeURIComponent(chat.id)}`;

  async function rename(): Promise<void> {
    setRenaming(false);
    if (title.trim() && title.trim() !== chat.title) await call("PATCH", path, { title: title.trim() });
    onChanged();
  }

  return (
    <li className={`chat-row${active ? " active" : ""}${working ? " working" : ""}`}>
      {renaming ? (
        <input
          className="chat-rename"
          autoFocus
          value={title}
          aria-label="Chat title"
          onChange={(event) => setTitle(event.target.value)}
          onBlur={() => void rename()}
          onKeyDown={(event) => {
            if (event.key === "Enter") void rename();
            if (event.key === "Escape") {
              setTitle(chat.title);
              setRenaming(false);
            }
          }}
        />
      ) : (
        <button className="chat-open" onClick={onOpen} onDoubleClick={() => setRenaming(true)} title={chat.title}>
          {chat.pinned && (
            <svg className="pin-star" width="13" height="13" viewBox="0 0 14 14" role="img" aria-label="Pinned">
              <path d="M7 1.3l1.7 3.6 3.9.5-2.9 2.7.8 3.9L7 10.1 3.5 12l.8-3.9L1.4 5.4l3.9-.5z" />
            </svg>
          )}
          <span className="chat-title">{chat.title}</span>
          {working && <span className="spinner" role="img" aria-label="Working" />}
          {chat.match && <span className="chat-match">{chat.match}</span>}
        </button>
      )}
      {confirming ? (
        <span className="chat-confirm">
          Delete?
          <button
            className="plain danger"
            onClick={() =>
              void call("DELETE", path)
                .catch(() => undefined)
                .then(onChanged)
            }
          >
            Yes
          </button>
          <button className="plain" onClick={() => setConfirming(false)}>
            No
          </button>
        </span>
      ) : (
        <span className="chat-actions">
          <button
            className={`plain icon${chat.pinned ? " on" : ""}`}
            title={chat.pinned ? "Unpin" : "Pin"}
            aria-label={chat.pinned ? `Unpin ${chat.title}` : `Pin ${chat.title}`}
            onClick={() => void call("PATCH", path, { pinned: !chat.pinned }).then(onChanged)}
          >
            {chat.pinned ? "★" : "☆"}
          </button>
          <button className="plain icon" title="Rename" aria-label={`Rename ${chat.title}`} onClick={() => setRenaming(true)}>
            ✎
          </button>
          <button className="plain icon" title="Delete" aria-label={`Delete ${chat.title}`} onClick={() => setConfirming(true)}>
            ×
          </button>
        </span>
      )}
    </li>
  );
}

function ChatList({
  chats,
  tick,
  onOpen,
  onNew,
  onFinishedElsewhere,
  onTitles,
}: {
  chats: Chats;
  tick: number;
  onOpen: (id: string) => void;
  onNew: () => void;
  onFinishedElsewhere: (id: string) => void;
  onTitles: (titles: Record<string, string>) => void;
}) {
  const [query, setQuery] = useState("");
  const [list, setList] = useState<ChatSummary[]>([]);
  // A failed load is not shown: the list keeps what it had and quietly tries again.
  const [failed, setFailed] = useState(false);
  const [refresh, setRefresh] = useState(0);
  const running = useRef(new Set<string>());

  useEffect(() => {
    let live = true;
    const timer = window.setTimeout(
      () => {
        call<{ conversations: ChatSummary[] }>("GET", `/v1/conversations?q=${encodeURIComponent(query)}`)
          .then((data) => {
            if (!live) return;
            setList(data.conversations);
            setFailed(false);
            if (!query) onTitles(Object.fromEntries(data.conversations.map((chat) => [chat.id, chat.title])));
            // Chats bobd was answering that this window did not start: reload when done.
            const now = new Set(data.conversations.filter((chat) => chat.running).map((chat) => chat.id));
            for (const id of running.current) if (!now.has(id)) onFinishedElsewhere(id);
            running.current = now;
          })
          .catch(() => live && setFailed(true));
      },
      query ? 250 : 0,
    );
    return () => {
      live = false;
      window.clearTimeout(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [query, tick, refresh]);

  // While bobd is answering a chat started elsewhere, look again now and then; after a
  // failed load (bobd restarting, say), try again every few seconds.
  const elsewhere = list.some((chat) => chat.running && !keyFor(chats, chat.id));
  useEffect(() => {
    if (!elsewhere && !failed) return;
    const timer = window.setInterval(() => setRefresh((value) => value + 1), failed ? 5000 : 3000);
    return () => window.clearInterval(timer);
  }, [elsewhere, failed]);

  const working = workingIds(chats, list);
  const activeId = activeTranscript(chats).conversationId;
  const groups = groupChats(list);
  return (
    <aside className="chat-list" aria-label="Chats">
      <div className="chat-list-head">
        <button className="primary new-chat" onClick={onNew}>
          New chat
        </button>
        <input
          type="search"
          placeholder="Search chats"
          aria-label="Search chats"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
        />
      </div>
      <div className="chat-groups">
        {groups.map((group) => (
          <section key={group.label}>
            <h4>{group.label}</h4>
            <ul>
              {group.chats.map((chat) => (
                <ChatRow
                  key={chat.id}
                  chat={chat}
                  active={chat.id === activeId}
                  working={working.has(chat.id)}
                  onOpen={() => onOpen(chat.id)}
                  onChanged={() => setRefresh((value) => value + 1)}
                />
              ))}
            </ul>
          </section>
        ))}
        {list.length === 0 && (
          <p className="muted chat-empty">
            {failed
              ? "Your earlier chats are not loading. bobd is probably out of date: choose Restart bobd in the menu bar."
              : query
                ? "No chat matches."
                : "Your chats appear here."}
          </p>
        )}
      </div>
    </aside>
  );
}

// -- the chat ---------------------------------------------------------------------------------

/**
 * In the panel, Esc hides the window and the header links to the main window.
 * In the main window the chat fills the Chat section, with the list of chats beside it,
 * and stays alive while you look at other sections.
 */
function MicIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <rect x="9" y="3" width="6" height="12" rx="3" />
      <path d="M5 11a7 7 0 0 0 14 0M12 18v3" />
    </svg>
  );
}

/** What the microphone is doing, and the way out when it cannot work. */
function VoiceNote({ voice }: { voice: ReturnType<typeof useVoice> }) {
  const { state } = voice;
  const listening = state.phase === "listening";
  if (voice.available?.available === false && !state.error) return null;
  return (
    <div className="voice-note" role="status">
      {state.error ? (
        <>
          <span className="error">{state.error}</span>
          {(state.errorCode === "denied" || state.errorCode === "restricted") && (
            <button className="plain" onClick={() => void bob.voice.openMicrophoneSettings()}>
              Open System Settings
            </button>
          )}
          <button className="plain" onClick={voice.clearError}>
            Dismiss
          </button>
        </>
      ) : (
        <>
          {state.phase === "starting" && <span className="muted">{state.progress || "Opening the microphone…"}</span>}
          {listening && (
            <>
              <span className="voice-meter" aria-hidden="true">
                <span style={{ transform: `scaleX(${Math.max(0.04, state.level)})` }} />
              </span>
              <span className="muted">
                {voice.conversation ? "Listening. It sends when you pause." : "Listening. Press Enter or the microphone when done."}
              </span>
            </>
          )}
          <span className="spacer" />
          <button
            className={voice.conversation ? "chip-button on" : "chip-button"}
            aria-pressed={voice.conversation}
            title="Send each message when you pause, and listen again after Bob answers"
            onClick={() => voice.setConversation(!voice.conversation)}
          >
            Conversation
          </button>
        </>
      )}
    </div>
  );
}

export function Chat({ variant, active = true }: { variant: "panel" | "app"; active?: boolean }) {
  const state = useAppState();
  const chats = useChats(variant === "app");
  const transcript = chats.transcript;
  const [titles, setTitles] = useState<Record<string, string>>({});
  const firstQuestion = transcript.turns[0]?.question.split("\n")[0] ?? "";
  const title =
    (transcript.conversationId && titles[transcript.conversationId]) ||
    (firstQuestion.length > 48 ? `${firstQuestion.slice(0, 47).trimEnd()}…` : firstQuestion) ||
    "New chat";
  const [draft, setDraft] = useState("");
  const [files, setFiles] = useState<string[]>([]);
  const [dropping, setDropping] = useState(false);
  const scroller = useRef<HTMLDivElement>(null);
  const input = useRef<HTMLTextAreaElement>(null);
  const waitingForId = transcript.busy && !transcript.conversationId;

  // Speaking: the words fill the box as you talk; a pause sends them in conversation mode.
  const latestDraft = useRef(draft);
  latestDraft.current = draft;
  const spoken = useRef(false);
  const dictating = useRef(false);
  const base = useRef("");
  const joined = (words: string): string => [base.current.trimEnd(), words].filter(Boolean).join(" ");
  const voice = useVoice({
    prefs: state?.prefs.voice,
    active,
    idle: !transcript.busy,
    onWords: (words, final) => {
      if (!dictating.current) {
        dictating.current = true;
        base.current = latestDraft.current;
      }
      if (words) spoken.current = true;
      setDraft(joined(words));
      if (final) {
        dictating.current = false;
        input.current?.focus();
      }
    },
    onUtterance: (words) => {
      chats.send(joined(words), files, false, undefined, true);
      setDraft("");
      setFiles([]);
      spoken.current = false;
    },
  });
  const listening = voice.state.phase === "listening" || voice.state.phase === "starting";

  // bob://ask?q=… and notification actions start a new chat.
  const sendToNew = useRef(chats.sendToNew);
  sendToNew.current = chats.sendToNew;
  useEffect(() => bob.onAsk((text) => sendToNew.current(text)), []);
  useEffect(() => {
    scroller.current?.scrollTo({ top: scroller.current.scrollHeight });
  }, [transcript, state?.confirmations.length]);
  useEffect(() => {
    if (!active) return;
    const focus = (): void => input.current?.focus();
    window.addEventListener("focus", focus);
    focus();
    return () => window.removeEventListener("focus", focus);
  }, [active, chats.chats.active]);

  function submit(): void {
    if (voice.state.phase === "listening") {
      // Enter while speaking: settle the last words and send them.
      void voice.finish().then((words) => {
        const text = joined(words);
        if (!text.trim() || waitingForId) return;
        chats.send(text, files, false, undefined, true);
        setDraft("");
        setFiles([]);
        spoken.current = false;
      });
      return;
    }
    if (!draft.trim() || waitingForId) return;
    chats.send(draft, files, false, undefined, spoken.current);
    spoken.current = false;
    setDraft("");
    setFiles([]);
  }

  function onKey(event: KeyboardEvent<HTMLTextAreaElement>): void {
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      submit();
    } else if (event.key === "Escape" && variant === "panel") {
      void bob.hideWindow();
    }
  }

  function onDrop(event: DragEvent): void {
    event.preventDefault();
    setDropping(false);
    const paths = Array.from(event.dataTransfer.files)
      .map((file) => bob.pathForFile(file))
      .filter(Boolean);
    setFiles((current) => [...new Set([...current, ...paths])]);
  }

  const confirmations = useMemo(
    () =>
      (state?.confirmations ?? []).filter(
        (confirmation) => variant === "panel" || !confirmation.session || confirmation.session === transcript.conversationId,
      ),
    [state?.confirmations, transcript.conversationId, variant],
  );

  const view = (
    <section
      className={dropping ? `chat chat-${variant} dropping` : `chat chat-${variant}`}
      onDragOver={(event) => {
        event.preventDefault();
        setDropping(true);
      }}
      onDragLeave={() => setDropping(false)}
      onDrop={onDrop}
    >
      <header className="titlebar drag">
        {variant === "panel" ? (
          <span className="brand">
            <Mark />
            Bob
          </span>
        ) : (
          <h1 className="chat-heading">{title}</h1>
        )}
        {variant === "app" && transcript.busy && (
          <span className="working-chip">
            <span className="spinner" aria-hidden="true" />
            Working
          </span>
        )}
        <span className="spacer" />
        {variant === "panel" && transcript.turns.length > 0 && (
          <button className="plain nodrag" onClick={chats.startNew}>
            New chat
          </button>
        )}
        {variant === "panel" && (
          <>
            <button className="plain nodrag" onClick={() => void bob.openView("app", "today")}>
              Open Bob
            </button>
            <button className="plain nodrag" onClick={() => void bob.openView("settings")}>
              Settings
            </button>
          </>
        )}
      </header>
      {state && <ConnectionNote state={state.connection} detail={state.detail} />}
      <div className="transcript" ref={scroller}>
        {transcript.turns.length === 0 && (
          <p className="muted empty">
            Ask anything, or give Bob a task. Drop files here to include them.
            {variant === "panel" ? " Esc hides this panel." : " Other chats keep working while you are here."}
          </p>
        )}
        {transcript.turns.map((turn) => (
          <TurnView key={turn.id} turn={turn} onAskAgain={() => chats.send(turn.question, [], true)} />
        ))}
        {jobsFor(transcript.jobs, transcript.conversationId).map((job) => (
          <JobView key={job.job} job={job} />
        ))}
        {confirmations.map((confirmation) => (
          <ConfirmCard key={confirmation.id} confirmation={confirmation} />
        ))}
      </div>
      {files.length > 0 && (
        <div className="files">
          {files.map((file) => (
            <span key={file} className="chip">
              {file.split("/").pop()}
              <button className="plain" aria-label={`Remove ${file}`} onClick={() => setFiles(files.filter((f) => f !== file))}>
                ×
              </button>
            </span>
          ))}
        </div>
      )}
      <footer className="composer">
        <VoiceNote voice={voice} />
        <textarea
          ref={input}
          rows={2}
          value={draft}
          readOnly={listening}
          placeholder={
            listening ? "Listening…" : transcript.busy ? "Ask a follow-up; it waits for this answer…" : "Ask Bob…"
          }
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={onKey}
        />
        <button
          className={listening ? "mic on" : "mic"}
          onClick={voice.toggle}
          disabled={voice.available?.available === false}
          aria-pressed={listening}
          aria-label={listening ? "Stop listening" : "Speak to Bob"}
          title={voice.available?.available === false ? voice.available.detail : listening ? "Stop listening" : "Speak to Bob"}
        >
          <MicIcon />
        </button>
        {transcript.busy && (
          <button onClick={() => void bob.cancel(transcript.conversationId)} disabled={!transcript.conversationId}>
            Stop
          </button>
        )}
        <button className="primary" onClick={submit} disabled={!draft.trim() || waitingForId}>
          Send
        </button>
      </footer>
    </section>
  );

  if (variant === "panel") return view;
  return (
    <div className="chat-shell">
      <ChatList
        chats={chats.chats}
        tick={chats.listTick}
        onOpen={(id) => void chats.open(id)}
        onNew={chats.startNew}
        onFinishedElsewhere={(id) => void chats.reload(id)}
        onTitles={setTitles}
      />
      {view}
    </div>
  );
}
