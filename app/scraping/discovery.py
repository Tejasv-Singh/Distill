"""robots.txt, sitemap discovery, and URL filtering for crawls."""
from __future__ import annotations

import fnmatch
import gzip
import re
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

from bs4 import BeautifulSoup

from ..config import settings
from .fetcher import Fetcher, FetchError


class Robots:
    def __init__(self, fetcher: Fetcher):
        self.fetcher = fetcher
        self._cache: dict[str, tuple[RobotFileParser | None, list[str], float | None]] = {}

    async def _load(self, url: str):
        p = urlsplit(url)
        root = f"{p.scheme}://{p.netloc}"
        if root not in self._cache:
            rp: RobotFileParser | None = RobotFileParser()
            sitemaps: list[str] = []
            delay = None
            try:
                status, text = await self.fetcher.get_text(root + "/robots.txt", max_bytes=500_000)
                if status >= 400:
                    rp = None
                else:
                    rp.parse(text.splitlines())
                    sitemaps = re.findall(r"(?im)^\s*sitemap:\s*(\S+)", text)
                    delay = rp.crawl_delay(settings.user_agent) or rp.crawl_delay("*")
            except FetchError:
                rp = None
            self._cache[root] = (rp, sitemaps, float(delay) if delay else None)
        return self._cache[root]

    async def allowed(self, url: str) -> bool:
        rp, _, _ = await self._load(url)
        return True if rp is None else rp.can_fetch(settings.user_agent, url)

    async def sitemaps(self, url: str) -> list[str]:
        _, sm, _ = await self._load(url)
        return sm

    async def crawl_delay(self, url: str) -> float | None:
        _, _, d = await self._load(url)
        return d


async def discover_sitemap_urls(fetcher: Fetcher, robots: Robots, start_url: str, limit: int = 500) -> list[str]:
    """URLs listed in the site's sitemap(s): robots.txt Sitemap: lines, then /sitemap.xml."""
    p = urlsplit(start_url)
    root = f"{p.scheme}://{p.netloc}"
    queue = list(dict.fromkeys(await robots.sitemaps(start_url) + [root + "/sitemap.xml", root + "/sitemap_index.xml"]))
    seen_maps: set[str] = set()
    found: list[str] = []
    while queue and len(found) < limit and len(seen_maps) < 15:
        sm = queue.pop(0)
        if sm in seen_maps or urlsplit(sm).netloc != p.netloc:
            continue
        seen_maps.add(sm)
        try:
            _, status, _, body = await fetcher.get(sm, max_bytes=20_000_000)
        except FetchError:
            continue
        if status >= 400 or not body:
            continue
        if body[:2] == b"\x1f\x8b":
            try:
                body = gzip.decompress(body)[:50_000_000]
            except OSError:
                continue
        soup = BeautifulSoup(body, "xml")
        if soup.find("sitemapindex"):
            queue.extend(loc.get_text(strip=True) for loc in soup.select("sitemap > loc"))
            continue
        for loc in soup.select("url > loc"):
            u = loc.get_text(strip=True)
            if u.startswith(("http://", "https://")):
                found.append(u)
                if len(found) >= limit:
                    break
    return list(dict.fromkeys(found))


def _match(pattern: str, url: str) -> bool:
    """Patterns: 're:<regex>', a glob with *, or a plain substring."""
    pattern = pattern.strip()
    if not pattern:
        return False
    if pattern.startswith("re:"):
        try:
            return re.search(pattern[3:], url) is not None
        except re.error:
            return False
    if "*" in pattern or "?" in pattern:
        return fnmatch.fnmatch(url, pattern) or fnmatch.fnmatch(urlsplit(url).path, pattern)
    return pattern in url


def url_passes(url: str, include: list[str], exclude: list[str]) -> bool:
    if include and not any(_match(p, url) for p in include):
        return False
    return not any(_match(p, url) for p in exclude)


_SKIP_EXT = re.compile(r"\.(png|jpe?g|gif|webp|svg|ico|css|js|mjs|json|xml|pdf|zip|gz|tar|rar|7z|exe|dmg|"
                       r"mp3|mp4|webm|mov|avi|woff2?|ttf|eot|docx?|xlsx?|pptx?|csv)(\?|$)", re.I)


def is_crawlable(url: str) -> bool:
    return url.startswith(("http://", "https://")) and not _SKIP_EXT.search(urlsplit(url).path)


_NEXT_TEXT = re.compile(r"^\s*(next|next page|older posts|more|›|»|→|>|>>|next\s*[›»→>])\s*$", re.I)


def find_next_link(html: str, base: str) -> str | None:
    """Detect a pagination "next" link without a selector: rel=next, aria-label, class or text."""
    from urllib.parse import urljoin
    soup = BeautifulSoup(html, "lxml")
    cand = soup.select_one('a[rel~="next"], link[rel~="next"]')
    if not cand:
        for a in soup.find_all("a", href=True):
            label = (a.get("aria-label") or "") + " " + " ".join(a.get("class") or []) + " " + \
                    " ".join((a.parent.get("class") or []) if a.parent else [])
            if re.search(r"\bnext\b", label, re.I) or _NEXT_TEXT.match(a.get_text(" ", strip=True)):
                cand = a
                break
    if cand and cand.get("href") and not cand["href"].startswith(("#", "javascript:")):
        return urljoin(base, cand["href"]).split("#")[0]
    return None
