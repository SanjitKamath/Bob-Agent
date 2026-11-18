"""The ``bob`` command line. It talks to bobd on localhost with the Keychain token."""

from __future__ import annotations

import getpass
import json
import shutil
import subprocess
from typing import Annotated, Any

import httpx
import typer

from .auth import read_token, set_secret
from .config import Config, ConfigError, load_config

app = typer.Typer(help="Bob: a local personal agent.", no_args_is_help=True, add_completion=False)
plugin_app = typer.Typer(help="Manage plugins.", no_args_is_help=True)
index_app = typer.Typer(help="Index knowledge sources.", invoke_without_command=True)
models_app = typer.Typer(help="Model roles.", no_args_is_help=True)
routine_app = typer.Typer(help="Routines.", no_args_is_help=True)
github_app = typer.Typer(help="GitHub credentials for knowledge sources.", no_args_is_help=True)
mail_app = typer.Typer(help="Mail account.", no_args_is_help=True)
cache_app = typer.Typer(help="The answer cache.", no_args_is_help=True)
app.add_typer(plugin_app, name="plugin")
app.add_typer(index_app, name="index")
app.add_typer(models_app, name="models")
app.add_typer(routine_app, name="routine")
app.add_typer(github_app, name="github")
app.add_typer(mail_app, name="mail")
app.add_typer(cache_app, name="cache")

ConfigOption = Annotated[
    str | None, typer.Option("--config", "-c", help="Path to config.yaml.", envvar="BOB_CONFIG")
]
_state: dict[str, str | None] = {"config": None}


@app.callback()
def _root(config: ConfigOption = None) -> None:
    _state["config"] = config


def fail(message: str, code: int = 1) -> typer.Exit:
    typer.secho(message, fg=typer.colors.RED, err=True)
    return typer.Exit(code)


def config() -> Config:
    try:
        return load_config(_state["config"])
    except ConfigError as exc:
        raise fail(str(exc), 2) from exc


def request(method: str, path: str, *, body: Any = None, timeout: float = 900.0) -> Any:
    """Call bobd. Exits with a clear message when it is not running or refuses."""
    cfg = config()
    token = read_token(cfg.data_dir)
    if token is None:
        raise fail("No API token yet. Start the service first: bob serve")
    try:
        response = httpx.request(
            method, f"{cfg.server.url}{path}", json=body, timeout=timeout,
            headers={"Authorization": f"Bearer {token}"},
        )  # fmt: skip
    except httpx.ConnectError as exc:
        raise fail(f"bobd is not running on {cfg.server.url}. Start it with: bob serve") from exc
    except httpx.HTTPError as exc:
        raise fail(f"bobd did not answer: {exc.__class__.__name__}") from exc
    if response.status_code >= 400:
        try:
            detail = response.json().get("detail", response.text)
        except ValueError:
            detail = response.text
        if isinstance(detail, dict) and detail.get("consent_required"):
            return {"consent_required": detail["plugin"]}
        raise fail(f"Error {response.status_code}: {detail}")
    return response.json()


def show(data: Any) -> None:
    typer.echo(json.dumps(data, indent=2, ensure_ascii=False, default=str))


def call_tool(plugin: str, tool: str, arguments: dict[str, Any]) -> Any:
    return request(
        "POST", "/v1/tools/call", body={"plugin": plugin, "tool": tool, "arguments": arguments}
    )["result"]


def _outcome_line(run: dict[str, Any]) -> str:
    metrics = run.get("metrics", {})
    return (
        f"{run['collection']}/{run['source']}: {run['outcome']}"
        f"  files parsed {metrics.get('files_parsed', 0)}/{metrics.get('files_total', 0)},"
        f" embedded {metrics.get('chunks_embedded', 0)}, cache hits {metrics.get('cache_hits', 0)}"
        + (f"  [{run['detail']}]" if run.get("detail") else "")
    )


# -- service ----------------------------------------------------------------------


@app.command()
def serve() -> None:
    """Run bobd in the foreground (launchd runs this for you once installed)."""
    from .service import run

    run(_state["config"])


