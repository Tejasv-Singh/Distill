"""Markdown -> HTML / PDF / DOCX / PPTX / TXT."""
from __future__ import annotations

import asyncio
import html as htmllib
import io
import re
import textwrap
from datetime import date

from markdown_it import MarkdownIt

from ..scraping.fetcher import Fetcher, FetchError
from .mdparse import parse, runs_text

_md_html = MarkdownIt("commonmark", {"html": False, "linkify": False, "typographer": True}).enable(["table", "strikethrough"])

ACCENT = (79, 70, 229)  # indigo-600

PRINT_CSS = """
@page { size: A4; margin: 22mm 18mm; }
* { box-sizing: border-box; }
body { font: 11pt/1.6 "Inter", "Segoe UI", system-ui, -apple-system, sans-serif; color: #1f2328; max-width: 760px; margin: 40px auto; padding: 0 20px; }
h1 { font-size: 24pt; line-height: 1.2; margin: 0 0 12px; letter-spacing: -0.02em; }
h2 { font-size: 16pt; margin: 28px 0 8px; padding-bottom: 4px; border-bottom: 1px solid #e5e7eb; }
h3 { font-size: 13pt; margin: 20px 0 6px; }
h4, h5, h6 { font-size: 11pt; margin: 16px 0 4px; }
p { margin: 0 0 10px; }
a { color: #4f46e5; text-decoration: none; }
blockquote { margin: 12px 0; padding: 8px 14px; border-left: 3px solid #4f46e5; background: #f5f5ff; color: #374151; }
code { font: 9.5pt/1.4 "JetBrains Mono", Consolas, monospace; background: #f3f4f6; padding: 1px 4px; border-radius: 4px; }
pre { background: #0f172a; color: #e2e8f0; padding: 12px 14px; border-radius: 8px; overflow-x: auto; white-space: pre-wrap; }
pre code { background: none; color: inherit; padding: 0; }
table { border-collapse: collapse; width: 100%; margin: 12px 0 16px; font-size: 9.5pt; page-break-inside: auto; }
th, td { border: 1px solid #e5e7eb; padding: 6px 8px; text-align: left; vertical-align: top; }
th { background: #f9fafb; font-weight: 600; }
tr { page-break-inside: avoid; }
img { max-width: 100%; height: auto; border-radius: 6px; margin: 8px 0; }
hr { border: 0; border-top: 1px solid #e5e7eb; margin: 20px 0; }
ul, ol { padding-left: 22px; margin: 0 0 10px; }
li { margin: 2px 0; }
.meta { color: #6b7280; font-size: 9pt; margin-bottom: 24px; }
"""


def first_title(md: str, fallback: str = "Document") -> str:
    m = re.search(r"(?m)^# (.+)$", md)
    return m.group(1).strip() if m else fallback


def strip_images(md: str) -> str:
    return re.sub(r"!\[[^\]]*\]\([^)]*\)\n?", "", md)


def render_html(md: str, opts: dict) -> str:
    if not opts.get("include_images", True):
        md = strip_images(md)
    body = _md_html.render(md)
    body = body.replace("<a href=", '<a rel="noopener noreferrer nofollow" target="_blank" href=')
    title = htmllib.escape(first_title(md))
    return (f'<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<meta name="generator" content="Distill"><title>{title}</title>'
            f"<style>{PRINT_CSS}</style></head><body>{body}"
            f'<p class="meta">Generated with Distill on {date.today().isoformat()}.</p></body></html>')


# --------------------------------------------------------------------- images
async def fetch_images(srcs: list[str], limit: int = 12, fetcher: Fetcher | None = None) -> dict[str, bytes]:
    """Download images through the SSRF-safe fetcher; convert anything unusual to PNG."""
    from PIL import Image

    own = fetcher is None
    fetcher = fetcher or Fetcher()
    out: dict[str, bytes] = {}
    try:
        for src in list(dict.fromkeys(s for s in srcs if s and s.startswith(("http://", "https://"))))[:limit]:
            try:
                _, status, headers, body = await asyncio.wait_for(fetcher.get(src, max_bytes=5_000_000), 15)
            except (FetchError, asyncio.TimeoutError):
                continue
            if status != 200 or not body:
                continue
            try:
                img = Image.open(io.BytesIO(body))
                img.load()
                if img.width < 16 or img.height < 16:
                    continue
                if img.format not in ("PNG", "JPEG", "GIF") or img.mode not in ("RGB", "RGBA", "L", "P"):
                    buf = io.BytesIO()
                    img.convert("RGBA" if "A" in img.getbands() else "RGB").save(buf, "PNG")
                    body = buf.getvalue()
                out[src] = body
            except Exception:  # noqa: BLE001 - not an image we can use
                continue
    finally:
        if own:
            await fetcher.close()
    return out


