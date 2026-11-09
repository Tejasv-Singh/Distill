"""Turn stored documents into generated outputs + files (no re-scraping needed)."""
from __future__ import annotations

import asyncio
import csv
import io
import json

from .ai import engine
from .ai.prompts import TITLES
from .ai.providers import AIError
from .config import settings
from .db import GeneratedOutput, Project, ScrapedPage, session
from .exporters import data as data_x
from .exporters.document_md import docs_to_markdown
from .exporters.render import (fetch_images, first_title, image_srcs, render_docx, render_html, render_pdf,
                               render_txt)
from .exporters.slides import render_pptx
from .storage import get_storage

EXT = {"pdf": "pdf", "docx": "docx", "markdown": "md", "txt": "txt", "html": "html", "csv": "csv",
       "xlsx": "xlsx", "json": "json", "xml": "xml", "pptx": "pptx"}
MIME = {"pdf": "application/pdf",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "markdown": "text/markdown; charset=utf-8", "txt": "text/plain; charset=utf-8",
        "html": "text/html; charset=utf-8", "csv": "text/csv; charset=utf-8",
        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "json": "application/json", "xml": "application/xml",
        "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation"}
DOC_FORMATS = {"pdf", "docx", "markdown", "txt", "html", "pptx"}
DATA_FORMATS = {"csv", "xlsx", "json", "xml"}

_sem = asyncio.Semaphore(max(1, settings.max_concurrent_jobs))


def file_format(o: GeneratedOutput) -> str:
    if o.kind in ("convert", "extract"):
        return o.format
    return o.configuration.get("download_format") or "markdown"


def output_key(project_id: str, oid: str, fmt: str) -> str:
    return f"{project_id}/outputs/{oid}.{EXT[fmt]}"


def store_output_file(project_id: str, oid: str, fmt: str, body: bytes) -> str:
    """Write an output's file to storage and return its key. Call this *outside* any open
    database session: the database storage backend uses its own short transaction, and
    nesting it inside another open transaction would hold two connections at once."""
    return get_storage().put(output_key(project_id, oid, fmt), body, MIME[fmt].split(";")[0])


def load_docs(project_id: str, page_ids: list[str] | None = None) -> tuple[dict, list[dict]]:
    with session() as s:
        p = s.get(Project, project_id)
        q = s.query(ScrapedPage).filter_by(project_id=project_id).order_by(ScrapedPage.position)
        pages = [pg for pg in q.all() if not page_ids or pg.id in page_ids]
        info = {"id": p.id, "name": p.name, "source_url": p.source_url, "site_type": p.site_type,
                "created_at": p.created_at.isoformat()}
        return info, [pg.extracted_content for pg in pages]


def _set(oid: str, **kw) -> None:
    with session() as s:
        o = s.get(GeneratedOutput, oid)
        if o:
            for k, v in kw.items():
                setattr(o, k, v)


async def render_document(md: str, fmt: str, opts: dict) -> tuple[bytes, list[str]]:
    """Markdown -> file bytes for a document format."""
    if fmt == "markdown":
        return md.encode("utf-8"), []
    if fmt == "txt":
        return render_txt(md, opts), []
    if fmt == "html":
        return render_html(md, opts).encode("utf-8"), []
    if fmt == "pdf":
        return await render_pdf(md, opts)
    imgs = await fetch_images(image_srcs(md)) if opts.get("include_images", True) else {}
    if fmt == "docx":
        return await asyncio.to_thread(render_docx, md, opts, imgs), []
    if fmt == "pptx":
        import re
        clean = re.sub(r"\s?\[S\d+\]", "", md)  # citations clutter slides; sources slide remains
        return await asyncio.to_thread(render_pptx, clean, opts, imgs)
    raise ValueError(f"unsupported document format {fmt}")


def _data_preview(fmt: str, docs: list[dict], opts: dict, body: bytes) -> str:
    if fmt in ("json", "xml"):
        text = body.decode("utf-8", "replace")
        return text[:300_000] + ("\n…(truncated in preview; the download is complete)" if len(text) > 300_000 else "")
    if fmt == "csv":
        return body.decode("utf-8-sig", "replace")[:300_000]
    # xlsx: CSV-style preview of each sheet
    out = io.StringIO()
    for t in data_x.build_tables(docs, opts)[:5]:
        out.write(f"## {t['name']}\n")
        w = csv.writer(out)
        w.writerow(t["headers"])
        w.writerows(t["rows"][:200])
        out.write("\n")
    return out.getvalue()