@app.command()
def status() -> None:
    """Models, plugins and the last read-only conformance result."""
    data = request("GET", "/v1/status")
    models = data["models"]
    typer.echo(f"bobd {data['version']}")
    if not models.get("roles"):
        typer.secho(f"Ollama: {models.get('error', 'unavailable')}", fg=typer.colors.RED)
    for role, info in models.get("roles", {}).items():
        mark = "ok" if info["installed"] else "NOT INSTALLED"
        typer.echo(f"  {role:<11} {info['model']:<28} ctx {info['num_ctx']:<6} {mark}")
    typer.echo("Plugins:")
    for plugin_id, state in sorted(data["plugins"].items()):
        typer.echo(f"  {plugin_id:<12} {state}")
    conformance = data.get("conformance")
    if conformance:
        typer.echo(f"Read-only conformance: {'passed' if conformance['ok'] else 'FAILED'}")
        for check in conformance["checks"]:
            if not check["passed"]:
                note = "warning" if check["advisory"] else "FAILED"
                typer.echo(f"  {note}: {check['name']}: {check['detail']}")


@app.command()
def ask(
    text: Annotated[str, typer.Argument(help="What to ask Bob.")],
    conversation: Annotated[str | None, typer.Option(help="Continue a conversation by id.")] = None,
    specialist: Annotated[
        str | None, typer.Option(help="Skip routing and use this plugin.")
    ] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Print the full reply as JSON.")] = False,
    fresh: Annotated[bool, typer.Option("--fresh", help="Ignore the answer cache.")] = False,
) -> None:
    """Ask a question or give a task."""
    body = {"text": text, "conversation_id": conversation, "specialist": specialist, "fresh": fresh}
    reply = request("POST", "/v1/ask", body=body)
    if as_json:
        show(reply)
        return
    typer.echo(reply["text"])
    if reply["sources"]:
        typer.echo("\nSources:")
        for source in reply["sources"]:
            typer.echo(f"  [{source['n']}] {source['citation']}")
    for warning in reply["warnings"]:
        typer.secho(f"\nNote: {warning}", fg=typer.colors.YELLOW)
    tools = ", ".join(f"{t['plugin']}.{t['tool']}" for t in reply["tools"]) or "none"
    timings = reply.get("timings") or {}
    split = " ".join(f"{k} {v}s" for k, v in timings.items() if k != "total")
    cached = f" · cached from {reply['cached_at']} (--fresh to redo)" if reply.get("cached") else ""
    typer.secho(
        f"\n[{reply['specialist']} · {reply['steps']} step(s) · {reply['seconds']}s"
        f"{f' ({split})' if split else ''} · tools: {tools}{cached}"
        f" · conversation {reply['conversation_id']}]",
        fg=typer.colors.BRIGHT_BLACK,
    )


@app.command()
def search(
    collection: Annotated[str, typer.Argument(help="Collection to search.")],
    query: Annotated[str, typer.Argument(help="What to look for.")],
    limit: Annotated[int, typer.Option(help="How many results.")] = 5,
) -> None:
    """Search a knowledge collection."""
    data = call_tool(
        "knowledge", "search", {"collection": collection, "query": query, "limit": limit}
    )
    if not data["found"]:
        typer.echo("Nothing relevant was found.")
        raise typer.Exit(1)
    for hit in data["results"]:
        typer.secho(
            f"{hit['score']:.2f}  {hit['citation']}  ({', '.join(hit['matched_by'])})", bold=True
        )
        typer.echo(f"      {hit['title']}")
        first = next((line for line in hit["text"].splitlines() if line.strip()), "")
        typer.echo(f"      {first[:140]}")


@app.command()
def remember(fact: Annotated[str, typer.Argument(help="A fact Bob should keep.")]) -> None:
    """Store a profile fact."""
    show(request("POST", "/v1/memory/facts", body={"fact": fact}))


@app.command()
def facts(
    forget: Annotated[int | None, typer.Option(help="Delete the fact with this id.")] = None,
) -> None:
    """List profile facts, or delete one."""
    if forget is not None:
        request("DELETE", f"/v1/memory/facts/{forget}")
        typer.echo(f"Forgot fact {forget}.")
        return
    for fact in request("GET", "/v1/memory/facts"):
        typer.echo(
            f"{fact['id']:>4}  {fact['created_at'][:10]}  {fact['fact']}  ({fact['source']})"
        )


