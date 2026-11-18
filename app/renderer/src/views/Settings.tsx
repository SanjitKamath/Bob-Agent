import { useEffect, useState } from "react";
import { moveCard, visibleCards } from "../../../shared/cards";
import type { AppState, Theme } from "../../../shared/types";
import { bob, call, useAppState, useLoad } from "../bob";
import { ConnectionNote, Mark, Shimmer } from "../components/Cards";
import { Dropdown } from "../components/Dropdown";
import { MODEL_CHOICES, type VoiceAvailability, type VoicePrefs, PAUSE_CHOICES, whisperModel } from "../../../shared/voice";
import { openMicrophone } from "../lib/voice";
import { addRoot, joinRoots, removeRoot, splitRoots } from "../lib/roots";
import { changedSettings, formFields, type SettingsSchema } from "../lib/schema";

type Tab = "Plugins" | "Knowledge" | "Models" | "Routines" | "General" | "Privacy";
const TABS: Tab[] = ["Plugins", "Knowledge", "Models", "Routines", "General", "Privacy"];

/** Run an action and show its error, if any, next to the control that caused it. */
function useAction(): { error: string; busy: boolean; run: (action: () => Promise<unknown>) => Promise<boolean> } {
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  async function run(action: () => Promise<unknown>): Promise<boolean> {
    setBusy(true);
    setError("");
    try {
      await action();
      return true;
    } catch (problem) {
      setError((problem as Error).message);
      return false;
    } finally {
      setBusy(false);
    }
  }
  return { error, busy, run };
}

// -- Plugins ----------------------------------------------------------------------------

interface PluginTool {
  name: string;
  tier: string;
  asks_first: boolean;
  admin: boolean;
  enabled: boolean;
}

interface PluginSummary {
  id: string;
  name: string;
  version: string;
  description: string;
  status: string;
  error: string;
  enabled: boolean;
  tools: PluginTool[];
  network: string | string[];
  write_folders: (string | null)[];
  settings: Record<string, unknown>;
  settings_schema: SettingsSchema;
}

function PluginSettingsForm({ plugin, onSaved }: { plugin: PluginSummary; onSaved: () => void }) {
  const fields = formFields(plugin.settings_schema, plugin.settings);
  const [edits, setEdits] = useState<Record<string, string | boolean>>({});
  const action = useAction();
  const [problems, setProblems] = useState<string[]>([]);
  if (fields.length === 0) return null;

  async function save(): Promise<void> {
    const { values, errors } = changedSettings(fields, edits);
    setProblems(errors);
    if (errors.length || Object.keys(values).length === 0) return;
    const saved = await action.run(() => call("PUT", `/v1/plugins/${plugin.id}/settings`, { settings: values }));
    if (saved) {
      setEdits({});
      onSaved();
    }
  }

  return (
    <form
      className="form"
      onSubmit={(event) => {
        event.preventDefault();
        void save();
      }}
    >
      {fields.map((field) => {
        const value = field.key in edits ? edits[field.key] : field.value;
        const set = (next: string | boolean): void => setEdits({ ...edits, [field.key]: next });
        return (
          <label key={field.key}>
            <span>{field.label}</span>
            {field.kind === "checkbox" ? (
              <input type="checkbox" checked={value === true} onChange={(event) => set(event.target.checked)} />
            ) : field.kind === "lines" ? (
              <textarea
                rows={Math.min(8, Math.max(3, String(value).split("\n").length + 1))}
                value={String(value)}
                spellCheck={false}
                placeholder="One per line"
                onChange={(event) => set(event.target.value)}
              />
            ) : field.kind === "select" ? (
              <select value={String(value)} onChange={(event) => set(event.target.value)}>
                {field.options.map((option) => (
                  <option key={option}>{option}</option>
                ))}
              </select>
            ) : (
              <input
                type={field.kind === "number" ? "number" : "text"}
                value={String(value)}
                spellCheck={false}
                onChange={(event) => set(event.target.value)}
              />
            )}
            {field.help && <small>{field.help}</small>}
          </label>
        );
      })}
      {[...problems, action.error].filter(Boolean).map((problem) => (
        <div key={problem} className="error">
          {problem}
        </div>
      ))}
      <div className="row end">
        <button className="primary" disabled={action.busy || Object.keys(edits).length === 0}>
          {action.busy ? "Saving…" : "Save"}
        </button>
      </div>
    </form>
  );
}

