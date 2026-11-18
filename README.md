# Bob

Bob is a personal agent that runs as one background service on your Mac, with every model
served by Ollama on `127.0.0.1`. A small core handles models, routing, memory and safety.
Everything else is a plugin: a folder with a `plugin.yaml` manifest and an MCP server.

Bob is strictly read-only for code. It has no tool that changes, commits or pushes code or
loads anything into a server, and a conformance test proves that on every start.

## Layout

```text
config.yaml          model roles, collections, enabled plugins
config.local.yaml    your own settings, laid over config.yaml (not in git)
core/                bobd: router, agent loop, memory, scheduler, events, permissions,
                     plugin manager, API, CLI, conformance test, evals
sdk/                 bob-plugin-sdk: manifest schema, MCP helpers, output-folder rules,
                     parser contract, plugin test kit
plugins/
  knowledge/         connectors (GitHub, folder, files), parsers, LanceDB, retrieval
  files/ tasks/ mail/ calendar/ documents/ system/ architect/
app/                 Electron + TypeScript + React
  main/              tray, shortcuts, windows, bob:// scheme, WebSocket client
  renderer/          Bob window, panel, menu bar dropdown, settings
  shared/            logic used by both: preferences, cards, bob:// parsing, notices
evals/golden/        golden sets for `bob eval`
custom_plugins/      your private plugins and their wheels (not in git; see its README)
deploy/              launchd plists, the optional pre-push hook, the `bob` user script
data/                bob.sqlite, plugin data, LanceDB (git-ignored)
```

## Set up

```bash
brew install uv
uv sync                                   # Python 3.12 and all packages
ollama pull gemma4:e4b && ollama pull gpt-oss:20b && ollama pull qwen3-embedding:0.6b
uv run bob serve                          # runs bobd on 127.0.0.1:8765
```

Or start it in the background with `deploy/bobd.sh start` (also `restart`, `stop`,
`status`). It uses your launchd service if you installed one, otherwise runs bobd from this
repo with its log in `data/bobd.log`, and starts Ollama first if it isn't running. The app's
**Start bobd** button runs the same script.

In another terminal:

```bash
uv run bob status
uv run bob ask "Add buy milk to my task list"
uv run bob plugin list
```

The first start creates an API token in the macOS Keychain (service `bob`). The CLI and
the app read it from there. To keep bobd running, install `deploy/launchd/com.bob.bobd.plist`.

## The Mac app

One Electron app. It only talks to bobd on this Mac, over one WebSocket. It starts quietly in
the menu bar at login; open it from Finder, Spotlight or the menu bar and the Bob window
appears, with a Dock icon while that window is open.

**The look.** Black, electric blue, white and gold. Settings → General → Appearance picks
System, Light or Dark; it sets macOS's own appearance for Bob, so every window and the menu
bar dropdown change together. Electric blue is the one action colour (buttons, links,
selection, progress); gold marks what needs you (pinned chats, a running job's step count,
urgent mail). The sidebar, the menu bar dropdown, the hotkey panel and the message box are
translucent; with macOS "Reduce transparency" on they turn solid. Animations are short
(120–250 ms) and stop when "Reduce motion" is on.

**The logo** is the glowing blue ribbon "B" in `app/build/logo-source.png`. The app icon
(`app/build/icon.png`, 1024 px, turned into the `.icns` when the app is packaged) and the
small mark in the sidebar, the hotkey panel and the menu bar dropdown
(`app/renderer/src/assets/logo-mark.png`) are both made from it: the logo on a black macOS
icon tile. The menu bar itself still reads just "Bob".

```bash
cd app
npm install          # npm asks you to approve install scripts: electron, esbuild
npm test             # unit tests
npm start            # build and run from source
npm run dist         # builds app/release/mac-arm64/Bob.app (ad-hoc signed)
npm run dmg          # also packs it into app/release/Bob-<version>-arm64.dmg
```

### Install from the DMG

1. `cd app && npm run dmg`
2. Open `app/release/Bob-0.1.0-arm64.dmg` and drag **Bob** into **Applications**.
3. Open Bob from Applications or Spotlight. Start `bobd` first (`uv run bob serve`, or the
   launchd plist) so the window has something to show.

