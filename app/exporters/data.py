"""Documents -> CSV / XLSX / JSON / XML."""
from __future__ import annotations

import csv
import io
import json
import re
from xml.etree import ElementTree as ET

_NUMLIKE = re.compile(r"^\s*[-+]?[$€£₹¥]?\s?\d[\d,]*(\.\d+)?\s?%?\s*$")


def _snake(s: str) -> str:
    s = re.sub(r"[^\w]+", "_", s.strip().lower()).strip("_")
    return s or "column"


def _to_number(v: str):  # noqa: ANN202
    if isinstance(v, str) and _NUMLIKE.match(v):
        raw = re.sub(r"[^\d.\-]", "", v)
        try:
            n = float(raw)
            return int(n) if n.is_integer() and "." not in raw else n
        except ValueError:
            return v
    return v


def _cell(v) -> str:  # noqa: ANN001
    if v is None:
        return ""
    if isinstance(v, (list, dict)):
        return json.dumps(v, ensure_ascii=False) if isinstance(v, dict) else "; ".join(map(str, v))
    return str(v)


def build_tables(docs: list[dict], opts: dict) -> list[dict]:
    """Return named tables [{name, headers, rows}] according to the sheet options."""
    source = opts.get("sheet_source", "auto")
    has_rows = any(d.get("rows") for d in docs)
    has_tables = any(d.get("tables") for d in docs)
    if source == "auto":
        source = "rows" if has_rows else "tables" if has_tables else "content"
    norm = opts.get("normalize_tables", True)
    combine = opts.get("combine_pages", True)
    multi = len(docs) > 1
    out: list[dict] = []

    if source == "rows":
        cols = list(dict.fromkeys(k for d in docs for r in d.get("rows", []) for k in r))
        rows = []
        for d in docs:
            for r in d.get("rows", []):
                rows.append(([d["source_url"]] if multi else []) + [_cell(r.get(c)) for c in cols])
        out.append({"name": "Records", "headers": (["source_url"] if multi else []) + cols, "rows": rows})
    elif source == "tables":
        groups: dict[tuple, dict] = {}
        n = 0
        for d in docs:
            for ti, t in enumerate(d.get("tables", [])):
                headers = [(_snake(h) if norm else h) for h in t["headers"]]
                key = tuple(headers) if combine else (d["source_url"], ti)
                if key not in groups:
                    n += 1
                    name = (t.get("caption") or f"Table {n}")[:28]
                    groups[key] = {"name": name, "headers": (["source_url"] if multi else []) + headers, "rows": []}
                for r in t["rows"]:
                    vals = [c.strip() for c in r] if norm else r
                    groups[key]["rows"].append(([d["source_url"]] if multi else []) + vals)
        out = list(groups.values())
        if norm:
            for g in out:
                seen, uniq = set(), []
                for r in g["rows"]:
                    k = tuple(r)
                    if k not in seen:
                        seen.add(k)
                        uniq.append(r)
                g["rows"] = uniq
    elif source == "links":
        rows = [[d["source_url"], lk["text"], lk["url"], "yes" if lk["internal"] else "no"]
                for d in docs for lk in d.get("links", [])]
        out.append({"name": "Links", "headers": ["page_url", "link_text", "url", "internal"], "rows": rows})
    else:  # content
        rows = []
        for d in docs:
            for i, b in enumerate(d.get("content", [])):
                if b["type"] == "list":
                    text = "\n".join(("- " + x) for x in b["items"])
                elif b["type"] == "table":
                    t = d["tables"][b["table"]]
                    text = f"[table: {len(t['rows'])} rows × {len(t['headers'])} columns]"
                elif b["type"] == "image":
                    text = b.get("src", "")
                else:
                    text = b.get("text", "")
                rows.append([d["source_url"], d["title"], i + 1, b["type"], b.get("level", ""), text])
        out.append({"name": "Content", "headers": ["page_url", "page_title", "position", "block_type",
                                                   "heading_level", "text"], "rows": rows})

    cols_wanted = [c.strip() for c in opts.get("columns") or [] if c.strip()]
    if cols_wanted:
        for t in out:
            idx = [i for i, h in enumerate(t["headers"])
                   if h in cols_wanted or _snake(h) in {_snake(c) for c in cols_wanted}]
            if idx:
                t["headers"] = [t["headers"][i] for i in idx]
                t["rows"] = [[r[i] if i < len(r) else "" for i in idx] for r in t["rows"]]
    return out or [{"name": "Empty", "headers": ["note"], "rows": [["No data found for this selection."]]}]