def image_srcs(md: str) -> list[str]:
    return re.findall(r"!\[[^\]]*\]\(([^)\s]+)", md)


# ------------------------------------------------------------------------ PDF
async def render_pdf(md: str, opts: dict) -> tuple[bytes, list[str]]:
    from ..config import settings
    if not settings.js_rendering:
        # no browser on this deployment (e.g. Vercel): go straight to the ReportLab renderer
        imgs = await fetch_images(image_srcs(md)) if opts.get("include_images", True) else {}
        return await asyncio.to_thread(render_pdf_reportlab, md, opts, imgs), []
    html = render_html(md, opts)
    fetcher = Fetcher()
    try:
        ctx, page = await fetcher.new_guarded_page()   # every image request is SSRF-checked
        try:
            await page.set_content(html, wait_until="load", timeout=30000)
            pdf = await page.pdf(format="A4", print_background=True,
                                 margin={"top": "18mm", "bottom": "18mm", "left": "16mm", "right": "16mm"},
                                 display_header_footer=True, header_template="<span></span>",
                                 footer_template='<div style="font-size:8px;color:#9ca3af;width:100%;text-align:center">'
                                                 '<span class="pageNumber"></span> / <span class="totalPages"></span></div>')
            return pdf, []
        finally:
            await ctx.close()
    except FetchError:
        imgs = await fetch_images(image_srcs(md), fetcher=fetcher) if opts.get("include_images", True) else {}
        return render_pdf_reportlab(md, opts, imgs), ["PDF made with the basic renderer (no browser available)."]
    finally:
        await fetcher.close()


def _rl_inline(runs: list[dict]) -> str:
    out = []
    for r in runs:
        if "image" in r:
            continue
        t = htmllib.escape(r["text"]).replace("\n", "<br/>")
        if r["code"]:
            t = f'<font face="Courier">{t}</font>'
        if r["bold"]:
            t = f"<b>{t}</b>"
        if r["italic"]:
            t = f"<i>{t}</i>"
        if r["link"] and r["link"].startswith(("http://", "https://")):
            t = f'<a href="{htmllib.escape(r["link"])}" color="#4f46e5">{t}</a>'
        out.append(t)
    return "".join(out)


def render_pdf_reportlab(md: str, opts: dict, images: dict[str, bytes]) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import (Image, ListFlowable, ListItem, Paragraph, Preformatted, SimpleDocTemplate,
                                    Spacer, Table, TableStyle)

    ss = getSampleStyleSheet()
    body = ParagraphStyle("b", parent=ss["BodyText"], fontSize=10.5, leading=15)
    cell = ParagraphStyle("c", parent=body, fontSize=8.5, leading=11)
    quote = ParagraphStyle("q", parent=body, leftIndent=12, textColor=colors.HexColor("#374151"),
                           borderColor=colors.HexColor("#4f46e5"), borderPadding=(4, 4, 4, 8), borderWidth=0)
    code = ParagraphStyle("code", parent=ss["Code"], fontSize=8, leading=10, backColor=colors.HexColor("#f3f4f6"))
    hs = {1: ss["Title"], 2: ss["Heading2"], 3: ss["Heading3"], 4: ss["Heading4"], 5: ss["Heading5"], 6: ss["Heading6"]}
    width = A4[0] - 36 * mm
    story = []
    for b in parse(md if opts.get("include_images", True) else strip_images(md)):
        t = b["type"]
        if t == "heading":
            story.append(Paragraph(_rl_inline(b["runs"]), hs[b["level"]]))
        elif t == "paragraph":
            story.append(Paragraph(_rl_inline(b["runs"]), body))
        elif t == "quote":
            story.append(Paragraph(_rl_inline(b["runs"]), quote))
        elif t == "list":
            items = [ListItem(Paragraph(_rl_inline(it["runs"]), body), leftIndent=12 + 12 * it["level"])
                     for it in b["items"]]
            story.append(ListFlowable(items, bulletType="1" if b["ordered"] else "bullet", start=1 if b["ordered"] else None))
        elif t == "code":
            story.append(Preformatted(b["text"], code))
        elif t == "table":
            data = ([[Paragraph(f"<b>{htmllib.escape(h)}</b>", cell) for h in b["headers"]]] if b["headers"] else []) + \
                   [[Paragraph(htmllib.escape(c), cell) for c in r] for r in b["rows"]]
            if data:
                ncol = max(len(r) for r in data)
                data = [r + [""] * (ncol - len(r)) for r in data]
                tbl = Table(data, colWidths=[width / ncol] * ncol, repeatRows=1 if b["headers"] else 0)
                tbl.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#e5e7eb")),
                                         ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f9fafb")),
                                         ("VALIGN", (0, 0), (-1, -1), "TOP")]))
                story.append(tbl)
        elif t == "image" and b["src"] in images:
            try:
                from PIL import Image as PILImage
                im = PILImage.open(io.BytesIO(images[b["src"]]))
                w = min(width, im.width)
                story.append(Image(io.BytesIO(images[b["src"]]), width=w, height=w * im.height / im.width))
            except Exception:  # noqa: BLE001
                pass
        elif t == "hr":
            story.append(Spacer(1, 8))
        story.append(Spacer(1, 4))
    buf = io.BytesIO()
    SimpleDocTemplate(buf, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=18 * mm,
                      bottomMargin=18 * mm, title=first_title(md)).build(story)
    return buf.getvalue()