function PluginRow({ plugin, reload }: { plugin: PluginSummary; reload: () => void }) {
  const [open, setOpen] = useState(false);
  const [consent, setConsent] = useState<PluginSummary | null>(null);
  const action = useAction();

  async function toggle(on: boolean, acknowledged = false): Promise<void> {
    const result = await bob.api({
      method: "POST",
      path: `/v1/plugins/${plugin.id}/${on ? "enable" : "disable"}`,
      body: on ? { acknowledged } : undefined,
    });
    if (!result.ok && result.status === 409) {
      // First time: show what it can do and ask.
      setConsent((result.data as { plugin: PluginSummary }).plugin);
      return;
    }
    setConsent(null);
    await action.run(async () => {
      if (!result.ok) throw new Error(result.error);
    });
    reload();
  }

  const state = plugin.enabled ? plugin.status : "off";
  return (
    <li className="item">
      <div className="row">
        <input
          type="checkbox"
          aria-label={`Enable ${plugin.name}`}
          checked={plugin.enabled}
          onChange={(event) => void toggle(event.target.checked)}
        />
        <div className="grow">
          <strong>{plugin.name}</strong> <span className="muted">{plugin.version}</span>
          <div className="muted">{plugin.description}</div>
        </div>
        <span className={`badge ${state}`}>{state}</span>
        <button className="plain" onClick={() => setOpen(!open)}>
          {open ? "Hide" : "Details"}
        </button>
      </div>
      {(plugin.error || action.error) && <div className="error">{plugin.error || action.error}</div>}
      {consent && (
        <div className="confirm">
          <header>
            Switch on <strong>{consent.name}</strong>? It can:
          </header>
          <ul className="plainlist">
            {consent.tools.map((tool) => (
              <li key={tool.name}>
                <code>{tool.name}</code> <span className="muted">{tool.tier}</span>
                {tool.asks_first ? " (always asks you first)" : ""}
              </li>
            ))}
          </ul>
          <p className="muted">
            Network: {Array.isArray(consent.network) ? consent.network.join(", ") : consent.network}. Writes to:{" "}
            {consent.write_folders.filter(Boolean).join(", ") || "its own data only"}.
          </p>
          <div className="row end">
            <button onClick={() => setConsent(null)}>Cancel</button>
            <button className="primary" onClick={() => void toggle(true, true)}>
              Switch on
            </button>
          </div>
        </div>
      )}
      {open && (
        <div className="details">
          <h4>Tools</h4>
          <ul className="plainlist">
            {plugin.tools.map((tool) => (
              <li key={tool.name} className="row">
                <input
                  type="checkbox"
                  aria-label={`Allow ${tool.name}`}
                  checked={tool.enabled}
                  onChange={(event) =>
                    void action
                      .run(() => call("PUT", `/v1/plugins/${plugin.id}/tools/${tool.name}`, { enabled: event.target.checked }))
                      .then(reload)
                  }
                />
                <code>{tool.name}</code>
                <span className="muted">
                  {tool.tier}
                  {tool.asks_first ? ", asks first" : ""}
                  {tool.admin ? ", only you" : ""}
                </span>
              </li>
            ))}
          </ul>
          <h4>Settings</h4>
          <PluginSettingsForm plugin={plugin} onSaved={reload} />
        </div>
      )}
    </li>
  );
}

interface WheelInspection {
  filename: string;
  dist: string;
  version: string;
  sha256: string;
  plugin_id: string;
  title: string;
  description: string;
  network: string | string[];
  write: string[];
  tools: { name: string; tier?: string }[];
  unmet: string[];
  warnings: string[];
}

interface WheelInstalled {
  dist: string;
  plugin: string;
  version: string;
}

/** Add a plugin from a .whl file: look first, show what it asks for, install only when told. */
function AddPlugin({ reload }: { reload: () => void }) {
  const [found, setFound] = useState<WheelInspection | null>(null);
  const [message, setMessage] = useState("");
  const added = useLoad<{ plugins: WheelInstalled[] }>("/v1/plugins/wheel/installed");
  const action = useAction();

  async function choose(): Promise<void> {
    setMessage("");
    const picked = await bob.pickWheel();
    if (!picked) return;
    await action.run(async () => {
      setFound(await call<WheelInspection>("POST", "/v1/plugins/wheel/inspect", picked));
    });
  }

  async function install(): Promise<void> {
    if (!found) return;
    const done = await action.run(() => call("POST", "/v1/plugins/wheel/install", { sha256: found.sha256 }));
    if (done) {
      setMessage(`${found.title || found.dist} is added. It is switched off until you turn it on.`);
      setFound(null);
      reload();
      added.reload();
    }
  }

  const network = Array.isArray(found?.network) ? found.network.join(", ") : (found?.network ?? "none");
  return (
    <div className="addplugin">
      <h3>Add a plugin</h3>
      <p className="muted">
        A plugin is a Python wheel file (.whl). Bob reads it first without running anything, shows what it asks for, and
        only adds it when you say so. It starts switched off.
      </p>
      {action.error && <div className="error">{action.error}</div>}
      {message && <div className="banner">{message}</div>}
      {!found && (
        <div className="row">
          <button disabled={action.busy} onClick={() => void choose()}>
            Choose a plugin file…
          </button>
        </div>
      )}
      {found && (
        <div className="item">
          <strong>{found.title || found.dist}</strong> <span className="muted">{found.version}</span>
          {found.description && <p>{found.description}</p>}
          <ul className="plainlist">
            <li>
              <span className="muted">Network:</span> {network || "none"}
            </li>
            <li>
              <span className="muted">Writes to:</span> {found.write.length ? found.write.join(", ") : "nowhere"}
            </li>
            <li>
              <span className="muted">Tools:</span>{" "}
              {found.tools.length ? found.tools.map((tool) => (tool.tier ? `${tool.name} (${tool.tier})` : tool.name)).join(", ") : "none"}
            </li>
          </ul>
          {found.warnings.map((warning) => (
            <div key={warning} className="banner">
              {warning}
            </div>
          ))}
          {found.unmet.length > 0 && (
            <div className="error">
              It needs things Bob does not have, so it cannot be added: {found.unmet.join(", ")}.
            </div>
          )}
          <div className="row end">
            <button className="plain" onClick={() => setFound(null)}>
              Cancel
            </button>
            <button className="primary" disabled={action.busy || found.unmet.length > 0} onClick={() => void install()}>
              Add this plugin
            </button>
          </div>
        </div>
      )}
      {(added.data?.plugins.length ?? 0) > 0 && (
        <>
          <h4>Added by you</h4>
          <ul className="items">
            {added.data?.plugins.map((plugin) => (
              <li key={plugin.dist} className="item row">
                <span className="grow">
                  {plugin.plugin || plugin.dist} <span className="muted">{plugin.version}</span>
                </span>
                <button
                  className="plain"
                  disabled={action.busy}
                  onClick={() =>
                    void action
                      .run(() => call("DELETE", `/v1/plugins/wheel/${plugin.dist}`))
                      .then((ok) => {
                        if (ok) {
                          setMessage("Removed. Restart Bob to finish clearing it out.");
                          added.reload();
                          reload();
                        }
                      })
                  }
                >
                  Remove
                </button>
              </li>
            ))}
          </ul>
        </>
      )}
    </div>
  );
}

