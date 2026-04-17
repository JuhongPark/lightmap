"""Stdlib static server with transparent pre-gzipped file support.

Drop-in replacement for `python -m http.server` that knows how to serve
pre-gzipped sidecars (e.g. docs/shadows.geojson.gz) with a
`Content-Encoding: gzip` header so the browser decompresses them
transparently. Used by scripts/render_bench.py for the r5-gzip render
strategy.

Behavior
--------
On every GET request:

  1. If the client's Accept-Encoding header contains "gzip" AND a sibling
     file exists at <path>.gz, serve that file with:
        - Content-Type guessed from the original <path> (so .geojson
          stays application/geo+json, not application/gzip)
        - Content-Encoding: gzip
        - Vary: Accept-Encoding
        - Content-Length: size of the .gz file on disk
     The browser's fetch() API unwraps the encoding before handing data
     to JS, so page code needs no changes.

  2. Otherwise, fall through to SimpleHTTPRequestHandler and serve the
     plain file.

Usage
-----
    .venv/bin/python scripts/serve.py 8765 docs
    .venv/bin/python scripts/serve.py --port 8765 --root docs
    .venv/bin/python scripts/serve.py            # defaults to 8765, docs/
"""

from __future__ import annotations

import argparse
import os
import sys
from http import HTTPStatus
from http.server import HTTPServer, SimpleHTTPRequestHandler


REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DEFAULT_ROOT = os.path.join(REPO_ROOT, "docs")
DEFAULT_PORT = 8765


class GzipStaticHandler(SimpleHTTPRequestHandler):
    """Serves `file.gz` sidecar with Content-Encoding: gzip if present."""

    def send_head(self):
        path = self.translate_path(self.path)
        if os.path.isdir(path):
            return super().send_head()

        accepts_gz = "gzip" in (self.headers.get("Accept-Encoding", "") or "")
        gz_path = path + ".gz"
        if accepts_gz and os.path.isfile(gz_path) and os.path.isfile(path):
            # Both the plain and .gz files exist. Only serve the .gz if
            # its mtime is at least as recent as the plain file's —
            # otherwise a stale .gz from a previous build (e.g. when the
            # plain sidecar was just regenerated without also re-gzipping)
            # would mask the fresh content. The correct fallback in that
            # case is to serve the plain file.
            try:
                if os.path.getmtime(gz_path) < os.path.getmtime(path):
                    return super().send_head()
            except OSError:
                return super().send_head()
            # Serve the gz but label it with the plain file's content
            # type so the browser knows what the decoded bytes mean.
            try:
                f = open(gz_path, "rb")
                fs = os.fstat(f.fileno())
            except OSError:
                return super().send_head()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", self.guess_type(path))
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Vary", "Accept-Encoding")
            self.send_header("Content-Length", str(fs.st_size))
            self.send_header("Last-Modified",
                             self.date_time_string(fs.st_mtime))
            self.end_headers()
            return f

        return super().send_head()


def run(port: int, root: str) -> None:
    root = os.path.abspath(root)
    if not os.path.isdir(root):
        print(f"error: {root} is not a directory", file=sys.stderr)
        sys.exit(2)
    os.chdir(root)
    server = HTTPServer(("", port), GzipStaticHandler)
    print(f"serving {root} on http://localhost:{port}/ (gzip-aware)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutdown")
    finally:
        server.server_close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("port", nargs="?", type=int, default=DEFAULT_PORT)
    parser.add_argument("root", nargs="?", default=DEFAULT_ROOT)
    parser.add_argument("--port", dest="port_kw", type=int, default=None)
    parser.add_argument("--root", dest="root_kw", default=None)
    args = parser.parse_args()

    port = args.port_kw if args.port_kw is not None else args.port
    root = args.root_kw if args.root_kw is not None else args.root
    run(port, root)
    return 0


if __name__ == "__main__":
    sys.exit(main())
