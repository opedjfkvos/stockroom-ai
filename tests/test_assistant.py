"""Regression checks for inventory results and the model integration."""

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from tempfile import TemporaryDirectory
from threading import Thread
import unittest
from unittest.mock import patch

from assistant import ModelUnavailable, UnsupportedQuestion, ollama_plan, plan_question, query_inventory
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
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        environment = patch.dict("os.environ", {"STOCKROOM_DB": directory.name + "/inventory.sqlite3"})
        environment.start()
        self.addCleanup(environment.stop)

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

    def test_demo_catalogue_word_forms_and_valuation(self):
        for question, sku, tool in (
            ("Do we have any mice?", "PER-502", "lookup"),
            ("Do we have any cables?", "ACC-102", "lookup"),
            ("Show stock of USB Drive", "STO-602", "lookup"),
            ("What is the value of STO-601?", "STO-601", "valuation"),
        ):
            with self.subTest(question=question):
                result = query_inventory(question)
                self.assertEqual(result["plan"]["tool"], tool)
                self.assertEqual([row["sku"] for row in result["rows"]], [sku])
        self.assertIn("1 product needs replenishment", query_inventory("What needs reordering in Audio?")["answer"])

    def test_demo_usb_search_and_single_product_valuation(self):
        result = query_inventory("Do we have USB?")
        self.assertEqual({row["sku"] for row in result["rows"]}, {"ACC-101", "ACC-102", "AUD-201", "STO-602"})
        self.assertIn("Found 4 matching products", result["answer"])
        self.assertIn("product name or SKU", result["answer"])
        self.assertEqual(
            query_inventory("What is the value of STO-601?")["answer"],
            "Current inventory value is $792.00 across 1 product.",
        )

    def test_invalid_inputs(self):
        for question, mode in [("", "demo"), ("a" * 281, "demo"), ("Hello", "other"), (42, "demo")]:
            with self.subTest(question=question, mode=mode):
                with self.assertRaises(ValueError):
                    query_inventory(question, mode)

    def test_out_of_scope_demo(self):
        for question in (
            "Hello", "How many products did we sell?", "How do I repair a USB Headset?",
            "Which product has the highest price?", "What is the value of pi?",
            "Show stock from yesterday",
        ):
            with self.subTest(question=question):
                with self.assertRaisesRegex(UnsupportedQuestion, "I can check current stock"):
                    query_inventory(question)

    def test_out_of_scope_model_does_not_query_inventory(self):
        refusal = {"tool": "unsupported", "category": "", "product": ""}
        with model_stub(refusal) as (endpoint, received):
            def route_to_stub(question, model):
                return ollama_plan(question, model, endpoint)

            with patch("assistant.ollama_plan", side_effect=route_to_stub):
                with patch("inventory.connect") as connect, patch("assistant.run_tool") as tool:
                    tool.side_effect = run_tool
                    with self.assertRaises(UnsupportedQuestion):
                        query_inventory("Tell me a joke", "ollama")
                    tool.assert_not_called()
                    connect.assert_not_called()
            self.assertEqual(len(received), 1)
        with patch("assistant.ollama_plan") as model:
            with self.assertRaises(UnsupportedQuestion):
                query_inventory("Update stock for PER-503", "ollama")
            model.assert_not_called()

    def test_lookup_handles_punctuation_as_data(self):
        self.assertEqual(run_tool(Plan("lookup", product="USB'")), [])
        self.assertEqual(snapshot()["metrics"]["products"], 14)

    def test_ollama_filters_are_grounded_in_the_question(self):
        for question, proposed, expected in (
            ("What is the entire stock worth?", Plan("valuation", "Audio"), Plan("valuation")),
            ("List what we have in Displays", Plan("lookup", "Audio"), Plan("lookup", "Displays")),
            ("How many webcams are on hand?", Plan("lookup", "Audio", "mouse"),
             Plan("lookup", product="PER-503")),
        ):
            with self.subTest(question=question), patch("assistant.ollama_plan", return_value=proposed):
                self.assertEqual(plan_question(question, "ollama", "test-model"), expected)
        with patch("assistant.ollama_plan", return_value=Plan("lookup", "Audio")):
            with self.assertRaisesRegex(UnsupportedQuestion, "Specify a product"):
                plan_question("Show me those items", "ollama", "test-model")

    def test_ollama_refuses_unsupported_intents_before_calling_model(self):
        with patch("assistant.ollama_plan") as model:
            for question in ("What is shipping for the router?", "Which product is cheapest?",
                             "Will we run out of webcams by Tuesday?"):
                with self.subTest(question=question), self.assertRaises(UnsupportedQuestion):
                    plan_question(question, "ollama", "test-model")
            model.assert_not_called()

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
