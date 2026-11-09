"""End-to-end check that Distill works against the configured Postgres (e.g. Neon).

    python scripts/check_neon.py

Reads DATABASE_URL / DATABASE_URL_UNPOOLED from the environment, .env.local or .env (where
`neon deploy` / `neon env pull` put them), then:
  1. checks the configuration (pooled URL for the app, direct URL for schema changes)
  2. connects over both and reports the server version
  3. creates the tables (over the direct URL)
  4. stores, reads and deletes a file through the database storage backend
  5. runs the app in-process: scrape the bundled demo site -> export CSV + PDF -> download -> delete
Passwords are never printed. Exits non-zero on the first failure.
"""
from __future__ import annotations

import functools
import http.server
import os
import re
import socket
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "demo_site"))

# Exercise the production code paths: files in the database, jobs finished inside the request.
os.environ.setdefault("STORAGE_BACKEND", "database")
os.environ.setdefault("JOB_MODE", "inline")
os.environ.setdefault("TAKE_SCREENSHOTS", "false")
os.environ.setdefault("RATE_LIMIT_PER_MINUTE", "0")

# the bundled demo site is the scrape target (it runs on localhost, so allow exactly that port)
_s = socket.socket()
_s.bind(("127.0.0.1", 0))
DEMO_PORT = _s.getsockname()[1]
_s.close()
os.environ["PRIVATE_HOST_ALLOWLIST"] = f"localhost:{DEMO_PORT}"


def redact(url: str | None) -> str:
    return re.sub(r"//([^:/@]+):[^@]*@", r"//\1:****@", url or "(not set)")


def step(n: int, title: str) -> None:
    print(f"\n[{n}] {title}")


def ok(msg: str) -> None:
    print(f"    PASS  {msg}")


def fail(msg: str) -> None:
    print(f"    FAIL  {msg}")
    sys.exit(1)


def main() -> None:
    from app.config import settings

    step(1, "Configuration")
    if not settings.database_explicit:
        fail("DATABASE_URL is not set (run `neon deploy` or `neon env pull`, or set it in .env)")
    if settings.database_is_sqlite:
        fail(f"DATABASE_URL points at SQLite, not Postgres: {redact(settings.database_url)}")
    host = re.search(r"@([^/:?]+)", settings.database_url)
    host = host.group(1) if host else "?"
    ok(f"app connection (DATABASE_URL): {redact(settings.database_url)}")
    print(f"          {'pooled (-pooler host): correct for app traffic' if '-pooler' in host else 'WARNING: not a -pooler host; use the pooled string for the app'}")
    if settings.database_url_direct:
        ok(f"schema connection (DATABASE_URL_UNPOOLED): {redact(settings.database_url_direct)}")
    else:
        print("    NOTE  DATABASE_URL_UNPOOLED not set; tables will be created over the pooled URL")

    step(2, "Connect")
    import sqlalchemy as sa

    from app.db import engine
    t = time.monotonic()
    with engine.connect() as c:
        version = c.execute(sa.text("select version()")).scalar()
        db = c.execute(sa.text("select current_database()")).scalar()
    ok(f"pooled: {version.split(' on ')[0]} · database '{db}' · {time.monotonic() - t:.2f}s (includes any cold start)")
    engine.dispose()  # release pooled connections before opening the direct one
    if settings.database_url_direct:
        de = sa.create_engine(settings.database_url_direct, poolclass=sa.pool.NullPool,
                              connect_args={"connect_timeout": 10, "prepare_threshold": None})
        with de.connect() as c:
            c.execute(sa.text("select 1"))
        de.dispose()
        ok("direct: connected")

    step(3, "Schema")
    from app.db import ensure_db
    ensure_db()
    with engine.connect() as c:
        tables = sorted(r[0] for r in c.execute(sa.text(
            "select table_name from information_schema.tables where table_schema = current_schema()")))
    need = {"users", "projects", "scraped_pages", "scraping_jobs", "generated_outputs", "chat_sessions", "stored_files"}
    if not need <= set(tables):
        fail(f"missing tables: {sorted(need - set(tables))}")
    ok(f"tables present: {', '.join(sorted(need))}")

    step(4, "File storage in the database")
    from app.storage import get_storage
    st = get_storage()
    if st.name != "database":
        fail(f"storage backend is {st.name}, expected database")
    size = int(os.getenv("CHECK_BLOB_BYTES", "300000"))
    blob = os.urandom(size)
    key = "zz-check/neon-roundtrip.bin"
    st.put(key, blob, "application/octet-stream")
    if st.get(key) != blob:
        fail("stored file came back different")
    st.delete_prefix("zz-check")
    if st.get(key) is not None:
        fail("delete did not remove the file")
    ok(f"{size // 1000} KB binary: write → read (byte-identical) → delete")

    step(5, "App end to end (scrape → export → download → delete)")
    from build_site import OUT, build
    if not (OUT / "index.html").exists():
        build()

    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a):  # noqa: ANN002
            pass
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", DEMO_PORT), functools.partial(Quiet, directory=str(OUT)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    from fastapi.testclient import TestClient

    from app.db import StoredFile, session
    from app.main import app
    with TestClient(app) as c:
        h = c.get("/api/health")
        if h.status_code != 200 or not h.json()["ok"]:
            fail(f"/api/health: {h.status_code} {h.text[:300]}")
        hj = h.json()
        ok(f"/api/health ok · database={hj['database']} · persistent={hj['persistent']} · storage={hj['storage_backend']}")
        r = c.post("/api/projects", json={"urls": [f"http://localhost:{DEMO_PORT}/reports/sales-2026.html"]})
        pid = r.json().get("project_id")
        p = c.get(f"/api/projects/{pid}").json()
        if r.status_code != 201 or p["status"] != "done":
            fail(f"scrape: {r.status_code} status={p.get('status')} {p.get('job')}")
        ok(f"scraped '{p['pages'][0]['title']}': {p['totals']['word_count']} words, {p['totals']['tables']} tables")
        outs = c.post(f"/api/projects/{pid}/outputs",
                      json={"items": [{"kind": "convert", "format": "csv"}, {"kind": "convert", "format": "pdf"}]}).json()
        for o in outs:
            if o["status"] != "done":
                fail(f"{o['format']} generation: {o['status']} {o.get('error_message')}")
            d = c.get(f"/api/outputs/{o['id']}/download")
            if d.status_code != 200 or not d.content:
                fail(f"{o['format']} download: {d.status_code}")
            magic = d.content[:4]
            if o["format"] == "pdf" and magic != b"%PDF":
                fail("PDF download is not a PDF")
            ok(f"{o['format'].upper()} generated, stored in Postgres and downloaded ({len(d.content):,} bytes)")
        with session() as s:
            n_before = s.query(StoredFile).filter(StoredFile.key.startswith(pid + "/")).count()
        if c.delete(f"/api/projects/{pid}").status_code != 204:
            fail("delete project")
        with session() as s:
            n_after = s.query(StoredFile).filter(StoredFile.key.startswith(pid + "/")).count()
        if n_after:
            fail(f"{n_after} stored files left after deleting the project")
        ok(f"deleted project and its {n_before} stored files")
    srv.shutdown()
    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    main()
