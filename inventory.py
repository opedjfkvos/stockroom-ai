"""Sample data and the read-only inventory tools available to the assistant."""

from contextlib import closing
from dataclasses import asdict, dataclass
import sqlite3

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

BASE_QUERY = """
SELECT sku, name, category, stock, minimum, unit_cents,
       CASE WHEN stock = 0 THEN 'Out of stock'
            WHEN stock < minimum THEN 'Low stock'
            ELSE 'Healthy' END AS status
FROM inventory
"""


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


def connect():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("""
        CREATE TABLE inventory (
            sku TEXT PRIMARY KEY, name TEXT NOT NULL, category TEXT NOT NULL,
            stock INTEGER NOT NULL CHECK(stock >= 0),
            minimum INTEGER NOT NULL CHECK(minimum > 0),
            unit_cents INTEGER NOT NULL CHECK(unit_cents >= 0)
        )
    """)
    db.executemany("INSERT INTO inventory VALUES (?, ?, ?, ?, ?, ?)", ITEMS)
    db.commit()
    db.execute("PRAGMA query_only = ON")
    return db


def snapshot():
    with closing(connect()) as db:
        rows = [dict(row) for row in db.execute(BASE_QUERY + " ORDER BY sku")]
    return {
        "rows": rows,
        "categories": CATEGORIES,
        "metrics": {
            "products": len(rows),
            "units": sum(row["stock"] for row in rows),
            "reorder": sum(row["stock"] < row["minimum"] for row in rows),
            "value_cents": sum(row["stock"] * row["unit_cents"] for row in rows),
        },
    }


def run_tool(plan):
    where, params = [], []
    if plan.category:
        where.append("category = ?")
        params.append(plan.category)
    if plan.product:
        where.append("(instr(lower(name), lower(?)) > 0 OR lower(sku) = lower(?))")
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
        return f"{len(rows)} products are out of stock."
    if plan.tool == "reorder":
        shortage = sum(row["minimum"] - row["stock"] for row in rows)
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
