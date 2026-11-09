# Distill: turn any website into anything

Paste a URL. Distill fetches the page (or crawls the site), extracts the real content into a
**standard structured document**, and turns that document into the outputs you choose:
documents, spreadsheets, data files, slides, or AI-written reports. Scrape once, then export
as many formats as you like without fetching the site again.

```
URL(s) ──► fetch (static or headless browser, SSRF-guarded)
       ──► extract (Trafilatura + fallbacks: headings, tables, images, links, metadata, site type)
       ──► standard document JSON  ──stored──►  PostgreSQL / SQLite
                                                   │
             ┌─────────────────────────────────────┴─────────────────────────────────────┐
     direct conversion                                                         AI pipeline
  PDF DOCX MD TXT HTML PPTX                                       chunk → map → generate → validate
  CSV XLSX JSON XML                                              (citations, injection defence,
                                                                   number checks) → any doc format
             └────────────────────► preview · edit · regenerate · download ◄──────────────┘
```

## Features

| Area | What you get |
|---|---|
| **Input** | Single URL, a list of URLs, or a whole-site crawl. URL validation. A live 5-stage progress view (connecting → fetching → extracting → structuring → preparing). |
| **Fetching** | Static HTTP with an automatic switch to headless Chromium for JavaScript pages. Infinite-scroll steps and wait-for-selector options. If HTTPS can't connect, retries over HTTP once, like a browser. |
| **Extraction** | Main content via Trafilatura, with a structural fallback for product grids and card layouts. Removes cookie banners, ads, navigation, sidebars, comments and popups. Keeps the heading hierarchy, paragraphs, lists, tables (with captions and header rows), code, quotes, images and links. |
| **Metadata** | Title, description, author, publish date, language, canonical URL, Open Graph, JSON-LD, keywords, and the **site type** (blog, news, article, documentation, product, listing, report, research, forum, homepage) with the signals that decided it. |
| **Crawling** | Max pages and depth (hard caps on the server), include/exclude patterns (substring, glob, or `re:` regex), same-domain lock, sitemap discovery (robots.txt `Sitemap:`, sitemap indexes, gzip), "next page" pagination, robots.txt rules and crawl-delay, a minimum delay between requests, and **page selection** (list candidate pages, then tick exactly the ones you want). |
| **Precise mode** | Optional CSS selectors that turn repeating items (product cards, listings) into records. These feed the CSV/XLSX exports. |
| **Outputs** | PDF, DOCX, Markdown, TXT, HTML, CSV, XLSX, JSON, XML, PPTX, plus 12 AI outputs: detailed summary, executive summary, research report, technical documentation, study notes, blog post, newsletter, comparison report, key insights, FAQ, AI presentation, and a custom output. Select several at once. |
| **Customization** | Title, style, detail level, length, audience, tone, focus, extra instructions; images/tables/sources/table of contents on or off; spreadsheet source, columns, normalization, combining across pages; slide count, theme, charts. |
| **Preview & edit** | Sandboxed document preview, a Markdown editor (saving rebuilds the file), comparison of the extracted content against the output, regenerating the whole output or **one section**, re-exporting the same content in another format, and download. |
| **History** | Projects, generated files, search, rename, delete, re-download, re-scrape. |
| **UI** | Light and dark themes, responsive down to phone width, keyboard-accessible, no placeholder buttons. |

## AI assistant (chat)

Open **Assistant** in the top bar, give it a URL and describe what you want in plain English:

> `https://books.toscrape.com`: title, price and star rating of every book on the first 3 pages

The assistant scrapes the site, follows pagination when you ask for "all pages", extracts
exactly those fields into a table, and shows a preview with **CSV / XLSX / JSON** downloads.
Then keep talking to refine it: *"also add the availability"*, *"only books under £20"*,
*"split price into amount and currency"*, *"export as Excel"*, or ask questions about the page.

