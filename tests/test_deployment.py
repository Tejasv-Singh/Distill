"""Deployment readiness: Vercel (read-only app directory, /tmp scratch, hosted Postgres,
database-backed files, inline jobs) and unchanged local behaviour.

Each scenario runs the app in a fresh interpreter so module-level settings see that
scenario's environment. An audit hook makes every write inside the application directory
fail with the same error Vercel raises: OSError [Errno 30] Read-only file system.
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

READONLY_PRELUDE = r'''
import os, sys
APP_ROOT = os.path.realpath(sys.argv[1])
WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC

def _inside(p):
    try:
        p = os.path.realpath(os.fsdecode(p))
    except Exception:
        return False
    return p == APP_ROOT or p.startswith(APP_ROOT + os.sep)

def _deny(p):
    raise OSError(30, "Read-only file system", os.fsdecode(p))

def _hook(event, args):
    if event == "open":
        path, mode, flags = args
        if path is None or isinstance(path, int):
            return
        wants_write = (isinstance(mode, str) and any(c in mode for c in "wax+")) or (flags or 0) & WRITE_FLAGS
        if wants_write and _inside(path):
            _deny(path)
    elif event in ("os.mkdir", "os.remove", "os.rmdir", "os.rename", "os.replace", "os.truncate", "shutil.rmtree"):
        if args and isinstance(args[0], (str, bytes, os.PathLike)) and _inside(args[0]):
            _deny(args[0])
    elif event == "sqlite3.connect":
        db = args[0]
        if isinstance(db, (str, bytes, os.PathLike)) and str(db) not in (":memory:", "") and _inside(db):
            _deny(db)

sys.addaudithook(_hook)
sys.path.insert(0, APP_ROOT)
'''

DEMO_SERVER = r'''
import functools, http.server, socket, threading
sys.path.insert(0, os.path.join(APP_ROOT, "demo_site"))
from build_site import OUT
class _Q(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a): pass
_s = socket.socket(); _s.bind(("127.0.0.1", 0)); DEMO_PORT = _s.getsockname()[1]; _s.close()
_srv = http.server.ThreadingHTTPServer(("127.0.0.1", DEMO_PORT), functools.partial(_Q, directory=str(OUT)))
threading.Thread(target=_srv.serve_forever, daemon=True).start()
DEMO = f"http://localhost:{DEMO_PORT}"
os.environ["PRIVATE_HOST_ALLOWLIST"] = f"localhost:{DEMO_PORT}"
'''

_SCRUB = ("DATABASE_URL", "DATABASE_URL_UNPOOLED", "POSTGRES_URL_NON_POOLING", "POSTGRES_URL", "POSTGRES_PRISMA_URL", "STORAGE_DIR", "STORAGE_BACKEND", "RUNTIME_DIR",
          "VERCEL", "SERVERLESS", "JOB_MODE", "ALLOW_EPHEMERAL_STORAGE", "PRIVATE_HOST_ALLOWLIST", "JS_RENDERING",
          "TAKE_SCREENSHOTS", "STALE_JOB_MINUTES")


def run_app(script: str, env: dict, demo: bool = False, timeout: int = 240) -> dict:
    """Run `script` in a fresh interpreter with a read-only application directory.
    The script must print one JSON line prefixed with RESULT=."""
    full_env = {k: v for k, v in os.environ.items() if k not in _SCRUB}
    # Blank rather than drop: the app fills *missing* variables from the repo's .env/.env.local,
    # so a developer's JS_RENDERING=true or Neon DATABASE_URL would otherwise leak into the test.
    # The app treats an empty value as unset.
    full_env.update({k: "" for k in _SCRUB})
    full_env.update({"PYTHONDONTWRITEBYTECODE": "1", "RATE_LIMIT_PER_MINUTE": "0", "MIN_CRAWL_DELAY": "0"})
    full_env.update(env)
    code = READONLY_PRELUDE + (DEMO_SERVER if demo else "") + textwrap.dedent(script)
    proc = subprocess.run([sys.executable, "-c", code, str(ROOT)], env=full_env, capture_output=True,
                          text=True, timeout=timeout)
    if proc.returncode != 0:
        raise AssertionError(f"app process failed ({proc.returncode})\nSTDOUT:\n{proc.stdout[-3000:]}\n"
                             f"STDERR:\n{proc.stderr[-6000:]}")
    line = next((ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT=")), None)
    assert line, f"no RESULT line\n{proc.stdout[-2000:]}\n{proc.stderr[-2000:]}"
    return json.loads(line[len("RESULT="):])


# ---------------------------------------------------------------- PostgreSQL
@pytest.fixture(scope="module")
def postgres_url():
    """A real PostgreSQL server (PGlite, Postgres compiled to WASM) if available."""
    try:
        from py_pglite import PGliteConfig, PGliteManager
    except ImportError:
        pytest.skip("py-pglite not installed (pip install -r requirements-dev.txt)")
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    class _Manager(PGliteManager):
        def _install_dependencies(self, work_dir):
            # py-pglite runs a bare ["npm", "install"]; on Windows npm is npm.cmd, which
            # CreateProcess can't find (WinError 2). Install via the resolved path first.
            npm = shutil.which("npm")
            if os.name == "nt" and npm and not (Path(work_dir) / "node_modules").exists():
                subprocess.run([npm, "install"], cwd=work_dir, capture_output=True,
                               text=True, check=True, timeout=300)
            super()._install_dependencies(work_dir)

    try:
        mgr = _Manager(PGliteConfig(use_tcp=True, tcp_port=port, timeout=120))
        mgr.start()
    except Exception as e:  # noqa: BLE001  (needs Node.js)
        pytest.skip(f"could not start PGlite: {e}")
    # the form Vercel/Neon hand out: plain postgresql:// (normalised by the app)
    yield f"postgresql://postgres:postgres@127.0.0.1:{port}/postgres?sslmode=disable"
    mgr.stop()


# --------------------------------------------------------------------- tests
def test_regression_vercel_import_crash_is_fixed():
    """The production error: importing the app on Vercel tried to mkdir /var/task/data."""
    out = run_app('''
        import json
        import app.main   # previously: OSError [Errno 30] Read-only file system: '.../data'
        from app.config import settings
        print("RESULT=" + json.dumps({"runtime_dir": str(settings.runtime_dir), "serverless": settings.serverless}))
    ''', {"VERCEL": "1"})
    # Vercel is Linux; on a Windows test host str(Path) uses backslashes for the same path
    assert Path(out["runtime_dir"]).as_posix() == "/tmp/distill" and out["serverless"] is True


def test_import_never_writes_locally_either():
    out = run_app('''
        import json
        import app.main
        from app.config import settings
        print("RESULT=" + json.dumps({"runtime": str(settings.runtime_dir)}))
    ''', {})
    assert out["runtime"] == str(ROOT / "data")   # local path unchanged, but not created at import


def test_vercel_full_workflow_on_postgres(postgres_url):
    out = run_app('''
        import json
        from fastapi.testclient import TestClient
        from app.main import app
        from app.config import settings
        from app.storage import get_storage
        r = {}
        with TestClient(app) as c:
            h = c.get("/api/health")
            r["health"] = (h.status_code, h.json())
            # inline job mode: the scrape is finished when the request returns
            p = c.post("/api/projects", json={"urls": [DEMO + "/blog/how-we-pick-books.html"]})
            pid = p.json()["project_id"]
            proj = c.get(f"/api/projects/{pid}").json()
            r["project"] = (p.status_code, proj["status"], proj["page_count"], proj["has_preview"])
            # PGlite's TCP bridge drops messages over ~64 KB (real PostgreSQL takes bytea up to 1 GB),
            # so the ~100 KB PDF is covered by the database-storage test below instead.
            outs = c.post(f"/api/projects/{pid}/outputs", json={"items": [
                {"kind": "convert", "format": f} for f in ("docx", "xlsx", "pptx", "markdown", "json", "csv")],
                "options": {"include_images": False}}).json()   # keeps every file under PGlite's limit
            r["outputs"] = sorted((o["format"], o["status"], o["has_file"]) for o in outs)
            magic = {}
            for o in outs:
                d = c.get(f"/api/outputs/{o['id']}/download")
                magic[o["format"]] = (d.status_code, d.content[:4].hex())
            r["magic"] = magic
            key = f"{pid}/outputs/{outs[0]['id']}"
            r["stored_in_db"] = get_storage().name
            r["local_files"] = os.path.exists(str(settings.storage_dir))
            ed = c.put(f"/api/outputs/{[o for o in outs if o['format']=='markdown'][0]['id']}",
                       json={"content": "# Edited on Vercel\\n\\nBody"})
            r["edit"] = (ed.status_code, ed.json().get("title"))
            r["delete"] = c.delete(f"/api/projects/{pid}").status_code
            from app.db import StoredFile, session
            with session() as s:
                r["files_left"] = s.query(StoredFile).filter(StoredFile.key.startswith(pid)).count()
        print("RESULT=" + json.dumps(r))
    ''', {"VERCEL": "1", "DATABASE_URL": postgres_url, "DATABASE_URL_UNPOOLED": postgres_url}, demo=True)
    code, health = out["health"]
    assert code == 200 and health["ok"] and health["database_ok"]
    assert health["platform"] == "vercel" and health["database"] == "postgresql" and health["persistent"]
    assert health["job_mode"] == "inline" and health["storage_backend"] == "database"
    assert out["project"] == [201, "done", 1, False]                 # no Chromium on Vercel: no screenshot
    assert all(status == "done" and has for _, status, has in out["outputs"]), out["outputs"]
    m = out["magic"]
    assert m["docx"] == m["xlsx"] == m["pptx"] == [200, "504b0304"]   # zip containers, served from Postgres
    assert m["markdown"][0] == m["json"][0] == m["csv"][0] == 200
    assert out["stored_in_db"] == "database" and out["local_files"] is False
    assert out["edit"] == [200, "Edited on Vercel"]
    assert out["delete"] == 204 and out["files_left"] == 0


def test_vercel_without_database_refuses_clearly(tmp_path):
    out = run_app('''
        import json
        from fastapi.testclient import TestClient
        from app.main import app
        with TestClient(app) as c:
            h = c.get("/api/health"); p = c.get("/api/projects"); cfg = c.get("/api/config"); ui = c.get("/")
            post = c.post("/api/projects", json={"urls": ["https://example.com"]})
        print("RESULT=" + json.dumps({"health": [h.status_code, h.json()["problems"]], "projects": [p.status_code, p.json()],
                                      "config": cfg.status_code, "problems": cfg.json()["deployment"]["problems"],
                                      "ui": ui.status_code, "post": post.status_code}))
    ''', {"VERCEL": "1", "RUNTIME_DIR": str(tmp_path)})
    assert out["health"][0] == 503 and "DATABASE_URL" in out["health"][1][0]
    assert out["projects"][0] == 503 and out["projects"][1]["kind"] == "deployment"
    assert "DATABASE_URL" in out["projects"][1]["detail"]
    assert out["config"] == 200 and out["problems"] and out["ui"] == 200 and out["post"] == 503
    assert not list(tmp_path.iterdir())          # nothing silently created


def test_vercel_ephemeral_demo_mode_is_explicit(tmp_path):
    out = run_app('''
        import json
        from fastapi.testclient import TestClient
        from app.main import app
        with TestClient(app) as c:
            h = c.get("/api/health").json()
            p = c.post("/api/projects", json={"urls": [DEMO + "/docs/api.html"]}).json()
            st = c.get(f"/api/projects/{p['project_id']}").json()["status"]
            o = c.post(f"/api/projects/{p['project_id']}/outputs", json={"items": [{"kind": "convert", "format": "pdf"}]}).json()[0]
            pdf = c.get(f"/api/outputs/{o['id']}/download").content
        from app.storage import get_storage
        print("RESULT=" + json.dumps({"ok": h["ok"], "warnings": h["warnings"], "status": st, "pdf": pdf[:4].hex(),
                                      "pdf_size": len(pdf), "backend": get_storage().name}))
    ''', {"VERCEL": "1", "ALLOW_EPHEMERAL_STORAGE": "1", "RUNTIME_DIR": str(tmp_path)}, demo=True)
    assert out["ok"] and out["status"] == "done" and "temporary storage" in out["warnings"][0]
    assert out["pdf"] == "25504446" and out["backend"] == "database"   # %PDF (ReportLab, no browser) via DB storage
    assert (tmp_path / "app.db").exists()        # the scratch DB lives in the runtime dir, not the app dir


def test_local_defaults_unchanged():
    out = run_app('''
        import json
        from app.config import settings
        print("RESULT=" + json.dumps({"db": settings.database_url, "files": str(settings.storage_dir),
              "backend": settings.storage_backend, "jobs": settings.job_mode, "js": settings.js_rendering,
              "shots": settings.take_screenshots, "problems": settings.deployment_problems()}))
    ''', {})
    assert out == {"db": f"sqlite:///{ROOT / 'data' / 'app.db'}", "files": str(ROOT / "data" / "files"),
                   "backend": "local", "jobs": "background", "js": True, "shots": True, "problems": []}


def test_database_url_normalisation():
    from app.config import normalize_database_url as n
    assert n("postgres://u:p@h:5432/db") == "postgresql+psycopg://u:p@h:5432/db"
    assert n("postgresql://u:p@h/db?sslmode=require") == "postgresql+psycopg://u:p@h/db?sslmode=require"
    assert n("postgresql+psycopg://u@h/db") == "postgresql+psycopg://u@h/db"
    assert n("sqlite:////tmp/x.db") == "sqlite:////tmp/x.db"


# ----------------------------------------------------------------- storage
def test_local_storage_keys_and_legacy_paths(tmp_path):
    from app.storage import LocalStorage, StorageError
    st = LocalStorage(tmp_path / "files")
    st.put("p1/outputs/a.pdf", b"%PDF-1")
    assert st.get("p1/outputs/a.pdf") == b"%PDF-1" and st.exists("p1/outputs/a.pdf")
    legacy = str(tmp_path / "files" / "p1" / "outputs" / "a.pdf")      # old rows stored absolute paths
    assert st.get(legacy) == b"%PDF-1"
    outside = tmp_path / "secret.txt"
    outside.write_text("nope")
    assert st.get(str(outside)) is None                                   # never outside the root
    for bad in ("../x", "p1/../../x", "/etc/passwd", ""):
        with pytest.raises(StorageError):
            st.put(bad, b"x")
    st.delete_prefix("p1")
    assert not (tmp_path / "files" / "p1").exists()


def test_database_storage_roundtrip():
    from app.db import ensure_db
    from app.storage import DatabaseStorage
    ensure_db()
    st = DatabaseStorage()
    st.put("proj_x/outputs/a.csv", b"a,b\n1,2\n", "text/csv")
    st.put("proj_x/preview.png", b"\x89PNG")
    st.put("proj_xy/outputs/b.csv", b"keep")          # same prefix characters, different project
    assert st.get("proj_x/outputs/a.csv") == b"a,b\n1,2\n"
    st.put("proj_x/outputs/a.csv", b"v2")               # overwrite
    assert st.get("proj_x/outputs/a.csv") == b"v2"
    big = os.urandom(3 * 1024 * 1024)                    # multi-MB binary survives the round trip
    st.put("proj_x/outputs/big.pdf", big, "application/pdf")
    assert st.get("proj_x/outputs/big.pdf") == big
    st.delete_prefix("proj_x")
    assert st.get("proj_x/preview.png") is None and st.get("proj_xy/outputs/b.csv") == b"keep"


def test_stale_jobs_are_reaped_not_left_spinning():
    from app.db import GeneratedOutput, Project, ensure_db, local_user_id, session
    from app.main import reap_interrupted
    ensure_db()
    old = datetime.now(timezone.utc) - timedelta(hours=2)
    with session() as s:
        p = Project(user_id=local_user_id(), name="reap", source_url="https://example.com", config={})
        s.add(p)
        s.flush()
        stale = GeneratedOutput(project_id=p.id, kind="convert", format="pdf", status="running")
        fresh = GeneratedOutput(project_id=p.id, kind="convert", format="csv", status="running")
        s.add_all([stale, fresh])
        s.flush()
        sid, fid = stale.id, fresh.id
    with session() as s:   # backdate without triggering onupdate
        s.execute(GeneratedOutput.__table__.update().where(GeneratedOutput.id == sid).values(updated_at=old))
    reap_interrupted(only_stale=True)
    with session() as s:
        assert s.get(GeneratedOutput, sid).status == "failed"
        assert "Interrupted" in s.get(GeneratedOutput, sid).error_message
        assert s.get(GeneratedOutput, fid).status == "running"


def test_direct_url_used_only_for_schema(tmp_path):
    """Like Neon: two URLs (pooled for the app, direct for DDL) reaching the same database."""
    out = run_app("""
        import json
        from sqlalchemy import event
        from sqlalchemy.engine import Engine
        ddl_urls, write_urls = set(), set()

        @event.listens_for(Engine, "before_cursor_execute")
        def _record(conn, cursor, statement, params, context, executemany):
            verb = statement.lstrip().split(None, 1)[0].upper()
            if verb == "CREATE":
                ddl_urls.add(str(conn.engine.url))
            elif verb in ("INSERT", "UPDATE", "DELETE"):
                write_urls.add(str(conn.engine.url))

        from app.config import settings
        from app.db import ensure_db, engine, local_user_id
        ensure_db()
        local_user_id()
        from sqlalchemy.engine import make_url
        # str(URL) percent-encodes Windows paths (C:\\ -> C%3A%5C), so compare URLs in that same form
        print("RESULT=" + json.dumps({"app": str(engine.url), "direct": str(make_url(settings.database_url_direct)),
                                      "ddl": sorted(ddl_urls), "writes": sorted(write_urls)}))
    """, {"DATABASE_URL": f"sqlite:///{tmp_path}/neon.db",
          "DATABASE_URL_UNPOOLED": f"sqlite:///{tmp_path}/neon.db?timeout=30"})
    assert out["direct"].endswith("neon.db?timeout=30") and out["app"].endswith("neon.db")
    assert out["ddl"] == [out["direct"]]          # every CREATE TABLE went over the direct URL
    assert out["writes"] == [out["app"]]          # application writes (the local user row) use the pooled URL
