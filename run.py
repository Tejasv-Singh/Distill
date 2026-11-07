"""Start Distill (works the same on Windows, macOS and Linux).

    python run.py            # the app on http://localhost:8000
    python run.py --demo     # also serve the bundled demo site on http://localhost:8765
    python run.py --port 9000 --host 0.0.0.0
"""
from __future__ import annotations

import argparse
import functools
import http.server
import os
import sys
import threading
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def serve_demo(port: int) -> None:
    sys.path.insert(0, str(ROOT / "demo_site"))
    from build_site import OUT, build

    if not (OUT / "index.html").exists():
        build()

    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a):  # noqa: ANN002
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), functools.partial(Quiet, directory=str(OUT)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"  Demo site      http://localhost:{port}/")


def main() -> None:
    ap = argparse.ArgumentParser(description="Run Distill")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--demo", action="store_true", help="also serve the demo website on port 8765")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--reload", action="store_true", help="auto-reload on code changes (development)")
    args = ap.parse_args()

    if args.demo:
        allow = os.environ.get("PRIVATE_HOST_ALLOWLIST", "")
        os.environ["PRIVATE_HOST_ALLOWLIST"] = ",".join(filter(None, [allow, "localhost:8765", "127.0.0.1:8765"]))
        serve_demo(8765)

    import uvicorn

    url = f"http://localhost:{args.port}/"
    print(f"  Distill        {url}\n  API docs       {url}docs\n")
    if not args.no_browser:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    uvicorn.run("app.main:app", host=args.host, port=args.port, reload=args.reload, log_level="info")


if __name__ == "__main__":
    main()
