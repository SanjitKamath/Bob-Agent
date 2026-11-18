"""Fetching a web page safely.

The page reader goes wherever it is asked, so the first rule is where it may not go: never an
address inside this Mac or the network around it (Bob's own API, Ollama, a router's admin page,
a cloud metadata address). That is checked on the address the name resolves to, again on every
redirect, and the connection is made to the address that was checked, so a name that answers
differently the second time cannot slip through.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx
from bob_sdk.runtime import ToolError

MAX_BYTES = 1_500_000
MAX_REDIRECTS = 5
ALLOWED_PORTS = {None, 80, 443, 8080, 8443}
TEXT_TYPES = (
    "text/html",
    "application/xhtml+xml",
    "text/plain",
    "text/markdown",
    "text/xml",
    "application/xml",
    "application/json",
    "application/rss+xml",
    "application/atom+xml",
)
USER_AGENT = "Mozilla/5.0 (Macintosh; Apple Silicon) Bob/0.1 (personal assistant)"

Resolver = Callable[[str, int], list[str]]


def resolve(host: str, port: int) -> list[str]:
    try:
        found = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ToolError(f"Could not find {host}: {exc}") from exc
    return list(dict.fromkeys(str(item[4][0]) for item in found))


def is_public(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        elif ip.sixtofour is not None:
            ip = ip.sixtofour
    return ip.is_global and not ip.is_multicast


@dataclass(frozen=True)
class Target:
    url: str
    host: str
    port: int | None
    address: str


def check_url(url: str, resolver: Resolver = resolve) -> Target:
    """Refuse anything but a plain public web address; return it with the address to use."""
    text = (url or "").strip()
    if len(text) > 2000 or any(ord(c) < 33 for c in text):
        raise ToolError("That is not a web address.")
    parts = urlsplit(text)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ToolError("Only http and https addresses can be read.")
    if parts.username or parts.password:
        raise ToolError("Addresses with a user name or password are not read.")
    try:
        port = parts.port
    except ValueError as exc:
        raise ToolError("That address has an invalid port.") from exc
    if port not in ALLOWED_PORTS:
        raise ToolError(f"Port {port} is not one Bob will connect to.")
    host = parts.hostname.rstrip(".").lower()
    literal = _literal(host)
    if literal is None and (
        "." not in host or host.endswith((".local", ".internal", ".lan", ".home"))
    ):
        raise ToolError("That looks like a name on a private network, so it was not read.")
    addresses = (
        [literal] if literal else resolver(host, port or (443 if parts.scheme == "https" else 80))
    )
    if not addresses or not all(is_public(a) for a in addresses):
        raise ToolError("That address is inside this computer or its network, so it was not read.")
    return Target(url=text, host=host, port=port, address=addresses[0])


def _literal(host: str) -> str | None:
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        return None


def _pinned(target: Target) -> tuple[str, dict[str, str], dict[str, object]]:
    """The request that connects to the checked address but still names the real site."""
    parts = urlsplit(target.url)
    ip = f"[{target.address}]" if ":" in target.address else target.address
    netloc = f"{ip}:{parts.port}" if parts.port else ip
    url = urlunsplit((parts.scheme, netloc, parts.path or "/", parts.query, ""))
    host_header = f"{target.host}:{parts.port}" if parts.port else target.host
    extensions: dict[str, object] = {"sni_hostname": target.host} if parts.scheme == "https" else {}
    return url, {"Host": host_header}, extensions


@dataclass
class Page:
    url: str
    content_type: str
    text: str
    truncated: bool
    bytes: int


def fetch(
    url: str,
    *,
    client: httpx.Client | None = None,
    resolver: Resolver = resolve,
    timeout_s: float = 15.0,
) -> Page:
    """Read one public page: at most 1.5 MB of text, five redirects, no cookies, no scripts."""
    own = client is None
    http = client or httpx.Client(timeout=timeout_s, trust_env=False, follow_redirects=False)
    try:
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            target = check_url(current, resolver)
            request_url, headers, extensions = _pinned(target)
            headers = {
                **headers,
                "User-Agent": USER_AGENT,
                "Accept": "text/html,text/plain,*/*;q=0.5",
            }
            try:
                with http.stream(
                    "GET", request_url, headers=headers, extensions=extensions
                ) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            raise ToolError("The site redirected without saying where.")
                        current = urljoin(target.url, location)
                        continue
                    return _read(target.url, response)
            except httpx.TimeoutException as exc:
                raise ToolError("The site took too long to answer.") from exc
            except httpx.HTTPError as exc:
                raise ToolError(f"Could not reach the site: {exc}") from exc
        raise ToolError("The site redirected too many times.")
    finally:
        if own:
            http.close()


def _read(url: str, response: httpx.Response) -> Page:
    if response.status_code >= 400:
        raise ToolError(f"The site answered with an error (HTTP {response.status_code}).")
    kind = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if kind and kind not in TEXT_TYPES:
        raise ToolError(f"That is {kind}, not a page of text, so it was not read.")
    body = bytearray()
    truncated = False
    for chunk in response.iter_bytes():
        body.extend(chunk)
        if len(body) >= MAX_BYTES:
            truncated = True
            del body[MAX_BYTES:]
            break
    encoding = response.encoding or "utf-8"
    try:
        text = bytes(body).decode(encoding, errors="replace")
    except LookupError:
        text = bytes(body).decode("utf-8", errors="replace")
    return Page(
        url=url, content_type=kind or "text/html", text=text, truncated=truncated, bytes=len(body)
    )
