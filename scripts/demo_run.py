"""Live end-to-end demo against the bundled site. Usage: python scripts/demo_run.py [out_dir]
Assumes `python run.py --demo` is running (app on :8000, demo site on :8765)."""
import json, sys, time
from pathlib import Path
import httpx

B, D = "http://localhost:8000", "http://localhost:8765"
OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "demo_outputs"); OUT.mkdir(exist_ok=True)
c = httpx.Client(timeout=60)

def wait(url):
    for _ in range(600):
        r = c.get(url).json()
        if r["status"] not in ("queued", "running"): return r
        time.sleep(0.5)

def scrape(body, label):
    p = wait(f"{B}/api/projects/{c.post(B + '/api/projects', json=body).json()['project_id']}")
    errs = [(e['url'].replace(D, ''), e['kind']) for e in p['job']['errors']]
    print(f"[scrape] {label}: {p['status']}, {p['page_count']} page(s), type={p['site_type']}, "
          f"{p['word_count']} words" + (f", skipped={errs}" if errs else ""))
    return p

def export(p, items, options, prefix):
    for o in c.post(f"{B}/api/projects/{p['id']}/outputs", json={"items": items, "options": options}).json():
        o = wait(f"{B}/api/outputs/{o['id']}")
        ext = {"markdown": "md"}.get(o["file_format"], o["file_format"])
        name = f"{prefix}_{o['format']}.{ext}"
        if o["status"] == "done":
            (OUT / name).write_bytes(c.get(f"{B}/api/outputs/{o['id']}/download").content)
        print(f"   -> {name:38} {o['status']}" + (f"  ({'; '.join(o['warnings'])[:110]})" if o['warnings'] else "")
              + (f"  ERROR: {o['error_message']}" if o['error_message'] else ""))

blog = scrape({"urls": [D + "/blog/how-we-pick-books.html"]}, "blog article")
export(blog, [{"kind": "convert", "format": f} for f in ("pdf", "docx", "markdown", "html", "txt")],
       {"include_toc": True}, "1_blog")

shop = scrape({"urls": [D + "/catalogue/page-1.html"], "mode": "crawl",
               "crawl": {"max_pages": 10, "max_depth": 0, "next_page_selector": "li.next a", "delay_seconds": 0.5},
               "data_extraction": {"item_selector": "article.product_pod", "fields": [
                   {"name": "title", "selector": "h3 a", "attr": "title"}, {"name": "price", "selector": ".price_color"},
                   {"name": "availability", "selector": ".availability"}, {"name": "url", "selector": "h3 a", "attr": "href"}]}},
              "catalogue with pagination + precise fields")
export(shop, [{"kind": "convert", "format": f} for f in ("csv", "xlsx", "json")], {}, "2_catalogue")

rep = scrape({"urls": [D + "/reports/sales-2026.html", D + "/docs/api.html"]}, "report + docs (multi-URL)")
export(rep, [{"kind": "convert", "format": f} for f in ("pptx", "xlsx", "xml")], {"slide_count": 20}, "3_report")

site = scrape({"urls": [D + "/index.html"], "mode": "crawl",
               "crawl": {"max_pages": 15, "max_depth": 2, "use_sitemap": True, "exclude_patterns": ["/book-"], "delay_seconds": 0.5}},
              "whole-site crawl via sitemap (robots.txt respected)")
print("   pages:", [pg["url"].replace(D, "") for pg in c.get(f"{B}/api/projects/{site['id']}").json()["pages"]])

bad = scrape({"urls": ["http://169.254.169.254/latest/meta-data/", D + "/admin/index.html"]}, "blocked targets")
print("   error shown to user:", bad["job"]["error_message"][:120])
cfg = c.get(B + "/api/config").json()
print(f"\nAI configured: {cfg['ai']['configured']} ({cfg['ai']['provider']})")
print(f"Files written to {OUT.resolve()}")