@app.command()
def audit(
    limit: int = 30, kind: Annotated[str | None, typer.Option(help="tool, git, plugin…")] = None
) -> None:
    """Show the audit log, newest first."""
    path = f"/v1/audit?limit={limit}" + (f"&kind={kind}" if kind else "")
    for entry in request("GET", path):
        typer.echo(
            f"{entry['at']}  {entry['actor']:<22} {entry['action']:<34} {entry['outcome']}"
            + (f"  {entry['detail'][:80]}" if entry["detail"] else "")
        )


@app.command()
def conformance() -> None:
    """Run the read-only conformance test now."""
    report = request("POST", "/v1/conformance/run")
    for check in report["checks"]:
        mark = "pass" if check["passed"] else ("warn" if check["advisory"] else "FAIL")
        typer.echo(f"  [{mark}] {check['level']:<6} {check['name']}: {check['detail']}")
    typer.echo("Passed." if report["ok"] else "FAILED: bobd is shutting down.")
    raise typer.Exit(0 if report["ok"] else 1)


@app.command(name="eval")
def run_eval(name: Annotated[str | None, typer.Argument(help="Golden set to run.")] = None) -> None:
    """Run an eval golden set, or list them."""
    if name is None:
        data = request("GET", "/v1/evals")
        typer.echo("Golden sets: " + (", ".join(data["available"]) or "none"))
        for run in data["history"][:10]:
            typer.echo(
                f"  {run['at']}  {run['eval_id']:<20} {run['metrics']}  {'pass' if run['passed'] else 'fail'}"
            )
        return
    result = request("POST", f"/v1/evals/{name}/run", timeout=3600)
    typer.echo(
        f"{result['eval']}: {result['metrics']} (target {result['target']}, {result['cases']} cases)"
    )
    for failure in result["failures"]:
        typer.echo(f"  miss: {json.dumps(failure, ensure_ascii=False)[:200]}")
    for metric, drop in result["regressions"].items():
        typer.secho(f"  regression: {metric} dropped {drop * 100:.1f} points", fg=typer.colors.RED)
    raise typer.Exit(0 if result["passed"] else 1)


# -- index ----------------------------------------------------------------------------


@index_app.callback()
def index(
    ctx: typer.Context,
    collection: Annotated[str | None, typer.Option(help="Only this collection.")] = None,
    repo: Annotated[
        str | None, typer.Option("--repo", "--source", help="Only this source.")
    ] = None,
    force: Annotated[bool, typer.Option(help="Rebuild even if nothing changed.")] = False,
) -> None:
    """Bring knowledge sources up to date."""
    if ctx.invoked_subcommand is not None:
        return
    body = {"collection": collection, "source": repo, "force": force}
    data = request("POST", "/v1/knowledge/reindex", body=body, timeout=7200)
    for run in data["runs"]:
        typer.echo(_outcome_line(run))
    raise typer.Exit(0 if data["ok"] else 1)


@index_app.command("status")
def index_status(collection: Annotated[str | None, typer.Argument()] = None) -> None:
    """Indexed versions, freshness and the last run's metrics."""
    data = request("GET", "/v1/knowledge/collections")
    for collection_id, info in data["status"].items():
        if collection and collection != collection_id:
            continue
        typer.secho(
            f"{collection_id}: {info['chunks']} chunks, {info['vectors']} vectors,"
            f" {info['invalid_chunks']} invalid, {info['files_failed_to_parse']} unparsed file(s)",
            bold=True,
        )
        if info.get("profile_error"):
            typer.secho(f"  {info['profile_error']}", fg=typer.colors.RED)
        for source in info["sources"]:
            version = (source["indexed_version"] or "never indexed")[:12]
            typer.echo(
                f"  {source['name']:<24} {source['kind']:<7} {version:<14} {source['indexed_at'] or ''}"
            )
            last = source.get("last_run")
            if last:
                m = last["metrics"]
                typer.echo(
                    f"      last run: {last['outcome']} ({last['trigger']}),"
                    f" re-embed ratio {m.get('reembed_ratio')}, touched share {m.get('touched_share')},"
                    f" cache hit rate {m.get('cache_hit_rate')}"
                )
    missing = [c["id"] for c in data["collections"] if c["id"] not in data["status"]]
    if missing:
        typer.echo(f"Configured but not active: {', '.join(missing)}")


