"""HTML -> the standard document representation.

Every scraped page becomes one document dict:

{
  "source_url", "title", "description", "author", "published_date", "language",
  "site_name", "site_type", "headings": [{level, text}],
  "content": [blocks...],           # ordered, hierarchy preserved
  "tables": [{caption, headers, rows}],
  "images": [{src, alt}], "links": [{text, url, internal}],
  "metadata": {...}, "stats": {...}, "scraped_at", "extractor"
}

Block types: heading{level,text} | paragraph{text,md} | list{ordered,items} |
table{table} (index into tables) | code{text} | quote{text} | image{src,alt}
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup, NavigableString, Tag

from ..security import clean_text
from .classifier import classify

try:
    import trafilatura
except ImportError:  # pragma: no cover
    trafilatura = None

# Elements that are never content
_ALWAYS_DROP = "script, style, noscript, template, iframe, svg, canvas, form, button, input, select, textarea, dialog"
_LAYOUT_DROP = "nav, footer, aside, [role=navigation], [role=banner], [role=contentinfo], [aria-hidden=true], [hidden]"
# class/id tokens that mark ads, cookie banners, popups and other clutter
_CLUTTER = re.compile(
    r"(^|[\s_-])(cookie|cookies|consent|gdpr|cc-banner|onetrust|advert|advertisement|ads?|adsbygoogle|"
    r"sponsor(ed)?|promo|banner-ad|newsletter|subscribe|popup|modal|overlay|share|social|related-posts|"
    r"breadcrumbs?|skip-link|sidebar|comments?)([\s_-]|$)",
    re.I,
)


def _attr_tokens(el: Tag) -> str:
    cls = el.get("class") or []
    return " ".join(cls if isinstance(cls, list) else [cls]) + " " + (el.get("id") or "")


def remove_clutter(soup: BeautifulSoup) -> int:
    removed = 0
    for el in soup.select(_ALWAYS_DROP):
        el.decompose()
        removed += 1
    for el in list(soup.find_all(True)):
        if getattr(el, "decomposed", False):
            continue
        if el.name in ("html", "body", "main", "article"):
            continue
        try:
            tokens = _attr_tokens(el)
        except AttributeError:
            continue
        if tokens.strip() and _CLUTTER.search(tokens):
            el.decompose()
            removed += 1
    return removed


# ------------------------------------------------------------------- metadata
def _meta(soup: BeautifulSoup, *names: str) -> str | None:
    for n in names:
        el = soup.find("meta", attrs={"property": n}) or soup.find("meta", attrs={"name": n}) \
            or soup.find("meta", attrs={"itemprop": n})
        if el and el.get("content"):
            return clean_text(el["content"].strip(), 2000)
    return None


def _json_ld(soup: BeautifulSoup) -> list[dict]:
    out: list[dict] = []
    for s in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            data = json.loads(s.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        items = data if isinstance(data, list) else [data]
        for it in items:
            if isinstance(it, dict) and "@graph" in it and isinstance(it["@graph"], list):
                out.extend(x for x in it["@graph"] if isinstance(x, dict))
            elif isinstance(it, dict):
                out.append(it)
    return out[:20]


def _ld_author(ld: list[dict]) -> str | None:
    for it in ld:
        a = it.get("author")
        if isinstance(a, list) and a:
            a = a[0]
        if isinstance(a, dict) and a.get("name"):
            return str(a["name"])
        if isinstance(a, str):
            return a
    return None


def extract_metadata(soup: BeautifulSoup, url: str) -> dict:
    ld = _json_ld(soup)
    h1 = soup.find("h1")
    h1_text = " ".join(h1.get_text(" ", strip=True).split()) if h1 else ""
    page_title = " ".join(soup.title.get_text(" ", strip=True).split()) if soup.title else ""
    site = _meta(soup, "og:site_name", "application-name")
    title = _meta(soup, "og:title", "twitter:title")
    if not title:
        if h1_text and page_title and h1_text.lower() in page_title.lower():
            title = h1_text            # "<h1> | Site name" -> "<h1>"
        elif page_title:
            title = re.sub(r"\s+[|\-–—·:]\s+" + re.escape(site) + r"$", "", page_title) if site else page_title
        else:
            title = h1_text or url
    author = (_meta(soup, "author", "article:author", "dc.creator", "parsely-author")
              or _ld_author(ld))
    if not author:
        a = soup.select_one('[rel="author"], .author, .byline, [itemprop="author"]')
        if a:
            author = a.get_text(" ", strip=True)[:120] or None
    date = _meta(soup, "article:published_time", "datePublished", "date", "dc.date", "pubdate")
    if not date:
        for it in ld:
            if it.get("datePublished"):
                date = str(it["datePublished"])
                break
    if not date:
        t = soup.find("time", attrs={"datetime": True})
        if t:
            date = t["datetime"]
    html_el = soup.find("html")
    canonical = soup.find("link", rel="canonical")
    keywords = _meta(soup, "keywords")
    return {
        "title": clean_text(title, 500),
        "description": _meta(soup, "description", "og:description", "twitter:description"),
        "author": clean_text(author, 200) or None,
        "published_date": date,
        "language": (html_el.get("lang") if html_el else None) or _meta(soup, "og:locale"),
        "site_name": site or urlsplit(url).hostname,
        "metadata": {
            "canonical_url": urljoin(url, canonical["href"]) if canonical and canonical.get("href") else None,
            "og_type": _meta(soup, "og:type"),
            "og_image": urljoin(url, _meta(soup, "og:image")) if _meta(soup, "og:image") else None,
            "keywords": [k.strip() for k in keywords.split(",")][:20] if keywords else [],
            "json_ld_types": sorted({str(t) for it in ld for t in
                                     (it.get("@type") if isinstance(it.get("@type"), list) else [it.get("@type")]) if t}),
            "structured_data": ld[:5],
        },
    }


# ------------------------------------------------------------------- tables
def parse_table(t: Tag) -> dict | None:
    rows: list[list[str]] = []
    headers: list[str] = []
    for tr in t.find_all("tr"):
        if tr.find_parent("table") is not t:
            continue  # nested table
        cells = tr.find_all(["th", "td"], recursive=False)
        vals = [clean_text(" ".join(c.get_text(" ", strip=True).split()), 2000) for c in cells]
        if not any(vals):
            continue
        is_header = all(c.name == "th" for c in cells) or (tr.find_parent("thead") is not None)
        if is_header and not headers and not rows:
            headers = vals
        else:
            rows.append(vals)
    if not rows and not headers:
        return None
    width0 = max(len(r) for r in rows) if rows else 0
    kv_table = not headers and width0 == 2 and all(
        (tr.find(["th", "td"]) or tr).name == "th" for tr in t.find_all("tr") if tr.find_parent("table") is t and tr.find(["th", "td"]))
    width = max([len(headers)] + [len(r) for r in rows])
    if width < 1 or (width == 1 and len(rows) < 2):
        return None
    if kv_table:
        headers = ["Field", "Value"]
    headers = headers + [""] * (width - len(headers)) if headers else [f"Column {i + 1}" for i in range(width)]
    rows = [r + [""] * (width - len(r)) for r in rows]
    cap = t.find("caption")
    return {"caption": cap.get_text(" ", strip=True) if cap else None, "headers": headers, "rows": rows[:5000]}


# ------------------------------------------------------------------- blocks
def _inline_md(el: Tag, base: str) -> str:
    """Inline text with links/bold/italic/code as Markdown."""
    parts: list[str] = []
    for node in el.children:
        if isinstance(node, NavigableString):
            parts.append(str(node))
        elif isinstance(node, Tag):
            inner = _inline_md(node, base)
            if node.name == "a" and node.get("href"):
                href = urljoin(base, node["href"])
                if href.startswith(("http://", "https://")) and inner.strip():
                    parts.append(f"[{inner.strip()}]({href})")
                else:
                    parts.append(inner)
            elif node.name in ("strong", "b") and inner.strip():
                parts.append(f"**{inner.strip()}**")
            elif node.name in ("em", "i") and inner.strip():
                parts.append(f"*{inner.strip()}*")
            elif node.name == "code" and inner.strip():
                parts.append(f"`{inner.strip()}`")
            elif node.name == "br":
                parts.append(" ")
            else:
                parts.append(inner)
    return " ".join("".join(parts).split())


def _plain(el: Tag) -> str:
    return " ".join(el.get_text(" ", strip=True).split())


def html_to_blocks(container: Tag, base: str) -> tuple[list[dict], list[dict], list[dict]]:
    """Walk content HTML into ordered blocks. Returns (blocks, tables, images)."""
    blocks: list[dict] = []
    tables: list[dict] = []
    images: list[dict] = []
    seen_img: set[str] = set()

    def add_image(img: Tag) -> None:
        src = img.get("src") or img.get("data-src") or ""
        if not src or src.startswith("data:"):
            return
        src = urljoin(base, src)
        try:
            w, h = int(img.get("width", 99)), int(img.get("height", 99))
        except ValueError:
            w = h = 99
        if w <= 2 or h <= 2 or src in seen_img:
            return
        seen_img.add(src)
        item = {"src": src, "alt": clean_text(img.get("alt", ""), 300)}
        images.append(item)
        blocks.append({"type": "image", **item})

    def walk(el: Tag) -> None:
        for child in el.children:
            if not isinstance(child, Tag):
                if isinstance(child, NavigableString) and child.strip() and el.name in ("body", "div", "section", "article", "main"):
                    t = " ".join(child.split())
                    if len(t) > 1:
                        blocks.append({"type": "paragraph", "text": clean_text(t), "md": clean_text(t)})
                continue
            name = child.name
            if name in ("h1", "h2", "h3", "h4", "h5", "h6"):
                t = _plain(child)
                if t:
                    blocks.append({"type": "heading", "level": int(name[1]), "text": clean_text(t, 500)})
            elif name == "p":
                for img in child.find_all("img"):
                    add_image(img)
                t = _plain(child)
                if t:
                    blocks.append({"type": "paragraph", "text": clean_text(t), "md": clean_text(_inline_md(child, base))})
            elif name in ("ul", "ol"):
                items = [clean_text(_inline_md(li, base)) for li in child.find_all("li", recursive=False)]
                items = [i for i in items if i]
                if items:
                    blocks.append({"type": "list", "ordered": name == "ol", "items": items})
            elif name == "table":
                t = parse_table(child)
                if t:
                    tables.append(t)
                    blocks.append({"type": "table", "table": len(tables) - 1})
            elif name == "pre":
                t = child.get_text("\n").strip("\n")
                t = "\n".join(line.rstrip() for line in t.splitlines())
                if t.strip():
                    blocks.append({"type": "code", "text": clean_text(_dedent(t))})
            elif name == "blockquote":
                t = _plain(child)
                if t:
                    blocks.append({"type": "quote", "text": clean_text(t)})
            elif name == "img":
                add_image(child)
            elif name in ("figure", "picture"):
                for img in child.find_all("img"):
                    add_image(img)
                cap = child.find("figcaption")
                if cap and _plain(cap):
                    blocks.append({"type": "paragraph", "text": _plain(cap), "md": f"*{_plain(cap)}*"})
            elif name in ("dl",):
                items = []
                for dt in child.find_all("dt"):
                    dd = dt.find_next_sibling("dd")
                    items.append(f"**{_plain(dt)}**: {_plain(dd) if dd else ''}")
                if items:
                    blocks.append({"type": "list", "ordered": False, "items": items})
            else:
                walk(child)

    walk(container)
    # merge consecutive duplicate paragraphs (common with trafilatura + figure captions)
    out: list[dict] = []
    for b in blocks:
        if out and b["type"] == "paragraph" and out[-1].get("text") == b.get("text"):
            continue
        out.append(b)
    return out, tables, images


def _dedent(t: str) -> str:
    lines = t.splitlines()
    indents = [len(line) - len(line.lstrip()) for line in lines if line.strip()]
    n = min(indents) if indents else 0
    return "\n".join(line[n:] for line in lines)


# ---------------------------------------------------------- main-content pick
def heuristic_main(soup: BeautifulSoup) -> Tag:
    for sel in ("article", "main", "[role=main]", "#content", ".content", ".post", ".entry-content", ".article"):
        el = soup.select_one(sel)
        if el and len(el.get_text(strip=True)) > 200:
            return el
    best, best_len = soup.body or soup, 0
    for el in soup.find_all(["div", "section"]):
        n = sum(len(p.get_text(strip=True)) for p in el.find_all(["p", "li", "tr"], recursive=False))
        n += sum(len(p.get_text(strip=True)) for c in el.find_all(recursive=False) for p in c.find_all("p", recursive=False))
        if n > best_len:
            best, best_len = el, n
    return best


def _content_links(container: Tag, base: str) -> list[dict]:
    host = urlsplit(base).hostname
    links, seen = [], set()
    for a in container.find_all("a", href=True):
        href = urljoin(base, a["href"]).split("#")[0]
        if not href.startswith(("http://", "https://")) or href in seen:
            continue
        seen.add(href)
        links.append({"text": clean_text(_plain(a), 300), "url": href,
                      "internal": urlsplit(href).hostname == host})
    return links[:500]


def page_links(html: str, base: str, selector: str | None = None) -> list[str]:
    """All followable links on the page (for crawling)."""
    soup = BeautifulSoup(html, "lxml")
    els = soup.select(selector) if selector else soup.find_all("a", href=True)
    out = []
    for a in els:
        href = a.get("href") if isinstance(a, Tag) else None
        if href and not href.startswith(("javascript:", "mailto:", "tel:", "#", "data:")):
            out.append(urljoin(base, href).split("#")[0])
    return list(dict.fromkeys(out))


# ------------------------------------------------------------------ entry point
def extract_document(html: str, url: str, rendered: bool = False) -> dict:
    full = BeautifulSoup(html, "lxml")
    meta = extract_metadata(full, url)
    signals_soup = BeautifulSoup(html, "lxml")  # untouched copy for classification

    work = BeautifulSoup(html, "lxml")
    remove_clutter(work)
    cleaned_html = str(work)

    extractor = "heuristic"
    container: Tag | None = None
    if trafilatura is not None:
        try:
            out = trafilatura.extract(
                cleaned_html, url=url, output_format="html", include_tables=True,
                include_images=True, include_links=True, include_formatting=True,
                include_comments=False, favor_recall=True, deduplicate=True,
            )
        except Exception:  # noqa: BLE001
            out = None
        if out:
            out = _fix_inline_code(out)
            tsoup = BeautifulSoup(out, "lxml")
            if len(tsoup.get_text(" ", strip=True)) > 150:
                container = tsoup.body or tsoup
                extractor = "trafilatura"

    if container is None:
        for el in work.select(_LAYOUT_DROP):
            el.decompose()
        container = heuristic_main(work)

    blocks, tables, images = html_to_blocks(container, url)

    # Trafilatura sometimes drops product grids / card layouts. If the heuristic finds
    # substantially more content, prefer it.
    if extractor == "trafilatura":
        h_soup = BeautifulSoup(cleaned_html, "lxml")
        for el in h_soup.select(_LAYOUT_DROP):
            el.decompose()
        h_main = heuristic_main(h_soup)
        h_blocks, h_tables, h_images = html_to_blocks(h_main, url)
        h_heads = sum(b["type"] == "heading" for b in h_blocks)
        t_heads = sum(b["type"] == "heading" for b in blocks)
        if _words(h_blocks) > 1.6 * _words(blocks) + 40 or len(h_tables) > len(tables) \
                or (h_heads >= t_heads + 3 and _words(h_blocks) >= _words(blocks) * 0.9):
            blocks, tables, images, container, extractor = h_blocks, h_tables, h_images, h_main, "heuristic"

    # Trafilatura flattens <caption>/<thead>. Swap each extracted table for the matching
    # table from the original HTML, which keeps the real caption and header row.
    if tables:
        originals = [t for t in (parse_table(el) for el in work.find_all("table")) if t]
        for i, t in enumerate(tables):
            cells = {c for r in t["rows"] for c in r if c}
            best, score = None, 0.0
            for o in originals:
                oc = {c for r in o["rows"] for c in r if c} | set(o["headers"])
                if not oc or not cells:
                    continue
                s = len(cells & oc) / len(cells | oc)
                if s > score:
                    best, score = o, s
            if best and score >= 0.6:
                tables[i] = best

    # images inside non-<img> wrappers can be missed; add og:image as a fallback
    og = meta["metadata"].get("og_image")
    if og and not any(i["src"] == og for i in images):
        images.insert(0, {"src": og, "alt": "Social preview image"})

    headings = [{"level": b["level"], "text": b["text"]} for b in blocks if b["type"] == "heading"]
    words = _words(blocks) + sum(len(" ".join(" ".join(r) for r in t["rows"]).split()) for t in tables)

    doc = {
        "source_url": url,
        "title": meta["title"],
        "description": meta["description"],
        "author": meta["author"],
        "published_date": meta["published_date"],
        "language": meta["language"],
        "site_name": meta["site_name"],
        "site_type": None,
        "headings": headings,
        "content": blocks,
        "tables": tables,
        "images": images[:60],
        "links": _content_links(container, url),
        "metadata": meta["metadata"],
        "stats": {
            "word_count": words,
            "reading_minutes": max(1, round(words / 230)),
            "headings": len(headings),
            "paragraphs": sum(1 for b in blocks if b["type"] == "paragraph"),
            "tables": len(tables),
            "images": len(images),
            "links": 0,
        },
        "rendered_with_browser": rendered,
        "extractor": extractor,
        "scraped_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    doc["stats"]["links"] = len(doc["links"])
    doc["site_type"], doc["site_type_signals"] = classify(signals_soup, url, doc)
    return doc


_PRE_ONE_LINE = re.compile(r"<pre>([^<\n]{1,120})</pre>")


def _fix_inline_code(html: str) -> str:
    """Trafilatura emits inline <code> as <pre>, which breaks paragraphs apart. Real code
    blocks come out as nested <pre><pre>…</pre></pre>; single-level one-liners were inline."""
    def repl(m: re.Match) -> str:
        before = html[max(0, m.start() - 40):m.start()].rstrip()
        after = html[m.end():m.end() + 40].lstrip()
        if before.endswith("<pre>") or after.startswith("</pre>"):
            return m.group(0)
        return f"<code>{m.group(1)}</code>"
    return _PRE_ONE_LINE.sub(repl, html)


def _words(blocks: list[dict]) -> int:
    n = 0
    for b in blocks:
        if b["type"] == "list":
            n += sum(len(i.split()) for i in b["items"])
        elif "text" in b:
            n += len(b["text"].split())
    return n


def document_text(doc: dict, with_tables: bool = True) -> str:
    """Plain-text rendering of a document (used for AI input and TXT export)."""
    lines: list[str] = []
    for b in doc.get("content", []):
        t = b["type"]
        if t == "heading":
            lines.append("\n" + "#" * b["level"] + " " + b["text"])
        elif t == "paragraph":
            lines.append(b["text"])
        elif t == "list":
            lines.extend((f"{i + 1}. " if b["ordered"] else "- ") + it for i, it in enumerate(b["items"]))
        elif t == "quote":
            lines.append("> " + b["text"])
        elif t == "code":
            lines.append("```\n" + b["text"] + "\n```")
        elif t == "table" and with_tables:
            tb = doc["tables"][b["table"]]
            lines.append(" | ".join(tb["headers"]))
            lines.extend(" | ".join(r) for r in tb["rows"])
    return "\n".join(lines).strip()
