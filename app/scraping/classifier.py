"""Rule-based website-type classification from structural signals.

Fast, free and explainable. The AI pipeline can refine it, but generation never depends
on an LLM being available just to know what kind of page this is.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

SITE_TYPES = ["article", "blog", "news", "documentation", "ecommerce", "product",
              "research", "report", "forum", "listing", "homepage", "other"]

_PRICE = re.compile(r"(?:[$€£₹¥]\s?\d[\d,]*(?:\.\d{2})?|\d[\d,]*(?:\.\d{2})?\s?(?:USD|EUR|GBP|INR))")


def classify(soup: BeautifulSoup, url: str, doc: dict) -> tuple[str, dict]:
    path = urlsplit(url).path.lower()
    host = (urlsplit(url).hostname or "").lower()
    text = soup.get_text(" ", strip=True)[:200000]
    meta = doc.get("metadata", {})
    ld_types = {t.lower() for t in meta.get("json_ld_types", [])}
    og = (meta.get("og_type") or "").lower()
    scores: dict[str, float] = {k: 0.0 for k in SITE_TYPES}
    signals: dict[str, list[str]] = {}

    def add(kind: str, pts: float, why: str) -> None:
        scores[kind] += pts
        signals.setdefault(kind, []).append(why)

    # structured data is the strongest signal
    if ld_types & {"newsarticle", "reportagenewsarticle"}:
        add("news", 5, "JSON-LD NewsArticle")
    if ld_types & {"blogposting", "blog"}:
        add("blog", 5, "JSON-LD BlogPosting")
    if "article" in ld_types:
        add("article", 4, "JSON-LD Article")
    if "product" in ld_types:
        add("product", 5, "JSON-LD Product")
    if ld_types & {"scholarlyarticle", "medicalscholarlyarticle"}:
        add("research", 5, "JSON-LD ScholarlyArticle")
    if ld_types & {"techarticle", "apireference"}:
        add("documentation", 5, "JSON-LD TechArticle")
    if ld_types & {"discussionforumposting", "qapage"}:
        add("forum", 5, "JSON-LD forum/Q&A")
    if ld_types & {"itemlist", "collectionpage", "offercatalog"}:
        add("listing", 3, "JSON-LD list")
    if og == "article":
        add("article", 2, "og:type=article")
    if og == "product":
        add("product", 3, "og:type=product")

    # URL shape
    if re.search(r"/(docs?|documentation|reference|api|guide|manual|handbook)(/|$)", path) or host.startswith("docs."):
        add("documentation", 3, "docs URL")
    if re.search(r"/(blog|posts?)(/|$)", path) or host.startswith("blog."):
        add("blog", 3, "blog URL")
    if re.search(r"/(news|story|stories)/", path) or re.search(r"/20\d\d/\d\d/", path):
        add("news", 2, "news/date URL")
    if re.search(r"/(product|item|dp|p)/", path):
        add("product", 2, "product URL")
    if re.search(r"/(category|catalogue|catalog|collections?|shop|search|tag)(/|$)", path):
        add("listing", 2, "category URL")
    if re.search(r"(arxiv\.org|doi\.org|/abs/|/pdf/|pubmed|researchgate|sciencedirect)", url):
        add("research", 4, "research host")
    title = (doc.get("title") or "").lower()
    numeric_tables = sum(1 for t in doc.get("tables", []) if t["rows"] and sum(
        1 for r in t["rows"] for c in r[1:] if re.fullmatch(r"[\s£$€₹%.,\d-]+", c or "")) >= len(t["rows"]))
    if re.search(r"\b(report|results|statistics|figures|annual|quarterly|survey)\b", title + " " + path):
        add("report", 2.5, "report title/URL")
    if numeric_tables:
        add("report", 1.5 * min(numeric_tables, 3), f"{numeric_tables} numeric tables")
    if path in ("", "/", "/index.html"):
        add("homepage", 1.5, "root path")

    # page content
    prices = len(_PRICE.findall(text))
    add_to_cart = len(soup.select('[class*="cart" i], [id*="cart" i], button[name*="add" i]'))
    if prices >= 6 and not numeric_tables:
        add("listing", 2, f"{prices} prices")
        add("ecommerce", 2, f"{prices} prices")
    elif prices >= 1 and add_to_cart:
        add("product", 2, "price + cart")
    if add_to_cart:
        add("ecommerce", 1.5, "cart elements")
    code_blocks = len(soup.find_all("pre"))
    if code_blocks >= 2:
        add("documentation", 1.5 + min(code_blocks, 6) * 0.3, f"{code_blocks} code blocks")
    if re.search(r"\b(abstract|references|doi:|et al\.)", text[:20000], re.I) and \
            re.search(r"\breferences\b", text, re.I):
        add("research", 2, "abstract/references")
    if doc.get("author") and doc.get("published_date"):
        add("article", 1.5, "author + date")
        add("blog", 1, "author + date")
        add("news", 1, "author + date")
    if soup.find("article"):
        add("article", 1, "<article> element")
    cards = len(soup.select("article, li.product, .product, .card, .item"))
    if cards >= 6:
        add("listing", 1.5, f"{cards} repeated cards")
    if re.search(r"\b(reply|replies|upvote|posted by|thread)\b", text[:30000], re.I) and \
            len(soup.select('[class*="comment" i], [class*="reply" i]')) > 5:
        add("forum", 2, "reply threads")

    # product pages on stores also look like ecommerce
    if scores["product"] >= 3:
        scores["ecommerce"] += 1

    best = max(scores, key=lambda k: scores[k])
    if scores[best] < 1.5:
        best = "article" if doc.get("stats", {}).get("word_count", 0) > 400 else "other"
    # 'ecommerce' alone is vague; prefer product/listing when they're close
    if best == "ecommerce":
        for alt in ("product", "listing"):
            if scores[alt] >= scores["ecommerce"] - 0.5:
                best = alt
                break
    top = sorted(((k, round(v, 1)) for k, v in scores.items() if v > 0), key=lambda x: -x[1])[:4]
    return best, {"scores": dict(top), "reasons": signals.get(best, [])}