# -- plugins ----------------------------------------------------------------------------


@plugin_app.command("list")
def plugin_list() -> None:
    """Plugins, their status and their tools."""
    data = request("GET", "/v1/plugins")
    for plugin in data["plugins"]:
        state = plugin["status"] if plugin["enabled"] else "off"
        typer.secho(
            f"{plugin['id']:<12} {state:<9} {plugin['version']}  {plugin['description']}", bold=True
        )
        tools = ", ".join(
            f"{t['name']}({t['tier']}{'' if t['enabled'] else ', off'})" for t in plugin["tools"]
        )
        typer.echo(f"    tools: {tools}")
        if plugin["error"]:
            typer.secho(f"    error: {plugin['error']}", fg=typer.colors.RED)
    for location, reason in data["rejected"].items():
        typer.secho(f"rejected: {location}: {reason}", fg=typer.colors.RED)


@plugin_app.command("enable")
def plugin_enable(
    plugin_id: str,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Approve without asking.")] = False,
) -> None:
    """Switch a plugin on. The first time, shows what it can do and asks."""
    data = request("POST", f"/v1/plugins/{plugin_id}/enable", body={"acknowledged": yes})
    if "consent_required" in data:
        summary = data["consent_required"]
        typer.secho(f"{summary['name']} can:", bold=True)
        for tool in summary["tools"]:
            note = "  (always asks you first)" if tool["asks_first"] else ""
            typer.echo(f"  {tool['name']:<18} {tool['tier']}{note}")
        typer.echo(f"  network: {summary['network']}")
        typer.echo(f"  writes to: {summary['write_folders'] or 'nothing outside its own data'}")
        if not typer.confirm("Switch it on?"):
            raise typer.Exit(1)
        data = request("POST", f"/v1/plugins/{plugin_id}/enable", body={"acknowledged": True})
    typer.echo(f"{plugin_id}: {data['status']}")


@plugin_app.command("disable")
def plugin_disable(plugin_id: str) -> None:
    """Switch a plugin off. Its data and settings are kept."""
    typer.echo(f"{plugin_id}: {request('POST', f'/v1/plugins/{plugin_id}/disable')['status']}")


@plugin_app.command("set")
def plugin_set(plugin_id: str, key: str, value: str) -> None:
    """Change one plugin setting. The value is parsed as JSON when it can be."""
    try:
        parsed: Any = json.loads(value)
    except ValueError:
        parsed = value
    show(request("PUT", f"/v1/plugins/{plugin_id}/settings", body={"settings": {key: parsed}}))


@plugin_app.command("new")
def plugin_new(name: str) -> None:
    """Scaffold a plugin: plugin.yaml, server.py, a prompt and tests."""
    from bob_sdk.testkit import scaffold_plugin

    try:
        folder = scaffold_plugin(config().plugins_dir, name)
    except (ValueError, FileExistsError) as exc:
        raise fail(str(exc)) from exc
    typer.echo(f"Created {folder}")
    typer.echo("Next: add it to the workspace (uv sync), then: bob plugin test " + folder.name)


@plugin_app.command("test")
def plugin_test(name: str) -> None:
    """Schema check, tool smoke test and read-only conformance for one plugin."""
    from bob_sdk.testkit import check_plugin

    folder = config().plugins_dir / name
    if not folder.is_dir():
        raise fail(f"no plugin folder {folder}")
    results = check_plugin(folder)
    for result in results:
        typer.echo(
            f"  [{'pass' if result.ok else 'FAIL'}] {result.name}"
            + (f": {result.detail}" if result.detail else "")
        )
    raise typer.Exit(0 if all(result.ok for result in results) else 1)


# -- models, routines -------------------------------------------------------------------------


@models_app.command("list")
def models_list() -> None:
    """Model roles and what Ollama has installed."""
    show(request("GET", "/v1/models"))


@models_app.command("set")
def models_set(role: str, model: str) -> None:
    """Point a role (router, main, coder, vision, embeddings) at an installed model."""
    show(request("PUT", f"/v1/models/{role}", body={"model": model}))


