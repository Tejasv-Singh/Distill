"""The scrape job: connect -> fetch -> extract -> structure -> prepare, with persistence."""
from __future__ import annotations

import asyncio
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from ..config import settings
from ..db import Project, ScrapedPage, ScrapingJob, session
from ..schemas import ScrapeRequest
from ..security import UnsafeURL, resolve_public
from .discovery import Robots, discover_sitemap_urls, is_crawlable, url_passes
from .extractor import extract_document, page_links
from .fetcher import FetchError, Fetcher
from .selectors import extract_rows

STAGES = ["connecting", "fetching", "extracting", "structuring", "preparing"]
STAGE_LABEL = {
    "queued": "Waiting to start",
    "connecting": "Connecting to website",
    "fetching": "Fetching webpage",
    "extracting": "Extracting content",
    "structuring": "Structuring information",
    "preparing": "Preparing output",
    "done": "Done",
    "failed": "Failed",
}


class JobControl:
    """In-memory cancel flags for running jobs."""
    cancelled: set[str] = set()


def _update_job(job_id: str, **kw) -> None:
    with session() as s:
        job = s.get(ScrapingJob, job_id)
        if job:
            for k, v in kw.items():
                setattr(job, k, v)


def _set_project(project_id: str, **kw) -> None:
    with session() as s:
        p = s.get(Project, project_id)
        if p:
            for k, v in kw.items():
                setattr(p, k, v)