# ----------------------------------------------------------------------- DOCX
def _docx_hyperlink(paragraph, url: str, text: str, bold: bool, italic: bool):  # noqa: ANN001, ANN202
    from docx.opc.constants import RELATIONSHIP_TYPE as RT
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    r_id = paragraph.part.relate_to(url, RT.HYPERLINK, is_external=True)
    link = OxmlElement("w:hyperlink")
    link.set(qn("r:id"), r_id)
    run = OxmlElement("w:r")
    rpr = OxmlElement("w:rPr")
    color = OxmlElement("w:color")
    color.set(qn("w:val"), "4F46E5")
    rpr.append(color)
    u = OxmlElement("w:u")
    u.set(qn("w:val"), "single")
    rpr.append(u)
    if bold:
        rpr.append(OxmlElement("w:b"))
    if italic:
        rpr.append(OxmlElement("w:i"))
    run.append(rpr)
    t = OxmlElement("w:t")
    t.text = text
    t.set(qn("xml:space"), "preserve")
    run.append(t)
    link.append(run)
    paragraph._p.append(link)


def _docx_runs(par, runs: list[dict]) -> None:  # noqa: ANN001
    from docx.shared import Pt

    for r in runs:
        if "image" in r or not r["text"]:
            continue
        if r["link"] and r["link"].startswith(("http://", "https://")):
            _docx_hyperlink(par, r["link"], r["text"], r["bold"], r["italic"])
            continue
        run = par.add_run(r["text"])
        run.bold, run.italic = r["bold"] or None, r["italic"] or None
        if r["code"]:
            run.font.name = "Consolas"
            run.font.size = Pt(9.5)


def _docx_toc(doc) -> None:  # noqa: ANN001
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    p = doc.add_paragraph()
    run = p.add_run()
    for kind, text in (("begin", None), (None, 'TOC \\o "1-3" \\h \\z \\u'), ("separate", None),
                       (None, None), ("end", None)):
        if kind:
            el = OxmlElement("w:fldChar")
            el.set(qn("w:fldCharType"), kind)
            run._r.append(el)
        elif text:
            instr = OxmlElement("w:instrText")
            instr.set(qn("xml:space"), "preserve")
            instr.text = text
            run._r.append(instr)
        else:
            t = OxmlElement("w:t")
            t.text = "Right-click and choose Update Field to build the table of contents."
            run._r.append(t)