@routine_app.command("list")
def routine_list() -> None:
    """Routines and when they next run."""
    for routine in request("GET", "/v1/routines"):
        when = routine["cron"] or (
            f"every {routine['every_minutes']} min"
            if routine["every_minutes"]
            else f"on {routine['on_event']}"
        )
        if routine["between"]:
            when += f" {routine['between'][0]}-{routine['between'][1]}"
        state = "on " if routine["enabled"] else "off"
        typer.echo(
            f"{routine['id']:<22} {state} {when:<28} next {routine['next_run'] or '-'}  ({routine['owner']})"
        )


@routine_app.command("run")
def routine_run(routine_id: str) -> None:
    """Run a routine now."""
    result = request("POST", f"/v1/routines/{routine_id}/run", timeout=7200)
    typer.echo(
        f"{routine_id}: {result['outcome']}"
        + (f" ({result['detail']})" if result["detail"] else "")
    )
    raise typer.Exit(0 if result["outcome"] == "ok" else 1)


# -- credentials (stored in the Keychain, never on disk) ----------------------------------------------


@github_app.command("login")
def github_login(
    mode: Annotated[str, typer.Option(help="'token' (read-only, recommended) or 'gh'.")] = "token",
) -> None:
    """Store the GitHub credential Bob uses to fetch knowledge sources."""
    if mode not in {"token", "gh"}:
        raise fail("mode must be 'token' or 'gh'")
    if mode == "gh":
        typer.secho(
            "This copies the login of your gh CLI into Bob's Keychain. That login can push, so "
            "GitHub itself would not stop a write. Bob's other safeguards still apply.",
            fg=typer.colors.YELLOW,
        )
        if not typer.confirm("Copy the gh token?"):
            raise typer.Exit(1)
        binary = shutil.which("gh")
        if binary is None:
            raise fail("the gh CLI is not installed")
        done = subprocess.run(
            [binary, "auth", "token"], capture_output=True, text=True, check=False
        )  # noqa: S603
        token = done.stdout.strip()
        if done.returncode != 0 or not token:
            raise fail("gh has no login; run: gh auth login")
    else:
        typer.echo(
            "Create a fine-grained token with only 'Contents: Read-only' on the repos you index."
        )
        token = getpass.getpass("Token: ").strip()
        if not token:
            raise fail("no token entered")
    show(request("PUT", "/v1/knowledge/github", body={"auth": mode, "token": token}))


# Where common providers keep their servers, and where you make an app password.
MAIL_PROVIDERS: dict[str, dict[str, Any]] = {
    "gmail": {
        "domains": ["gmail.com", "googlemail.com"],
        "imap": ("imap.gmail.com", 993), "smtp": ("smtp.gmail.com", 465),
        "help": "Turn on 2-Step Verification, then make an app password at "
                "https://myaccount.google.com/apppasswords (name it Bob). Paste the 16 letters.",
    },
    "icloud": {
        "domains": ["icloud.com", "me.com", "mac.com"],
        "imap": ("imap.mail.me.com", 993), "smtp": ("smtp.mail.me.com", 587),
        "help": "Make an app-specific password at https://account.apple.com "
                "(Sign-In and Security → App-Specific Passwords).",
    },
    "yahoo": {
        "domains": ["yahoo.com", "yahoo.co.in", "ymail.com"],
        "imap": ("imap.mail.yahoo.com", 993), "smtp": ("smtp.mail.yahoo.com", 465),
        "help": "Make an app password in Yahoo Account Security → Generate app password.",
    },
    "zoho": {
        "domains": ["zoho.com", "zohomail.com", "zohomail.in"],
        "imap": ("imap.zoho.com", 993), "smtp": ("smtp.zoho.com", 465),
        "help": "Turn on IMAP in Zoho Mail settings, and make an application-specific password.",
    },
}  # fmt: skip
MICROSOFT_DOMAINS = {"outlook.com", "hotmail.com", "live.com", "msn.com"}


def mail_provider(address: str) -> tuple[str, dict[str, Any]] | None:
    domain = address.rsplit("@", 1)[-1].strip().lower()
    for name, provider in MAIL_PROVIDERS.items():
        if domain in provider["domains"]:
            return name, provider
    return None


