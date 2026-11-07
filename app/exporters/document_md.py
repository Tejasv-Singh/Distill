"""Standard documents -> Markdown (the editable source for every document format)."""
from __future__ import annotations

import re

from ..ai.engine import md_table


def _esc_heading(t: str) -> str:
    return t.replace("\n", " ").strip()


def _slug(t: str) -> str:
    return re.sub(r"[^\w\- ]", "", t.lower()).strip().replace(" ", "-")


def docs_to_markdown(docs: list[dict], opts: dict) -> str:
    """Faithful conversion (no AI): preserves heading hierarchy, lists, tables, code, images."""
    multi = len(docs) > 1
    title = opts.get("title") or (docs[0]["title"] if not multi else f"{docs[0].get('site_name') or 'Website'} — {len(docs)} pages")
    out: list[str] = [f"# {_esc_heading(title)}", ""]

    d0 = docs[0]
    if not multi:
        byline = [f"By {d0['author']}"] if d0.get("author") else []
        if d0.get("published_date"):
            byline.append(str(d0["published_date"])[:10])
        if byline:
            out += ["*" + " · ".join(byline) + "*", ""]
        out += [f"Source: <{d0['source_url']}>", ""]
        if d0.get("description"):
            out += [f"> {d0['description']}", ""]

    if opts.get("include_toc"):
        out += ["## Contents", ""]
        for d in docs:
            if multi:
                out.append(f"- [{d['title']}](#{_slug(d['title'])})")
            for h in d.get("headings", []):
                if h["level"] in (2, 3) and h["text"] != d["title"]:
                    out.append(("  " * (h["level"] - 2 + multi)) + f"- {h['text']}")
        out.append("")

    for d in docs:
        shift = 1 if multi else 0
        if multi:
            out += [f"## {_esc_heading(d['title'])}", "", f"Source: <{d['source_url']}>", ""]
        skipped_title = False
        for b in d.get("content", []):
            t = b["type"]
            if t == "heading":
                if not skipped_title and b["level"] == 1 and b["text"].strip() == d["title"].strip():
                    skipped_title = True  # already used as the document/page title
                    continue
                lvl = min(6, max(2, b["level"] + shift))
                out += [f"{'#' * lvl} {_esc_heading(b['text'])}", ""]
            elif t == "paragraph":
                out += [b.get("md") or b["text"], ""]
            elif t == "list":
                out += [(f"{i + 1}. " if b["ordered"] else "- ") + it for i, it in enumerate(b["items"])]
                out.append("")
            elif t == "quote":
                out += ["> " + b["text"], ""]
            elif t == "code":
                out += ["```", b["text"], "```", ""]
            elif t == "table" and opts.get("include_tables", True):
                out += [md_table(d["tables"][b["table"]]), ""]
            elif t == "image" and opts.get("include_images", True):
                alt = (b.get("alt") or "").replace("]", "")
                out += [f"![{alt}]({b['src']})", ""]
        if d.get("rows"):
            out += [f"{'###' if multi else '##'} Extracted records", ""]
            cols = list(dict.fromkeys(k for r in d["rows"] for k in r))
            out.append(md_table({"headers": cols, "rows": [[_cell(r.get(c)) for c in cols] for r in d["rows"]]}))
            out.append("")

    if opts.get("include_sources", True):
        out += ["## Sources", ""] + [f"- [{d['title']}]({d['source_url']})" for d in docs]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip() + "\n"


def _cell(v) -> str:  # noqa: ANN001
    if v is None:
        return ""
    if isinstance(v, list):
        return ", ".join(map(str, v))
    return str(v)
