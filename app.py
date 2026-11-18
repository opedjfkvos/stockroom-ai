"""Run the Stockroom AI dashboard on localhost."""

import argparse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sqlite3
from urllib.parse import parse_qs, urlsplit

from assistant import EXAMPLES, ModelUnavailable, query_inventory
from inventory import snapshot, use_inventory
from inventory_import import MAX_BYTES, import_inventory, latest_import

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

    def local_host(self):
        host = self.headers.get("Host", "").split(":", 1)[0].lower()
        if host in {"127.0.0.1", "localhost"}:
            return True
        self.json_response(HTTPStatus.FORBIDDEN, {"error": "Open Stockroom AI on localhost."})
        return False

    def do_GET(self):
        if not self.local_host():
            return
        route = urlsplit(self.path)
        path = route.path
        if path == "/api/inventory":
            source = parse_qs(route.query).get("source", ["sample"])[0]
            try:
                with use_inventory(source):
                    currency, imported_at = latest_import() if source == "imported" else ("USD", None)
                    if source == "imported" and not currency:
                        raise ValueError("No imported inventory is available. Choose a stock file and confirm the import.")
                    inventory = snapshot()
            except ValueError as exc:
                self.json_response(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            except sqlite3.Error as exc:
                self.database_error(exc)
            else:
                self.json_response(HTTPStatus.OK, {**inventory, "examples": EXAMPLES, "currency": currency,
                                                   "imported_at": imported_at})
        elif path == "/one-c-stock.csv":
            template = STATIC.parent / "examples" / "one-c-stock.csv"
            self.respond(HTTPStatus.OK, b"\xef\xbb\xbf" + template.read_bytes(), "text/csv; charset=utf-8")
        elif path in ASSETS:
            filename, mime = ASSETS[path]
            self.respond(HTTPStatus.OK, (STATIC / filename).read_bytes(), mime)
        else:
            self.json_response(HTTPStatus.NOT_FOUND, {"error": "Page not found."})

    def do_POST(self):
        if not self.local_host():
            return
        route = urlsplit(self.path)
        if route.path not in ("/api/ask", "/api/import/preview", "/api/import/confirm"):
            self.json_response(HTTPStatus.NOT_FOUND, {"error": "Endpoint not found."})
            return
        try:
            if route.path == "/api/ask":
                payload = json.loads(self.read_body(4096, "application/json"))
                if not isinstance(payload, dict):
                    raise ValueError("Send a JSON object.")
                source = payload.get("source", "sample")
                with use_inventory(source):
                    currency = latest_import()[0] if source == "imported" else "USD"
                    if not currency:
                        raise ValueError("Import a spreadsheet before asking about imported inventory.")
                    result = query_inventory(payload.get("question"), payload.get("mode", "demo"),
                                             currency=currency)
            else:
                result = self.handle_import(route)
        except ValueError as exc:
            self.json_response(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except sqlite3.Error as exc:
            self.database_error(exc)
        except ModelUnavailable as exc:
            self.json_response(HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(exc)})
        else:
            self.json_response(HTTPStatus.OK, result)

    def read_body(self, limit, mime):
        size = int(self.headers.get("Content-Length", "0"))
        if not 0 < size <= limit or self.headers.get_content_type() != mime:
            raise ValueError(f"Send {mime} data up to {limit // 1024:,} KB.")
        return self.rfile.read(size)

    def handle_import(self, route):
        if self.headers.get("X-Stockroom-Import") != "1":
            raise ValueError("Use the Stockroom import form.")
        data = self.read_body(MAX_BYTES, "application/octet-stream")
        params = parse_qs(route.query)
        confirmation = None
        if route.path.endswith("/confirm"):
            confirmation = self.headers.get("X-Stockroom-Confirmation", "")
            if not confirmation:
                raise ValueError("Preview the spreadsheet before importing.")
        return import_inventory(params.get("filename", [""])[0], data, params.get("currency", [""])[0],
                                confirmation=confirmation)

    def database_error(self, exc):
        self.log_error("SQLite inventory error: %s", exc)
        self.json_response(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "The inventory database is unavailable."})


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