def render_docx(md: str, opts: dict, images: dict[str, bytes]) -> bytes:
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Inches, Pt, RGBColor

    doc = Document()
    st = doc.styles["Normal"]
    st.font.name = "Calibri"
    st.font.size = Pt(11)
    for lvl in range(1, 4):
        doc.styles[f"Heading {lvl}"].font.color.rgb = RGBColor(*ACCENT) if lvl > 1 else RGBColor(0x11, 0x18, 0x27)
    sec = doc.sections[0]
    sec.left_margin = sec.right_margin = Inches(0.9)
    doc.core_properties.title = first_title(md)
    doc.core_properties.author = "Distill"

    blocks = parse(md)
    first_h1 = True
    for b in blocks:
        t = b["type"]
        if t == "heading":
            if b["level"] == 1 and first_h1:
                doc.add_heading(runs_text(b["runs"]), level=0)
                first_h1 = False
                if opts.get("include_toc") and "## Contents" not in md:
                    _docx_toc(doc)
                continue
            p = doc.add_heading(level=min(b["level"], 4))
            _docx_runs(p, b["runs"])
        elif t == "paragraph":
            _docx_runs(doc.add_paragraph(), b["runs"])
        elif t == "quote":
            p = doc.add_paragraph(style="Intense Quote" if "AI interpretation" not in runs_text(b["runs"]) else "Quote")
            _docx_runs(p, b["runs"])
        elif t == "list":
            for it in b["items"]:
                base = "List Number" if b["ordered"] else "List Bullet"
                style = base if it["level"] == 0 else f"{base} {min(it['level'] + 1, 3)}"
                _docx_runs(doc.add_paragraph(style=style), it["runs"])
        elif t == "code":
            p = doc.add_paragraph()
            shd = OxmlElement("w:shd")
            shd.set(qn("w:val"), "clear")
            shd.set(qn("w:fill"), "F3F4F6")
            p._p.get_or_add_pPr().append(shd)
            run = p.add_run(b["text"])
            run.font.name = "Consolas"
            run.font.size = Pt(9)
        elif t == "table":
            ncol = max([len(b["headers"])] + [len(r) for r in b["rows"]] + [1])
            table = doc.add_table(rows=0, cols=ncol)
            table.style = "Light Grid Accent 1"
            if b["headers"]:
                cells = table.add_row().cells
                for i, h in enumerate(b["headers"]):
                    cells[i].text = h
                    for r in cells[i].paragraphs[0].runs:
                        r.bold = True
            for row in b["rows"][:2000]:
                cells = table.add_row().cells
                for i, c in enumerate(row[:ncol]):
                    cells[i].text = c
            doc.add_paragraph()
        elif t == "image" and b["src"] in images:
            try:
                doc.add_picture(io.BytesIO(images[b["src"]]), width=Inches(5.8))
                doc.paragraphs[-1].alignment = WD_ALIGN_PARAGRAPH.CENTER
                if b.get("alt"):
                    cap = doc.add_paragraph(b["alt"])
                    cap.alignment = WD_ALIGN_PARAGRAPH.CENTER
                    cap.runs[0].italic = True
                    cap.runs[0].font.size = Pt(9)
            except Exception:  # noqa: BLE001
                pass
        elif t == "hr":
            doc.add_paragraph()
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


# ------------------------------------------------------------------------ TXT
def render_txt(md: str, opts: dict) -> bytes:
    out: list[str] = []
    for b in parse(strip_images(md)):
        t = b["type"]
        if t == "heading":
            text = runs_text(b["runs"])
            under = "=" if b["level"] == 1 else "-" if b["level"] == 2 else ""
            out += ["", text.upper() if b["level"] == 1 else text] + ([under * min(len(text), 80)] if under else []) + [""]
        elif t in ("paragraph", "quote"):
            text = _txt_inline(b["runs"])
            out += textwrap.wrap(text, 88, initial_indent="  " if t == "quote" else "",
                                 subsequent_indent="  " if t == "quote" else "") + [""]
        elif t == "list":
            for n, it in enumerate(b["items"], 1):
                bullet = f"{n}." if b["ordered"] and it["level"] == 0 else "-"
                ind = "  " * it["level"]
                out += textwrap.wrap(_txt_inline(it["runs"]), 88, initial_indent=f"{ind}{bullet} ",
                                     subsequent_indent=ind + " " * (len(bullet) + 1))
            out.append("")
        elif t == "code":
            out += ["    " + line for line in b["text"].splitlines()] + [""]
        elif t == "table":
            rows = ([b["headers"]] if b["headers"] else []) + b["rows"]
            if rows:
                ncol = max(len(r) for r in rows)
                rows = [r + [""] * (ncol - len(r)) for r in rows]
                widths = [min(40, max(len(r[i]) for r in rows)) for i in range(ncol)]
                fmt = lambda r: "  ".join(c[:40].ljust(widths[i]) for i, c in enumerate(r)).rstrip()  # noqa: E731
                out.append(fmt(rows[0]))
                if b["headers"]:
                    out.append("  ".join("-" * w for w in widths))
                out += [fmt(r) for r in rows[1:]] + [""]
        elif t == "hr":
            out += ["-" * 40, ""]
    return ("\n".join(out).strip() + "\n").encode("utf-8")


def _txt_inline(runs: list[dict]) -> str:
    parts = []
    for r in runs:
        if "image" in r:
            continue
        parts.append(f"{r['text']} ({r['link']})" if r["link"] and r["link"] != r["text"] else r["text"])
    return "".join(parts)
