"""Validate inventory spreadsheets and apply confirmed snapshots to a separate database."""

from contextlib import closing
import csv
from decimal import Decimal, InvalidOperation
import hashlib
import hmac
from io import BytesIO, StringIO
from itertools import islice
from pathlib import Path
import re
import secrets
import sqlite3
from zipfile import BadZipFile, ZipFile

from inventory import initialize, inventory_path, use_inventory

MAX_BYTES = 4 * 1024 * 1024
MAX_ROWS = 5000
CURRENCIES = {"USD", "PLN", "UAH", "EUR", "RUB"}
COLUMNS = ("sku", "name", "category", "stock", "minimum", "unit_cost")
ALIASES = {
    "sku": ("sku", "article", "item code", "артикул", "код", "код номенклатуры", "код номенклатури"),
    "name": ("name", "product", "product name", "наименование", "номенклатура", "найменування", "назва"),
    "category": ("category", "group", "категория", "товарная группа", "группа", "категорія", "група"),
    "stock": ("stock", "quantity", "on hand", "остаток", "конечный остаток", "остаток на конец", "залишок"),
    "minimum": ("minimum", "minimum stock", "reorder point", "минимум", "минимальный остаток", "мінімальний залишок"),
    "unit_cost": ("unit cost", "purchase price", "закупочная цена", "цена закупки", "закупівельна ціна"),
}
HEADERS = {alias: field for field, aliases in ALIASES.items() for alias in aliases}
SIGNING_KEY = secrets.token_bytes(32)


def spreadsheet_rows(filename, data):
    extension = Path(filename).suffix.lower()
    if extension == ".csv":
        try:
            content = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            content = data.decode("cp1251", errors="replace")
        if "\ufffd" in content or "\0" in content:
            raise ValueError("Save the CSV as UTF-8 or Windows-1251.")
        separators = (";", "\t", ",")
        def header_score(separator):
            try:
                rows = islice(csv.reader(StringIO(content, newline=""), delimiter=separator, strict=True), 25)
                return max((sum(normalized_header(value) in HEADERS for value in row) for row in rows), default=0)
            except csv.Error:
                return 0

        separator = max(separators, key=header_score)
        yield from csv.reader(StringIO(content, newline=""), delimiter=separator, strict=True)
        return
    if extension != ".xlsx":
        raise ValueError("Choose an .xlsx or .csv file. Export legacy .xls files as .xlsx or CSV.")

    try:
        from defusedxml import ElementTree  # noqa: F401
        from openpyxl import load_workbook
    except ImportError as exc:
        raise ValueError("Install requirements.txt to enable Excel imports.") from exc
    try:
        with ZipFile(BytesIO(data)) as archive:
            entries = archive.infolist()
            if len(entries) > 256 or sum(entry.file_size for entry in entries) > 24 * 1024 * 1024:
                raise ValueError("The Excel workbook is too large after decompression.")
        workbook = load_workbook(BytesIO(data), read_only=True, data_only=False, keep_links=False)
        try:
            if not workbook.worksheets:
                raise ValueError("The workbook has no worksheet.")
            sheet = workbook.worksheets[0]
            sheet.reset_dimensions()
            for row in sheet.iter_rows(max_col=64):
                yield [cell.value for cell in row]
        finally:
            workbook.close()
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("Unable to read this Excel workbook. Re-save it as .xlsx or CSV.") from exc


def normalized_header(value):
    return " ".join(str(value or "").casefold().replace("_", " ").replace("ё", "е").split())


def integer(value, field, minimum):
    result = amount(value, field)
    if result != result.to_integral_value() or not minimum <= result <= 1_000_000:
        raise ValueError(f"{field} must be a whole number from {minimum} to 1,000,000.")
    return int(result)


def amount(value, field):
    try:
        cleaned = re.sub(r"[ \u00a0\u202f]", "", str(value)).replace(",", ".")
        if not re.fullmatch(r"\d+(?:\.\d+)?", cleaned):
            raise InvalidOperation()
        return Decimal(cleaned)
    except InvalidOperation as exc:
        raise ValueError(f"{field} must be numeric.") from exc


def parse_rows(filename, data):
    products = []
    issues = []
    mapping = None
    partial = {}
    seen = set()
    try:
        for line, row in enumerate(spreadsheet_rows(filename, data), start=1):
            if line > MAX_ROWS + 25:
                raise ValueError(f"Import at most {MAX_ROWS:,} products per file.")
            if mapping is None:
                if line > 25:
                    break
                fields = [HEADERS.get(normalized_header(value)) for value in row]
                candidate = {field: index for index, field in enumerate(fields) if field}
                if len(candidate) > len(partial):
                    partial = candidate
                if set(COLUMNS) <= candidate.keys():
                    if len(candidate) != sum(field is not None for field in fields):
                        raise ValueError("A required column appears more than once.")
                    mapping = candidate
                continue
            if all(value is None or str(value).strip() == "" for value in row):
                continue
            values = [row[mapping[field]] if mapping[field] < len(row) else None for field in COLUMNS]
            sku, name, category = (str(value).strip() if value is not None else "" for value in values[:3])
            try:
                if any(isinstance(value, str) and value.lstrip().startswith("=") for value in values):
                    raise ValueError("Replace formulas with values before exporting.")
                if not re.fullmatch(r"[\w.-]{1,64}", sku):
                    raise ValueError("SKU must be 1–64 letters, digits, dots, dashes or underscores.")
                sku = sku.upper()
                if sku in seen:
                    raise ValueError(f"Duplicate SKU {sku}. Export one row per item.")
                if not name or not category or len(name) > 200 or len(category) > 100:
                    raise ValueError("Enter a product name and category.")
                stock = integer(values[3], "Stock", 0)
                minimum = integer(values[4], "Minimum stock", 1)
                cents = amount(values[5], "Unit cost") * 100
                if cents != cents.to_integral_value() or not 0 <= cents <= 100_000_000:
                    raise ValueError("Unit cost must be between 0 and 1,000,000 with at most two decimal places.")
                products.append({"sku": sku, "name": name, "category": category, "stock": stock,
                                 "minimum": minimum, "unit_cents": int(cents)})
                seen.add(sku)
            except ValueError as exc:
                if len(issues) < 25:
                    issues.append(f"Row {line}: {exc}")
            if len(products) > MAX_ROWS:
                raise ValueError(f"Import at most {MAX_ROWS:,} products per file.")
    except (csv.Error, BadZipFile) as exc:
        raise ValueError("Could not read the spreadsheet.") from exc
    if mapping is None:
        missing = ", ".join(field.replace("_", " ") for field in COLUMNS if field not in partial)
        raise ValueError(f"Required columns missing: {missing}. Download the template to check the headers.")
    if not products and not issues:
        raise ValueError("The spreadsheet has no product rows.")
    return products, issues


