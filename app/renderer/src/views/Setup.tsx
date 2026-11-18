import { useEffect, useState } from "react";
import {
  ROLES,
  ROLE_LABELS,
  formatBytes,
  isLoopbackUrl,
  optionsFor,
  validTag,
  type ModelRole,
  type SetupChoices,
  type SetupSnapshot,
  type StepStatus,
} from "../../../shared/setup";
import { bob } from "../bob";
import { Mark } from "../components/Cards";

const OTHER = "__other__";

function useSnapshot(): SetupSnapshot | null {
  const [snapshot, setSnapshot] = useState<SetupSnapshot | null>(null);
  useEffect(() => {
    let live = true;
    void bob.setup.snapshot().then((first) => live && setSnapshot(first));
    const stop = bob.setup.onChange(setSnapshot);
    return () => {
      live = false;
      stop();
    };
  }, []);
  return snapshot;
}

/** One model per role: a drop-down of every model that can do the job, and a box for any other tag. */
function ModelPicker({ role, snapshot, value, onChange }: { role: ModelRole; snapshot: SetupSnapshot; value: string; onChange: (tag: string) => void }) {
  const options = optionsFor(role, snapshot.catalogue);
  const listed = options.includes(value);
  const [typing, setTyping] = useState(!listed);
  const invalid = typing && value !== "" && !validTag(value);
  const id = `model-${role}`;
  return (
    <div className="setup-field">
      <label htmlFor={id}>{ROLE_LABELS[role]}</label>
      <select
        id={id}
        value={typing ? OTHER : value}
        onChange={(event) => {
          if (event.target.value === OTHER) setTyping(true);
          else {
            setTyping(false);
            onChange(event.target.value);
          }
        }}
      >
        {options.map((tag) => (
          <option key={tag} value={tag}>
            {tag}
          </option>
        ))}
        <option value={OTHER}>Another model…</option>
      </select>
      {typing && (
        <input
          aria-label={`${ROLE_LABELS[role]}: model tag`}
          value={value}
          spellCheck={false}
          placeholder="name:tag"
          aria-invalid={invalid}
          onChange={(event) => onChange(event.target.value.trim())}
        />
      )}
      {invalid && <small className="error">That is not a plain Ollama model name.</small>}
    </div>
  );
}

function Welcome({ snapshot, start }: { snapshot: SetupSnapshot; start: (choices: SetupChoices) => void }) {
  const [choices, setChoices] = useState<SetupChoices>(snapshot.choices);
  const [own, setOwn] = useState(snapshot.choices.externalOllama !== null);
  const [address, setAddress] = useState(snapshot.choices.externalOllama ?? "http://127.0.0.1:11434");
  const addressBad = own && !isLoopbackUrl(address);
  const tagsBad = ROLES.some((role) => !validTag(choices.models[role]));
  const ready = !addressBad && !tagsBad;
  const { facts } = snapshot;
  return (
    <>
      <p className="setup-lead">
        Bob runs on this Mac. It needs a small program to run language models, and the models themselves. Setup puts both in
        Bob&rsquo;s own folder, once. Nothing needs an administrator password.
      </p>
      <ul className="setup-facts">
        <li>
          {facts.freeBytes > 0 ? `${formatBytes(facts.freeBytes)} free on this Mac.` : "Free space is not known."} Each model is
          several gigabytes, so pick smaller ones on a small disk.
        </li>
        {facts.found.map((line) => (
          <li key={line}>{line}</li>
        ))}
      </ul>
      <h3>Models</h3>
      <p className="muted">Choose one for each job. The first in each list is the one Bob was tested with.</p>
      {ROLES.map((role) => (
        <ModelPicker
          key={role}
          role={role}
          snapshot={snapshot}
          value={choices.models[role]}
          onChange={(tag) => setChoices({ ...choices, models: { ...choices.models, [role]: tag } })}
        />
      ))}
      {snapshot.catalogue.models.length === 0 && <p className="muted">The model list did not load, so type a model name for each job.</p>}
      <h3>Options</h3>
      <label className="setup-check">
        <input type="checkbox" checked={choices.startAtLogin} onChange={(event) => setChoices({ ...choices, startAtLogin: event.target.checked })} />
        Open Bob when I log in
      </label>
      <label className="setup-check">
        <input type="checkbox" checked={own} onChange={(event) => setOwn(event.target.checked)} />
        I already run Ollama myself and want Bob to use it
      </label>
      {own && (
        <div className="setup-field">
          <label htmlFor="own-ollama">Ollama address</label>
          <input id="own-ollama" value={address} spellCheck={false} aria-invalid={addressBad} onChange={(event) => setAddress(event.target.value.trim())} />
          <small className={addressBad ? "error" : "muted"}>
            Only addresses on this Mac are accepted. Bob installs no Ollama then, but still downloads the models you chose into it.
          </small>
        </div>
      )}
      <div className="setup-actions">
        <button
          className="primary"
          disabled={!ready}
          onClick={() => start({ ...choices, externalOllama: own ? address.replace(/\/+$/, "") : null })}
        >
          Set up Bob
        </button>
      </div>
    </>
  );
}