export function Plugins() {
  const { data, error, reload } = useLoad<{ plugins: PluginSummary[]; rejected: Record<string, string> }>("/v1/plugins");
  return (
    <section>
      <h2>Plugins</h2>
      {error && <div className="error">{error}</div>}
      {!data && !error && <Shimmer />}
      <ul className="items">
        {data?.plugins.map((plugin) => (
          <PluginRow key={plugin.id} plugin={plugin} reload={reload} />
        ))}
      </ul>
      {Object.entries(data?.rejected ?? {}).map(([where, why]) => (
        <div key={where} className="error">
          Rejected: {where}: {why}
        </div>
      ))}
      <AddPlugin reload={reload} />
    </section>
  );
}

// -- Knowledge ------------------------------------------------------------------------------

interface SourceStatus {
  name: string;
  kind: string;
  location: string;
  indexed_version: string | null;
  indexed_at: string | null;
  last_run: { outcome: string; detail: string; trigger: string } | null;
}

interface CollectionStatus {
  chunks: number;
  vectors: number;
  profile_error: string;
  sources: SourceStatus[];
}

interface KnowledgeData {
  collections: { id: string; profile?: string }[];
  status: Record<string, CollectionStatus>;
}

export function Knowledge() {
  const { data, error, reload } = useLoad<KnowledgeData>("/v1/knowledge/collections");
  const github = useLoad<{ auth: string; token_stored: boolean }>("/v1/knowledge/github");
  const action = useAction();
  const [draft, setDraft] = useState({ id: "", kind: "folder", location: "", branch: "main", include: "**/*" });
  const [token, setToken] = useState("");

  async function add(): Promise<void> {
    const include = draft.include.split(",").map((part) => part.trim()).filter(Boolean);
    const source =
      draft.kind === "github"
        ? { github: { repo: draft.location, branch: draft.branch, include } }
        : draft.kind === "folder"
          ? { folder: { path: draft.location, include } }
          : { files: {} };
    const saved = await action.run(() => call("PUT", "/v1/knowledge/collections", { id: draft.id, sources: [source] }));
    if (saved) {
      setDraft({ ...draft, id: "", location: "" });
      reload();
    }
  }

  return (
    <section>
      <h2>Knowledge</h2>
      {(error || action.error) && <div className="error">{error || action.error}</div>}
      {!data && !error && <Shimmer />}
      <ul className="items">
        {data?.collections.map((collection) => {
          const status = data.status[collection.id];
          return (
            <li key={collection.id} className="item">
              <div className="row">
                <div className="grow">
                  <strong>{collection.id}</strong>{" "}
                  <span className="muted">
                    {status ? `${status.chunks} chunks` : "not active (its parser plugin is off, or Knowledge is off)"}
                  </span>
                </div>
                <button
                  disabled={action.busy || !status}
                  onClick={() => void action.run(() => call("POST", "/v1/knowledge/reindex", { collection: collection.id })).then(reload)}
                >
                  Reindex
                </button>
                <button
                  className="plain"
                  onClick={() => void action.run(() => call("DELETE", `/v1/knowledge/collections/${collection.id}`)).then(reload)}
                >
                  Remove
                </button>
              </div>
              {status?.profile_error && <div className="error">{status.profile_error}</div>}
              <table>
                <tbody>
                  {status?.sources.map((source) => (
                    <tr key={source.name}>
                      <td>{source.name}</td>
                      <td className="muted">{source.kind}</td>
                      <td>
                        <code>{source.indexed_version?.slice(0, 12) ?? "never indexed"}</code>
                      </td>
                      <td className="muted">{source.indexed_at?.replace("T", " ").slice(0, 16) ?? ""}</td>
                      <td className={source.last_run && !["ok", "unchanged"].includes(source.last_run.outcome) ? "error" : "muted"}>
                        {source.last_run ? `${source.last_run.outcome} ${source.last_run.detail}`.trim() : ""}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {status?.sources.some((source) => source.kind === "files") && (
                <label className="row">
                  <span className="muted">Add a file:</span>
                  <input
                    type="file"
                    onChange={(event) => {
                      const file = event.target.files?.[0];
                      if (!file) return;
                      const path = bob.pathForFile(file);
                      event.target.value = "";
                      void action.run(() => call("POST", "/v1/knowledge/files", { collection: collection.id, path })).then(reload);
                    }}
                  />
                </label>
              )}
            </li>
          );
        })}
      </ul>

      <h3>New collection</h3>
      <form
        className="form"
        onSubmit={(event) => {
          event.preventDefault();
          void add();
        }}
      >
        <label>
          <span>Name</span>
          <input value={draft.id} placeholder="contracts" onChange={(event) => setDraft({ ...draft, id: event.target.value })} />
        </label>
        <label>
          <span>Source</span>
          <select value={draft.kind} onChange={(event) => setDraft({ ...draft, kind: event.target.value })}>
            <option value="folder">A folder (read in place)</option>
            <option value="github">A GitHub repo</option>
            <option value="files">Files I drop in</option>
          </select>
        </label>
        {draft.kind !== "files" && (
          <>
            <label>
              <span>{draft.kind === "github" ? "Repo URL (https)" : "Folder"}</span>
              <input
                value={draft.location}
                spellCheck={false}
                placeholder={draft.kind === "github" ? "https://github.com/owner/repo.git" : "~/Documents/Contracts"}
                onChange={(event) => setDraft({ ...draft, location: event.target.value })}
              />
              {draft.kind === "folder" && (
                <button
                  type="button"
                  onClick={() =>
                    void bob.pickFolder("Choose a folder to index").then((folder) => {
                      if (folder) setDraft({ ...draft, location: folder });
                    })
                  }
                >
                  Choose…
                </button>
              )}
            </label>
            {draft.kind === "github" && (
              <label>
                <span>Branch</span>
                <input value={draft.branch} onChange={(event) => setDraft({ ...draft, branch: event.target.value })} />
              </label>
            )}
            <label>
              <span>Include</span>
              <input value={draft.include} spellCheck={false} onChange={(event) => setDraft({ ...draft, include: event.target.value })} />
              <small>File patterns, separated by commas.</small>
            </label>
          </>
        )}
        <div className="row end">
          <button className="primary" disabled={action.busy || !draft.id}>
            Add collection
          </button>
        </div>
      </form>

      <h3>GitHub access</h3>
      <form
        className="form"
        onSubmit={(event) => {
          event.preventDefault();
          void action
            .run(() => call("PUT", "/v1/knowledge/github", { auth: "token", token }))
            .then(() => {
              setToken("");
              github.reload();
            });
        }}
      >
        <p className="muted">
          {github.data?.token_stored ? "A token is stored in the Keychain." : "No token stored: only public repos can be fetched."}{" "}
          Use a fine-grained token with only “Contents: Read-only”.
        </p>
        <label>
          <span>Read-only token</span>
          <input type="password" value={token} autoComplete="off" onChange={(event) => setToken(event.target.value)} />
        </label>
        <div className="row end">
          <button className="primary" disabled={!token || action.busy}>
            Store in Keychain
          </button>
        </div>
      </form>
    </section>
  );
}

// -- Models ---------------------------------------------------------------------------------

interface ModelsData {
  ok: boolean;
  error?: string;
  roles: Record<string, { model: string; num_ctx: number; installed: boolean; parallel?: number }>;
  installed?: string[];
}

export function Models() {
  const { data, error, reload } = useLoad<ModelsData>("/v1/models");
  const action = useAction();
  return (
    <section>
      <h2>Models</h2>
      <p className="muted">Every model runs in Ollama on this Mac. Choose from what is installed.</p>
      {(error || data?.error || action.error) && <div className="error">{error || data?.error || action.error}</div>}
      {!data && !error && <Shimmer />}
      <div className="form">
        {Object.entries(data?.roles ?? {}).map(([role, info]) => (
          <label key={role}>
            <span>{role}</span>
            <select
              value={info.model}
              disabled={action.busy}
              onChange={(event) => void action.run(() => call("PUT", `/v1/models/${role}`, { model: event.target.value })).then(reload)}
            >
              {!info.installed && <option value={info.model}>{info.model} (not installed)</option>}
              {(data?.installed ?? []).filter((name) => !name.startsWith("bob-")).map((name) => (
                <option key={name}>{name}</option>
              ))}
            </select>
            <small>context {info.num_ctx}</small>
          </label>
        ))}
      </div>
      <h3>At once</h3>
      <p className="muted">
        How many requests each model runs at the same time. When a model is busy, your chats go ahead of background work like form
        builds, triage and routines. Roles on the same model share its limit (the larger one). Form builds draft one section fewer
        than the coder&rsquo;s limit, so a chat always has a slot; they pick up a change the next time bobd starts.
      </p>
      <div className="form">
        {Object.entries(data?.roles ?? {}).map(([role, info]) => (
          <label key={role}>
            <span>{role}</span>
            <select
              aria-label={`Requests at once for ${role}`}
              value={info.parallel ?? 1}
              disabled={action.busy}
              onChange={(event) =>
                void action.run(() => call("PUT", "/v1/models/limits", { limits: { [role]: Number(event.target.value) } })).then(reload)
              }
            >
              {[1, 2, 3, 4, 5, 6, 7, 8].map((count) => (
                <option key={count} value={count}>
                  {count}
                </option>
              ))}
            </select>
            <small>{info.model}</small>
          </label>
        ))}
      </div>
    </section>
  );
}

// -- Routines -------------------------------------------------------------------------------

interface Routine {
  id: string;
  title: string;
  owner: string;
  enabled: boolean;
  cron: string | null;
  every_minutes: number | null;
  between: [string, string] | null;
  on_event: string | null;
  next_run: string | null;
}

function RoutineRow({ routine, reload }: { routine: Routine; reload: () => void }) {
  const action = useAction();
  const [when, setWhen] = useState(routine.cron ?? String(routine.every_minutes ?? ""));
  const [result, setResult] = useState("");
  const isCron = routine.cron !== null;
  const editable = routine.on_event === null;

  async function saveTime(): Promise<void> {
    const body = isCron ? { cron: when.trim() } : { every_minutes: Number(when) };
    await action.run(() => call("PUT", `/v1/routines/${routine.id}`, body));
    reload();
  }

  return (
    <li className="item">
      <div className="row">
        <input
          type="checkbox"
          aria-label={`Enable ${routine.title}`}
          checked={routine.enabled}
          onChange={(event) => void action.run(() => call("PUT", `/v1/routines/${routine.id}`, { enabled: event.target.checked })).then(reload)}
        />
        <div className="grow">
          <strong>{routine.title}</strong> <span className="muted">{routine.owner}</span>
          <div className="muted">
            {routine.on_event ? `on ${routine.on_event}` : isCron ? "cron" : "every (minutes)"}
            {routine.between ? `, ${routine.between[0]}–${routine.between[1]}` : ""}
            {routine.next_run ? ` · next ${routine.next_run.replace("T", " ").slice(0, 16)}` : ""}
          </div>
        </div>
        {editable && (
          <>
            <input className="short" value={when} spellCheck={false} onChange={(event) => setWhen(event.target.value)} />
            <button disabled={action.busy} onClick={() => void saveTime()}>
              Set
            </button>
          </>
        )}
        <button
          disabled={action.busy}
          onClick={() =>
            void action.run(async () => {
              const run = await call<{ outcome: string; detail: string }>("POST", `/v1/routines/${routine.id}/run`);
              setResult(`${run.outcome}${run.detail ? `: ${run.detail}` : ""}`);
            })
          }
        >
          {action.busy ? "Running…" : "Run now"}
        </button>
      </div>
      {(action.error || result) && <div className={action.error ? "error" : "muted"}>{action.error || result}</div>}
    </li>
  );
}

export function Routines() {
  const { data, error, reload } = useLoad<Routine[]>("/v1/routines");
  return (
    <section>
      <h2>Routines</h2>
      {error && <div className="error">{error}</div>}
      {!data && !error && <Shimmer />}
      <ul className="items">
        {data?.map((routine) => (
          <RoutineRow key={routine.id} routine={routine} reload={reload} />
        ))}
      </ul>
    </section>
  );
}

const THEMES: { id: Theme; label: string; note: string }[] = [
  { id: "system", label: "System", note: "Follows macOS" },
  { id: "light", label: "Light", note: "White, black text" },
  { id: "dark", label: "Dark", note: "Black, white text" },
];

// -- General: cards and app ---------------------------------------------------------------------

export function GeneralTab({ state }: { state: AppState }) {
  const order = state.prefs.cards;
  const cards = state.snapshot?.cards ?? [];
  const ordered = visibleCards(cards, { order: order.order, hidden: [] });
  const [server, setServer] = useState(state.prefs.serverUrl);
  const [home, setHome] = useState(state.prefs.bobHome);
  return (
    <section>
      <h2>General</h2>
      <h3>Appearance</h3>
      <div className="themes" role="radiogroup" aria-label="Theme">
        {THEMES.map((theme) => (
          <button
            key={theme.id}
            role="radio"
            aria-checked={state.prefs.theme === theme.id}
            className={state.prefs.theme === theme.id ? "theme active" : "theme"}
            onClick={() => void bob.setPrefs({ theme: theme.id })}
          >
            <span className="theme-preview" aria-hidden="true">
              {theme.id !== "dark" && (
                <span className="light">
                  <i className="blue" />
                  <i />
                  <i className="gold" />
                </span>
              )}
              {theme.id !== "light" && (
                <span className="dark">
                  <i className="blue" />
                  <i />
                  <i className="gold" />
                </span>
              )}
            </span>
            <strong>{theme.label}</strong>
            <small>{theme.note}</small>
          </button>
        ))}
      </div>
      <p className="muted">
        The theme applies to every Bob window and the menu bar dropdown at once. Translucency follows macOS &ldquo;Reduce
        transparency&rdquo;, and animations stop when &ldquo;Reduce motion&rdquo; is on.
      </p>
      <h3>Cards</h3>
      <p className="muted">Cards come from enabled plugins. They appear on Today and in the menu bar; untick one to keep it out of the menu bar.</p>
      <ul className="items">
        {ordered.map((card, index) => {
          const hidden = order.hidden.includes(card.plugin);
          return (
            <li key={card.plugin} className="item row">
              <input
                type="checkbox"
                aria-label={`Show ${card.title} in the menu bar`}
                checked={!hidden}
                onChange={(event) =>
                  void bob.setPrefs({
                    cards: {
                      hidden: event.target.checked ? order.hidden.filter((id) => id !== card.plugin) : [...order.hidden, card.plugin],
                    },
                  })
                }
              />
              <span className="grow">{card.title}</span>
              <button disabled={index === 0} onClick={() => void bob.setPrefs({ cards: { order: moveCard(cards, order.order, card.plugin, -1) } })}>
                Up
              </button>
              <button
                disabled={index === ordered.length - 1}
                onClick={() => void bob.setPrefs({ cards: { order: moveCard(cards, order.order, card.plugin, 1) } })}
              >
                Down
              </button>
            </li>
          );
        })}
        {ordered.length === 0 && <li className="muted">No enabled plugin has a card.</li>}
      </ul>
      <h3>App</h3>
      <form
        className="form"
        onSubmit={(event) => {
          event.preventDefault();
          void bob.setPrefs({ serverUrl: server, bobHome: home });
        }}
      >
        <label>
          <span>Start at login</span>
          <input type="checkbox" checked={state.prefs.openAtLogin} onChange={(event) => void bob.setPrefs({ openAtLogin: event.target.checked })} />
        </label>
        <label>
          <span>Keep running in the menu bar when I quit (⌘Q)</span>
          <input type="checkbox" checked={state.prefs.keepRunning} onChange={(event) => void bob.setPrefs({ keepRunning: event.target.checked })} />
        </label>
        <label>
          <span>Notifications</span>
          <input type="checkbox" checked={state.prefs.notifications} onChange={(event) => void bob.setPrefs({ notifications: event.target.checked })} />
        </label>
        <label>
          <span>bobd address</span>
          <input value={server} spellCheck={false} onChange={(event) => setServer(event.target.value)} />
          <small>Only addresses on this Mac are accepted.</small>
        </label>
        <label>
          <span>Bob folder</span>
          <input value={home} spellCheck={false} placeholder="/Users/you/Bob" onChange={(event) => setHome(event.target.value)} />
          <small>Where Bob lives. Used to start bobd (deploy/bobd.sh) and, if needed, to find the API token. Empty means ~/Bob.</small>
        </label>
        <div className="row end">
          <button className="primary">Apply</button>
        </div>
      </form>
      <VoiceSettings voice={state.prefs.voice} />
      <RunSetupAgain />
    </section>
  );
}

const LANGUAGES = [
  ["en-US", "English (US)"],
  ["en-GB", "English (UK)"],
  ["en-IN", "English (India)"],
  ["en-AU", "English (Australia)"],
  ["hi-IN", "Hindi"],
  ["es-ES", "Spanish"],
  ["fr-FR", "French"],
  ["de-DE", "German"],
] as const;

/** Speech to text: the Whisper model, language, hotkey and how a spoken message ends. */
function VoiceSettings({ voice }: { voice: VoicePrefs }) {
  const [availability, setAvailability] = useState<VoiceAvailability | null>(null);
  const [devices, setDevices] = useState<MediaDeviceInfo[]>([]);
  const [hotkey, setHotkey] = useState(voice.hotkey);
  const [level, setLevel] = useState<number | null>(null);
  const [note, setNote] = useState("");
  const set = (patch: Partial<VoicePrefs>): void => void bob.setPrefs({ voice: patch });

  useEffect(() => {
    void bob.voice.availability().then(setAvailability, () => undefined);
  }, [voice.model, voice.language]);
  useEffect(() => {
    void navigator.mediaDevices
      ?.enumerateDevices()
      .then((all) => setDevices(all.filter((device) => device.kind === "audioinput" && device.deviceId)))
      .catch(() => undefined);
  }, []);

  /** Open the microphone for three seconds and show how loud it is; nothing is sent anywhere. */
  async function test(): Promise<void> {
    setNote("");
    try {
      const capture = await openMicrophone(voice.inputDevice, (samples) => {
        let sum = 0;
        for (const value of samples) sum += value * value;
        setLevel(Math.min(1, Math.sqrt(sum / samples.length) * 6));
      });
      setTimeout(() => {
        capture.stop();
        setLevel(null);
      }, 3000);
    } catch (error) {
      setNote(`The microphone could not be opened: ${error instanceof Error ? error.message : String(error)}`);
    }
  }

  return (
    <>
      <h3>Voice</h3>
      <p className="muted">
        {availability?.available
          ? `Speak to Bob with the microphone button or the hotkey. ${availability.detail}. Audio stays in memory on this Mac and is never saved.`
          : (availability?.detail ?? "Checking…")}
      </p>
      <form className="form" onSubmit={(event) => event.preventDefault()}>
        <label>
          <span>Language</span>
          <Dropdown
            label="Language"
            value={voice.language}
            choices={[...LANGUAGES.map(([value, label]) => ({ value, label })), ...(LANGUAGES.some(([value]) => value === voice.language) ? [] : [{ value: voice.language, label: voice.language }])]}
            onChange={(language) => set({ language })}
          />
          <small>Other languages use Whisper's multilingual model.</small>
        </label>
        <label>
          <span>Speech model</span>
          <Dropdown
            label="Speech model"
            value={voice.model}
            choices={MODEL_CHOICES.map((choice) => ({ value: choice.value, label: choice.label }))}
            onChange={(model) => set({ model })}
          />
          <small>
            Whisper {whisperModel(voice.language, voice.model).name}, {whisperModel(voice.language, voice.model).megabytes} MB. It is downloaded
            once, the first time you speak, and kept on this Mac.
          </small>
        </label>
        <label>
          <span>Microphone</span>
          <Dropdown
            label="Microphone"
            value={voice.inputDevice}
            choices={[{ value: "", label: "Mac default" }, ...devices.map((device) => ({ value: device.deviceId, label: device.label || "Microphone" }))]}
            onChange={(inputDevice) => set({ inputDevice })}
          />
        </label>
        <label>
          <span>Pause that sends a message</span>
          <Dropdown
            label="Pause"
            value={String(voice.pauseMs)}
            choices={PAUSE_CHOICES.map((ms) => ({ value: String(ms), label: `${(ms / 1000).toFixed(1)} seconds` }))}
            onChange={(ms) => set({ pauseMs: Number(ms) })}
          />
          <small>Used in conversation mode.</small>
        </label>
        <label>
          <span>Start in conversation mode</span>
          <input type="checkbox" checked={voice.conversation} onChange={(event) => set({ conversation: event.target.checked })} />
        </label>
        <label>
          <span>Hotkey</span>
          <input
            value={hotkey}
            spellCheck={false}
            placeholder="Alt+Shift+Space"
            onChange={(event) => setHotkey(event.target.value)}
            onBlur={() => hotkey !== voice.hotkey && set({ hotkey })}
          />
          <small>Starts and stops listening, for example Alt+Shift+Space. Empty turns it off.</small>
        </label>
        <div className="row">
          <button type="button" onClick={() => void test()} disabled={level !== null}>
            Test microphone
          </button>
          {level !== null && (
            <span className="voice-meter" aria-hidden="true">
              <span style={{ transform: `scaleX(${Math.max(0.04, level)})` }} />
            </span>
          )}
          <button type="button" className="plain" onClick={() => void bob.voice.openMicrophoneSettings()}>
            Microphone permission
          </button>
        </div>
        {note && <p className="error">{note}</p>}
      </form>
    </>
  );
}

/** Shown only in the shared build, which carries its own setup. */
function RunSetupAgain() {
  const [bundled, setBundled] = useState(false);
  useEffect(() => {
    void bob.setup.bundled().then(setBundled);
  }, []);
  if (!bundled) return null;
  return (
    <>
      <h3>Setup</h3>
      <p className="muted">
        Opens the setup window again, to change the models Bob uses or to repair a missing piece. What is already done is
        skipped, and the settings file you have is kept beside the new one.
      </p>
      <div className="row">
        <button onClick={() => void bob.setup.open()}>Run setup again…</button>
      </div>
    </>
  );
}

// -- Privacy ----------------------------------------------------------------------------------

interface Check {
  name: string;
  level: string;
  passed: boolean;
  detail: string;
  advisory: boolean;
}

interface AuditEntry {
  id: number;
  at: string;
  actor: string;
  action: string;
  tier: string | null;
  outcome: string;
  detail: string;
}

export function Privacy() {
  const status = useLoad<{ conformance: { ok: boolean; checks: Check[] } | null }>("/v1/status");
  const audit = useLoad<AuditEntry[]>("/v1/audit?limit=60");
  const files = useLoad<PluginSummary>("/v1/plugins/files");
  const documents = useLoad<PluginSummary>("/v1/plugins/documents");
  const action = useAction();
  const [report, setReport] = useState<{ ok: boolean; checks: Check[] } | null>(null);
  const conformance = report ?? status.data?.conformance ?? null;
  const roots = splitRoots(files.data?.settings.read_roots);

  /** Change the folders in every plugin that reads files, so they never disagree. */
  async function changeRoots(change: (roots: string[]) => string[]): Promise<void> {
    for (const [id, plugin] of [["files", files], ["documents", documents]] as const) {
      const current = splitRoots(plugin.data?.settings.read_roots);
      if (!plugin.data) continue;
      const next = joinRoots(change(current));
      if (next !== joinRoots(current)) await call("PUT", `/v1/plugins/${id}/settings`, { settings: { read_roots: next } });
    }
    files.reload();
    documents.reload();
  }

  async function addFolder(): Promise<void> {
    const folder = await bob.pickFolder("Choose a folder Bob may read");
    if (!folder) return;
    await action.run(() => changeRoots((current) => addRoot(current, folder)));
  }
  return (
    <section>
      <h2>Privacy</h2>
      <h3>Folders Bob can read</h3>
      {roots.length ? (
        <ul className="plainlist">
          {roots.map((root) => (
            <li key={root} className="row">
              <code className="grow">{root}</code>
              <button
                className="plain"
                disabled={action.busy}
                onClick={() => void action.run(() => changeRoots((current) => removeRoot(current, root)))}
              >
                Remove
              </button>
            </li>
          ))}
        </ul>
      ) : (
        <p className="muted">{files.data ? "No folders are allowed, so Bob reads nothing on this Mac." : "The Files plugin is not available."}</p>
      )}
      <div className="row">
        <button disabled={action.busy || !files.data} onClick={() => void addFolder()}>
          Add a folder…
        </button>
      </div>
      <p className="muted">
        Bob reads these folders and everything inside them, and only reads: it never changes or deletes anything there.
        The Files and Documents plugins use the same list. Credentials and <code>.env</code> files are never read.
        To make a folder part of what Bob searches by meaning (code, PDFs), add it under Knowledge too.
      </p>

      <h3>Read-only check</h3>
      <div className="row">
        <span className={`badge ${conformance ? (conformance.ok ? "running" : "error") : "off"}`}>
          {conformance ? (conformance.ok ? "passed" : "FAILED") : "not run yet"}
        </span>
        <span className="spacer" />
        <button
          disabled={action.busy}
          onClick={() => void action.run(async () => setReport(await call("POST", "/v1/conformance/run")))}
        >
          {action.busy ? "Running…" : "Run now"}
        </button>
      </div>
      {action.error && <div className="error">{action.error}</div>}
      <table>
        <tbody>
          {conformance?.checks.map((check) => (
            <tr key={check.name}>
              <td className={check.passed ? "ok" : check.advisory ? "warning" : "error"}>
                {check.passed ? "pass" : check.advisory ? "warn" : "FAIL"}
              </td>
              <td>{check.name}</td>
              <td className="muted">{check.detail}</td>
            </tr>
          ))}
        </tbody>
      </table>

      <h3>Audit log</h3>
      {audit.error && <div className="error">{audit.error}</div>}
      <table>
        <tbody>
          {audit.data?.map((entry) => (
            <tr key={entry.id}>
              <td className="muted">{entry.at.replace("T", " ").slice(0, 19)}</td>
              <td>{entry.actor}</td>
              <td>
                <code>{entry.action}</code>
              </td>
              <td className={entry.outcome === "ok" ? "muted" : "warning"}>{entry.outcome}</td>
              <td className="muted">{entry.detail.slice(0, 80)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

export function Settings() {
  const state = useAppState();
  const [tab, setTab] = useState<Tab>("Plugins");
  if (!state) return null;
  const connected = state.connection === "connected";
  return (
    <main className="settings">
      <nav className="sidebar drag">
        <div className="brand">
          <Mark />
          Bob
        </div>
        {TABS.map((name) => (
          <button key={name} className={name === tab ? "tab active nodrag" : "tab nodrag"} onClick={() => setTab(name)}>
            {name}
          </button>
        ))}
      </nav>
      <div className="content">
        <ConnectionNote state={state.connection} detail={state.detail} />
        {tab === "General" ? (
          <GeneralTab state={state} />
        ) : !connected ? (
          <p className="muted">These settings live in bobd. They appear once it is running.</p>
        ) : tab === "Plugins" ? (
          <Plugins />
        ) : tab === "Knowledge" ? (
          <Knowledge />
        ) : tab === "Models" ? (
          <Models />
        ) : tab === "Routines" ? (
          <Routines />
        ) : (
          <Privacy />
        )}
      </div>
    </main>
  );
}
