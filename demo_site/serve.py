"""Serve the demo site on http://localhost:8765  (python demo_site/serve.py)"""
import functools, http.server, os, sys
from pathlib import Path

ROOT = Path(__file__).parent / "public"
PORT = int(os.getenv("DEMO_PORT", sys.argv[1] if len(sys.argv) > 1 else 8765))

class Handler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a): pass

if __name__ == "__main__":
    if not (ROOT / "index.html").exists():
        from build_site import build; build()
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), functools.partial(Handler, directory=str(ROOT)))
    print(f"Demo site: http://localhost:{PORT}/"); httpd.serve_forever()