function StepMark({ step }: { step: StepStatus }) {
  const glyph = { waiting: "", running: "", done: "✓", skipped: "–", failed: "✕" }[step.state];
  return (
    <span className={`setup-mark ${step.state}`} aria-hidden="true">
      {glyph}
    </span>
  );
}

function Progress({ snapshot, onTryAgain }: { snapshot: SetupSnapshot; onTryAgain: () => void }) {
  const [copied, setCopied] = useState(false);
  const failed = snapshot.phase === "failed";
  return (
    <>
      <ol className="setup-steps" aria-live="polite">
        {snapshot.steps.map((step) => (
          <li key={step.id} className={step.state}>
            <StepMark step={step} />
            <div className="grow">
              <strong>{step.label}</strong>
              {step.detail && <div className={step.state === "failed" ? "error" : "muted"}>{step.detail}</div>}
              {step.progress && step.progress.total > 0 && (
                <progress max={step.progress.total} value={Math.min(step.progress.done, step.progress.total)} aria-label={`${step.label} progress`} />
              )}
            </div>
          </li>
        ))}
      </ol>
      <div className="setup-actions">
        {failed ? (
          <>
            <button
              className="plain"
              onClick={() => {
                void navigator.clipboard.writeText([snapshot.error, ...snapshot.log].join("\n")).then(() => setCopied(true));
              }}
            >
              {copied ? "Copied" : "Copy details"}
            </button>
            <button className="primary" onClick={onTryAgain}>
              Try again
            </button>
          </>
        ) : (
          <button onClick={() => void bob.setup.cancel()}>Stop</button>
        )}
      </div>
      {failed && <p className="muted">Try again carries on from where it stopped; finished steps and downloaded models are kept.</p>}
    </>
  );
}

export function Setup() {
  const snapshot = useSnapshot();
  const [choices, setChoices] = useState<SetupChoices | null>(null);
  if (!snapshot) return null;
  const start = (next: SetupChoices): void => {
    setChoices(next);
    void bob.setup.start(next);
  };
  return (
    <main className="setup">
      <header className="drag setup-head">
        <Mark size={28} />
        <h1>{snapshot.phase === "done" ? "Bob is ready" : "Set up Bob"}</h1>
      </header>
      <div className="setup-body">
        {snapshot.phase === "welcome" && <Welcome snapshot={snapshot} start={start} />}
        {(snapshot.phase === "running" || snapshot.phase === "failed") && (
          <Progress snapshot={snapshot} onTryAgain={() => start(choices ?? snapshot.choices)} />
        )}
        {snapshot.phase === "done" && (
          <>
            <p className="setup-lead">Everything is in place and Bob is running. It lives in the menu bar; the Bob window is one click away.</p>
            <div className="setup-actions">
              <button
                className="primary"
                onClick={() => {
                  void bob.openView("app").then(() => window.close());
                }}
              >
                Open Bob
              </button>
            </div>
          </>
        )}
      </div>
    </main>
  );
}