def state(db=None):
    if db is None:
        path = inventory_path()
        if not path.exists():
            return 0, None
        with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as connection:
            return state(connection)
    exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='inventory_imports'").fetchone()
    if not exists:
        return 0, None
    previous = db.execute("SELECT id, currency FROM inventory_imports ORDER BY id DESC LIMIT 1").fetchone()
    return tuple(previous) if previous else (0, None)


def latest_import():
    path = inventory_path()
    if not path.is_file():
        return None, None
    with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as db:
        revision, currency = state(db)
        if not revision:
            return None, None
        applied_at = db.execute(
            "SELECT applied_at FROM inventory_imports WHERE id = ?", (revision,)
        ).fetchone()[0]
        return currency, applied_at.replace(" ", "T") + "Z"


def token_for(filename, data, currency, revision):
    digest = hashlib.sha256(data).hexdigest()
    detail = f"{revision}:{currency}:{Path(filename).suffix.lower()}:{digest}".encode()
    return hmac.new(SIGNING_KEY, detail, hashlib.sha256).hexdigest()


def import_inventory(filename, data, currency, confirmation=None):
    if not data or len(data) > MAX_BYTES:
        raise ValueError("Choose a non-empty file up to 4 MB.")
    if currency not in CURRENCIES:
        raise ValueError("Choose the currency used in your spreadsheet.")
    rows, errors = parse_rows(filename, data)
    with use_inventory("imported"):
        path = inventory_path()
        if confirmation is None:
            revision, saved_currency = state()
            if saved_currency and currency != saved_currency:
                raise ValueError(f"Imported inventory already uses {saved_currency}. Export costs in that currency.")
            return {"count": len(rows), "preview": rows[:10], "errors": errors, "currency": currency,
                    "confirmation": token_for(filename, data, currency, revision) if not errors else None}
        if errors:
            raise ValueError("Correct all rows and preview the file again before importing.")
        revision, _ = state()
        if not hmac.compare_digest(confirmation, token_for(filename, data, currency, revision)):
            raise ValueError("The file, currency or catalogue changed. Preview again before importing.")
        initialize(path, seed=False)
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("PRAGMA foreign_keys = ON")
            db.execute("BEGIN IMMEDIATE")
            revision, saved_currency = state(db)
            if not hmac.compare_digest(confirmation, token_for(filename, data, currency, revision)):
                raise ValueError("Another import was saved. Preview again before importing.")
            if saved_currency and currency != saved_currency:
                raise ValueError(f"Imported inventory already uses {saved_currency}.")
            db.execute("""CREATE TABLE IF NOT EXISTS inventory_imports (
                id INTEGER PRIMARY KEY, currency TEXT NOT NULL, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )""")
            catalogue = {sku: stock * cost for sku, stock, cost in db.execute(
                "SELECT sku, stock, unit_cents FROM inventory_status"
            )}
            catalogue.update((row["sku"], row["stock"] * row["unit_cents"]) for row in rows)
            if sum(abs(value) for value in catalogue.values()) > 2**53 - 1:
                raise ValueError("The total inventory value is too large to display precisely.")
            batch = db.execute("INSERT INTO inventory_imports(currency) VALUES (?)", (currency,)).lastrowid
            for row in rows:
                db.execute("INSERT INTO categories(name) VALUES (?) ON CONFLICT DO NOTHING", (row["category"],))
                db.execute("""
                    INSERT INTO products(sku,name,category_id,minimum,unit_cents)
                    VALUES (:sku,:name,(SELECT id FROM categories WHERE name=:category),:minimum,:unit_cents)
                    ON CONFLICT(sku) DO UPDATE SET name=excluded.name, category_id=excluded.category_id,
                    minimum=excluded.minimum, unit_cents=excluded.unit_cents
                """, row)
                previous = db.execute(
                    "SELECT COALESCE(SUM(quantity_delta),0) FROM stock_movements WHERE sku=?", (row["sku"],)
                ).fetchone()[0]
                delta = row["stock"] - previous
                if delta:
                    db.execute("INSERT INTO stock_movements(sku,quantity_delta,reason) VALUES (?,?,?)",
                               (row["sku"], delta, f"Spreadsheet import {batch}"))
            return {"count": len(rows), "currency": currency}