The DMG is ad-hoc signed, not notarized. It opens without a warning on the Mac that built it.
If you copy it to another Mac, macOS will block it the first time: right-click Bob.app and
choose **Open**, or run `xattr -dr com.apple.quarantine /Applications/Bob.app`.

| Surface | How to reach it |
| --- | --- |
| Start bobd | Shown wherever Bob says bobd is not running: the Bob window, the panel and the menu bar. **Restart bobd** is in the menu bar dropdown and the Bob menu. Runs `deploy/bobd.sh`; set the Bob folder under General if Bob is not in `~/Bob` |
| Bob window | Everything in one place: **Chat**, **Today** (cards, next tasks, the brief), **Tasks** (add, tick off, filter), and Knowledge, Plugins, Models, Routines, General, Privacy. ⌘1–⌘9 switch sections, ⌘, opens settings |
| Menu bar dropdown | Click **Bob** in the menu bar: status, cards, quick actions, **Open Bob**. Right-click opens the Bob window |
| Hotkey panel | ⌥Space. Streaming chat, drop files in, confirm cards, source links. Esc hides it |
| `bob://` links | `bob://app`, `bob://app?section=tasks`, `bob://ask?q=…`, `bob://triage`, `bob://reindex`, `bob://panel`, `bob://settings` |

Cards come from plugins. A plugin declares one in its manifest and it appears on Today
and in the menu bar dropdown with no app change. General in Settings orders them and
keeps any out of the menu bar:

```yaml
widget_card:
  title: Mail
  value: counts.urgent              # dotted path into the source tool's result
  status: "{needs_reply} need a reply"
  source: summary                   # read-tier tool feeding the card (default: summary)
  buttons:
    - {label: Triage now, tool: mail.triage}
  refresh: on_event                 # or: every 5m
```

A button calls its tool through bobd with the same checks as chat. A button for an
`external` tool only opens its confirm card in the panel.

`bob://` accepts a fixed set of actions and can never name a tool, because any web page
can ask macOS to open such a link. `bob://ask` does send the question, so treat a link
from an untrusted page like a prompt typed by someone else; external actions still need
your approval.

### The shared build (for other people's Macs)

`npm run dmg:share` (in `app/`) makes a second DMG, `app/release-share/Bob-Shared-<version>-arm64.dmg`,
that a person can open, drag to Applications and use, with no Python, Ollama or terminal on
their side. Your own `npm run dmg` is unchanged and still uses `~/Bob`.

What goes in it and what happens on their Mac:

- **Inside the app:** a private Python 3.12, Bob's packages as compiled bytecode only (no
  `.py` source), the plugins Knowledge, Files, Tasks, Documents, System, Web, Mail, Calendar and
  Architect, a clean settings template, the model list and the pinned Ollama download. Your
  private plugins (`custom_plugins/`) and your knowledge sources are **not** included, and the
  build refuses to finish if one of them, or any word you list in `custom_plugins/forbidden.txt`,
  turns up in it. Build it with `uv` on an Apple Silicon Mac.
- **First launch:** a setup window appears. The person picks a model for each job from a
  drop-down (every local model on ollama.com/library that can do that job; a box takes any other
  Ollama tag). Setup then makes `~/Library/Application Support/Bob`, downloads Ollama
  v0.40.11 (SHA-256 pinned, signature checked), runs it as a background service on port 11435
  with its models in Bob's folder, downloads the chosen models, writes the settings and starts
  Bob. No administrator password and no Ollama.app. If the Mac already runs Ollama, Bob leaves
  it alone, or uses it if the person says so (it still downloads the chosen models into it).
- **Later launches:** setup only reappears if Ollama or a chosen model has gone missing, or
  setup never finished (it then carries on). Settings → General → **Run setup again** reopens it.
  A settings file the person edited is kept beside the new one as `config.yaml.bak-<time>`.
- **Their own plugins:** Settings → Plugins → **Add a plugin** takes a `.whl` file. Bob reads
  it without running it, shows the tools, network and folders it asks for, and installs it
  only after a click; it starts switched off, and its dependencies must already be in Bob.
- **Updating the model list:** `deploy/share/models.txt` is a snapshot of the library page
  (2026-11-17). Edit it and rebuild; people can always type a newer tag.