async def run_output(oid: str) -> None:
    async with _sem:
        with session() as s:
            o = s.get(GeneratedOutput, oid)
            kind, fmt, opts, project_id = o.kind, o.format, dict(o.configuration), o.project_id
        try:
            _set(oid, status="running", stage="Loading scraped content")
            project, docs = load_docs(project_id, opts.get("page_ids") or None)
            if not docs:
                raise ValueError("This project has no scraped pages yet.")
            warnings: list[str] = []
            if kind == "convert" and fmt in DOC_FORMATS:
                _set(oid, stage="Structuring document")
                md = docs_to_markdown(docs, opts)
                _set(oid, stage=f"Rendering {fmt.upper()}")
                body, w = await render_document(md, fmt, opts)
                content, title = md, first_title(md)
                warnings += w
            elif kind == "convert":
                _set(oid, stage=f"Building {fmt.upper()}")
                if fmt == "csv":
                    body, w = data_x.to_csv(docs, opts)
                    warnings += w
                elif fmt == "xlsx":
                    body = await asyncio.to_thread(data_x.to_xlsx, docs, opts)
                elif fmt == "json":
                    body = data_x.to_json(docs, project)
                else:
                    body = data_x.to_xml(docs, project)
                content = _data_preview(fmt, docs, opts, body)
                title = opts.get("title") or (docs[0]["title"] if len(docs) == 1 else project["name"])
            else:
                result = await engine.generate(fmt, docs, opts, progress=lambda m: _set(oid, stage=m))
                content, title = result.markdown, first_title(result.markdown, TITLES[fmt])
                warnings += result.warnings
                dl = opts.get("download_format") or "markdown"
                _set(oid, stage=f"Rendering {dl.upper()}")
                body, w = await render_document(content, dl, opts)
                warnings += w
                opts["ai_stats"] = result.stats
            out_fmt = fmt if kind in ("convert", "extract") else (opts.get("download_format") or "markdown")
            key = store_output_file(project_id, oid, out_fmt, body)
            with session() as s:
                o = s.get(GeneratedOutput, oid)
                o.file_path = key
                o.generated_content, o.title = content, title[:500]
                o.warnings, o.status, o.stage, o.configuration = warnings, "done", "Done", opts
                o.error_message = None
        except AIError as e:
            _set(oid, status="failed", stage="Failed", error_message=str(e))
        except Exception as e:  # noqa: BLE001
            _set(oid, status="failed", stage="Failed",
                 error_message=f"Couldn't generate this output: {type(e).__name__}: {str(e)[:300]}")


async def rerender(oid: str) -> None:
    """After an edit (or a format change), rebuild the file from the stored Markdown."""
    with session() as s:
        o = s.get(GeneratedOutput, oid)
        fmt, md, opts, project_id = file_format(o), o.generated_content or "", dict(o.configuration), o.project_id
    if fmt not in DOC_FORMATS:
        raise ValueError("Only document outputs can be edited.")
    body, warnings = await render_document(md, fmt, opts)
    key = store_output_file(project_id, oid, fmt, body)
    with session() as s:
        o = s.get(GeneratedOutput, oid)
        o.file_path = key
        o.title = first_title(md, o.title)
        o.warnings = [w for w in (o.warnings or []) if "figures" in w or "instructions" in w] + warnings


async def rerun_extraction(oid: str, extra: str | None) -> None:
    from .ai.extract import extract_table, table_to_file
    with session() as s:
        o = s.get(GeneratedOutput, oid)
        cfg, project_id, table_fmt, title = dict(o.configuration), o.project_id, o.format, o.title
    instruction = cfg["instruction"] + (f"\n{extra}" if extra else "")
    try:
        _set(oid, status="running", stage="Extracting")
        _, docs = load_docs(project_id, cfg.get("page_ids") or None)
        cols = [c for c in cfg.get("columns", []) if c != "source_url"] if not extra else None
        table = await extract_table(docs, instruction, cols, progress=lambda m: _set(oid, stage=m))
        key = store_output_file(project_id, oid, table_fmt, table_to_file(table, table_fmt, title))
        with session() as s:
            o = s.get(GeneratedOutput, oid)
            o.generated_content = json.dumps({"columns": table["columns"], "rows": table["rows"]}, ensure_ascii=False, default=str)
            o.configuration = {**cfg, "instruction": instruction, "columns": table["columns"], "stats": table["stats"]}
            o.warnings, o.status, o.stage, o.error_message = table["warnings"], "done", "Done", None
            o.file_path = key
    except Exception as e:  # noqa: BLE001
        _set(oid, status="failed", stage="Failed", error_message=f"Extraction failed: {e}")


async def regenerate(oid: str, section: str | None, extra: str | None) -> None:
    with session() as s:
        o = s.get(GeneratedOutput, oid)
        kind, project_id, opts, md = o.kind, o.project_id, dict(o.configuration), o.generated_content or ""
    if kind == "extract":
        await rerun_extraction(oid, extra)
        return
    if section and kind == "ai":
        _set(oid, status="running", stage=f"Rewriting “{section}”")
        try:
            _, docs = load_docs(project_id, opts.get("page_ids") or None)
            new_md, warnings = await engine.regenerate_section(md, section, docs, opts, extra)
            _set(oid, generated_content=new_md, warnings=warnings)
            await rerender(oid)
            _set(oid, status="done", stage="Done", error_message=None)
        except Exception as e:  # noqa: BLE001
            _set(oid, status="done", stage="Done", error_message=f"Section rewrite failed: {e}")
        return
    if extra:
        opts["instructions"] = ((opts.get("instructions") or "") + "\n" + extra).strip()
        _set(oid, configuration=opts)
    _set(oid, status="queued", stage="Queued", error_message=None)
    await run_output(oid)


def to_json_safe(v):  # noqa: ANN001, ANN201
    return json.loads(json.dumps(v, default=str))
