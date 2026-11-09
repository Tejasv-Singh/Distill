"""Distill: FastAPI application."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles

from . import generation
from .ai.providers import AIError, ai_status
from .config import BASE_DIR, settings
from .db import ChatSession, GeneratedOutput, Project, ScrapedPage, ScrapingJob, ensure_db, local_user_id, session
from .exporters.render import render_html
from .schemas import (AI_TYPES, CONVERT_FORMATS, ChatMessageIn, DiscoverRequest, GenerateRequest, OutputEdit, ProjectPatch,
                      RegenerateRequest, RenderRequest, ScrapeRequest)
from .scraping.extractor import document_text
from .scraping.fetcher import FetchError
from .scraping.pipeline import STAGE_LABEL, JobControl, discover_pages, run_scrape_job
from .security import rate_limiter, safe_filename
from .storage import get_storage

log = logging.getLogger("distill")

STATIC = Path(__file__).parent / "static"
_tasks: set[asyncio.Task] = set()
_scrape_sem: asyncio.Semaphore | None = None


def _get_scrape_sem() -> asyncio.Semaphore:
    global _scrape_sem
    if _scrape_sem is None:
        _scrape_sem = asyncio.Semaphore(max(1, settings.max_concurrent_jobs))
    return _scrape_sem


async def _dispatch(coro) -> None:  # noqa: ANN001
    """Run a job. JOB_MODE=background: after the response (long-lived servers).
    JOB_MODE=inline: before the response, because a serverless instance can be frozen as soon
    as the response is sent and an orphaned task would never finish."""
    if settings.job_mode == "inline":
        await coro
        return
    t = asyncio.create_task(coro)
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)


def reap_interrupted(only_stale: bool) -> int:
    """Mark jobs that can no longer finish as failed so the UI never spins forever.
    only_stale=False (process start on a long-lived server): everything still "running" died with
    the old process. only_stale=True (serverless, any instance): only work older than
    STALE_JOB_MINUTES, because other instances may be running the rest right now."""
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=settings.stale_job_minutes)
    msg = ("Interrupted by a server restart." if not only_stale else
           "Interrupted: it ran longer than the time limit or the server stopped.")
    n = 0
    with session() as s:
        q = s.query(ScrapingJob).filter(ScrapingJob.status.in_(["queued", "running"]))
        if only_stale:
            q = q.filter(ScrapingJob.started_at < cutoff)
        for j in q:
            j.status, j.stage, j.error_message, j.completed_at = "failed", "failed", msg + " Retry the scrape.", now
            p = s.get(Project, j.project_id)
            if p and p.scraping_status in ("queued", "running"):
                p.scraping_status = "done" if p.pages else "failed"
            n += 1
        q = s.query(GeneratedOutput).filter(GeneratedOutput.status.in_(["queued", "running"]))
        if only_stale:
            q = q.filter(GeneratedOutput.updated_at < cutoff)
        for o in q:
            o.status, o.stage, o.error_message = "failed", "Failed", msg + " Regenerate it."
            n += 1
        q = s.query(ChatSession).filter(ChatSession.status == "running")
        if only_stale:
            q = q.filter(ChatSession.updated_at < cutoff)
        for c in q:
            c.status, c.status_detail = "idle", ""
            c.messages = list(c.messages or []) + [{"role": "assistant", "content": "", "tool_calls": [],
                                                    "error": msg + " Send your message again."}]
            n += 1
    return n


_last_reap = 0.0


def _maybe_reap() -> None:
    global _last_reap
    if settings.serverless and time.monotonic() - _last_reap > 30:
        _last_reap = time.monotonic()
        try:
            reap_interrupted(only_stale=True)
        except Exception:  # noqa: BLE001  (housekeeping must never break a request)
            log.exception("stale-job cleanup failed")


@asynccontextmanager
async def lifespan(_: FastAPI):
    problems = settings.deployment_problems()
    for p in problems:
        log.error("Deployment configuration problem: %s", p)
    if not problems:
        try:
            ensure_db()
            reap_interrupted(only_stale=settings.serverless)
        except Exception:  # noqa: BLE001  (reported per request by the readiness guard)
            log.exception("Database initialisation failed")
    yield
    for t in list(_tasks):
        t.cancel()


app = FastAPI(title="Distill API", version="2.0.0", lifespan=lifespan,
              description="Scrape websites into a structured document, then turn it into any format.")


_OPEN_PATHS = {"/api/health", "/api/config"}


@app.middleware("http")
async def readiness(request: Request, call_next):  # noqa: ANN001, ANN201
    """Refuse API work with a clear message when the deployment can't store data, and make
    sure the database is initialised even if the platform skipped the lifespan hook."""
    path = request.url.path
    if path.startswith("/api/") and path not in _OPEN_PATHS:
        problems = settings.deployment_problems()
        if problems:
            return JSONResponse({"detail": " ".join(problems), "kind": "deployment"}, status_code=503)
        try:
            await asyncio.to_thread(ensure_db)
        except Exception as e:  # noqa: BLE001
            log.exception("database unavailable")
            return JSONResponse({"detail": f"The database is unavailable: {type(e).__name__}. Check DATABASE_URL.",
                                 "kind": "database"}, status_code=503)
        if request.method == "GET":
            await asyncio.to_thread(_maybe_reap)
    return await call_next(request)


@app.middleware("http")
async def security_headers(request: Request, call_next):  # noqa: ANN001, ANN201
    resp = await call_next(request)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    if not request.url.path.startswith("/api/"):
        # always revalidate the UI so an updated app.js / index.html is picked up immediately
        resp.headers.setdefault("Cache-Control", "no-cache")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    if not request.url.path.endswith("/preview.html"):
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' https: http: data:; frame-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'")
    return resp


@app.exception_handler(AIError)
async def _ai_err(_: Request, e: AIError):  # noqa: ANN202
    return JSONResponse({"detail": str(e), "kind": e.kind}, status_code=503 if e.kind == "not_configured" else 502)


def rate_limit(request: Request) -> None:
    key = request.client.host if request.client else "local"
    wait = rate_limiter.check(key)
    if wait is not None:
        raise HTTPException(429, f"Too many requests. Try again in {wait:.0f}s.", headers={"Retry-After": str(int(wait) + 1)})


# ----------------------------------------------------------------------- helpers
def _iso(d: datetime | None) -> str | None:
    if d is None:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.isoformat()


def _job_json(j: ScrapingJob | None) -> dict | None:
    if not j:
        return None
    return {"id": j.id, "status": j.status, "stage": j.stage, "stage_label": STAGE_LABEL.get(j.stage, j.stage),
            "progress": j.progress, "detail": j.detail, "errors": j.page_errors or [],
            "error_message": j.error_message, "started_at": _iso(j.started_at), "completed_at": _iso(j.completed_at)}


def _output_json(o: GeneratedOutput, content: bool = False) -> dict:
    d = {"id": o.id, "project_id": o.project_id, "kind": o.kind, "format": o.format,
         "file_format": generation.file_format(o), "title": o.title, "status": o.status, "stage": o.stage,
         "warnings": o.warnings or [], "error_message": o.error_message, "configuration": o.configuration,
         "editable": o.kind != "extract" and generation.file_format(o) in generation.DOC_FORMATS,
         "has_file": bool(o.file_path) and o.status == "done",
         "created_at": _iso(o.created_at), "updated_at": _iso(o.updated_at)}
    if content:
        d["content"] = o.generated_content
    return d


def _page_summary(p: ScrapedPage) -> dict:
    d = p.extracted_content
    return {"id": p.id, "url": p.url, "title": p.title, "site_type": d.get("site_type"),
            "stats": d.get("stats", {}), "author": d.get("author"), "published_date": d.get("published_date"),
            "description": d.get("description"), "rendered": d.get("rendered_with_browser"),
            "extractor": d.get("extractor"), "rows": len(d.get("rows") or []), "warnings": d.get("warnings", [])}


def _project_json(p: Project, detail: bool = False) -> dict:
    pages = p.pages
    words = sum((pg.extracted_content.get("stats") or {}).get("word_count", 0) for pg in pages)
    d = {"id": p.id, "name": p.name, "source_url": p.source_url, "status": p.scraping_status,
         "site_type": p.site_type, "page_count": len(pages), "word_count": words,
         "output_count": len(p.outputs), "has_preview": bool(p.screenshot_path),
         "created_at": _iso(p.created_at), "updated_at": _iso(p.updated_at),
         "job": _job_json(p.jobs[0] if p.jobs else None)}
    if detail:
        d["config"] = p.config
        d["pages"] = [_page_summary(pg) for pg in pages]
        d["outputs"] = [_output_json(o) for o in p.outputs]
        d["totals"] = {k: sum((pg.extracted_content.get("stats") or {}).get(k, 0) for pg in pages)
                       for k in ("word_count", "headings", "paragraphs", "tables", "images", "links")}
    return d


def _get_project(s, pid: str) -> Project:  # noqa: ANN001
    p = s.get(Project, pid)
    if not p:
        raise HTTPException(404, "Project not found.")
    return p


def _get_output(s, oid: str) -> GeneratedOutput:  # noqa: ANN001
    o = s.get(GeneratedOutput, oid)
    if not o:
        raise HTTPException(404, "Output not found.")
    return o


async def _run_scrape(pid: str, jid: str, req: ScrapeRequest) -> None:
    async with _get_scrape_sem():
        await run_scrape_job(pid, jid, req)


def _new_job(s, project: Project) -> ScrapingJob:  # noqa: ANN001
    """Create the job row; the caller commits, then dispatches _run_scrape."""
    job = ScrapingJob(project_id=project.id, status="queued", stage="queued")
    s.add(job)
    project.scraping_status = "queued"
    s.flush()
    return job


# ---------------------------------------------------------------------- meta
def _deployment_info() -> dict:
    return {"platform": "vercel" if settings.on_vercel else ("serverless" if settings.serverless else "server"),
            "job_mode": settings.job_mode, "storage_backend": settings.storage_backend,
            "database": "sqlite" if settings.database_is_sqlite else "postgresql",
            "persistent": settings.database_persistent,
            "problems": settings.deployment_problems(), "warnings": settings.deployment_warnings()}


@app.get("/api/health")
def health() -> JSONResponse:
    info = _deployment_info()
    db_ok, db_error = True, None
    if not info["problems"]:
        try:
            ensure_db()
            with session() as s:
                s.query(Project.id).limit(1).all()
        except Exception as e:  # noqa: BLE001
            db_ok, db_error = False, type(e).__name__
    ok = db_ok and not info["problems"]
    return JSONResponse({"ok": ok, "time": _iso(datetime.now(timezone.utc)), "database_ok": db_ok,
                         "database_error": db_error, **info}, status_code=200 if ok else 503)


@app.get("/api/config")
def config() -> dict:
    return {"ai": ai_status(), "convert_formats": CONVERT_FORMATS, "ai_types": AI_TYPES,
            "limits": {"max_pages": settings.max_pages_cap, "max_depth": settings.max_depth_cap,
                       "rate_limit_per_minute": settings.rate_limit_per_minute},
            "js_rendering": settings.js_rendering, "deployment": _deployment_info()}


# ------------------------------------------------------------------ projects
@app.post("/api/projects", status_code=201, dependencies=[Depends(rate_limit)])
async def create_project(req: ScrapeRequest) -> dict:
    name = req.name or req.urls[0].split("://", 1)[1].rstrip("/")[:120]
    if len(req.urls) > 1 and not req.name:
        name += f" +{len(req.urls) - 1}"
    uid = local_user_id()
    with session() as s:
        p = Project(user_id=uid, name=name, source_url=req.urls[0],
                    config=json.loads(req.model_dump_json()), scraping_status="queued")
        s.add(p)
        s.flush()
        job = _new_job(s, p)
        pid, jid = p.id, job.id
    await _dispatch(_run_scrape(pid, jid, req))
    return {"project_id": pid, "job_id": jid}


@app.post("/api/discover", dependencies=[Depends(rate_limit)])
async def discover(req: DiscoverRequest) -> dict:
    try:
        return await discover_pages(req.url, req.use_sitemap, req.limit)
    except FetchError as e:
        raise HTTPException(422, str(e)) from e


@app.get("/api/projects")
def list_projects(q: str | None = None, limit: int = 100) -> list[dict]:
    with session() as s:
        query = s.query(Project).order_by(Project.created_at.desc())
        if q:
            like = f"%{q[:100]}%"
            query = query.filter((Project.name.ilike(like)) | (Project.source_url.ilike(like)))
        return [_project_json(p) for p in query.limit(min(limit, 500)).all()]


@app.get("/api/projects/{pid}")
def get_project(pid: str) -> dict:
    with session() as s:
        return _project_json(_get_project(s, pid), detail=True)


@app.patch("/api/projects/{pid}")
def rename_project(pid: str, body: ProjectPatch) -> dict:
    with session() as s:
        p = _get_project(s, pid)
        p.name = body.name.strip()
        return {"id": p.id, "name": p.name}


@app.delete("/api/projects/{pid}", status_code=204)
def delete_project(pid: str) -> Response:
    with session() as s:
        p = _get_project(s, pid)
        for j in p.jobs:
            JobControl.cancelled.add(j.id)
        s.delete(p)
    get_storage().delete_prefix(pid)
    return Response(status_code=204)


@app.post("/api/projects/{pid}/rescrape", dependencies=[Depends(rate_limit)])
async def rescrape(pid: str) -> dict:
    with session() as s:
        p = _get_project(s, pid)
        if p.scraping_status in ("queued", "running"):
            raise HTTPException(409, "A scrape is already running for this project.")
        req = ScrapeRequest(**p.config)
        for pg in list(p.pages):
            s.delete(pg)
        job = _new_job(s, p)
        jid = job.id
    await _dispatch(_run_scrape(pid, jid, req))
    return {"project_id": pid, "job_id": jid}


@app.post("/api/projects/{pid}/cancel")
def cancel(pid: str) -> dict:
    with session() as s:
        p = _get_project(s, pid)
        for j in p.jobs:
            if j.status in ("queued", "running"):
                JobControl.cancelled.add(j.id)
    return {"ok": True}


@app.get("/api/projects/{pid}/pages/{page_id}")
def get_page(pid: str, page_id: str) -> dict:
    with session() as s:
        pg = s.get(ScrapedPage, page_id)
        if not pg or pg.project_id != pid:
            raise HTTPException(404, "Page not found.")
        return {"id": pg.id, "url": pg.url, "document": pg.extracted_content}


@app.get("/api/projects/{pid}/text", response_class=PlainTextResponse)
def project_text(pid: str) -> str:
    with session() as s:
        p = _get_project(s, pid)
        parts = [f"{pg.title}\n{pg.url}\n\n{document_text(pg.extracted_content)}" for pg in p.pages]
    return "\n\n" + ("\n\n" + "=" * 60 + "\n\n").join(parts)


@app.get("/api/projects/{pid}/preview.png")
def project_preview(pid: str) -> Response:
    with session() as s:
        p = _get_project(s, pid)
        key = p.screenshot_path
    data = get_storage().get(key) if key else None
    if not data:
        raise HTTPException(404, "No preview available.")
    return Response(data, media_type="image/png", headers={"Cache-Control": "private, max-age=3600"})


# ------------------------------------------------------------------- outputs
@app.post("/api/projects/{pid}/outputs", status_code=201, dependencies=[Depends(rate_limit)])
async def create_outputs(pid: str, req: GenerateRequest) -> list[dict]:
    with session() as s:
        p = _get_project(s, pid)
        if not p.pages:
            raise HTTPException(409, "Nothing has been scraped yet.")
        if any(it.kind == "ai" for it in req.items) and not ai_status()["configured"]:
            raise HTTPException(503, AIError("not_configured").args[0])
        if any(it.kind == "ai" and it.format == "custom" for it in req.items) and not (req.options.instructions or "").strip():
            raise HTTPException(422, "Describe what you want for the custom output.")
        created = []
        for it in req.items:
            cfg = json.loads(req.options.model_dump_json())
            if it.kind == "ai":
                cfg["download_format"] = it.download_format or "markdown"
            o = GeneratedOutput(project_id=pid, kind=it.kind, format=it.format, configuration=cfg,
                                status="queued", stage="Queued")
            s.add(o)
            s.flush()
            created.append(o)
        ids = [o.id for o in created]
    if settings.job_mode == "inline":
        await asyncio.gather(*(generation.run_output(i) for i in ids))
    else:
        for i in ids:
            await _dispatch(generation.run_output(i))
    with session() as s:
        return [_output_json(_get_output(s, i)) for i in ids]


@app.get("/api/outputs")
def list_outputs(limit: int = 200) -> list[dict]:
    with session() as s:
        rows = s.query(GeneratedOutput, Project.name).join(Project).order_by(GeneratedOutput.created_at.desc()).limit(min(limit, 1000))
        return [{**_output_json(o), "project_name": name} for o, name in rows]


@app.get("/api/outputs/{oid}")
def get_output(oid: str) -> dict:
    with session() as s:
        return _output_json(_get_output(s, oid), content=True)


@app.put("/api/outputs/{oid}")
async def edit_output(oid: str, body: OutputEdit) -> dict:
    with session() as s:
        o = _get_output(s, oid)
        if o.kind == "extract" or generation.file_format(o) not in generation.DOC_FORMATS:
            raise HTTPException(400, "Data exports (CSV, XLSX, JSON, XML) can't be edited. Regenerate them with new options instead.")
        if o.status != "done":
            raise HTTPException(409, "Wait until generation finishes.")
        o.generated_content = body.content
        if body.title:
            o.title = body.title
    try:
        await generation.rerender(oid)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"Saved, but the file couldn't be rebuilt: {e}") from e
    with session() as s:
        return _output_json(_get_output(s, oid), content=True)


@app.post("/api/outputs/{oid}/render", status_code=201)
async def render_as(oid: str, body: RenderRequest) -> dict:
    """Same content, another document format (a new output, so the original is kept)."""
    with session() as s:
        o = _get_output(s, oid)
        if o.kind == "extract" or generation.file_format(o) not in generation.DOC_FORMATS or o.status != "done":
            raise HTTPException(400, "Only finished document outputs can be converted to another format.")
        cfg = dict(o.configuration)
        if o.kind == "ai":
            cfg["download_format"] = body.format
            new = GeneratedOutput(project_id=o.project_id, kind="ai", format=o.format, configuration=cfg,
                                  generated_content=o.generated_content, title=o.title, warnings=o.warnings,
                                  status="running", stage="Rendering")
        else:
            new = GeneratedOutput(project_id=o.project_id, kind="convert", format=body.format, configuration=cfg,
                                  generated_content=o.generated_content, title=o.title, status="running", stage="Rendering")
        s.add(new)
        s.flush()
        nid = new.id

    async def _do() -> None:
        try:
            await generation.rerender(nid)
            generation._set(nid, status="done", stage="Done")
        except Exception as e:  # noqa: BLE001
            generation._set(nid, status="failed", stage="Failed", error_message=f"Rendering failed: {e}")
    await _dispatch(_do())
    with session() as s:
        return _output_json(_get_output(s, nid))


@app.post("/api/outputs/{oid}/regenerate", dependencies=[Depends(rate_limit)])
async def regenerate(oid: str, body: RegenerateRequest) -> dict:
    with session() as s:
        o = _get_output(s, oid)
        if o.status in ("queued", "running"):
            raise HTTPException(409, "This output is still being generated.")
        if o.kind in ("ai", "extract") and not ai_status()["configured"]:
            raise HTTPException(503, AIError("not_configured").args[0])
        if body.section and o.kind != "ai":
            raise HTTPException(400, "Section rewrites are only available for AI outputs.")
        o.status, o.stage = "queued", "Queued"
    await _dispatch(generation.regenerate(oid, body.section, body.instructions))
    with session() as s:
        return _output_json(_get_output(s, oid))


@app.get("/api/outputs/{oid}/download")
def download(oid: str, format: str | None = None) -> Response:
    with session() as s:
        o = _get_output(s, oid)
        if o.kind == "extract" and format and format != o.format:
            from .ai.extract import TABLE_FORMATS_EXT, table_to_file
            if format not in TABLE_FORMATS_EXT:
                raise HTTPException(400, f"Tables download as {', '.join(TABLE_FORMATS_EXT)}.")
            table = json.loads(o.generated_content or '{"columns": [], "rows": []}')
            name = safe_filename(o.title or "extraction") + "." + generation.EXT[format]
            return Response(table_to_file(table, format, o.title), media_type=generation.MIME[format],
                            headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"})
        key = o.file_path
        fmt = generation.file_format(o)
        name = safe_filename(o.title or o.format) + "." + generation.EXT[fmt]
    data = get_storage().get(key) if key else None
    if data is None:
        raise HTTPException(404, "The file isn't ready (or it is no longer stored). Regenerate it.")
    return Response(data, media_type=generation.MIME[fmt],
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"})


@app.get("/api/outputs/{oid}/preview.html")
def preview_html(oid: str) -> Response:
    """Rendered preview for a sandboxed iframe; no scripts, no forms."""
    with session() as s:
        o = _get_output(s, oid)
        md, opts = o.generated_content or "", o.configuration or {}
        if o.kind == "extract" or generation.file_format(o) not in generation.DOC_FORMATS:
            raise HTTPException(400, "No document preview for data exports.")
    html = render_html(md, opts)
    return Response(html, media_type="text/html; charset=utf-8", headers={
        "Content-Security-Policy": "default-src 'none'; img-src https: http: data:; style-src 'unsafe-inline'; "
                                   "frame-ancestors 'self'; base-uri 'none'; form-action 'none'",
        "X-Frame-Options": "SAMEORIGIN"})


@app.delete("/api/outputs/{oid}", status_code=204)
def delete_output(oid: str) -> Response:
    with session() as s:
        o = _get_output(s, oid)
        key = o.file_path
        s.delete(o)
    if key:
        get_storage().delete(key)
    return Response(status_code=204)


# ---------------------------------------------------------------------- chat
def _chat_json(c: ChatSession, full: bool = True) -> dict:
    d = {"id": c.id, "title": c.title, "status": c.status, "status_detail": c.status_detail,
         "created_at": _iso(c.created_at), "updated_at": _iso(c.updated_at)}
    if full:
        out = []
        for m in c.messages or []:
            if m["role"] == "user":
                out.append({"role": "user", "content": m["content"]})
            elif m["role"] == "assistant":
                if m.get("error"):
                    out.append({"role": "error", "content": m["error"]})
                elif m.get("content"):
                    out.append({"role": "assistant", "content": m["content"]})
                for tc in m.get("tool_calls") or []:
                    out.append({"role": "step", "tool": tc["name"], "id": tc["id"], "display": None})
            elif m["role"] == "tool":
                for item in reversed(out):
                    if item["role"] == "step" and item["id"] == m["tool_call_id"]:
                        item["display"] = m.get("display")
                        break
        d["messages"] = out
    return d


async def _run_agent(chat_id: str) -> None:
    from .ai.agent import Agent
    await Agent(chat_id).run()


@app.get("/api/chats")
def list_chats() -> list[dict]:
    with session() as s:
        return [_chat_json(c, full=False) for c in s.query(ChatSession).order_by(ChatSession.updated_at.desc()).limit(100)]


@app.get("/api/chats/{cid}")
def get_chat(cid: str) -> dict:
    with session() as s:
        c = s.get(ChatSession, cid)
        if not c:
            raise HTTPException(404, "Chat not found.")
        return _chat_json(c)


@app.post("/api/chats", status_code=201, dependencies=[Depends(rate_limit)])
async def create_chat(body: ChatMessageIn) -> dict:
    from .ai.agent import chat_title
    if not ai_status()["configured"]:
        raise HTTPException(503, AIError("not_configured").args[0])
    uid = local_user_id()
    with session() as s:
        c = ChatSession(user_id=uid, title=chat_title(body.message),
                        messages=[{"role": "user", "content": body.message}], status="running",
                        status_detail="Thinking…")
        s.add(c)
        s.flush()
        cid = c.id
    await _dispatch(_run_agent(cid))
    with session() as s:
        return _chat_json(s.get(ChatSession, cid))


@app.post("/api/chats/{cid}/messages", dependencies=[Depends(rate_limit)])
async def send_message(cid: str, body: ChatMessageIn) -> dict:
    if not ai_status()["configured"]:
        raise HTTPException(503, AIError("not_configured").args[0])
    with session() as s:
        c = s.get(ChatSession, cid)
        if not c:
            raise HTTPException(404, "Chat not found.")
        if c.status == "running":
            raise HTTPException(409, "The assistant is still working on your last message.")
        c.messages = list(c.messages or []) + [{"role": "user", "content": body.message}]
        c.status, c.status_detail = "running", "Thinking…"
    await _dispatch(_run_agent(cid))
    with session() as s:
        return _chat_json(s.get(ChatSession, cid))


@app.delete("/api/chats/{cid}", status_code=204)
def delete_chat(cid: str) -> Response:
    with session() as s:
        c = s.get(ChatSession, cid)
        if not c:
            raise HTTPException(404, "Chat not found.")
        s.delete(c)
    return Response(status_code=204)


# ------------------------------------------------------------------- frontend
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/{path:path}", include_in_schema=False)
def spa(path: str) -> FileResponse:
    if path.startswith("api/"):
        raise HTTPException(404, "Not found.")
    return FileResponse(STATIC / "index.html")


_ = BASE_DIR  # (kept for tooling that imports it from here)
