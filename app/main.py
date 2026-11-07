"""Distill: FastAPI application."""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles

from . import generation
from .ai.providers import AIError, ai_status
from .config import BASE_DIR, settings
from .db import GeneratedOutput, Project, ScrapedPage, ScrapingJob, init_db, local_user_id, session
from .exporters.render import render_html
from .schemas import (AI_TYPES, CONVERT_FORMATS, DiscoverRequest, GenerateRequest, OutputEdit, ProjectPatch,
                      RegenerateRequest, RenderRequest, ScrapeRequest)
from .scraping.extractor import document_text
from .scraping.fetcher import FetchError
from .scraping.pipeline import STAGE_LABEL, JobControl, discover_pages, run_scrape_job
from .security import rate_limiter, safe_filename

STATIC = Path(__file__).parent / "static"
_tasks: set[asyncio.Task] = set()
_scrape_sem: asyncio.Semaphore | None = None


def _spawn(coro) -> None:  # noqa: ANN001
    t = asyncio.create_task(coro)
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)


@asynccontextmanager
async def lifespan(_: FastAPI):
    global _scrape_sem
    init_db()
    _scrape_sem = asyncio.Semaphore(max(1, settings.max_concurrent_jobs))
    # never leave anything "running" forever after a restart
    with session() as s:
        now = datetime.now(timezone.utc)
        for j in s.query(ScrapingJob).filter(ScrapingJob.status.in_(["queued", "running"])):
            j.status, j.stage, j.error_message, j.completed_at = "failed", "failed", "Interrupted by a server restart. Retry the scrape.", now
        for p in s.query(Project).filter(Project.scraping_status.in_(["queued", "running"])):
            p.scraping_status = "failed"
        for o in s.query(GeneratedOutput).filter(GeneratedOutput.status.in_(["queued", "running"])):
            o.status, o.error_message = "failed", "Interrupted by a server restart. Regenerate it."
    yield
    for t in list(_tasks):
        t.cancel()


app = FastAPI(title="Distill API", version="2.0.0", lifespan=lifespan,
              description="Scrape websites into a structured document, then turn it into any format.")


@app.middleware("http")
async def security_headers(request: Request, call_next):  # noqa: ANN001, ANN201
    resp = await call_next(request)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
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
         "editable": generation.file_format(o) in generation.DOC_FORMATS,
         "has_file": bool(o.file_path and Path(o.file_path).exists()),
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
         "output_count": len(p.outputs), "has_preview": bool(p.screenshot_path and Path(p.screenshot_path).exists()),
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
    assert _scrape_sem is not None
    async with _scrape_sem:
        await run_scrape_job(pid, jid, req)


def _start_job(s, project: Project, req: ScrapeRequest) -> ScrapingJob:  # noqa: ANN001
    job = ScrapingJob(project_id=project.id, status="queued", stage="queued")
    s.add(job)
    project.scraping_status = "queued"
    s.flush()
    _spawn(_run_scrape(project.id, job.id, req))
    return job


# ---------------------------------------------------------------------- meta
@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "time": _iso(datetime.now(timezone.utc))}


@app.get("/api/config")
def config() -> dict:
    return {"ai": ai_status(), "convert_formats": CONVERT_FORMATS, "ai_types": AI_TYPES,
            "limits": {"max_pages": settings.max_pages_cap, "max_depth": settings.max_depth_cap,
                       "rate_limit_per_minute": settings.rate_limit_per_minute},
            "js_rendering": settings.js_rendering}


# ------------------------------------------------------------------ projects
@app.post("/api/projects", status_code=201, dependencies=[Depends(rate_limit)])
async def create_project(req: ScrapeRequest) -> dict:
    name = req.name or req.urls[0].split("://", 1)[1].rstrip("/")[:120]
    if len(req.urls) > 1 and not req.name:
        name += f" +{len(req.urls) - 1}"
    with session() as s:
        p = Project(user_id=local_user_id(), name=name, source_url=req.urls[0],
                    config=json.loads(req.model_dump_json()), scraping_status="queued")
        s.add(p)
        s.flush()
        job = _start_job(s, p, req)
        return {"project_id": p.id, "job_id": job.id}


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
    folder = settings.storage_dir / pid
    if folder.exists() and folder.resolve().is_relative_to(settings.storage_dir.resolve()):
        for f in sorted(folder.rglob("*"), reverse=True):
            f.unlink() if f.is_file() else f.rmdir()
        folder.rmdir()
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
        job = _start_job(s, p, req)
        return {"project_id": p.id, "job_id": job.id}


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
def project_preview(pid: str) -> FileResponse:
    with session() as s:
        p = _get_project(s, pid)
        path = p.screenshot_path
    if not path or not Path(path).exists():
        raise HTTPException(404, "No preview available.")
    return FileResponse(path, media_type="image/png")


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
        out = [_output_json(o) for o in created]
    for o in out:
        _spawn(generation.run_output(o["id"]))
    return out


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
        if generation.file_format(o) not in generation.DOC_FORMATS:
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
        if generation.file_format(o) not in generation.DOC_FORMATS or o.status != "done":
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
    _spawn(_do())
    with session() as s:
        return _output_json(_get_output(s, nid))


@app.post("/api/outputs/{oid}/regenerate", dependencies=[Depends(rate_limit)])
async def regenerate(oid: str, body: RegenerateRequest) -> dict:
    with session() as s:
        o = _get_output(s, oid)
        if o.status in ("queued", "running"):
            raise HTTPException(409, "This output is still being generated.")
        if o.kind == "ai" and not ai_status()["configured"]:
            raise HTTPException(503, AIError("not_configured").args[0])
        if body.section and o.kind != "ai":
            raise HTTPException(400, "Section rewrites are only available for AI outputs.")
        o.status, o.stage = "queued", "Queued"
    _spawn(generation.regenerate(oid, body.section, body.instructions))
    with session() as s:
        return _output_json(_get_output(s, oid))


@app.get("/api/outputs/{oid}/download")
def download(oid: str) -> FileResponse:
    with session() as s:
        o = _get_output(s, oid)
        if not o.file_path or not Path(o.file_path).exists():
            raise HTTPException(404, "The file isn't ready.")
        fmt = generation.file_format(o)
        path = Path(o.file_path)
        name = safe_filename(o.title or o.format) + "." + generation.EXT[fmt]
    if not path.resolve().is_relative_to(settings.storage_dir.resolve()):
        raise HTTPException(403, "Invalid file location.")
    return FileResponse(path, media_type=generation.MIME[fmt],
                        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"})


@app.get("/api/outputs/{oid}/preview.html")
def preview_html(oid: str) -> Response:
    """Rendered preview for a sandboxed iframe; no scripts, no forms."""
    with session() as s:
        o = _get_output(s, oid)
        md, opts = o.generated_content or "", o.configuration or {}
        if generation.file_format(o) not in generation.DOC_FORMATS:
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
        if o.file_path and Path(o.file_path).exists():
            Path(o.file_path).unlink()
        s.delete(o)
    return Response(status_code=204)


# ------------------------------------------------------------------- frontend
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/{path:path}", include_in_schema=False)
def spa(path: str) -> FileResponse:
    if path.startswith("api/"):
        raise HTTPException(404, "Not found.")
    return FileResponse(STATIC / "index.html")


_ = BASE_DIR  # (kept for tooling that imports it from here)
