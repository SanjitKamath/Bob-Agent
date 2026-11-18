"""``bobd``: the background service launchd keeps running."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
from collections.abc import AsyncIterator

import uvicorn
from fastapi import FastAPI

from .api import create_api
from .app import Bob
from .config import Config, ConfigError, load_config

log = logging.getLogger("bobd")


def build(config: Config) -> tuple[Bob, FastAPI, uvicorn.Server]:
    """Create the service, its API and the server that binds to loopback."""
    holder: dict[str, uvicorn.Server] = {}

    async def shut_down(reason: str) -> None:
        log.critical("shutting down: %s", reason)
        if "server" in holder:
            holder["server"].should_exit = True

    bob = Bob(config, on_fatal=shut_down)
    api = create_api(bob)

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        await bob.start()
        # Prove the read-only guarantee before taking any request.
        startup_check = asyncio.create_task(bob.run_conformance())
        try:
            yield
        finally:
            startup_check.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await startup_check
            await bob.stop()

    api.router.lifespan_context = lifespan
    server = uvicorn.Server(
        uvicorn.Config(
            api,
            host=config.server.host,
            port=config.server.port,
            log_level="warning",
            access_log=False,
        )
    )
    holder["server"] = server
    return bob, api, server


def main() -> None:
    """The `bobd` entry point: `bobd [/path/to/config.yaml]`, as the launchd plist runs it."""
    run(sys.argv[1] if len(sys.argv) > 1 else None)


def run(config_path: str | None = None) -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        print(f"bobd: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    _bob, _api, server = build(config)
    log.info("bobd listening on %s", config.server.url)
    server.run()


if __name__ == "__main__":
    main()