- **Limits:** Apple Silicon and macOS 13 or later. The app is ad-hoc signed, so on another Mac
  the first open is right-click → **Open** (or the `xattr` line above). Bytecode keeps casual
  readers out of the source; it can still be decompiled by someone determined.

### Talking to Bob

The microphone button next to Send (or the hotkey, Alt+Shift+Space by default) turns what you say
into text in the message box; press Enter or the button when you are done. Turn on
**Conversation** to have each message sent when you pause and the microphone reopen after Bob
answers. Bob does not speak: answers appear on screen.

Recognition runs on this Mac with Whisper (whisper.cpp, using the Mac's GPU), on any macOS Bob
supports; there is nothing to build. A small Python helper, `voice/` (`bob_voice`), does it, and
`uv sync` installs it. English uses `base.en` (148 MB, quick) or `small.en` (488 MB, more
accurate); other languages use the multilingual `base`/`small`. A model is downloaded once, the
first time you speak, checked against a pinned SHA-256 and kept in the app's data folder; the
shared build ships `base.en` inside the app so it needs no download. Try it on a recording:
`uv run python -c "from bob_voice.cli import main; main()" --wav recording.wav`. Settings →
General → Voice has the model, language, microphone, pause length, hotkey and a microphone test.

Audio is captured by the Bob window, held in memory and passed to the helper; it is never saved
and never leaves the Mac. The microphone only opens after you press the button or hotkey, closes
when the window is hidden, the screen locks or the Mac sleeps, and the menu bar shows "Bob ●"
while it is open. Spoken words are never used to name a folder for the Editor plugin: type or
paste the path. macOS asks for microphone permission the first time; an ad-hoc signed app looks
new to macOS after each rebuild, so it may ask again (`tccutil reset Microphone com.bob.app`
clears a stale answer).

## Speed

Each reply shows where the time went, e.g. `[general · 1.4s (route 0.3s model 1.1s) …]`.

- **Small talk is instant.** "Hi", "thanks", "good morning" get a fixed reply with no model at
  all. Set `agent.small_talk: model` to have the router model write it instead.
- **Models stay loaded.** bobd keeps the router and main models (and the embedding model the
  cache needs) in memory (`ollama.keep_warm`), so a reply never waits for gpt-oss to load. If
  they do not all fit in memory, bobd notices, stops reloading the big one every few minutes,
  and `bob models list` says so under `memory`. Set it to `[]` if you need the memory back.
- **No tools when none are needed.** When the router decides a general question needs no
  lookups, the model gets no tool definitions, so the prompt is short. A request routed to a
  specialist (Mail, Files, …) always gets its tools.
- **Answer cache.** A first question asked again (or rephrased closely) within 24 hours is
  answered from the cache in milliseconds. Only answers that cannot go stale are kept: ones
  that used no tools, or only knowledge searches, and those are dropped as soon as a searched
  collection is re-indexed. Mail, tasks, files and anything that acts are always answered
  fresh. In the middle of a conversation, a question that stands on its own is reused too;
  a follow-up that leans on what was said ("and the other one?", "why?") never is. `bob ask --fresh "…"` or **Ask again**
  in the app skips the cache; `bob cache stats` and `bob cache clear` manage it.
- **Switched-off plugins.** Ask about mail while the Mail plugin is off and Bob says so at
  once, instead of searching your knowledge base for it.

## Several chats at once

The Chat section lists your chats beside the transcript: **New chat**, a search box, then
pinned chats and the rest by day. Every chat keeps its history in bobd's database, forever,
until you delete it (hover a chat: ☆ pins it, ✎ or a double-click renames it, × deletes it and
all its messages). The small router model names each new chat after its first answer; a name
you give it is never replaced. Chats that routines run for themselves stay out of the list.

Chats run at the same time: start a form build in one, ask about mail in another, and a quick
question in a third; none waits for the others except at the model. A second message in the
same chat waits for the first ("Waiting for the previous question…"). **Stop** stops only the
chat you are looking at. When a chat you are not looking at finishes, the app says so with a
notification (if notifications are on). Closing the app does not stop an answer: it finishes
and is saved to its chat.

**The model scheduler.** Every request bobd sends to Ollama waits in one queue per model. A
model runs at most its limit at once (Settings → Models → At once; default 2 for gpt-oss,
4 for the router and embeddings). When a model is full, a chat you are waiting on goes ahead
of background work: form builds, triage, routines and plugins working on their own. A plugin
working for your chat (mail summaries while you wait) counts as the chat. Roles on the same
Ollama model (main and coder are both gpt-oss) share its queue and the larger of their limits.
Ollama's own `OLLAMA_NUM_PARALLEL` (2 in `deploy/launchd/com.bob.ollama.plist`) caps what it
runs at once; a higher limit in Bob just queues inside Ollama.

A plugin that works in the background (a long build, say) should leave one slot free for a chat.
A plugin can do this by using one fewer than the coder's limit.

## Everyday commands

| Command | What it does |
| --- | --- |
| `bob ask "<text>" [--fresh]` | Route, act, verify and reply. Prints sources, warnings and timings. `--fresh` skips the answer cache |
| `bob search <collection> "<query>"` | Search a knowledge collection |
| `bob index [--repo <name>] [--force]` | Bring knowledge sources up to date |
| `bob index status` | Indexed versions, freshness and efficiency metrics |
| `bob plugin list / enable / disable / set` | Manage plugins and their settings |
| `bob plugin new <name>` / `bob plugin test <name>` | Scaffold and check a plugin |
| `bob routine list / run <id>` | Scheduled jobs |
| `bob remember "<fact>"` / `bob facts` | Profile facts |
| `bob conformance` | Run the read-only conformance test |
| `bob eval [<name>]` | Run a golden set |
| `bob audit` | The append-only audit log |
| `bob cache stats` / `bob cache clear` | The answer cache |
| `open 'bob://ask?q=hello'` | Trigger the app from Shortcuts, Raycast or Alfred |
| `bob github login` / `bob mail login` | Store a credential in the Keychain |
| `bob mail setup` / `bob mail check` | Set up the mail account and test its sign-in |

## Plugins

Plugins that hold your own information are kept in `custom_plugins/`, out of git, and given
to Bob as wheel files: see `custom_plugins/README.md`.

| Plugin | On by default | Needs |
| --- | --- | --- |
| Knowledge | yes | A GitHub token for private repos: `bob github login` |
| Files, Tasks, Documents, System | yes | — |
| Mail | no | An app password: `bob mail setup` fills in the servers and tests the sign-in |
| Calendar | no | Calendar access in System Settings → Privacy & Security |
| Architect | no | `mmdc` or `d2` on PATH to render diagrams |
| Web | no | Nothing. Search, read a page, weather; see below |
| Editor | no | Nothing. Reads and changes files in a folder you name in the chat; see below |

### Editor

Turn it on in Settings → Plugins, then write a folder path in your message ("tidy
`~/Documents/notes`"; put paths with spaces in quotes). Bob asks once for that folder. The
approval covers the folder and everything inside it, for that chat only; it ends when you delete
the chat, revoke it (`DELETE /v1/grants/<id>`) or restart bobd, and after 12 hours. Bob cannot
ask for a folder you did not write, and it never touches credentials, git repositories (reading
is allowed, changing is not, and there are no git commands), system folders, hidden folders in
your home, or Bob's own folders. Every change is kept first: say "undo" (several steps work too)
and Bob puts files back, newest first, refusing if you edited a file since. Deleted things are
kept aside, not destroyed. The journal is in the plugin's data folder.

### Web

Switch **Web** on in Settings → Plugins and ask for anything that needs the internet: "weather
in Pune", "what changed in httpx 0.28?", "latest stable Node". The router sends such questions
to it. Three read-only tools, no account or key:

- `search`: DuckDuckGo's plain page, or Wikipedia if DuckDuckGo refuses. It is a scraped page,
  so if DuckDuckGo changes its layout or limits requests, search falls back or reports that it
  found nothing.
- `fetch`: reads one page as plain text (1.5 MB at most, `page_characters` of it kept). It
  refuses addresses on this Mac or its network (Bob's own API, Ollama, a router, `localhost`,
  `.local` names), connects to the address it checked, re-checks every redirect, sends no
  cookies, and drops hidden text and comments.
- `weather`: Open-Meteo. Set "Your usual place" and the unit in the plugin's settings.

Whatever comes back is marked as text from strangers, and the Web specialist has no tools that
can change anything, so a page cannot make Bob act. Answers list the pages used; click one to
open it in your browser. The plugin is off by default because its permission is the widest of
any: it can reach any public website. Questions to the General specialist do not search the
web on their own; they say so and point you to the Web plugin.

Permission tiers are a fixed vocabulary: `read`, `draft`, `write_output`, `external`. A
manifest that declares anything else is rejected. An `external` tool (send, archive,
delete mail) always waits for your approval on a confirm card.

### Mail

```bash
bob mail setup        # asks for your address and an app password, then signs in to test
bob mail check        # test the sign-in again later
```

Gmail, iCloud, Yahoo and Zoho servers are filled in from your address; for anything else
(Google Workspace on your own domain: `imap.gmail.com` / `smtp.gmail.com`) you type them.
Gmail needs 2-Step Verification and an app password from
https://myaccount.google.com/apppasswords; your normal password does not work. Outlook.com and
Hotmail need OAuth, which the plugin does not have yet. Until mail is set up, the mail card
says so and the triage routine is recorded as skipped instead of failing.

To keep mail out of Bob's way, list words or phrases under **Plugins → Mail → Skip mail whose
subject contains**, one per line. A match ignores case and can be anywhere in the subject.
Skipped mail stays in your mailbox untouched; Bob leaves it out of new-mail lists, counts,
triage, tasks and the morning brief, and still finds it when you search. Changing the list
re-checks mail Bob has already fetched.

## Safety model

| Level | Rule | Where |
| --- | --- | --- |
| Tools | Only declared tools, fixed tiers, no shell tool | `sdk/.../manifest.py`, `core/.../plugins.py`, `broker.py` |
| Your files | Only folders you wrote in the chat and approved, per chat; hard rules no approval lifts; every change journaled for undo | `sdk/.../writeguard.py`, `core/.../grants.py`, `broker.py`, `plugins/editor` |
| Git | Allow-list of read commands, only inside `~/knowledge_base`; push URL disabled and a failing `pre-push` hook in every clone | `plugins/knowledge/.../git.py` |
| GitHub | A read-only fine-grained token; other credential helpers are switched off | `git.py`, `bob github login` |
| macOS | bobd runs as a separate user with no write access to your home or `~/dev` | `deploy/setup_bob_user.sh` (you run it) |
| Credentials | No credentials for the platforms you write for in the environment | `core/.../conformance.py` |

`bob conformance` tries to break each rule. If a write gets through, bobd shuts down.
Until you create the `bob` macOS user, the macOS level is reported as a warning
(`security.os_isolation: warn`). Set it to `required` afterwards.

## Tests

```bash
uv run pytest
uv run ruff check .
```

Private plugins are tested from their source: `uv run python deploy/custom_plugins.py test`.

## Differences from the design document

- **Update diffing** compares file content hashes instead of running `git diff`. The git
  allow-list stays exactly as designed (`clone`, `fetch`, `ls-remote`, `rev-parse`,
  `sparse-checkout`, `reset`, `clean`), and the same mechanism serves folder and file sources.
- **Reranker**: Qwen3-Reranker is not included. Results are fused by reciprocal rank, and
  "found nothing" uses a vector-similarity floor (`min_similarity`, default 0.45).
- **grep** is implemented in Python; ripgrep is not installed on this Mac.
- **Repo watch and "yesterday's commits"** are not built. They need git in `~/dev`, which
  the read-only rules forbid. Decide which rule wins before adding them.
- **Documents** uses PyMuPDF and python-docx directly, not `pdfintel` and `LMAT`.
- **Tasks** are stored in SQLite only; there is no Apple Reminders sync.
- **Mail** supports IMAP/SMTP with an app password; OAuth is not implemented.
- **Menu bar item** shows the text "Bob" instead of an icon, with no count next to it.
- **Notifications** come from the app: urgent mail, failed routines, a failed read-only
  check, new cards. macOS shows action buttons only for apps it treats as signed; the
  ad-hoc build may show the notification without buttons, and a click still opens Bob.
- **Not built**: the mail-triage golden set, and holding back a snapshot when the retrieval
  eval drops.
