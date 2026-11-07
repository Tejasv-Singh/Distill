"""End-to-end API flows against the bundled demo site."""
import time

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def wait(client, url, key="status", timeout=90):
    end = time.time() + timeout
    while time.time() < end:
        r = client.get(url).json()
        if r[key] not in ("queued", "running"):
            return r
        time.sleep(0.3)
    raise AssertionError(f"timed out waiting for {url}")


def test_full_workflow(client, demo_url, fake_llm):
    # 1. scrape
    r = client.post("/api/projects", json={"urls": [f"{demo_url}/blog/how-we-pick-books.html"]})
    assert r.status_code == 201
    pid = r.json()["project_id"]
    p = wait(client, f"/api/projects/{pid}")
    assert p["status"] == "done" and p["page_count"] == 1 and p["site_type"] == "blog"
    assert p["job"]["stage"] == "done" and p["job"]["progress"] == 1.0

    # 2. structured preview of the stored document
    page = client.get(f"/api/projects/{pid}/pages/{p['pages'][0]['id']}").json()
    assert page["document"]["tables"]
    assert "Step one" in client.get(f"/api/projects/{pid}/text").text

    # 3. several outputs at once, one of them AI
    items = [{"kind": "convert", "format": f} for f in ("pdf", "docx", "xlsx", "pptx", "json")]
    items.append({"kind": "ai", "format": "executive_summary", "download_format": "docx"})
    outs = client.post(f"/api/projects/{pid}/outputs", json={"items": items, "options": {"style": "casual"}}).json()
    assert len(outs) == 6
    done = [wait(client, f"/api/outputs/{o['id']}") for o in outs]
    assert all(o["status"] == "done" for o in done), [o["error_message"] for o in done]
    for o in done:
        dl = client.get(f"/api/outputs/{o['id']}/download")
        assert dl.status_code == 200 and len(dl.content) > 200
        assert "attachment" in dl.headers["content-disposition"]
    ai = next(o for o in done if o["kind"] == "ai")
    assert ai["file_format"] == "docx" and ai["warnings"]

    # 4. edit -> file rebuilt
    edited = client.put(f"/api/outputs/{ai['id']}", json={"content": "# Edited title\n\nNew body."}).json()
    assert edited["title"] == "Edited title"
    prev = client.get(f"/api/outputs/{ai['id']}/preview.html")
    assert "Edited title" in prev.text and "default-src 'none'" in prev.headers["content-security-policy"]

    # 5. same content, another format
    copy = client.post(f"/api/outputs/{ai['id']}/render", json={"format": "pdf"}).json()
    copy = wait(client, f"/api/outputs/{copy['id']}")
    assert copy["status"] == "done" and client.get(f"/api/outputs/{copy['id']}/download").content[:4] == b"%PDF"

    # 6. section regeneration
    md = "# T\n\n## Step two: reading and scoring\n\nOld [S1]\n"
    client.put(f"/api/outputs/{ai['id']}", json={"content": md})
    client.post(f"/api/outputs/{ai['id']}/regenerate", json={"section": "Step two: reading and scoring"})
    o = wait(client, f"/api/outputs/{ai['id']}")
    assert "Rewritten section text" in client.get(f"/api/outputs/{ai['id']}").json()["content"]

    # 7. data exports can't be edited
    xlsx = next(o for o in done if o["format"] == "xlsx")
    assert client.put(f"/api/outputs/{xlsx['id']}", json={"content": "x"}).status_code == 400

    # 8. history, rename, delete
    assert any(x["id"] == pid for x in client.get("/api/projects").json())
    assert client.patch(f"/api/projects/{pid}", json={"name": "Renamed"}).json()["name"] == "Renamed"
    assert len([x for x in client.get("/api/outputs").json() if x["project_id"] == pid]) == 7
    assert client.delete(f"/api/projects/{pid}").status_code == 204
    assert client.get(f"/api/projects/{pid}").status_code == 404


def test_crawl_with_sitemap_robots_and_patterns(client, demo_url):
    r = client.post("/api/projects", json={
        "urls": [f"{demo_url}/index.html"], "mode": "crawl",
        "crawl": {"max_pages": 6, "max_depth": 2, "use_sitemap": True, "include_patterns": ["/catalogue/"],
                  "exclude_patterns": ["book-1"], "delay_seconds": 0}})
    p = wait(client, f"/api/projects/{r.json()['project_id']}")
    urls = [pg["url"] for pg in p["pages"]]
    assert p["status"] == "done" and 1 < len(urls) <= 6
    assert all("book-1" not in u for u in urls)
    assert all("/catalogue/" in u or u.endswith("index.html") for u in urls)


def test_pagination_and_records(client, demo_url):
    r = client.post("/api/projects", json={
        "urls": [f"{demo_url}/catalogue/page-1.html"], "mode": "crawl",
        "crawl": {"max_pages": 10, "max_depth": 0, "next_page_selector": "li.next a", "delay_seconds": 0},
        "data_extraction": {"item_selector": "article.product_pod",
                            "fields": [{"name": "title", "selector": "h3 a", "attr": "title"},
                                       {"name": "price", "selector": ".price_color"}]}})
    p = wait(client, f"/api/projects/{r.json()['project_id']}")
    assert [pg["rows"] for pg in p["pages"]] == [8, 8, 8]
    out = client.post(f"/api/projects/{p['id']}/outputs", json={"items": [{"kind": "convert", "format": "csv"}]}).json()[0]
    wait(client, f"/api/outputs/{out['id']}")
    csv_text = client.get(f"/api/outputs/{out['id']}/download").content.decode("utf-8-sig")
    assert csv_text.splitlines()[0] == "source_url,title,price" and len(csv_text.splitlines()) == 25


def test_errors_are_reported_not_stuck(client, demo_url):
    r = client.post("/api/projects", json={"urls": [f"{demo_url}/admin/index.html", f"{demo_url}/missing.html",
                                                    "http://169.254.169.254/latest/"]})
    p = wait(client, f"/api/projects/{r.json()['project_id']}")
    assert p["status"] == "failed"
    kinds = {e["kind"] for e in p["job"]["errors"]}
    assert {"robots", "not_found", "blocked_url"} <= kinds
    assert client.post(f"/api/projects/{p['id']}/outputs", json={"items": [{"kind": "convert", "format": "pdf"}]}).status_code == 409


def test_validation(client):
    assert client.post("/api/projects", json={"urls": ["file:///etc/passwd"]}).status_code == 422
    assert client.post("/api/projects", json={"urls": []}).status_code == 422
    r = client.post("/api/projects", json={"urls": ["https://example.com"], "crawl": {"max_pages": 100000}})
    assert r.status_code == 422


def test_ai_disabled_without_key(client, demo_url):
    r = client.post("/api/projects", json={"urls": [f"{demo_url}/docs/api.html"]})
    p = wait(client, f"/api/projects/{r.json()['project_id']}")
    resp = client.post(f"/api/projects/{p['id']}/outputs", json={"items": [{"kind": "ai", "format": "faq"}]})
    assert resp.status_code == 503 and "ANTHROPIC_API_KEY" in resp.json()["detail"]


def test_security_headers(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "script-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY"
