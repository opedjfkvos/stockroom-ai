"""Edge-case checks for inventory, model failures and HTTP errors."""

from contextlib import closing, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from threading import Thread
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from app import Handler
from assistant import ModelUnavailable, UnsupportedQuestion, ollama_plan, query_inventory
from inventory import Plan, run_tool, snapshot


@contextmanager
def serve(handler_class):
    with ThreadingHTTPServer(("127.0.0.1", 0), handler_class) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}"
        finally:
            server.shutdown()
            thread.join()


def raw_model_stub(body: bytes):
    """A fake Ollama that always answers with the given raw HTTP body."""

    class Stub(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    return serve(Stub)


def plan_reply(plan):
    return json.dumps({"message": {"content": json.dumps(plan)}}).encode()


class TempDatabaseCase(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db_path = Path(directory.name) / "inventory.sqlite3"
        environment = patch.dict("os.environ", {"STOCKROOM_DB": str(self.db_path)})
        environment.start()
        self.addCleanup(environment.stop)

    def write(self, sql, params=()):
        """Write through a normal connection; the app itself is read-only."""
        snapshot()  # make sure the schema exists first
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute(sql, params)

    def adjust(self, sku, delta):
        self.write(
            "INSERT INTO stock_movements(sku, quantity_delta, reason) VALUES (?, ?, 'Test')",
            (sku, delta),
        )


class StockBoundaryTest(TempDatabaseCase):
    def skus(self, tool, **filters):
        return [row["sku"] for row in run_tool(Plan(tool, **filters))]

    def test_stock_equal_to_minimum_is_not_a_reorder(self):
        # ACC-101 starts at 6 with a minimum of 8.
        self.assertIn("ACC-101", self.skus("reorder"))
        self.adjust("ACC-101", 2)  # now exactly at the minimum
        self.assertNotIn("ACC-101", self.skus("reorder"))
        self.adjust("ACC-101", -1)  # one below the minimum again
        self.assertIn("ACC-101", self.skus("reorder"))

    def test_product_without_movements_counts_as_zero_stock(self):
        self.write("INSERT INTO products(sku, name, category_id, minimum, unit_cents) VALUES "
                   "('ACC-199', 'Cable Tidy', (SELECT id FROM categories WHERE name = 'Accessories'), 5, 500)")
        self.assertIn("ACC-199", self.skus("out_of_stock"))
        self.assertIn("ACC-199", self.skus("reorder"))
        self.assertEqual(snapshot()["metrics"]["products"], 15)

    def test_stock_that_returns_to_zero_is_out_of_stock(self):
        self.adjust("PER-502", -3)  # Wireless Mouse: 3 -> 0
        self.assertIn("PER-502", self.skus("out_of_stock"))

    def test_single_unit_shortage_uses_singular_wording(self):
        self.adjust("ACC-101", 1)  # 7 of 8, the only Accessories item below its minimum
        answer = query_inventory("What needs reordering in Accessories?")["answer"]
        self.assertEqual(answer, "1 product needs replenishment. Add 1 unit to reach its reorder point.")

    def test_reorder_results_are_ordered_by_shortage(self):
        rows = run_tool(Plan("reorder"))
        shortages = [row["stock"] - row["minimum"] for row in rows]
        self.assertEqual(shortages, sorted(shortages))
        self.assertEqual(rows[0]["sku"], "PER-503")  # 0 of 7 is the largest gap

    def test_filters_combine(self):
        self.assertEqual(self.skus("out_of_stock", category="Displays"), ["DSP-301"])
        self.assertEqual(self.skus("out_of_stock", category="Audio"), [])
        self.assertEqual(self.skus("lookup", category="Audio", product="STO-601"), [])

    def test_compare_reports_ties_and_zero_stock(self):
        self.adjust("DSP-301", 7)
        question = "Which Displays product has the highest stock?"
        answer = query_inventory(question)["answer"]
        self.assertIn("27-inch Monitor", answer)
        self.assertIn("Portable Monitor", answer)
        self.assertIn("7 units each", answer)
        self.adjust("DSP-301", -8)
        self.adjust("DSP-302", -7)
        self.assertIn("All matching products", query_inventory(question)["answer"])
        self.assertEqual(self.skus("out_of_stock", category="Displays"), ["DSP-302", "DSP-301"])
        self.assertEqual(next(row for row in snapshot()["rows"] if row["sku"] == "DSP-301")["status"],
                         "Out of stock")

    def test_empty_catalogue_is_not_reported_as_healthy_stock(self):
        self.write("DELETE FROM stock_movements")
        self.write("DELETE FROM products")
        self.assertEqual(query_inventory("What needs reordering?")["answer"],
                         "No inventory records match that request.")


class PlanValidationTest(unittest.TestCase):
    def test_rejects_bad_plans(self):
        for kwargs in (
            {"tool": "raw_query"},
            {"tool": "lookup", "category": "Toys"},
            {"tool": "lookup", "category": "audio"},  # case-sensitive on purpose
            {"tool": "lookup", "product": "x" * 101},
            {"tool": "lookup", "product": 5},
            {"tool": "lookup", "category": ["Audio"]},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    Plan(**kwargs)

    def test_accepts_boundary_values(self):
        self.assertEqual(Plan("lookup", product="x" * 100).product, "x" * 100)
        self.assertEqual(Plan("valuation").to_dict(), {"tool": "valuation", "category": "", "product": ""})


class DemoRoutingEdgeCasesTest(TempDatabaseCase):
    def test_question_length_boundaries(self):
        with self.assertRaisesRegex(UnsupportedQuestion, "I can check current stock"):
            query_inventory("a" * 280)  # allowed length, but not a supported question
        with self.assertRaisesRegex(ValueError, "1 to 280") as caught:
            query_inventory("a" * 281)
        self.assertNotIsInstance(caught.exception, UnsupportedQuestion)
        with self.assertRaisesRegex(ValueError, "1 to 280"):
            query_inventory("   \n\t ")

    def test_case_and_whitespace_do_not_change_routing(self):
        result = query_inventory("   WHAT ABOUT net-402?   ")
        self.assertEqual(result["plan"], {"tool": "lookup", "category": "", "product": "NET-402"})
        self.assertIn("19 units", result["answer"])

    def test_unknown_sku_reports_no_match(self):
        result = query_inventory("What about ACC-999?")
        self.assertEqual(result["rows"], [])
        self.assertEqual(result["answer"], "No inventory records match that request.")

    def test_write_requests_are_refused(self):
        for question in (
            "Order 50 webcams", "Add 5 units of ACC-101", "Delete PER-503",
            "Update the stock for NET-402", "Set the USB Headset to zero",
            "Forecast demand for monitors", "How much stock did we have last month?",
        ):
            with self.subTest(question=question):
                with self.assertRaises(UnsupportedQuestion):
                    query_inventory(question)
        self.assertEqual(snapshot()["metrics"]["units"], 178)

    def test_compound_scopes_are_rejected(self):
        for question in ("Show Audio and Storage", "Do we have monitors or routers?"):
            with self.subTest(question=question):
                with self.assertRaisesRegex(UnsupportedQuestion, "one (?:product or )?category"):
                    query_inventory(question)

    def test_plural_and_irregular_forms_resolve_to_the_same_product(self):
        for question in ("Do we have any webcam?", "Do we have any webcams?", "Do we have any 1080p webcams?"):
            with self.subTest(question=question):
                self.assertEqual([r["sku"] for r in query_inventory(question)["rows"]], ["PER-503"])

    def test_ambiguous_keyword_returns_every_match(self):
        result = query_inventory("Which monitors are out of stock?")
        self.assertEqual(result["plan"]["product"], "monitor")
        self.assertEqual([row["sku"] for row in result["rows"]], ["DSP-301"])
        self.assertEqual(len(query_inventory("Do we have any monitors?")["rows"]), 2)

    def test_healthy_product_is_not_reported_as_missing(self):
        for question, expected in (
            ("Do we need to reorder the Wi-Fi Router?", "No products below their reorder point"),
            ("Is the Wi-Fi Router out of stock?", "No products are out of stock"),
            ("Does NET-999 need reordering?", "No inventory records match that request."),
        ):
            with self.subTest(question=question):
                result = query_inventory(question)
                self.assertIn(expected, result["answer"])
                self.assertEqual(result["rows"], [])

    def test_category_synonym_does_not_silently_widen_scope(self):
        result = query_inventory("What network products need reordering?")
        self.assertEqual(result["plan"]["category"], "Networking")
        self.assertEqual(result["rows"], [])

    def test_unknown_variant_is_not_substituted_with_a_different_product(self):
        for question in (
            "How many units of the Gaming Mouse are in stock?",
            "Do we have any wired keyboards?",
        ):
            with self.subTest(question=question):
                with self.assertRaisesRegex(UnsupportedQuestion, "product name or SKU"):
                    query_inventory(question)

    def test_reviewed_questions_keep_exact_scope(self):
        for question, expected in (
            ("Do we need to reorder 27-inch Monitors?", Plan("reorder", product="DSP-301")),
            ("Should we reorder 64 GB USB Drives?", Plan("reorder", product="STO-602")),
            ("Do we need to reorder 1 TB Portable SSDs?", Plan("reorder", product="STO-601")),
            ("What monitors are out of stock?", Plan("out_of_stock", product="monitor")),
            ("How many available Wireless Mice do we have?", Plan("lookup", product="PER-502")),
            ("What is our webcam stock?", Plan("lookup", product="PER-503")),
            ("How many units of PER-999 are in stock?", Plan("lookup", product="PER-999")),
            ("What is the total inventory value of Networking?", Plan("valuation", "Networking")),
            ("What should we buy more of?", Plan("reorder")),
        ):
            proposed = Plan("compare") if expected.tool == "reorder" else Plan(expected.tool, "Audio")
            for mode in ("demo", "ollama"):
                with self.subTest(question=question, mode=mode):
                    with patch("assistant.ollama_plan", return_value=proposed):
                        result = query_inventory(question, mode)
                    self.assertEqual(result["plan"], expected.to_dict())

    def test_unknown_subjects_and_unsupported_operations_do_not_run_reports(self):
        for question in (
            "Do we have any cables for iPhones?",
            "Do we have a stand for the microphone?",
            "Do we have an adapter for HDMI?",
            "Is the Docking Station out of stock?",
            "Does the Desk Lamp in Accessories need reordering?",
            "Are all thumb drives out of stock?",
            "Which Peripherals product has the lowest stock?",
            "Which category has the most stock across the whole warehouse?",
            "How many units of the Wi-Fi Router were in stock on Monday?",
            "How many USB-C Hubs will be in stock in 3 days?",
            "Reduce the stock of NET-402 by 5 units.",
            "How much is a 1080p Webcam worth?",
        ):
            for mode in ("demo", "ollama"):
                with self.subTest(question=question, mode=mode):
                    with patch("assistant.ollama_plan", return_value=Plan("out_of_stock", "Accessories")):
                        with patch("assistant.run_tool") as run:
                            with self.assertRaises(UnsupportedQuestion):
                                query_inventory(question, mode)
                            run.assert_not_called()

    def test_new_products_and_categories_are_available_in_both_modes(self):
        self.write("INSERT INTO categories (name) VALUES (?)", ("Store Supplies",))
        self.write(
            "INSERT INTO products VALUES (?, ?, (SELECT id FROM categories WHERE name = ?), ?, ?)",
            ("SUP-199", "Cable Tidy", "Store Supplies", 5, 500),
        )
        with patch("assistant.ollama_plan", return_value=Plan("reorder")):
            for mode in ("demo", "ollama"):
                with self.subTest(mode=mode):
                    product = query_inventory("Does Cable Tidy need reordering?", mode)
                    category = query_inventory("What needs reordering in Store Supplies?", mode)
                    self.assertEqual(product["plan"]["product"], "SUP-199")
                    self.assertEqual(category["plan"]["category"], "Store Supplies")
                    self.assertEqual([row["sku"] for row in category["rows"]], ["SUP-199"])
                    self.assertIn("Add 5 units", product["answer"])


class OllamaFailureModesTest(unittest.TestCase):
    def assert_unavailable(self, body):
        with raw_model_stub(body) as endpoint:
            with self.assertRaises(ModelUnavailable):
                ollama_plan("Any question", "test-model", endpoint)

    def test_rejects_unusable_model_output(self):
        good = {"tool": "lookup", "category": "", "product": "PER-503"}
        for name, body in {
            "not json at all": b"<html>bad gateway</html>",
            "missing message": b"{}",
            "content is prose": json.dumps({"message": {"content": "Sure! Here you go"}}).encode(),
            "extra key": plan_reply({**good, "sql": "DROP TABLE products"}),
            "missing key": plan_reply({"tool": "lookup"}),
            "non-string category": plan_reply({**good, "category": ["Toys"]}),
            "plan is a list": plan_reply([good]),
            "oversized product": plan_reply({**good, "product": "x" * 101}),
            "response too large": b"x" * 256_001,
        }.items():
            with self.subTest(name):
                self.assert_unavailable(body)

    def test_unsupported_plan_returns_none(self):
        for category, product in (("", ""), ("Audio", "headset")):
            with self.subTest(category=category, product=product):
                refusal = {"tool": "unsupported", "category": category, "product": product}
                with raw_model_stub(plan_reply(refusal)) as endpoint:
                    self.assertIsNone(ollama_plan("Tell me a joke", "test-model", endpoint))

    def test_refused_connection_is_model_unavailable(self):
        with self.assertRaises(ModelUnavailable):
            ollama_plan("Anything", "test-model", "http://127.0.0.1:9")

    def test_connection_dropped_mid_request_is_model_unavailable(self):
        import socket

        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)

            def drop():
                connection, _ = listener.accept()
                connection.recv(65536)
                connection.close()

            Thread(target=drop, daemon=True).start()
            with self.assertRaises(ModelUnavailable):
                ollama_plan("Anything", "test-model", f"http://127.0.0.1:{listener.getsockname()[1]}")


class HttpErrorHandlingTest(TempDatabaseCase):
    def post(self, base, body, content_type="application/json"):
        request = Request(base + "/api/ask", data=body, headers={"Content-Type": content_type})
        return urlopen(request, timeout=5)

    def error_for(self, base, body, **kwargs):
        with self.assertRaises(HTTPError) as caught:
            self.post(base, body, **kwargs)
        with caught.exception as response:
            return response.code, json.load(response)

    def test_malformed_requests_get_a_json_400(self):
        with serve(Handler) as base:
            for name, body, kwargs in (
                ("invalid json", b"{not json", {}),
                ("json array", b"[1, 2]", {}),
                ("json string", b'"hello"', {}),
                ("wrong content type", b'{"question": "hi"}', {"content_type": "text/plain"}),
                ("missing question", b"{}", {}),
                ("question is a number", b'{"question": 7}', {}),
                ("unknown mode", b'{"question": "Inventory?", "mode": "gpt"}', {}),
                ("invalid utf-8", b'{"question": "\xff\xfe"}', {}),
            ):
                with self.subTest(name):
                    status, payload = self.error_for(base, body, **kwargs)
                    self.assertEqual(status, 400)
                    self.assertIn("error", payload)

    def test_unknown_paths_return_404(self):
        with serve(Handler) as base:
            with self.assertRaises(HTTPError) as caught:
                urlopen(base + "/api/unknown", timeout=5)
            self.assertEqual(caught.exception.code, 404)
            caught.exception.close()
            request = Request(base + "/elsewhere", data=b"{}", headers={"Content-Type": "application/json"})
            with self.assertRaises(HTTPError) as caught:
                urlopen(request, timeout=5)
            self.assertEqual(caught.exception.code, 404)
            caught.exception.close()

    def test_responses_carry_security_headers(self):
        with serve(Handler) as base, urlopen(base + "/api/inventory", timeout=5) as response:
            self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
            self.assertIn("default-src 'self'", response.headers["Content-Security-Policy"])
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            body = json.load(response)
        self.assertEqual(body["metrics"]["products"], 14)
        self.assertIn("Networking", body["categories"])

    def test_model_outage_maps_to_503(self):
        with serve(Handler) as base, patch("assistant.ollama_plan", side_effect=ModelUnavailable("down")):
            status, payload = self.error_for(base, b'{"question": "Inventory?", "mode": "ollama"}')
        self.assertEqual((status, payload), (503, {"error": "down"}))

    def test_database_failure_returns_a_json_error(self):
        with serve(Handler) as base:
            body = b'{"question": "What needs reordering?"}'
            self.post(base, body).close()
            self.db_path.unlink()
            status, payload = self.error_for(base, body)
            self.assertEqual(status, 500)
            self.assertEqual(payload, {"error": "The inventory database is unavailable."})

            with self.assertRaises(HTTPError) as caught:
                urlopen(base + "/api/inventory", timeout=5)
            with caught.exception as response:
                self.assertEqual(response.code, 500)
                self.assertEqual(json.load(response), payload)


if __name__ == "__main__":
    unittest.main()