async def run_scrape_job(project_id: str, job_id: str, req: ScrapeRequest) -> None:
    fetcher = Fetcher()
    robots = Robots(fetcher)
    errors: list[dict] = []
    saved = 0
    start = time.monotonic()
    first_html: tuple[str, str] | None = None
    crawl = req.crawl
    max_pages = min(crawl.max_pages, settings.max_pages_cap) if req.mode == "crawl" else len(req.urls)
    max_depth = min(crawl.max_depth, settings.max_depth_cap) if req.mode == "crawl" else 0

    def progress(stage: str, frac: float, detail: str = "") -> None:
        _update_job(job_id, stage=stage, progress=round(min(max(frac, 0), 0.99), 3), detail=detail[:500],
                    page_errors=errors[-50:])

    try:
        _update_job(job_id, status="running")
        _set_project(project_id, scraping_status="running")

        # ---- 1. connecting: validate hosts, read robots.txt, discover sitemap
        progress("connecting", 0.02, req.urls[0])
        queue: deque[tuple[str, int]] = deque()
        seen: set[str] = set()
        for u in req.urls:
            try:
                await resolve_public(u)
            except UnsafeURL as e:
                errors.append({"url": u, "error": str(e), "kind": "blocked_url"})
                continue
            queue.append((u, 0))
            seen.add(u)
        if not queue:
            raise FetchError("blocked_url", errors[0]["error"] if errors else "")

        start_host = urlsplit(req.urls[0]).netloc
        if req.mode == "crawl" and crawl.use_sitemap:
            progress("connecting", 0.04, "Reading sitemap")
            for u in await discover_sitemap_urls(fetcher, robots, req.urls[0], limit=max_pages * 5):
                if u not in seen and (not crawl.same_domain or urlsplit(u).netloc == start_host) \
                        and url_passes(u, crawl.include_patterns, crawl.exclude_patterns):
                    seen.add(u)
                    queue.append((u, 1))
        robots_delay = await robots.crawl_delay(req.urls[0]) if crawl.respect_robots else None
        delay = max(crawl.delay_seconds, settings.min_delay if req.mode == "crawl" else 0, robots_delay or 0)

        # ---- 2-4. per page
        position = 0
        last_fetch = 0.0
        attempted = 0
        while queue and attempted < max_pages:
            if job_id in JobControl.cancelled:
                break
            url, depth = queue.popleft()
            attempted += 1
            base = (attempted - 1) / max_pages
            step = 1 / max_pages * 0.9
            progress("fetching", 0.05 + base * 0.9, url)
            try:
                if crawl.respect_robots and not await robots.allowed(url):
                    raise FetchError("robots")
                wait = delay - (time.monotonic() - last_fetch)
                if wait > 0 and last_fetch:
                    await asyncio.sleep(wait)
                last_fetch = time.monotonic()
                page = await fetcher.fetch(url, req.render, req.wait_for_selector, req.scroll_steps)
            except FetchError as e:
                errors.append({"url": url, "error": str(e), "kind": e.kind})
                continue

            progress("extracting", 0.05 + (base + step * 0.5) * 0.9, page.url)
            try:
                doc = await asyncio.to_thread(extract_document, page.html, page.url, page.rendered)
            except Exception as e:  # noqa: BLE001
                errors.append({"url": url, "error": f"Extraction failed: {e}", "kind": "extraction"})
                continue
            if page.headers.get("x-distill-js-error"):
                doc["warnings"] = [f"JavaScript rendering failed, used static HTML instead: "
                                   f"{page.headers['x-distill-js-error']}"]
            rows = []
            if req.data_extraction and req.data_extraction.fields:
                try:
                    rows = extract_rows(page.html, page.url, req.data_extraction.item_selector,
                                        req.data_extraction.fields)
                except ValueError as e:
                    errors.append({"url": url, "error": str(e), "kind": "selector"})
            doc["rows"] = rows

            if doc["stats"]["word_count"] < 5 and not doc["tables"] and not rows:
                js_err = page.headers.get("x-distill-js-error")
                if js_err:   # an empty JS shell and the browser couldn't render it: say why
                    errors.append({"url": page.url, "kind": "js_failed", "error":
                                   "This page builds its content with JavaScript and the headless browser isn't "
                                   "available. Run `python -m playwright install chromium`, then re-scrape."})
                else:
                    errors.append({"url": page.url, "error": FetchError("empty").args[0], "kind": "empty"})
                continue

            progress("structuring", 0.05 + (base + step * 0.8) * 0.9, page.url)
            with session() as s:
                s.add(ScrapedPage(project_id=project_id, position=position, url=page.url,
                                  title=doc["title"][:1000], extracted_content=doc,
                                  page_metadata={"status": page.status, "rendered": page.rendered,
                                                 "site_type": doc["site_type"], "depth": depth,
                                                 "word_count": doc["stats"]["word_count"]}))
            position += 1
            saved += 1
            if first_html is None:
                first_html = (page.html, page.url)
                extra = {"name": doc["title"][:200]} if not req.name and len(req.urls) == 1 and req.mode == "single" else {}
                _set_project(project_id, site_type=doc["site_type"], source_url=page.url, **extra)

            # queue more pages
            if req.mode == "crawl":
                candidates: list[tuple[str, int, bool]] = []
                if crawl.next_page_selector:
                    for u in page_links(page.html, page.url, crawl.next_page_selector)[:1]:
                        candidates.append((u, depth, True))   # pagination doesn't add depth
                if depth < max_depth:
                    candidates += [(u, depth + 1, False)
                                   for u in page_links(page.html, page.url, crawl.link_selector)]
                for u, d, is_next in candidates:
                    if u in seen or not is_crawlable(u):
                        continue
                    if crawl.same_domain and urlsplit(u).netloc != start_host:
                        continue
                    # include patterns filter discovered links, not "next page" hops; exclude always applies
                    if not url_passes(u, [] if is_next else crawl.include_patterns, crawl.exclude_patterns):
                        continue
                    seen.add(u)
                    queue.append((u, d))

        # ---- 5. preparing: screenshot + final status
        progress("preparing", 0.97, "Capturing preview")
        if first_html and settings.take_screenshots and settings.js_rendering:
            shot = settings.storage_dir / project_id / "preview.png"
            shot.parent.mkdir(parents=True, exist_ok=True)
            try:
                await asyncio.wait_for(fetcher.screenshot_html(first_html[0], first_html[1], str(shot)), 25)
                _set_project(project_id, screenshot_path=str(shot))
            except Exception:  # noqa: BLE001  (preview is optional)
                pass

        cancelled = job_id in JobControl.cancelled
        if saved:
            status = "cancelled" if cancelled else "done"
            msg = None
        else:
            status = "cancelled" if cancelled else "failed"
            msg = errors[0]["error"] if errors else "No pages could be scraped."
        _update_job(job_id, status=status, stage="done" if saved else "failed", progress=1.0,
                    detail=f"{saved} page(s) in {time.monotonic() - start:.1f}s", page_errors=errors[-50:],
                    error_message=msg, completed_at=datetime.now(timezone.utc))
        _set_project(project_id, scraping_status="done" if saved else status)
    except FetchError as e:
        _update_job(job_id, status="failed", stage="failed", progress=1.0, error_message=str(e),
                    page_errors=errors[-50:], completed_at=datetime.now(timezone.utc))
        _set_project(project_id, scraping_status="failed")
    except Exception as e:  # noqa: BLE001
        _update_job(job_id, status="failed", stage="failed", progress=1.0,
                    error_message=f"Unexpected error: {type(e).__name__}: {e}",
                    page_errors=errors[-50:], completed_at=datetime.now(timezone.utc))
        _set_project(project_id, scraping_status="failed")
    finally:
        JobControl.cancelled.discard(job_id)
        await fetcher.close()


async def discover_pages(url: str, use_sitemap: bool, limit: int) -> dict:
    """For page selection: list candidate pages from the sitemap and the page's own links."""
    fetcher = Fetcher()
    robots = Robots(fetcher)
    try:
        sitemap = await discover_sitemap_urls(fetcher, robots, url, limit) if use_sitemap else []
        page = await fetcher.fetch_static(url)
        host = urlsplit(page.url).netloc
        links = [u for u in page_links(page.html, page.url)
                 if urlsplit(u).netloc == host and is_crawlable(u)][:limit]
        out = []
        for u in dict.fromkeys(sitemap + links):
            out.append({"url": u, "source": "sitemap" if u in sitemap else "link",
                        "allowed": await robots.allowed(u)})
            if len(out) >= limit:
                break
        return {"start_url": page.url, "pages": out, "sitemap_found": bool(sitemap)}
    finally:
        await fetcher.close()


def preview_path(project_id: str) -> Path:
    return settings.storage_dir / project_id / "preview.png"
