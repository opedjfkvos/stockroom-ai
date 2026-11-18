"""Inventory import validation, confirmation and dataset isolation."""

from contextlib import closing
from io import BytesIO
import json
import sqlite3
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from app import Handler
from inventory import snapshot, use_inventory
from inventory_import import import_inventory
from test_edge_cases import TempDatabaseCase, serve

HEADER = "Артикул;Наименование;Категория;Остаток;Минимальный остаток;Закупочная цена\n"
CSV = ("Ведомость остатков\n\n" + HEADER + "SKU-100;Кофе 250 г;Напитки;3;10;22,50\n").encode("cp1251")


class ImportTest(TempDatabaseCase):
    def test_csv_preview_then_confirm_without_altering_demo(self):
        before = snapshot()
        preview = import_inventory("export.csv", CSV, "PLN")
        self.assertEqual(preview["errors"], [])
        self.assertEqual(preview["preview"][0]["unit_cents"], 2250)
        self.assertFalse(self.db_path.with_name("inventory-imported.sqlite3").exists())
        import_inventory("export.csv", CSV, "PLN", preview["confirmation"])
        self.assertEqual(snapshot(), before)
        with use_inventory("imported"):
            self.assertEqual(snapshot()["metrics"], {"products": 1, "units": 3, "reorder": 1, "value_cents": 6750})

    def test_xlsx_parses_actual_workbook(self):
        from openpyxl import Workbook
        workbook = Workbook()
        workbook.active.append(HEADER.strip().split(";"))
        workbook.active.append(["0002", "Coffee", "Drinks", 3, 5, 22.5])
        with BytesIO() as stream:
            workbook.save(stream)
            data = stream.getvalue()
        result = import_inventory("store.xlsx", data, "UAH")
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["preview"][0]["sku"], "0002")
        self.assertEqual(result["preview"][0]["unit_cents"], 2250)
        import_inventory("store.xlsx", data, "UAH", result["confirmation"])
        with use_inventory("imported"):
            self.assertEqual(snapshot()["metrics"]["units"], 3)

    def test_invalid_rows_cannot_be_confirmed(self):
        bad = CSV + "sku-100;Дубликат;Напитки;2;1;1\nSKU-200;Ошибка;Напитки;-1;1;1\n".encode("cp1251")
        result = import_inventory("export.csv", bad, "PLN")
        self.assertEqual(result["count"], 1)
        self.assertEqual(len(result["errors"]), 2)
        self.assertIsNone(result["confirmation"])
        with self.assertRaises(ValueError):
            import_inventory("export.csv", bad, "PLN", "invalid")

    def test_reimports_update_absolute_stock_and_reject_stale_confirmation(self):
        initial = import_inventory("export.csv", CSV, "PLN")
        import_inventory("export.csv", CSV, "PLN", initial["confirmation"])
        changed = CSV.replace(b";3;10;", b";7;10;")
        before_change = import_inventory("export.csv", CSV, "PLN")
        updated = import_inventory("export.csv", changed, "PLN")
        with self.assertRaisesRegex(ValueError, "changed"):
            import_inventory("export.csv", changed, "PLN", before_change["confirmation"])
        import_inventory("export.csv", changed, "PLN", updated["confirmation"])
        with self.assertRaisesRegex(ValueError, "changed"):
            import_inventory("export.csv", changed, "PLN", updated["confirmation"])
        same_count = import_inventory("export.csv", changed, "PLN")
        import_inventory("export.csv", changed, "PLN", same_count["confirmation"])
        imported = self.db_path.with_name("inventory-imported.sqlite3")
        with closing(sqlite3.connect(imported)) as db:
            self.assertEqual(db.execute("SELECT quantity_delta FROM stock_movements ORDER BY id").fetchall(),
                             [(3,), (4,)])

    def test_http_preview_confirm_and_query_imported_inventory(self):
        with serve(Handler) as base:
            def upload(action, token=None):
                headers = {"Content-Type": "application/octet-stream", "X-Stockroom-Import": "1"}
                if token:
                    headers["X-Stockroom-Confirmation"] = token
                request = Request(f"{base}/api/import/{action}?filename=export.csv&currency=PLN",
                                  data=CSV, headers=headers)
                with urlopen(request, timeout=5) as response:
                    return json.load(response)

            with self.assertRaises(HTTPError) as error:
                upload("confirm")
            error.exception.close()
            with self.assertRaises(HTTPError) as missing:
                urlopen(base + "/api/inventory?source=imported", timeout=5)
            with missing.exception as response:
                self.assertIn("No imported inventory", json.load(response)["error"])
            self.assertFalse(self.db_path.with_name("inventory-imported.sqlite3").exists())
            preview = upload("preview")
            upload("confirm", preview["confirmation"])
            with urlopen(base + "/api/inventory?source=imported", timeout=5) as response:
                inventory = json.load(response)
                self.assertEqual(inventory["currency"], "PLN")
                self.assertRegex(inventory["imported_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
            request = Request(base + "/api/ask", data=json.dumps({
                "question": "What is the stock value for Кофе 250 г?", "source": "imported",
            }).encode(), headers={"Content-Type": "application/json"})
            with urlopen(request, timeout=5) as response:
                result = json.load(response)
                self.assertIn("PLN 67.50", result["answer"])
                self.assertEqual(result["rows"][0]["sku"], "SKU-100")
