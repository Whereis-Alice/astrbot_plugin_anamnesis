"""Serve browser fixtures with consistent JavaScript MIME types on Windows.

Run: python tests/browser/serve.py
Open: http://127.0.0.1:8766/tests/browser/profile-layout.html
Only synthetic fixture data is used; no AstrBot instance is required.
"""

import argparse
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class FixtureHandler(SimpleHTTPRequestHandler):
    # Windows registry MIME mappings may otherwise serve .js as text/plain,
    # which browsers correctly reject for ES modules.
    extensions_map = {
        **SimpleHTTPRequestHandler.extensions_map,
        ".js": "text/javascript",
        ".mjs": "text/javascript",
    }

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    handler = partial(FixtureHandler, directory=str(root))
    with ThreadingHTTPServer(("127.0.0.1", args.port), handler) as server:
        print(f"Open http://127.0.0.1:{args.port}/tests/browser/profile-layout.html", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