How it works: a tool-calling agent (Anthropic or OpenAI) with four tools: `scrape_website`,
`get_page_content`, `extract_data` and `export`. Extraction reads each page in chunks with a fixed
column set, merges and de-duplicates rows, and **flags values that don't appear word-for-word
on the page**. For safety, the agent can **only visit sites whose address you typed**. A
page telling it to fetch another URL is refused, and all scraped text is treated as untrusted
data. Needs an AI key in `.env`.

## Quick start

Requires **Python 3.10+**.

**Windows (PowerShell)**
```powershell
cd C:\coding\Web_Scraper_app
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m playwright install chromium     # for JavaScript sites, screenshots and PDF
copy .env.example .env                    # then add an AI key if you want AI outputs
python run.py --demo
```

**macOS / Linux**
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
cp .env.example .env
python run.py --demo
```

The app opens at **http://localhost:8000**. `--demo` also serves the bundled demo site at
**http://localhost:8765** and allows the app to scrape it (it's on localhost, which is
normally blocked).

### Try it on the demo site

| URL | Shows |
|---|---|
| `http://localhost:8765/blog/how-we-pick-books.html` | Article extraction, author and date, clutter removal, a table, and a prompt-injection line that gets flagged and ignored |
| `http://localhost:8765/catalogue/page-1.html` + Crawl, "next page" = `li.next a` | Pagination. Add precise fields (`article.product_pod`; `h3 a` → `title`; `.price_color`) to get a 24-row spreadsheet |
| `http://localhost:8765/reports/sales-2026.html` | Numeric tables: XLSX sheets and PPTX bar charts |
| `http://localhost:8765/docs/api.html` | Documentation: code blocks and a parameter table |
| `http://localhost:8765/staff-picks.html` | A JavaScript-only page, rendered in the headless browser |
| `http://localhost:8765/index.html` + Crawl + "Use sitemap.xml" | Sitemap discovery, robots.txt blocking `/admin/` |

Real sites to try once you're online: `https://books.toscrape.com/`, `https://quotes.toscrape.com/js/`
(a JavaScript site) and `https://en.wikipedia.org/wiki/Web_scraping`.

## Configuration

Everything comes from environment variables or `.env`. See [`.env.example`](.env.example) for the full list.

| Variable | Default | Purpose |
|---|---|---|
| `AI_PROVIDER` | `anthropic` | `anthropic` or `openai` (any OpenAI-compatible endpoint via `OPENAI_BASE_URL`) |
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` | — | Enables AI outputs. File conversions don't need it. |
| `ANTHROPIC_MODEL`, `ANTHROPIC_FAST_MODEL` | `claude-sonnet-5-5`, `claude-haiku-5-5` | Main model writes the output; fast model reads long pages in chunks |
| `OPENAI_MODEL`, `OPENAI_FAST_MODEL` | `gpt-5`, `gpt-5-mini` | Same, for OpenAI |
| `AI_CHUNK_CHARS` / `AI_MAX_TOTAL_CHARS` | 24 000 / 400 000 | Chunk size and per-output budget (token management) |
| `DATABASE_URL` | SQLite in `data/` | e.g. `postgresql+psycopg://user:pw@host/db` |
| `STORAGE_DIR` | `data/files` | Generated files and screenshots |
| `JS_RENDERING`, `TAKE_SCREENSHOTS` | `true` | Headless browser features |
| `MAX_PAGES_CAP`, `MAX_DEPTH_CAP` | 100, 5 | Server-side crawl limits that requests can't exceed |
| `RATE_LIMIT_PER_MINUTE` | 30 | Per-client limit on scrape and generate calls |
| `MAX_CONCURRENT_JOBS` | 3 | Parallel scrapes / generations |
| `PRIVATE_HOST_ALLOWLIST` | — | `host:port` pairs allowed to resolve to private IPs (for local testing only) |

## Docker

```bash
cp .env.example .env          # add your AI key
docker compose up --build     # app + PostgreSQL on http://localhost:8000
```

The image is based on Microsoft's Playwright image, so Chromium is included. It runs as a
non-root user, generated files persist in the `files` volume, and the database in `pgdata`.

## API

Interactive docs are at **`/docs`** (OpenAPI). Main endpoints:

