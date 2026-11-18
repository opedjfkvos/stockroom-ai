"""Sample data and the read-only inventory tools available to the assistant."""

from contextlib import closing
from dataclasses import asdict, dataclass
import os
from pathlib import Path
import re
import sqlite3
from functools import lru_cache

CATEGORIES = ("Accessories", "Audio", "Displays", "Networking", "Peripherals", "Storage")
TOOLS = ("lookup", "reorder", "out_of_stock", "compare", "valuation")
ITEMS = (
    ("ACC-101", "USB-C Hub", "Accessories", 6, 8, 3490),
    ("ACC-102", "Braided USB-C Cable", "Accessories", 48, 20, 1290),
    ("ACC-103", "Laptop Stand", "Accessories", 14, 10, 3590),
    ("AUD-201", "USB Headset", "Audio", 4, 6, 4490),
    ("AUD-202", "Podcast Microphone", "Audio", 10, 6, 7990),
    ("DSP-301", "27-inch Monitor", "Displays", 0, 4, 18900),
    ("DSP-302", "Portable Monitor", "Displays", 7, 5, 20900),
    ("NET-401", "Wi-Fi Router", "Networking", 12, 6, 8900),
    ("NET-402", "Ethernet Adapter", "Networking", 19, 10, 1590),
    ("PER-501", "Mechanical Keyboard", "Peripherals", 16, 10, 6990),
    ("PER-502", "Wireless Mouse", "Peripherals", 3, 8, 2490),
    ("PER-503", "1080p Webcam", "Peripherals", 0, 7, 3990),
    ("STO-601", "1 TB Portable SSD", "Storage", 8, 6, 9900),
    ("STO-602", "64 GB USB Drive", "Storage", 31, 12, 790),
)

BASE_QUERY = "SELECT sku, name, category, stock, minimum, unit_cents, status FROM inventory_status"
SCHEMA = Path(__file__).resolve().parent / "sql" / "schema.sql"
DEFAULT_DB = SCHEMA.parent.parent / "data" / "stockroom.sqlite3"


def tokens(text):
    words = re.findall(r"[a-z0-9]+", text.casefold())
    return tuple(
        "mouse" if word == "mice" else
        word[:-3] + "y" if word.endswith("ies") else
        word[:-1] if len(word) > 3 and word.endswith("s") and not word.endswith(("ss", "us")) else word
        for word in words
    )


def name_matches(name, product):
    if re.search(r"[^a-z0-9 -]", product.casefold()):
        return product.casefold() in name.casefold()
    name_words, product_words = tokens(name), tokens(product)
    width = len(product_words)
    return bool(width) and any(
        name_words[i:i + width] == product_words
        for i in range(len(name_words) - width + 1)
    )


@dataclass(frozen=True)
class Plan:
    tool: str
    category: str = ""
    product: str = ""

    def __post_init__(self):
        if self.tool not in TOOLS:
            raise ValueError("Choose one of the available tools.")
        if not isinstance(self.category, str) or (self.category and self.category not in CATEGORIES):
            raise ValueError("Unknown inventory category.")
        if not isinstance(self.product, str) or len(self.product) > 100:
            raise ValueError("Invalid product search.")

    def to_dict(self):
        return asdict(self)


@lru_cache(maxsize=4)
def initialize(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("PRAGMA foreign_keys = ON")
        db.execute("BEGIN IMMEDIATE")
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version == 1:
            return
        if version != 0:
            raise RuntimeError(f"Unsupported inventory schema version {version}.")
        # Execute the bundled migration inside the same transaction as its seed.
        for statement in SCHEMA.read_text(encoding="utf-8").split(";"):
            if statement.strip():
                db.execute(statement)
        db.executemany("INSERT INTO categories(name) VALUES (?)", ((name,) for name in CATEGORIES))
        db.executemany(
            """INSERT INTO products(sku, name, category_id, minimum, unit_cents)
               SELECT ?, ?, id, ?, ? FROM categories WHERE name = ?""",
            ((sku, name, minimum, price, category) for sku, name, category, _, minimum, price in ITEMS),
        )
        db.executemany(
            "INSERT INTO stock_movements(sku, quantity_delta, reason) VALUES (?, ?, ?)",
            ((sku, stock, "Opening balance") for sku, _, _, stock, _, _ in ITEMS if stock),
        )
        db.execute("PRAGMA user_version = 1")


def connect():
    path = Path(os.environ.get("STOCKROOM_DB", DEFAULT_DB)).resolve()
    initialize(path)
    db = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    db.create_function("name_matches", 2, name_matches, deterministic=True)
    return db


def snapshot():
    with closing(connect()) as db:
        rows = [dict(row) for row in db.execute(BASE_QUERY + " ORDER BY sku")]
        metrics = dict(db.execute("""
            SELECT COUNT(*) AS products, COALESCE(SUM(stock), 0) AS units,
                   COUNT(CASE WHEN stock < minimum THEN 1 END) AS reorder,
                   COALESCE(SUM(stock * unit_cents), 0) AS value_cents
            FROM inventory_status
        """).fetchone())
        categories = [row[0] for row in db.execute("SELECT name FROM categories ORDER BY name")]
    return {"rows": rows, "categories": categories, "metrics": metrics}


def run_tool(plan):
    where, params = [], []
    if plan.category:
        where.append("category = ?")
        params.append(plan.category)
    if plan.product:
        where.append("(name_matches(name, ?) OR lower(sku) = lower(?))")
        params.extend([plan.product, plan.product])
    if plan.tool == "reorder":
        where.append("stock < minimum")
    elif plan.tool == "out_of_stock":
        where.append("stock = 0")

    query = BASE_QUERY
    if where:
        query += " WHERE " + " AND ".join(where)
    query += " ORDER BY " + ("stock - minimum, sku" if plan.tool == "reorder" else "stock DESC, sku")
    with closing(connect()) as db:
        rows = [dict(row) for row in db.execute(query, params)]
    return rows


def answer_from_rows(plan, rows):
    if not rows:
        return (
            "No products below their reorder point in this scope."
            if plan.tool == "reorder" and not plan.product
            else "No inventory records match that request."
        )
    if plan.tool == "out_of_stock":
        return "1 product is out of stock." if len(rows) == 1 else f"{len(rows)} products are out of stock."
    if plan.tool == "reorder":
        shortage = sum(row["minimum"] - row["stock"] for row in rows)
        if len(rows) == 1:
            units = "unit" if shortage == 1 else "units"
            return f"1 product needs replenishment. Add {shortage} {units} to reach its reorder point."
        return f"{len(rows)} products need replenishment. Add {shortage} units to reach their reorder points."
    if plan.tool == "valuation":
        cents = sum(row["stock"] * row["unit_cents"] for row in rows)
        return f"Current inventory value is ${cents / 100:,.2f} across {len(rows)} products."
    if plan.tool == "compare":
        highest = rows[0]
        return f"{highest['name']} has the most stock in this result, with {highest['stock']} units."
    if len(rows) == 1:
        row = rows[0]
        return f"{row['name']} has {row['stock']} units in stock. Reorder point: {row['minimum']}."
    units = sum(row["stock"] for row in rows)
    return f"Found {len(rows)} matching products with {units} units in stock."
