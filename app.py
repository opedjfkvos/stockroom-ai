"""Run the Stockroom AI dashboard on localhost."""

import argparse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from urllib.parse import urlsplit

from assistant import EXAMPLES, ModelUnavailable, query_inventory
from inventory import snapshot

STATIC = Path(__file__).resolve().parent / "static"
ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
}


class Handler(BaseHTTPRequestHandler):
    def respond(self, status, payload, mime):
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(payload)

    def json_response(self, status, data):
        self.respond(status, json.dumps(data).encode(), "application/json; charset=utf-8")

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/api/inventory":
            self.json_response(HTTPStatus.OK, {**snapshot(), "examples": EXAMPLES})
        elif path in ASSETS:
            filename, mime = ASSETS[path]
            self.respond(HTTPStatus.OK, (STATIC / filename).read_bytes(), mime)
        else:
            self.json_response(HTTPStatus.NOT_FOUND, {"error": "Page not found."})

    def do_POST(self):
        if urlsplit(self.path).path != "/api/ask":
            self.json_response(HTTPStatus.NOT_FOUND, {"error": "Endpoint not found."})
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 4096 or self.headers.get_content_type() != "application/json":
                raise ValueError("Send a JSON object up to 4 KB.")
            payload = json.loads(self.rfile.read(size))
            if not isinstance(payload, dict):
                raise ValueError("Send a JSON object.")
            result = query_inventory(payload.get("question"), payload.get("mode", "demo"))
        except ValueError as exc:
            self.json_response(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except ModelUnavailable as exc:
            self.json_response(HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(exc)})
        else:
            self.json_response(HTTPStatus.OK, result)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    with ThreadingHTTPServer(("127.0.0.1", args.port), Handler) as server:
        print(f"Stockroom AI is running at http://127.0.0.1:{server.server_port}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
