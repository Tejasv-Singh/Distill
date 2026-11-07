import asyncio

from app.scraping.extractor import extract_document, remove_clutter
from app.scraping.fetcher import Fetcher, looks_like_js_shell
from app.scraping.discovery import url_passes
from app.scraping.selectors import extract_rows
from app.schemas import FieldSpec
from bs4 import BeautifulSoup


def fetch(url, mode="static"):
    async def go():
        f = Fetcher()
        try:
            return await f.fetch(url, mode)
        finally:
            await f.close()
    return asyncio.run(go())


def doc_for(demo_url, path):
    p = fetch(f"{demo_url}/{path}")
    return extract_document(p.html, p.url, p.rendered)


def test_article_extraction(demo_url):
    d = doc_for(demo_url, "blog/how-we-pick-books.html")
    assert d["title"] == "How we pick the books we stock"
    assert d["author"] == "Priya Raman"
    assert d["published_date"].startswith("2026-09-14")
    assert d["site_type"] == "blog"
    heads = [h["text"] for h in d["headings"]]
    assert "Step two: reading and scoring" in heads
    text = " ".join(b.get("text", "") for b in d["content"])
    for clutter in ("cookies", "ADVERTISEMENT", "Popular posts", "Subscribe", "Great post"):
        assert clutter not in text, clutter
    assert d["tables"][0]["headers"] == ["Criterion", "Weight", "What we look for"]
    assert any(i["src"].endswith("reading-room.png") for i in d["images"])
    assert {"source_url", "title", "content", "tables", "images", "links", "metadata", "scraped_at"} <= d.keys()


def test_table_caption_and_headers_preserved(demo_url):
    d = doc_for(demo_url, "reports/sales-2026.html")
    t = d["tables"][0]
    assert t["caption"] == "Units and revenue by month"
    assert t["headers"] == ["Month", "Units sold", "Revenue", "Online share"]
    assert len(t["rows"]) == 9
    assert d["site_type"] == "report"


def test_documentation_keeps_code_and_inline_code(demo_url):
    d = doc_for(demo_url, "docs/api.html")
    assert d["site_type"] == "documentation"
    codes = [b["text"] for b in d["content"] if b["type"] == "code"]
    assert any("import requests" in c for c in codes)
    first_para = next(b for b in d["content"] if b["type"] == "paragraph")
    assert "X-Api-Key" in first_para["text"] and "header" in first_para["text"]  # not split apart


def test_product_and_listing_types(demo_url):
    assert doc_for(demo_url, "catalogue/book-3.html")["site_type"] == "product"
    listing = doc_for(demo_url, "catalogue/page-1.html")
    assert listing["site_type"] == "listing"
    assert sum(b["type"] == "heading" for b in listing["content"]) >= 8   # product titles kept


def test_key_value_table_headers(demo_url):
    t = doc_for(demo_url, "catalogue/book-3.html")["tables"][0]
    assert t["headers"] == ["Field", "Value"]
    assert t["rows"][0][0] == "UPC"


def test_js_rendering(demo_url):
    p = fetch(f"{demo_url}/staff-picks.html", "auto")
    if not p.rendered:
        import pytest
        pytest.skip("no headless browser available")
    d = extract_document(p.html, p.url, True)
    assert "Staff picks this month" in [h["text"] for h in d["headings"]]


def test_js_shell_detection():
    assert looks_like_js_shell('<html><body><div id="root"></div><script>x</script></body></html>')
    assert not looks_like_js_shell("<html><body><p>" + "word " * 400 + "</p></body></html>")


def test_clutter_removal_keeps_content():
    soup = BeautifulSoup('<div class="cookie-banner">c</div><div class="add-to-cart">keep</div>'
                         '<div id="ad-top">ad</div><article class="post">body</article>', "lxml")
    remove_clutter(soup)
    t = soup.get_text()
    assert "body" in t and "keep" in t and "ad" not in t.replace("body", "") and "c" not in t.replace("body", "").replace("keep", "")


def test_url_patterns():
    assert url_passes("https://a.com/blog/x", ["/blog/"], [])
    assert not url_passes("https://a.com/shop/x", ["/blog/"], [])
    assert not url_passes("https://a.com/blog/tag/x", ["/blog/"], ["/tag/"])
    assert url_passes("https://a.com/docs/a/b", ["*/docs/*"], [])
    assert url_passes("https://a.com/p/123", ["re:/p/\\d+$"], [])


def test_precise_rows(demo_url):
    p = fetch(f"{demo_url}/catalogue/page-1.html")
    rows = extract_rows(p.html, p.url, "article.product_pod", [
        FieldSpec(name="title", selector="h3 a", attr="title"),
        FieldSpec(name="price", selector=".price_color"),
        FieldSpec(name="link", selector="h3 a", attr="href")])
    assert len(rows) == 8
    assert rows[0]["price"].startswith("£") and rows[0]["link"].startswith(demo_url)