def to_csv(docs: list[dict], opts: dict) -> tuple[bytes, list[str]]:
    tables = build_tables(docs, opts)
    warnings = []
    if len(tables) > 1:
        # CSV holds one table: stack them with a table column
        headers = ["table"] + list(dict.fromkeys(h for t in tables for h in t["headers"]))
        rows = []
        for t in tables:
            pos = {h: i for i, h in enumerate(t["headers"])}
            for r in t["rows"]:
                rows.append([t["name"]] + [r[pos[h]] if h in pos and pos[h] < len(r) else "" for h in headers[1:]])
        tables = [{"name": "combined", "headers": headers, "rows": rows}]
        warnings.append("Several tables were combined into one CSV with a 'table' column. Use XLSX to get one sheet per table.")
    t = tables[0]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(t["headers"])
    w.writerows(t["rows"])
    return ("﻿" + buf.getvalue()).encode("utf-8"), warnings


def to_xlsx(docs: list[dict], opts: dict) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = "Overview"
    head_fill = PatternFill("solid", fgColor="4F46E5")
    head_font = Font(bold=True, color="FFFFFF")

    def write(ws, headers: list[str], rows: list[list]) -> None:  # noqa: ANN001
        ws.append(headers)
        for c in ws[1]:
            c.fill, c.font = head_fill, head_font
            c.alignment = Alignment(vertical="center")
        for r in rows:
            ws.append([_to_number(v) if opts.get("normalize_tables", True) else v for v in r])
        ws.freeze_panes = "A2"
        if rows:
            ws.auto_filter.ref = ws.dimensions
        for i, h in enumerate(headers, 1):
            width = max([len(str(h))] + [len(str(r[i - 1])) for r in rows[:300] if i - 1 < len(r)])
            ws.column_dimensions[get_column_letter(i)].width = min(60, max(10, width + 2))

    write(ws, ["title", "url", "site_type", "author", "published_date", "words", "tables", "images", "links"],
          [[d["title"], d["source_url"], d.get("site_type") or "", d.get("author") or "", d.get("published_date") or "",
            d["stats"]["word_count"], d["stats"]["tables"], d["stats"]["images"], d["stats"]["links"]] for d in docs])

    used = {"Overview"}
    for t in build_tables(docs, opts):
        name = re.sub(r"[\[\]\*\?/\\:]", "", t["name"])[:28] or "Sheet"
        base, k = name, 2
        while name in used:
            name = f"{base[:25]} {k}"
            k += 1
        used.add(name)
        write(wb.create_sheet(name), t["headers"], t["rows"])
    if opts.get("sheet_source", "auto") != "links" and any(d.get("links") for d in docs):
        write(wb.create_sheet("Links"), ["page_url", "link_text", "url", "internal"],
              [[d["source_url"], lk["text"], lk["url"], "yes" if lk["internal"] else "no"]
               for d in docs for lk in d.get("links", [])])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _public_doc(d: dict) -> dict:
    return {k: v for k, v in d.items() if k not in ("site_type_signals",)}


def to_json(docs: list[dict], project: dict) -> bytes:
    return json.dumps({"project": project, "pages": [_public_doc(d) for d in docs]},
                      ensure_ascii=False, indent=2, default=str).encode("utf-8")


_XML_BAD = re.compile(r"[^\x09\x0a\x0d\x20-퟿-�]")


def _xml_tag(k: str) -> str:
    t = re.sub(r"[^\w.-]", "_", str(k))
    return t if re.match(r"[A-Za-z_]", t) else "_" + t


def _to_xml(parent: ET.Element, key: str, value) -> None:  # noqa: ANN001
    if isinstance(value, dict):
        el = ET.SubElement(parent, _xml_tag(key))
        for k, v in value.items():
            _to_xml(el, k, v)
    elif isinstance(value, list):
        el = ET.SubElement(parent, _xml_tag(key))
        item = "page" if key == "pages" else "row" if key in ("rows",) else "item"
        for v in value:
            _to_xml(el, item, v)
    else:
        el = ET.SubElement(parent, _xml_tag(key))
        el.text = _XML_BAD.sub("", "" if value is None else str(value))


def to_xml(docs: list[dict], project: dict) -> bytes:
    root = ET.Element("distill_export")
    _to_xml(root, "project", project)
    _to_xml(root, "pages", [_public_doc(d) for d in docs])
    ET.indent(root)
    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="utf-8")
