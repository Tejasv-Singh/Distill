"""Parse Markdown into simple blocks with inline runs, shared by the DOCX/PPTX/PDF/TXT renderers."""
from __future__ import annotations

from markdown_it import MarkdownIt

_md = MarkdownIt("commonmark", {"html": False, "linkify": False}).enable(["table", "strikethrough"])


def inline_runs(token) -> list[dict]:  # noqa: ANN001
    """Inline token -> [{text, bold, italic, code, link}]."""
    runs: list[dict] = []
    bold = italic = 0
    link: str | None = None
    for c in token.children or []:
        t = c.type
        if t == "text":
            runs.append({"text": c.content, "bold": bold > 0, "italic": italic > 0, "code": False, "link": link})
        elif t == "code_inline":
            runs.append({"text": c.content, "bold": bold > 0, "italic": italic > 0, "code": True, "link": link})
        elif t in ("softbreak", "hardbreak"):
            runs.append({"text": " " if t == "softbreak" else "\n", "bold": False, "italic": False, "code": False, "link": None})
        elif t == "strong_open":
            bold += 1
        elif t == "strong_close":
            bold -= 1
        elif t == "em_open":
            italic += 1
        elif t == "em_close":
            italic -= 1
        elif t == "link_open":
            link = c.attrs.get("href")
        elif t == "link_close":
            link = None
        elif t == "image":
            runs.append({"image": c.attrs.get("src"), "text": c.content or "", "bold": False,
                         "italic": False, "code": False, "link": None})
    return runs


def runs_text(runs: list[dict]) -> str:
    return "".join(r.get("text", "") for r in runs if "image" not in r)


def parse(md: str) -> list[dict]:
    """Blocks: heading{level,runs} paragraph{runs} list{ordered,items:[{runs,level}]}
    table{headers,rows} (cells are text) code{text} quote{runs} image{src,alt} hr"""
    tokens = _md.parse(md)
    blocks: list[dict] = []
    i = 0
    list_stack: list[dict] = []
    in_quote = 0
    while i < len(tokens):
        t = tokens[i]
        if t.type == "heading_open":
            blocks.append({"type": "heading", "level": int(t.tag[1]), "runs": inline_runs(tokens[i + 1])})
            i += 3
            continue
        if t.type in ("bullet_list_open", "ordered_list_open"):
            if not list_stack:
                blk = {"type": "list", "ordered": t.type == "ordered_list_open", "items": []}
                blocks.append(blk)
                list_stack.append(blk)
            else:
                list_stack.append(list_stack[-1])
            i += 1
            continue
        if t.type in ("bullet_list_close", "ordered_list_close"):
            list_stack.pop()
            i += 1
            continue
        if t.type == "paragraph_open":
            runs = inline_runs(tokens[i + 1])
            if list_stack:
                list_stack[-1]["items"].append({"runs": runs, "level": len(list_stack) - 1})
            elif in_quote:
                blocks.append({"type": "quote", "runs": runs})
            elif len(runs) == 1 and "image" in runs[0]:
                blocks.append({"type": "image", "src": runs[0]["image"], "alt": runs[0]["text"]})
            else:
                imgs = [r for r in runs if "image" in r]
                rest = [r for r in runs if "image" not in r]
                if runs_text(rest).strip():
                    blocks.append({"type": "paragraph", "runs": rest})
                blocks.extend({"type": "image", "src": r["image"], "alt": r["text"]} for r in imgs)
            i += 3
            continue
        if t.type == "blockquote_open":
            in_quote += 1
        elif t.type == "blockquote_close":
            in_quote -= 1
        elif t.type in ("fence", "code_block"):
            blocks.append({"type": "code", "text": t.content.rstrip("\n"), "lang": (t.info or "").strip()})
        elif t.type == "hr":
            blocks.append({"type": "hr"})
        elif t.type == "table_open":
            headers: list[str] = []
            rows: list[list[str]] = []
            row: list[str] = []
            in_head = False
            j = i + 1
            while tokens[j].type != "table_close":
                tj = tokens[j]
                if tj.type == "thead_open":
                    in_head = True
                elif tj.type == "thead_close":
                    in_head = False
                elif tj.type == "tr_open":
                    row = []
                elif tj.type == "inline":
                    row.append(runs_text(inline_runs(tj)))
                elif tj.type == "tr_close":
                    if in_head:
                        headers = row
                    else:
                        rows.append(row)
                j += 1
            blocks.append({"type": "table", "headers": headers, "rows": rows})
            i = j + 1
            continue
        i += 1
    return blocks
