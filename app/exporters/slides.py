"""Markdown -> PPTX. '## ' headings become slides; tables become table slides (+ charts)."""
from __future__ import annotations

import io
import re

from .mdparse import parse, runs_text

THEMES = {
    "light": {"bg": (255, 255, 255), "fg": (17, 24, 39), "muted": (107, 114, 128), "accent": (79, 70, 229),
              "panel": (245, 245, 255)},
    "dark": {"bg": (17, 19, 26), "fg": (241, 245, 249), "muted": (148, 163, 184), "accent": (129, 140, 248),
             "panel": (30, 33, 45)},
}
MAX_BULLETS = 7
MAX_WORDS = 95


def _num(s: str) -> float | None:
    s = re.sub(r"[^\d.\-]", "", s.replace(",", ""))
    try:
        return float(s) if s not in ("", "-", ".") else None
    except ValueError:
        return None


def build_slide_plan(md: str) -> tuple[str, str, list[dict]]:
    """Group blocks into slides: {title, bullets:[(text,level,strong)], notes, table, image, code}."""
    blocks = parse(md)
    title, subtitle = "Presentation", ""
    slides: list[dict] = []
    cur: dict | None = None
    section = ""          # current heading text; every slide title derives from it
    skip = False          # inside a "Contents" section (a TOC is meaningless in a deck)

    def new(t: str) -> dict:
        s = {"title": t, "bullets": [], "notes": [], "table": None, "image": None, "code": None}
        slides.append(s)
        return s

    def cont() -> dict:
        n = sum(1 for s in slides if s["title"].startswith(section or title))
        return new(f"{section or title} ({n + 1})" if n else (section or title))

    for b in blocks:
        t = b["type"]
        if t == "heading" and b["level"] == 1 and title == "Presentation":
            title = runs_text(b["runs"])
            continue
        if t == "heading" and b["level"] <= 3:
            section = runs_text(b["runs"])
            skip = section.strip().lower() in ("contents", "table of contents")
            if not skip:
                cur = new(section)
            continue
        if skip:
            continue
        if cur is None:
            if t == "paragraph" and not subtitle:
                subtitle = runs_text(b["runs"])[:200]
                continue
            cur = new(title)
        if t == "heading":                       # h4+ becomes a highlighted bullet
            cur = _room(cur, cont)
            cur["bullets"].append((runs_text(b["runs"]), 0, True))
        elif t == "paragraph":
            txt = runs_text(b["runs"])
            if txt.lower().startswith(("source:", "by ")):
                cur["notes"].append(txt)
                continue
            for part in _split_long(txt):
                cur = _room(cur, cont)
                cur["bullets"].append((part, 0, False))
        elif t == "quote":
            txt = runs_text(b["runs"])
            if txt.lower().startswith("notes:"):
                cur["notes"].append(txt[6:].strip())
            else:
                cur = _room(cur, cont)
                cur["bullets"].append(("“" + txt[:300] + "”", 0, False))
        elif t == "list":
            for it in b["items"]:
                cur = _room(cur, cont)
                cur["bullets"].append((runs_text(it["runs"])[:300], min(it["level"], 2), False))
        elif t == "table" and (b["rows"] or b["headers"]):
            s = cur if _empty(cur) else cont()
            s["table"] = b
            cur = s
        elif t == "image":
            s = cur if _empty(cur) else cont()
            s["image"] = b["src"]
            cur = s
        elif t == "code":
            if b["text"].count("\n") < 2 and len(b["text"]) < 90:   # short snippet: keep it inline
                cur = _room(cur, cont)
                cur["bullets"].append((b["text"], 1, False))
            else:
                s = cur if _empty(cur) else cont()
                s["code"] = b["text"][:1200]
                cur = s
    return title, subtitle, [s for s in slides if not _empty(s)]


def _empty(s: dict) -> bool:
    return not (s["bullets"] or s["table"] or s["image"] or s["code"])


def _split_long(txt: str) -> list[str]:
    if len(txt.split()) <= 40:
        return [txt]
    sentences = re.split(r"(?<=[.!?])\s+", txt)
    out, cur = [], ""
    for s in sentences:
        if len((cur + " " + s).split()) > 40 and cur:
            out.append(cur.strip())
            cur = s
        else:
            cur += " " + s
    if cur.strip():
        out.append(cur.strip())
    return out


