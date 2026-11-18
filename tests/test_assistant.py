"""Regression checks for inventory results and the model integration."""

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from threading import Thread
import unittest

from assistant import ModelUnavailable, ollama_plan, query_inventory
from inventory import Plan, run_tool, snapshot


@contextmanager
def model_stub(plan):
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            received.append((self.path, request))
            body = json.dumps({"message": {"content": json.dumps(plan)}}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}", received
        finally:
            server.shutdown()
            thread.join()


class AssistantTest(unittest.TestCase):
    def test_metrics(self):
        self.assertEqual(snapshot()["metrics"], {
            "products": 14, "units": 178, "reorder": 5, "value_cents": 737290,
        })

    def test_answers(self):
        cases = [
            ("What needs reordering?", "reorder", 5, "20 units"),
            ("Do we have any webcams?", "lookup", 1, "0 units"),
            ("Which accessories have the most stock?", "compare", 3, "Braided USB-C Cable"),
            ("What is our current inventory value?", "valuation", 14, "$7,372.90"),
            ("Which products are out of stock?", "out_of_stock", 2, "2 products"),
            ("What needs reordering in Networking?", "reorder", 0, "No products"),
            ("What about NET-402?", "lookup", 1, "19 units"),
        ]
        for question, tool, count, expected in cases:
            with self.subTest(question=question):
                result = query_inventory(question)
                self.assertEqual(result["plan"]["tool"], tool)
                self.assertEqual(len(result["rows"]), count)
                self.assertIn(expected, result["answer"])

    def test_invalid_inputs(self):
        for question, mode in [("", "demo"), ("a" * 281, "demo"), ("Hello", "other"), (42, "demo")]:
            with self.subTest(question=question, mode=mode):
                with self.assertRaises(ValueError):
                    query_inventory(question, mode)

    def test_lookup_handles_punctuation_as_data(self):
        self.assertEqual(run_tool(Plan("lookup", product="USB'")), [])
        self.assertEqual(snapshot()["metrics"]["products"], 14)

    def test_structured_model_provider(self):
        with model_stub({"tool": "lookup", "category": "", "product": "PER-503"}) as (endpoint, received):
            plan = ollama_plan("Do we have a webcam?", "test-model", endpoint)
        self.assertEqual(run_tool(plan)[0]["stock"], 0)
        path, request = received[0]
        self.assertEqual(path, "/api/chat")
        self.assertEqual(request["model"], "test-model")
        self.assertFalse(request["stream"])
        self.assertIn("valuation", request["format"]["properties"]["tool"]["enum"])

    def test_rejects_malformed_or_unavailable_model(self):
        with model_stub({"tool": "raw_query", "category": "", "product": ""}) as (endpoint, _):
            with self.assertRaises(ModelUnavailable):
                ollama_plan("Inventory?", "test-model", endpoint)


if __name__ == "__main__":
    unittest.main()