| Method & path | Purpose |
|---|---|
| `POST /api/projects` | Start a scrape. Body: `{urls[], mode: single\|crawl, render: auto\|static\|js, crawl{…}, data_extraction{…}}` → `{project_id, job_id}` |
| `POST /api/discover` | List candidate pages (sitemap + links) for page selection |
| `GET /api/projects` / `GET /api/projects/{id}` | History / project with job progress, pages, stats, outputs |
| `PATCH` / `DELETE /api/projects/{id}` | Rename / delete (also removes its files) |
| `POST /api/projects/{id}/rescrape` · `/cancel` | Retry or cancel |
| `GET /api/projects/{id}/pages/{page_id}` | The stored standard document |
| `GET /api/projects/{id}/text` · `/preview.png` | All extracted text / screenshot |
| `POST /api/chats` · `POST /api/chats/{id}/messages` | Talk to the extraction assistant (`{message}`); poll `GET /api/chats/{id}` for steps and results |
| `POST /api/projects/{id}/outputs` | Generate one or more outputs: `{items:[{kind: convert\|ai, format, download_format?}], options{…}}` |
| `GET /api/outputs` / `GET /api/outputs/{id}` | All generated files / one output with its content |
| `PUT /api/outputs/{id}` | Save edited Markdown and rebuild the file |
| `POST /api/outputs/{id}/regenerate` | `{section?, instructions?}`: the whole output or one section |
| `POST /api/outputs/{id}/render` | Same content in another document format (as a new output) |
| `GET /api/outputs/{id}/download` · `/preview.html` | File download / sandboxed preview |

Example:
```bash
curl -X POST localhost:8000/api/projects -H 'content-type: application/json' \
  -d '{"urls":["https://books.toscrape.com/"],"mode":"crawl","crawl":{"max_pages":5,"next_page_selector":"li.next a"}}'
```

### The standard document

Every page is stored once as:

```json
{
  "source_url": "...", "title": "...", "description": "...", "author": "...", "published_date": "...",
  "language": "en", "site_name": "...", "site_type": "blog",
  "headings": [{"level": 2, "text": "..."}],
  "content": [{"type": "heading|paragraph|list|table|code|quote|image", "...": "..."}],
  "tables": [{"caption": "...", "headers": ["..."], "rows": [["..."]]}],
  "images": [{"src": "...", "alt": "..."}], "links": [{"text": "...", "url": "...", "internal": true}],
  "metadata": {"canonical_url": "...", "og_type": "...", "json_ld_types": [], "structured_data": []},
  "stats": {"word_count": 0, "reading_minutes": 1, "tables": 0, "images": 0, "links": 0},
  "rows": [], "scraped_at": "2026-11-07T12:00:00+00:00"
}
```

## How the AI pipeline works

1. **Sources.** Each page becomes a numbered source `[S1]…` with its text, tables and outline.
2. **Token management.** Short content goes to the model directly. Long content is split on
   headings into chunks; the fast model extracts exact notes from each chunk (with the page
   outline for context, run in parallel), notes are de-duplicated, and a second reduction
   runs if they're still too long. A hard character budget caps cost.
3. **Generation.** The main model writes Markdown following per-type instructions and your
   options: cite every fact as `[S#]`, keep interpretation in an "AI analysis" section or in
   `AI interpretation:` lines, and never invent figures.
4. **Validation.** Citations to sources that don't exist are removed. **Numbers that don't appear
   in the scraped text are flagged.** Citation coverage is measured, and pages containing
   AI-directed instructions are reported. Warnings appear above the preview.
5. **Rendering.** The Markdown becomes any document format, and you can edit it, rewrite one
   section, or export it again.

Switching providers is one environment variable. Adding one means subclassing `LLMProvider`
in `app/ai/providers.py`.

## Security

* **SSRF protection.** Only http/https, no credentials in URLs. Every hostname is resolved and *all* its
  addresses must be public: loopback, private, link-local (incl. `169.254.169.254` cloud metadata),
  CGNAT, reserved, multicast and IPv4-mapped IPv6 are refused. Connections are **pinned to the
  checked IP** (no DNS-rebinding window). **Every redirect hop is re-validated.** In the headless
  browser, **every request** (documents, XHR, images) goes through the same check; the PDF
  renderer uses the same guarded browser.