def _room(cur: dict, cont) -> dict:  # noqa: ANN001
    words = sum(len(b[0].split()) for b in cur["bullets"])
    if len(cur["bullets"]) >= MAX_BULLETS or words > MAX_WORDS or cur["table"] or cur["image"] or cur["code"]:
        return cont()
    return cur


def render_pptx(md: str, opts: dict, images: dict[str, bytes]) -> tuple[bytes, list[str]]:
    from pptx import Presentation
    from pptx.chart.data import CategoryChartData
    from pptx.dml.color import RGBColor
    from pptx.enum.chart import XL_CHART_TYPE, XL_LEGEND_POSITION
    from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
    from pptx.util import Emu, Inches, Pt

    th = THEMES.get(opts.get("theme", "light"), THEMES["light"])
    C = lambda k: RGBColor(*th[k])  # noqa: E731
    warnings: list[str] = []
    title, subtitle, plan = build_slide_plan(md)
    limit = opts.get("slide_count") or 40
    if len(plan) > limit:
        warnings.append(f"Content needed {len(plan)} slides; kept the first {limit} (change 'Number of slides' to include more).")
        plan = plan[:limit]

    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    W, H = prs.slide_width, prs.slide_height
    blank = prs.slide_layouts[6]

    def base_slide():
        s = prs.slides.add_slide(blank)
        s.background.fill.solid()
        s.background.fill.fore_color.rgb = C("bg")
        return s

    def text(s, x, y, w, h, value, size, color="fg", bold=False, align=PP_ALIGN.LEFT, anchor=MSO_ANCHOR.TOP):  # noqa: ANN001, ANN202
        tb = s.shapes.add_textbox(x, y, w, h)
        tf = tb.text_frame
        tf.word_wrap = True
        tf.vertical_anchor = anchor
        p = tf.paragraphs[0]
        p.alignment = align
        r = p.add_run()
        r.text = value
        r.font.size, r.font.bold, r.font.color.rgb = Pt(size), bold, C(color)
        r.font.name = "Calibri"
        return tf

    def header(s, t: str, n: int) -> None:  # noqa: ANN001
        bar = s.shapes.add_shape(1, Inches(0.6), Inches(0.55), Inches(0.08), Inches(0.62))
        bar.fill.solid()
        bar.fill.fore_color.rgb = C("accent")
        bar.line.fill.background()
        text(s, Inches(0.85), Inches(0.42), W - Inches(1.7), Inches(0.9), t[:110], 28, bold=True)
        text(s, W - Inches(1.2), H - Inches(0.55), Inches(0.8), Inches(0.35), str(n), 11, "muted", align=PP_ALIGN.RIGHT)

    # title slide
    s = base_slide()
    band = s.shapes.add_shape(1, 0, 0, Inches(0.35), H)
    band.fill.solid()
    band.fill.fore_color.rgb = C("accent")
    band.line.fill.background()
    text(s, Inches(1.1), Inches(2.3), W - Inches(2.2), Inches(1.8), title[:150], 44, bold=True, anchor=MSO_ANCHOR.BOTTOM)
    if subtitle:
        text(s, Inches(1.1), Inches(4.25), W - Inches(2.2), Inches(1.2), subtitle[:220], 18, "muted")

    for n, sl in enumerate(plan, 2):
        s = base_slide()
        header(s, sl["title"], n)
        top, left, width, height = Inches(1.55), Inches(0.85), W - Inches(1.7), H - Inches(2.2)
        if sl["table"]:
            tb = sl["table"]
            headers = tb["headers"] or [f"Column {i + 1}" for i in range(len(tb["rows"][0]) if tb["rows"] else 1)]
            ncol = min(len(headers), 7)
            rows = tb["rows"][:10]
            chart_col = None
            if opts.get("include_charts", True) and len(tb["rows"]) >= 2:
                for ci in range(1, len(headers)):
                    vals = [_num(r[ci]) if ci < len(r) else None for r in tb["rows"][:15]]
                    if sum(v is not None for v in vals) >= max(3, round(len(vals) * 0.8)):
                        chart_col = ci
                        break
            tw = width if chart_col is None else int(width * 0.55)
            shape = s.shapes.add_table(len(rows) + 1, ncol, left, top, tw, Inches(0.4) * (len(rows) + 1))
            table = shape.table
            for ci in range(ncol):
                cell = table.cell(0, ci)
                cell.text = headers[ci][:40]
                cell.fill.solid()
                cell.fill.fore_color.rgb = C("accent")
                para = cell.text_frame.paragraphs[0]
                para.runs[0].font.size, para.runs[0].font.bold = Pt(12), True
                para.runs[0].font.color.rgb = RGBColor(255, 255, 255)
            for ri, row in enumerate(rows, 1):
                for ci in range(ncol):
                    cell = table.cell(ri, ci)
                    cell.text = (row[ci] if ci < len(row) else "")[:60]
                    cell.fill.solid()
                    cell.fill.fore_color.rgb = C("panel") if ri % 2 else C("bg")
                    r0 = cell.text_frame.paragraphs[0].runs
                    if r0:
                        r0[0].font.size = Pt(11)
                        r0[0].font.color.rgb = C("fg")
            if len(tb["rows"]) > 10:
                text(s, left, H - Inches(0.6), Inches(6), Inches(0.35),
                     f"Showing 10 of {len(tb['rows'])} rows", 11, "muted")
            if chart_col is not None:
                data = CategoryChartData()
                pts = [(r[0][:25], _num(r[chart_col])) for r in tb["rows"][:15] if chart_col < len(r) and _num(r[chart_col]) is not None]
                data.categories = [p[0] for p in pts]
                data.add_series(headers[chart_col][:30], [p[1] for p in pts])
                gf = s.shapes.add_chart(XL_CHART_TYPE.BAR_CLUSTERED, left + tw + Inches(0.3), top,
                                        width - tw - Inches(0.3), height, data)
                ch = gf.chart
                ch.has_legend = True
                ch.legend.position = XL_LEGEND_POSITION.BOTTOM
                ch.legend.include_in_layout = False
                ch.legend.font.color.rgb = C("muted")
                ch.plots[0].series[0].format.fill.solid()
                ch.plots[0].series[0].format.fill.fore_color.rgb = C("accent")
                ch.category_axis.tick_labels.font.size = Pt(10)
                ch.category_axis.tick_labels.font.color.rgb = C("muted")
                ch.value_axis.tick_labels.font.size = Pt(10)
                ch.value_axis.tick_labels.font.color.rgb = C("muted")
        elif sl["image"]:
            data = images.get(sl["image"])
            if data:
                from PIL import Image as PILImage
                im = PILImage.open(io.BytesIO(data))
                ratio = im.width / im.height
                h = height
                w = int(h * ratio)
                if w > width:
                    w, h = width, int(width / ratio)
                s.shapes.add_picture(io.BytesIO(data), left + (width - w) // 2, top, Emu(w), Emu(h))
            else:
                text(s, left, top, width, Inches(1), f"Image: {sl['image']}", 14, "muted")
        elif sl["code"]:
            box = s.shapes.add_shape(1, left, top, width, height)
            box.fill.solid()
            box.fill.fore_color.rgb = RGBColor(15, 23, 42)
            box.line.fill.background()
            tf = text(s, left + Inches(0.2), top + Inches(0.15), width - Inches(0.4), height - Inches(0.3), sl["code"], 13)
            for p in tf.paragraphs:
                for r in p.runs:
                    r.font.name = "Consolas"
                    r.font.color.rgb = RGBColor(226, 232, 240)
        else:
            tb = s.shapes.add_textbox(left, top, width, height)
            tf = tb.text_frame
            tf.word_wrap = True
            total = sum(len(b[0].split()) for b in sl["bullets"])
            size = 22 if total < 45 else 19 if total < 75 else 16
            for i, (txt, lvl, strong) in enumerate(sl["bullets"]):
                p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
                p.level = lvl
                p.space_after = Pt(10)
                r = p.add_run()
                r.text = ("" if strong else ("•  " if lvl == 0 else "–  ")) + txt
                r.font.size = Pt(size - 3 * lvl)
                r.font.bold = strong
                r.font.color.rgb = C("accent") if strong else (C("fg") if lvl == 0 else C("muted"))
        if sl["notes"]:
            s.notes_slide.notes_text_frame.text = "\n".join(sl["notes"])

    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue(), warnings
