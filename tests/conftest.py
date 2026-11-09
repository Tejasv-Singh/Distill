"""Test setup: isolated DB/storage, the bundled demo site on a free port, and a fake LLM."""
import os
import socket
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "demo_site"))

_tmp = tempfile.mkdtemp(prefix="distill-test-")


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


DEMO_PORT = _free_port()
os.environ.update({
    "DATABASE_URL": f"sqlite:///{_tmp}/test.db",
    "DATABASE_URL_UNPOOLED": "",          # a developer's .env (e.g. from `neon env pull`) must not leak in
    "POSTGRES_URL_NON_POOLING": "",
    "STORAGE_DIR": f"{_tmp}/files",
    "PRIVATE_HOST_ALLOWLIST": f"localhost:{DEMO_PORT},127.0.0.1:{DEMO_PORT}",
    "RATE_LIMIT_PER_MINUTE": "0",
    "MIN_CRAWL_DELAY": "0",
    "TAKE_SCREENSHOTS": "false",
    "ANTHROPIC_API_KEY": "",
    "OPENAI_API_KEY": "",
})

import functools  # noqa: E402
import http.server  # noqa: E402

import pytest  # noqa: E402

from build_site import OUT, build  # noqa: E402


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):  # noqa: ANN002
        pass

    def do_GET(self):  # noqa: N802
        # test-only endpoints for redirect-based SSRF checks
        if self.path.startswith("/redirect-internal"):
            self.send_response(302)
            self.send_header("Location", "http://169.254.169.254/latest/meta-data/")
            self.end_headers()
            return
        if self.path.startswith("/bot-wall"):
            body = b"<html><title>Just a moment...</title><body>Checking your browser cf-chl-</body></html>"
            self.send_response(503)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()


@pytest.fixture(scope="session")
def demo_url() -> str:
    if not (OUT / "index.html").exists():
        build()
    # the site's sitemap hardcodes port 8765; serve a copy rewritten for our port
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", DEMO_PORT), functools.partial(_Quiet, directory=str(OUT)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://localhost:{DEMO_PORT}"
    srv.shutdown()


class FakeLLM:
    """Deterministic stand-in for an LLM, used to test the pipeline without network calls."""
    name = "fake"
    model = "fake-model"
    fast_model = "fake-fast"

    def __init__(self):
        self.calls = []

    async def complete(self, system, user, *, fast=False, max_tokens=4000, temperature=0.3):  # noqa: ANN001
        self.calls.append({"system": system, "user": user, "fast": fast})
        if fast:  # map step
            return "- Buyers read roughly 400 titles a month [S1]\n- About 60 are chosen [S1]"
        if "Rewrite ONLY the section" in user:
            return "## Step two: reading and scoring\n\nRewritten section text [S1]."
        return ("# Executive summary\n\nNorthwind buyers read roughly 400 new titles a month and choose about 60 [S1].\n\n"
                "## Key points\n\n- Two buyers read every shortlisted book [S1].\n- Revenue grew 999% [S1][S7].\n\n"
                "## AI analysis\n\n**AI interpretation:** The process favours quality over volume.\n\n## Sources\n\n- junk")


@pytest.fixture()
def fake_llm():
    from app.ai.providers import set_provider
    llm = FakeLLM()
    set_provider(llm)
    yield llm
    set_provider(None)