* **Limits.** Timeouts, an 8 MB page cap, a redirect cap, crawl page and depth caps on the server,
  minimum crawl delay, robots.txt, per-client rate limiting, and a cap on concurrent jobs.
* **Untrusted content.** Scraped text is wrapped in `<source>` tags that the page can't close;
  the system prompt marks it as data and forbids following instructions inside it. The LLM has
  no tools, so it can't take actions. Injection attempts are detected and shown to the user.
* **Safe rendering.** The UI only inserts scraped text with `textContent`. Markdown is rendered
  with raw HTML disabled, and `javascript:` links are dropped. Previews are served with
  `default-src 'none'` inside a sandboxed iframe. The app sends a strict CSP, `X-Frame-Options`,
  `nosniff` and `no-referrer`.
* **Files & secrets.** Server-generated file names, path checks on download and delete, and API keys
  only on the server.
* **Ethics.** Distill doesn't bypass logins, paywalls or CAPTCHAs. It detects bot walls (Cloudflare and
  similar) and says so. Respect each site's terms and copyright.

## Tests

```bash
python -m pytest -q
```

There are 65 tests covering URL validation and SSRF (private ranges, metadata IP, redirects to internal
hosts), bot-wall detection, extraction on every demo page type, table captions and headers,
inline code, JavaScript rendering, the AI pipeline (with a fake LLM: citation cleanup,
invented-number detection, injection flagging, map-reduce chunking, section rewrites), the chat
assistant (scripted LLM: scrape → extract → refine → export, the host restriction, provider message formats), every
exporter (files are re-opened with their libraries), and full API workflows (crawl, pagination,
records, multi-output generation, edit, re-render, history, delete, error states).

## Deployment

* **Docker Compose** (above) on any VM. Put it behind a reverse proxy (Caddy or Nginx) for TLS.
* **Single process.** `uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers` with
  `DATABASE_URL` pointing at PostgreSQL.
* Jobs run in-process (asyncio with concurrency limits); a restart marks unfinished jobs as failed
  with a retry prompt, so nothing hangs. To scale across machines, move `run_scrape_job` /
  `run_output` onto a queue (Celery, RQ or arq with Redis) and the rate limiter onto Redis. Both
  are already isolated functions.

## Project layout

```
app/
  main.py              API routes, security headers, background tasks
  config.py            settings from env / .env
  db.py                SQLAlchemy models: User, Project, ScrapedPage, ScrapingJob, GeneratedOutput
  schemas.py           request validation
  security.py          URL normalisation, SSRF checks, rate limiting
  generation.py        documents → outputs → files
  scraping/            fetcher (static + guarded browser), extractor, classifier, discovery (robots,
                       sitemaps, patterns), selectors (precise mode), pipeline (the job)
  ai/                  providers (Anthropic, OpenAI), prompts, engine (chunking, validation)
  exporters/           document_md, mdparse, render (HTML/PDF/DOCX/TXT), slides (PPTX), data (CSV/XLSX/JSON/XML)
  static/              the single-page UI (no build step)
demo_site/             Northwind Books: a bundled site for testing
tests/                 pytest suite
```

## Design decisions

* **No Next.js frontend (yet).** The UI is a dependency-free single page served by FastAPI, so
  there's one process, no Node build, and it runs on Windows with `python run.py`. The API is
  complete and documented, so a Next.js + shadcn/ui frontend can replace `app/static` without
  backend changes.
* **SQLAlchemy instead of Prisma.** The backend is Python, and Prisma is a JS/TS ORM; mixing them
  would mean two schemas for one database. The models match the spec's schema.
* **PDF via Chromium**, with a ReportLab fallback when no browser is installed.
* **Authentication** isn't included: this runs as a single-user local tool. The `User` model and
  `user_id` on projects are in place for adding auth.
