"""Smoke checks for the desktop model setup transport."""

from http.server import BaseHTTPRequestHandler
import json
from unittest import TestCase

from ollama_setup import installed_models, pull_model
from test_edge_cases import serve


class OllamaSetupTest(TestCase):
    def test_download_reports_progress_and_verifies_installed_model(self):
        tags = []
        requests = []

        class Stub(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path != "/api/tags":
                    self.send_error(404)
                    return
                self.reply(json.dumps({"models": [{"name": model} for model in tags]}).encode())

            def do_POST(self):
                if self.path != "/api/pull":
                    self.send_error(404)
                    return
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append(request)
                if request["model"] == "available:1":
                    tags.append(request["model"])
                    payload = [{"status": "pulling", "total": 100, "completed": 37}, {"status": "success"}]
                else:
                    payload = [{"error": "model could not be pulled"}]
                self.reply(b"\n".join(json.dumps(item).encode() for item in payload) + b"\n")

            def reply(self, body):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(body)

        progress = []
        with serve(Stub) as endpoint:
            self.assertEqual(installed_models(endpoint), set())
            pull_model("available:1", progress.append, endpoint)
            self.assertEqual(installed_models(endpoint), {"available:1"})
            with self.assertRaisesRegex(ValueError, "could not be pulled"):
                pull_model("missing:1", progress.append, endpoint)
        self.assertEqual(requests[0], {"model": "available:1", "stream": True})
        self.assertEqual(progress, [37])

