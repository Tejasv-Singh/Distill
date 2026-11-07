"""URL validation, SSRF protection, rate limiting, and untrusted-text helpers."""
from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
import time
from collections import defaultdict, deque
from urllib.parse import urlsplit, urlunsplit

from .config import settings

MAX_URL_LEN = 2048
_ALLOWED_SCHEMES = {"http", "https"}


class UnsafeURL(ValueError):
    """The URL is malformed, uses a blocked scheme, or points at an internal address."""


def normalize_url(raw: str) -> str:
    """Validate and normalise a user-supplied URL. Adds https:// if no scheme was given."""
    raw = (raw or "").strip()
    if not raw:
        raise UnsafeURL("Enter a URL.")
    if len(raw) > MAX_URL_LEN:
        raise UnsafeURL("URL is too long.")
    if re.search(r"[\s\x00-\x1f]", raw):
        raise UnsafeURL("URL contains spaces or control characters.")
    if "://" not in raw:
        raw = "https://" + raw
    parts = urlsplit(raw)
    if parts.scheme.lower() not in _ALLOWED_SCHEMES:
        raise UnsafeURL(f"Only http and https URLs are supported (got '{parts.scheme}').")
    if not parts.hostname:
        raise UnsafeURL("URL has no host name.")
    if parts.username or parts.password:
        raise UnsafeURL("URLs with embedded credentials are not allowed.")
    host = parts.hostname.lower().rstrip(".")
    if "." not in host and not _is_ip(host) and host != "localhost":
        raise UnsafeURL(f"'{host}' is not a valid public host name.")
    try:
        port = parts.port
    except ValueError as e:
        raise UnsafeURL("URL has an invalid port.") from e
    netloc = host if ":" not in host else f"[{host}]"
    if port:
        netloc += f":{port}"
    path = parts.path or "/"
    return urlunsplit((parts.scheme.lower(), netloc, path, parts.query, ""))


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def _host_port(url: str) -> tuple[str, int]:
    p = urlsplit(url)
    port = p.port or (443 if p.scheme == "https" else 80)
    return (p.hostname or "").lower(), port


def is_allowlisted(url: str) -> bool:
    host, port = _host_port(url)
    return f"{host}:{port}" in settings.private_host_allowlist


def ip_is_public(ip: ipaddress._BaseAddress) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast and not ip.is_reserved


_dns_cache: dict[str, tuple[float, list[str]]] = {}


async def resolve_public(url: str) -> list[str]:
    """Resolve the URL's host and return its IPs. Raises UnsafeURL if any IP is internal.

    All addresses must be public: a host that resolves to both a public and a private
    address is rejected, since the connection could use either.
    """
    p = urlsplit(url)
    if p.scheme not in _ALLOWED_SCHEMES or not p.hostname:
        raise UnsafeURL(f"Unsupported URL: {url[:200]}")
    host, port = _host_port(url)
    if is_allowlisted(url):
        return []  # explicitly trusted (e.g. local demo site); let the client resolve normally
    if _is_ip(host):
        ips = [host]
    else:
        cached = _dns_cache.get(host)
        if cached and cached[0] > time.monotonic():
            ips = cached[1]
        else:
            loop = asyncio.get_running_loop()
            try:
                infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
            except socket.gaierror as e:
                raise UnsafeURL(f"Could not resolve '{host}'. Check the address.") from e
            ips = sorted({i[4][0] for i in infos})
            _dns_cache[host] = (time.monotonic() + 60, ips)
    if not ips:
        raise UnsafeURL(f"'{host}' has no addresses.")
    for ip in ips:
        if not ip_is_public(ipaddress.ip_address(ip.split("%")[0])):
            raise UnsafeURL(
                f"'{host}' points to a private or internal address ({ip}). "
                "Only public websites can be scraped."
            )
    return ips


# --------------------------------------------------------------------- rate limit
class RateLimiter:
    """Sliding-window limiter keyed by client (IP). In-memory; use Redis when scaling out."""

    def __init__(self, per_minute: int):
        self.per_minute = per_minute
        self.hits: dict[str, deque[float]] = defaultdict(deque)

    def check(self, key: str) -> float | None:
        """Returns None if allowed, otherwise seconds until the next request is allowed."""
        if self.per_minute <= 0:
            return None
        now = time.monotonic()
        q = self.hits[key]
        while q and q[0] <= now - 60:
            q.popleft()
        if len(q) >= self.per_minute:
            return round(60 - (now - q[0]), 1)
        q.append(now)
        return None


rate_limiter = RateLimiter(settings.rate_limit_per_minute)


# ------------------------------------------------------------- untrusted content
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def clean_text(s: str | None, limit: int | None = None) -> str:
    """Strip control characters from scraped text."""
    if not s:
        return ""
    s = _CTRL.sub("", s)
    return s[:limit] if limit else s


def safe_filename(name: str, default: str = "output") -> str:
    name = re.sub(r"[^\w\-. ]+", "", name or "").strip().replace(" ", "_")
    name = re.sub(r"_+", "_", name).strip("._")
    return (name or default)[:80]
