"""Sample data and the read-only inventory tools available to the assistant."""

from contextlib import closing, contextmanager
from contextvars import ContextVar
from dataclasses import InitVar, asdict, dataclass
import os
from pathlib import Path
import re
import sqlite3
from functools import lru_cache

from localized_stock import stock_text

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
INVENTORY_SOURCE = ContextVar("inventory_source", default="sample")


@contextmanager
def use_inventory(source):
    if source not in ("sample", "imported"):
        raise ValueError("Unknown inventory source.")
    token = INVENTORY_SOURCE.set(source)
    try:
        yield
    finally:
        INVENTORY_SOURCE.reset(token)


def inventory_path():
    sample = Path(os.environ.get("STOCKROOM_DB", DEFAULT_DB)).resolve()
    if INVENTORY_SOURCE.get() == "imported":
        imported = sample.with_name(f"{sample.stem}-imported.sqlite3")
        path = Path(os.environ.get("STOCKROOM_IMPORT_DB", imported)).resolve()
        if path == sample or (sample.exists() and path.exists() and sample.samefile(path)):
            raise ValueError("Configure separate databases for sample and imported inventory.")
        return path
    return sample



def tokens(text):
    words = re.findall(r"[^\W_]+", text.casefold())
    return tuple(
        "mouse" if word == "mice" else
        word[:-3] + "y" if word.endswith("ies") else
        word[:-1] if len(word) > 3 and word.endswith("s") and not word.endswith(("ss", "us")) else word
        for word in words
    )


def name_matches(name, product):
    if "'" in product or "’" in product:
        return product.casefold() in name.casefold()
    name_words, product_words = tokens(name), tokens(product)
    width = len(product_words)
    return bool(width) and any(
        name_words[i:i + width] == product_words
        for i in range(len(name_words) - width + 1)
    )


def product_matches(name, subject):
    if "'" in subject or "’" in subject:
        return name_matches(name, subject)
    name_words, subject_words = tokens(name), tokens(subject)
    if not subject_words:
        return False
    # A noun phrase must name the item itself, rather than a modifier:
    # "laptop" cannot match "Laptop Stand". Common specs can form groups.
    return (name_words[-len(subject_words):] == subject_words
            or subject_words[0] in {"usb", "wi", "hdmi", "ssd", "bluetooth"}
            and name_matches(name, subject))


@dataclass(frozen=True)
class Plan:
    tool: str
    category: str = ""
    product: str = ""

    allowed_categories: InitVar[tuple | None] = CATEGORIES

    def __post_init__(self, allowed_categories):
        if self.tool not in TOOLS:
            raise ValueError("Choose one of the available tools.")
        if not isinstance(self.category, str) or len(self.category) > 100 or (
            allowed_categories is not None and self.category and self.category not in allowed_categories
        ):
            raise ValueError("Unknown inventory category.")
        if not isinstance(self.product, str) or len(self.product) > 100:
            raise ValueError("Invalid product search.")

    def to_dict(self):
        return asdict(self)


@lru_cache(maxsize=4)
def initialize(path, seed=True, *, existing=False):
    if not existing:
        path.parent.mkdir(parents=True, exist_ok=True)
    location = f"{path.as_uri()}?mode=rw" if existing else path
    with closing(sqlite3.connect(location, uri=existing)) as db, db:
        db.execute("PRAGMA foreign_keys = ON")
        db.execute("BEGIN IMMEDIATE")
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version == 2:
            return
        if version == 1:
            migrate_stock_status(db)
            return
        if version != 0 or existing:
            raise sqlite3.DatabaseError(f"Unsupported inventory schema version {version}.")
        # Execute the bundled migration inside the same transaction as its seed.
        for statement in SCHEMA.read_text(encoding="utf-8").split(";"):
            if statement.strip():
                db.execute(statement)
        if seed:
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
        migrate_stock_status(db)


def migrate_stock_status(db):
    migration = SCHEMA.with_name("002_stock_status.sql")
    for statement in migration.read_text(encoding="utf-8").split(";"):
        if statement.strip():
            db.execute(statement)
    db.execute("PRAGMA user_version = 2")