@mail_app.command("setup")
def mail_setup(
    address: Annotated[str | None, typer.Option(help="Your mail address.")] = None,
    imap_host: Annotated[
        str | None, typer.Option(help="IMAP server, if not a known provider.")
    ] = None,
    smtp_host: Annotated[
        str | None, typer.Option(help="SMTP server, if not a known provider.")
    ] = None,
    imap_port: Annotated[int | None, typer.Option()] = None,
    smtp_port: Annotated[int | None, typer.Option()] = None,
) -> None:
    """Set up your mail account: servers, the app password, and a test sign-in."""
    address = (address or typer.prompt("Your mail address")).strip()
    if "@" not in address:
        raise fail(f"{address!r} is not a mail address")
    domain = address.rsplit("@", 1)[-1].lower()
    if domain in MICROSOFT_DOMAINS:
        raise fail(
            "Outlook.com and Hotmail no longer accept passwords over IMAP; they need OAuth, "
            "which Bob's Mail plugin does not have yet."
        )
    found = mail_provider(address)
    if found and not imap_host:
        name, provider = found
        imap_host, default_imap_port = provider["imap"]
        smtp_host, default_smtp_port = provider["smtp"]
        typer.echo(
            f"{name.title()}: {imap_host}:{default_imap_port} and {smtp_host}:{default_smtp_port}"
        )
        typer.secho(provider["help"], fg="cyan")
    else:
        typer.echo(
            "Not a provider Bob knows. For Google Workspace use imap.gmail.com / smtp.gmail.com."
        )
        imap_host = imap_host or typer.prompt("IMAP server")
        smtp_host = smtp_host or typer.prompt("SMTP server")
        default_imap_port, default_smtp_port = 993, 465
    password = getpass.getpass("App password (not your normal password): ").replace(" ", "")
    if not password:
        raise fail("no password entered")
    if not set_secret("mail-password", password):
        raise fail("the Keychain is not available")
    settings = {
        "username": address,
        "imap_host": imap_host,
        "imap_port": imap_port or default_imap_port,
        "smtp_host": smtp_host,
        "smtp_port": smtp_port or default_smtp_port,
    }
    request("PUT", "/v1/plugins/mail/settings", body={"settings": settings})
    plugin_enable("mail", yes=False)
    typer.echo("Signing in…")
    result = call_tool("mail", "check_account", {})
    data = result.get("result", result) if isinstance(result, dict) else {}
    typer.echo(f"  IMAP: {data.get('imap', '?')}")
    typer.echo(f"  SMTP: {data.get('smtp', '?')}")
    if data.get("ok"):
        typer.secho(
            "Mail is set up. Triage runs every 15 minutes between 08:00 and 20:00.", fg="green"
        )
        return
    typer.secho("Mail is not working yet.", fg="red")
    if found and found[0] == "gmail":
        typer.echo(
            "- Use an app password, not your Google password; 2-Step Verification must be on.\n"
            "- A Google Workspace admin can turn IMAP or app passwords off for your domain.\n"
            "- Run `bob mail setup` again with a new app password if this one was mistyped."
        )
    raise typer.Exit(1)


@mail_app.command("check")
def mail_check() -> None:
    """Test the mail account's sign-in without reading or sending mail."""
    result = call_tool("mail", "check_account", {})
    show(result.get("result", result) if isinstance(result, dict) else result)


@mail_app.command("login")
def mail_login() -> None:
    """Store the mail password (an app password) in the Keychain."""
    password = getpass.getpass("Mail app password: ")
    if not password:
        raise fail("no password entered")
    if not set_secret("mail-password", password):
        raise fail("the Keychain is not available")
    typer.echo(
        "Stored. Set imap_host, smtp_host and username with: bob plugin set mail <key> <value>"
    )


@cache_app.command("stats")
def cache_stats() -> None:
    """How many answers are kept and how often they were reused."""
    show(request("GET", "/v1/cache"))


@cache_app.command("clear")
def cache_clear() -> None:
    """Forget every cached answer."""
    removed = request("DELETE", "/v1/cache")["removed"]
    typer.echo(f"Removed {removed} cached answer(s).")


if __name__ == "__main__":
    app()
