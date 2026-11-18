"""Integration checks for the HTTP server and the on-disk inventory."""

from contextlib import closing
from http.server import ThreadingHTTPServer
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
from assistant import query_inventory
from inventory import connect, snapshot


class ApiTest(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db_path = Path(directory.name) / "inventory.sqlite3"
        environment = patch.dict("os.environ", {"STOCKROOM_DB": str(self.db_path)})
        environment.start()
        self.addCleanup(environment.stop)

    def test_http_ask(self):
        with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(thread.join)
            self.addCleanup(server.shutdown)
            url = f"http://127.0.0.1:{server.server_port}/api/ask"

            def ask(question):
                request = Request(
                    url, data=json.dumps({"question": question}).encode(),
                    headers={"Content-Type": "application/json"},
                )
                return urlopen(request, timeout=5)

            with ask("Which display is out of stock?") as response:
                self.assertEqual(response.status, 200)
                answer = json.load(response)
            self.assertEqual(answer["plan"]["category"], "Displays")
            self.assertEqual([row["sku"] for row in answer["rows"]], ["DSP-301"])
            self.assertEqual(answer["answer"], "1 product is out of stock.")

            with self.assertRaises(HTTPError) as caught:
                ask("Do we have monitors and routers?")
            with caught.exception as response:
                self.assertEqual(response.code, 400)
                self.assertIn("one product or category", json.load(response)["error"])

    def test_stock_movements_persist_and_queries_are_read_only(self):
        self.assertEqual(snapshot()["metrics"]["units"], 178)
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute(
                "INSERT INTO stock_movements(sku, quantity_delta, reason) VALUES (?, ?, ?)",
                ("DSP-301", 5, "Delivery"),
            )
        with closing(connect()) as db:
            row = db.execute("SELECT stock, status FROM inventory_status WHERE sku = ?", ("DSP-301",)).fetchone()
            self.assertEqual(tuple(row), (5, "Healthy"))
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 2)
            with self.assertRaisesRegex(sqlite3.OperationalError, "readonly"):
                db.execute("DELETE FROM stock_movements")
        self.assertEqual(snapshot()["metrics"]["units"], 183)
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute(
                "INSERT INTO stock_movements(sku, quantity_delta, reason) VALUES (?, ?, ?)",
                ("DSP-301", -3, "Dispatch"),
            )
        with closing(connect()) as db:
            row = db.execute("SELECT stock, status FROM inventory_status WHERE sku = ?", ("DSP-301",)).fetchone()
            self.assertEqual(tuple(row), (2, "Low stock"))
        self.assertEqual(snapshot()["metrics"]["units"], 180)

    def test_sql_reports_reconcile_with_inventory(self):
        reports = Path(__file__).resolve().parents[1] / "sql" / "reports.sql"
        valuation, reorder, remainder = reports.read_text(encoding="utf-8").split(";")
        self.assertFalse(remainder.strip())
        with closing(connect()) as db:
            valuation_rows = db.execute(valuation).fetchall()
            reorder_rows = db.execute(reorder).fetchall()

        metrics = snapshot()["metrics"]
        for report_column, metric in (
            ("products", "products"), ("units_on_hand", "units"), ("inventory_value_cents", "value_cents"),
        ):
            self.assertEqual(sum(row[report_column] for row in valuation_rows), metrics[metric])
        self.assertEqual(sum(row["products_below_minimum"] for row in reorder_rows), metrics["reorder"])
        needs = query_inventory("What needs reordering?")["rows"]
        self.assertEqual(sum(row["units_to_order"] for row in reorder_rows),
                         sum(row["minimum"] - row["stock"] for row in needs))
        self.assertEqual(sum(row["reorder_cost_cents"] for row in reorder_rows),
                         sum((row["minimum"] - row["stock"]) * row["unit_cents"] for row in needs))