def connect():
    path = inventory_path()
    sample = INVENTORY_SOURCE.get() == "sample"
    allow_seed = sample and "STOCKROOM_DB" not in os.environ
    initialize(path, seed=allow_seed, existing=not allow_seed)
    db = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    db.create_function("product_matches", 2, product_matches, deterministic=True)
    return db


def catalog_scope():
    with closing(connect()) as db:
        products = tuple((row["sku"], row["name"]) for row in db.execute("SELECT sku, name FROM products"))
        categories = tuple(row["name"] for row in db.execute("SELECT name FROM categories"))
    return products, categories


def snapshot():
    with closing(connect()) as db:
        rows = [dict(row) for row in db.execute(BASE_QUERY + " ORDER BY sku")]
        metrics = dict(db.execute("""
            SELECT COUNT(*) AS products, COALESCE(SUM(stock), 0) AS units,
                   COUNT(CASE WHEN stock < minimum THEN 1 END) AS reorder,
                   COALESCE(SUM(MAX(stock, 0) * unit_cents), 0) AS value_cents
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
        where.append("""(sku = ? COLLATE NOCASE OR (
            product_matches(name, ?) AND NOT EXISTS (
                SELECT 1 FROM products WHERE sku = ? COLLATE NOCASE
            )))""")
        params.extend([plan.product.upper(), plan.product, plan.product.upper()])
    if plan.tool == "reorder":
        where.append("stock < minimum")
    elif plan.tool == "out_of_stock":
        where.append("stock <= 0")

    query = BASE_QUERY
    if where:
        query += " WHERE " + " AND ".join(where)
    query += " ORDER BY " + ("stock - minimum, sku" if plan.tool == "reorder" else "stock DESC, sku")
    with closing(connect()) as db:
        rows = [dict(row) for row in db.execute(query, params)]
    return rows


def answer_from_rows(plan, rows, *, scope_exists=False, currency="USD", language="en"):
    def say(key, english, **values):
        return stock_text(language, key, english, **values)

    if not rows:
        if scope_exists:
            if plan.tool == "reorder":
                return say("no_reorder", "No products below their reorder point in this scope.")
            if plan.tool == "out_of_stock":
                return say("no_out", "No products are out of stock in this scope.")
        return say("missing", "No inventory records match that request.")
    if plan.tool == "out_of_stock":
        english = "1 product is out of stock." if len(rows) == 1 else f"{len(rows)} products are out of stock."
        return say("out", english, n=len(rows))
    if plan.tool == "reorder":
        shortage = sum(row["minimum"] - row["stock"] for row in rows)
        if len(rows) == 1:
            units = "unit" if shortage == 1 else "units"
            return say("reorder", f"1 product needs replenishment. Add {shortage} {units} to reach its reorder point.", n=1, units=shortage)
        return say("reorder", "{n} products need replenishment. Add {units} units to reach their reorder points.", n=len(rows), units=shortage)
    if plan.tool == "valuation":
        cents = sum(max(row["stock"], 0) * row["unit_cents"] for row in rows)
        scope = "1 product" if len(rows) == 1 else f"{len(rows)} products"
        amount = f"{cents / 100:,.2f}"
        value = f"${amount}" if currency == "USD" else f"{currency} {amount}"
        note = say("negative", " Negative balances were valued at zero. Check the source records.") if any(
            row["stock"] < 0 for row in rows
        ) else ""
        return say("value", f"Current inventory value is {value} across {scope}.", value=value, n=len(rows)) + note
    if plan.tool == "compare":
        highest = rows[0]["stock"]
        if highest <= 0:
            return say("all_out", "All matching products are currently out of stock.")
        leaders = [row["name"] for row in rows if row["stock"] == highest]
        if len(leaders) > 1:
            return say("tie", "{names} are tied for the most stock, with {units} units each.", names=", ".join(leaders), units=highest)
        return say("leader", "{names} has the most stock in this result, with {units} units.", names=leaders[0], units=highest)
    if len(rows) == 1:
        row = rows[0]
        return say("single", "{name} has {stock} units in stock. Reorder point: {minimum}.", **row)
    units = sum(row["stock"] for row in rows)
    answer = say("many", "Found {n} matching products with {units} units in stock.", n=len(rows), units=units)
    if plan.product:
        answer += say("narrow", " Use a product name or SKU to narrow the search.")
    return answer
