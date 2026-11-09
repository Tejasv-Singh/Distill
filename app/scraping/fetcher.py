"""Fetching pages safely: static HTTP with validated redirects + DNS pinning, and a
guarded headless browser for JavaScript-rendered sites."""
from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from ..config import settings
from ..security import UnsafeURL, is_allowlisted, resolve_public

_USING_PROXY = bool(os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY")
                    or os.getenv("https_proxy") or os.getenv("http_proxy"))

FRIENDLY = {
    "invalid_url": "That URL isn't valid.",
    "blocked_url": "That address can't be scraped.",
    "dns": "The website's address couldn't be found. Check the URL for typos.",
    "timeout": "The website took too long to respond.",
    "connection": "Couldn't connect to the website. It may be down.",
    "access_denied": "The website refused access (it may require a login).",
    "not_found": "The page doesn't exist (404).",
    "rate_limited": "The website is rate-limiting requests. Try again in a few minutes.",
    "server_error": "The website returned a server error.",
    "bot_protection": "The website is protected by an anti-bot check (e.g. Cloudflare or a CAPTCHA). "
                      "Distill doesn't bypass these.",
    "too_large": "The page is larger than the size limit.",
    "not_html": "That URL isn't a web page.",
    "js_failed": "The page needed JavaScript and the browser couldn't render it.",
    "robots": "The site's robots.txt asks crawlers not to visit this page.",
    "empty": "The page loaded but had no readable content.",
}


class FetchError(Exception):
    def __init__(self, kind: str, detail: str = "", status: int | None = None):
        self.kind, self.detail, self.status = kind, detail, status
        msg = FRIENDLY.get(kind, "Fetching failed.")
        super().__init__(f"{msg} {detail}".strip())


@dataclass
class Page:
    url: str
    status: int
    html: str
    headers: dict = field(default_factory=dict)
    rendered: bool = False


_BOT_MARKERS = re.compile(
    r"(Just a moment\.\.\.|cf-chl-|challenge-platform|Attention Required! \| Cloudflare|"
    r"captcha-delivery|g-recaptcha|hcaptcha\.com/1/api|px-captcha|"
    r"Please verify you are a human|Access to this page has been denied)",
    re.I,
)


def detect_bot_wall(status: int, headers: dict, html: str) -> bool:
    if headers.get("cf-mitigated", "").lower() == "challenge":
        return True
    if status in (403, 429, 503) and _BOT_MARKERS.search(html[:20000]):
        return True
    # tiny challenge pages that return 200
    return len(html) < 15000 and bool(_BOT_MARKERS.search(html))


def looks_like_js_shell(html: str) -> bool:
    body = re.sub(r"(?is)<(script|style|noscript|template)[^>]*>.*?</\1>", " ", html)
    words = len(re.sub(r"(?s)<[^>]+>", " ", body).split())
    markers = ('id="root"', 'id="app"', "id='root'", "__NEXT_DATA__", "ng-version", "data-reactroot", 'id="__nuxt"')
    return words < 80 or (words < 250 and any(m in html for m in markers))


def _status_error(status: int) -> FetchError | None:
    if status in (401, 403):
        return FetchError("access_denied", f"(HTTP {status})", status)
    if status == 404 or status == 410:
        return FetchError("not_found", status=status)
    if status == 429:
        return FetchError("rate_limited", status=status)
    if status >= 500:
        return FetchError("server_error", f"(HTTP {status})", status)
    if status >= 400:
        return FetchError("connection", f"(HTTP {status})", status)
    return None


class Fetcher:
    """One per scrape job: owns an HTTP client and (lazily) a browser."""

    def __init__(self) -> None:
        self.client = httpx.AsyncClient(
            headers={"User-Agent": settings.user_agent,
                     "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.5",
                     "Accept-Language": "en;q=0.9,*;q=0.5"},
            follow_redirects=False,
            timeout=settings.request_timeout,
        )
        self._pw = None
        self._browser = None
        self._lock = asyncio.Lock()
        self.browser_error: str | None = None

    async def close(self) -> None:
        await self.client.aclose()
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()

    # ------------------------------------------------------------ static HTTP
    async def _one_request(self, url: str, max_bytes: int) -> tuple[httpx.Response, bytes]:
        """One hop, connecting to a pre-validated IP (DNS pinning) so the name can't
        be re-resolved to an internal address between the check and the connection."""
        ips = await resolve_public(url)
        req_url, headers, ext = url, {}, {}
        if ips and not _USING_PROXY:
            p = urlsplit(url)
            ip = ips[0]
            host_ip = f"[{ip}]" if ":" in ip else ip
            netloc = host_ip + (f":{p.port}" if p.port else "")
            req_url = urlunsplit((p.scheme, netloc, p.path or "/", p.query, ""))
            headers["Host"] = p.netloc
            if p.scheme == "https":
                ext["sni_hostname"] = p.hostname
        req = self.client.build_request("GET", req_url, headers=headers, extensions=ext)
        resp = await self.client.send(req, stream=True)
        try:
            chunks, size = [], 0
            if resp.status_code < 300 or resp.status_code >= 400:
                async for chunk in resp.aiter_bytes():
                    size += len(chunk)
                    if size > max_bytes:
                        raise FetchError("too_large", f"(limit {max_bytes // 1_000_000} MB)")
                    chunks.append(chunk)
            return resp, b"".join(chunks)
        finally:
            await resp.aclose()

    async def get(self, url: str, max_bytes: int | None = None) -> tuple[str, int, dict, bytes]:
        """GET with manual redirect following; every hop is re-validated."""
        max_bytes = max_bytes or settings.max_page_bytes
        current = url
        for _ in range(settings.max_redirects + 1):
            try:
                resp, body = await self._one_request(current, max_bytes)
            except UnsafeURL as e:
                kind = "dns" if "resolve" in str(e) else "blocked_url"
                raise FetchError(kind, str(e) if kind == "blocked_url" else "") from e
            except httpx.TimeoutException as e:
                raise FetchError("timeout") from e
            except httpx.HTTPError as e:
                raise FetchError("connection", type(e).__name__) from e
            if resp.status_code in (301, 302, 303, 307, 308) and resp.headers.get("location"):
                current = urljoin(current, resp.headers["location"])
                continue
            return current, resp.status_code, {k.lower(): v for k, v in resp.headers.items()}, body
        raise FetchError("connection", "(too many redirects)")

    async def get_text(self, url: str, max_bytes: int = 2_000_000) -> tuple[int, str]:
        _, status, headers, body = await self.get(url, max_bytes)
        return status, body.decode(_charset(headers) or "utf-8", errors="replace")

    async def fetch_static(self, url: str) -> Page:
        try:
            final, status, headers, body = await self.get(url)
        except FetchError as e:
            # like a browser: if https can't connect at all, try plain http once
            if e.kind != "connection" or not url.startswith("https://"):
                raise
            final, status, headers, body = await self.get("http://" + url[len("https://"):])
        ctype = headers.get("content-type", "").lower()
        html = body.decode(_charset(headers) or _sniff_charset(body) or "utf-8", errors="replace")
        if detect_bot_wall(status, headers, html):
            raise FetchError("bot_protection", status=status)
        err = _status_error(status)
        if err:
            raise err
        if ctype and not any(t in ctype for t in ("html", "xml", "text/plain")):
            raise FetchError("not_html", f"(content-type: {ctype.split(';')[0]})")
        return Page(final, status, html, headers)

    # ---------------------------------------------------------- headless browser
    async def _ensure_browser(self):
        async with self._lock:
            if self._browser:
                return self._browser
            if self.browser_error:
                raise FetchError("js_failed", self.browser_error)
            try:
                from playwright.async_api import async_playwright
            except ImportError:
                self.browser_error = "(Playwright is not installed)"
                raise FetchError("js_failed", self.browser_error) from None
            self._pw = await async_playwright().start()
            errors = []
            channels = [settings.browser_channel] if settings.browser_channel else [None, "chrome"]
            for ch in channels:
                try:
                    self._browser = await self._pw.chromium.launch(
                        headless=True, channel=ch,
                        args=["--disable-dev-shm-usage", "--no-first-run", "--disable-extensions"])
                    return self._browser
                except Exception as e:  # noqa: BLE001
                    errors.append(str(e).splitlines()[0][:150])
            self.browser_error = "(no browser available; run `playwright install chromium`)"
            raise FetchError("js_failed", self.browser_error)

    async def _guard_route(self, route) -> None:  # noqa: ANN001
        """Every request the browser makes (documents, XHR, images, ...) is validated."""
        req = route.request
        url = req.url
        if url.startswith(("data:", "blob:")):
            return await route.continue_()
        if req.resource_type in ("media", "font", "websocket", "eventsource"):
            return await route.abort()
        try:
            await resolve_public(url)
        except UnsafeURL:
            return await route.abort("blockedbyclient")
        await route.continue_()

    async def new_guarded_page(self):
        browser = await self._ensure_browser()
        ctx = await browser.new_context(
            user_agent=settings.user_agent, viewport={"width": 1280, "height": 900},
            java_script_enabled=True, accept_downloads=False, service_workers="block")
        await ctx.route("**/*", self._guard_route)
        return ctx, await ctx.new_page()

    async def fetch_js(self, url: str, wait_for: str | None = None, scrolls: int = 0) -> Page:
        ctx, page = await self.new_guarded_page()
        try:
            try:
                resp = await page.goto(url, wait_until="domcontentloaded",
                                       timeout=settings.request_timeout * 1000)
            except Exception as e:  # noqa: BLE001
                msg = str(e).splitlines()[0]
                if "Timeout" in msg:
                    raise FetchError("timeout") from e
                raise FetchError("js_failed", f"({msg[:160]})") from e
            try:
                await page.wait_for_load_state("networkidle", timeout=6000)
            except Exception:  # noqa: BLE001 - some pages never go idle
                pass
            if wait_for:
                try:
                    await page.wait_for_selector(wait_for, timeout=settings.request_timeout * 1000)
                except Exception as e:  # noqa: BLE001
                    raise FetchError("js_failed", f"(selector '{wait_for}' never appeared)") from e
            for _ in range(max(0, min(scrolls, 30))):
                await page.mouse.wheel(0, 20000)
                await page.wait_for_timeout(900)
            # the final URL (after in-browser redirects) must also be public
            try:
                await resolve_public(page.url)
            except UnsafeURL as e:
                raise FetchError("blocked_url", str(e)) from e
            html = await page.content()
            status = resp.status if resp else 200
            headers = {k.lower(): v for k, v in (resp.headers if resp else {}).items()}
            if detect_bot_wall(status, headers, html):
                raise FetchError("bot_protection", status=status)
            err = _status_error(status)
            if err:
                raise err
            return Page(page.url, status, html, headers, rendered=True)
        finally:
            await ctx.close()

    async def screenshot_html(self, html: str, base_url: str) -> bytes:
        """Screenshot already-fetched HTML (used for the project preview). Returns PNG bytes;
        nothing is written to disk."""
        ctx, page = await self.new_guarded_page()
        try:
            await page.set_content(_with_base(html, base_url), wait_until="domcontentloaded", timeout=15000)
            await page.wait_for_timeout(500)
            return await page.screenshot(full_page=False, type="png")
        finally:
            await ctx.close()

    async def fetch(self, url: str, mode: str = "auto", wait_for: str | None = None,
                    scrolls: int = 0) -> Page:
        if mode == "js":
            # follow + validate redirects on the static side first, then render the final URL,
            # so the browser never follows a redirect chain we haven't checked
            final, *_ = await self.get(url)
            return await self.fetch_js(final, wait_for, scrolls)
        page = await self.fetch_static(url)
        if mode == "auto" and settings.js_rendering and (looks_like_js_shell(page.html) or wait_for or scrolls):
            try:
                return await self.fetch_js(page.url, wait_for, scrolls)
            except FetchError as e:
                if e.kind in ("bot_protection", "blocked_url"):
                    raise
                page.headers["x-distill-js-error"] = str(e)
                return page
        return page


def _charset(headers: dict) -> str | None:
    m = re.search(r"charset=([\w-]+)", headers.get("content-type", ""), re.I)
    return m.group(1) if m else None


def _sniff_charset(body: bytes) -> str | None:
    m = re.search(rb'<meta[^>]+charset=["\']?([\w-]+)', body[:4096], re.I)
    return m.group(1).decode("ascii", "ignore") if m else None


def _with_base(html: str, base_url: str) -> str:
    base = f'<base href="{base_url}">'
    if re.search(r"<head[^>]*>", html, re.I):
        return re.sub(r"(<head[^>]*>)", r"\1" + base.replace("\\", "\\\\"), html, count=1, flags=re.I)
    return base + html
