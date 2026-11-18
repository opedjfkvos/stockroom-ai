CREATE TABLE categories (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE products (
    sku TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    category_id INTEGER NOT NULL REFERENCES categories(id),
    minimum INTEGER NOT NULL CHECK (minimum > 0),
    unit_cents INTEGER NOT NULL CHECK (unit_cents >= 0)
);

CREATE TABLE stock_movements (
    id INTEGER PRIMARY KEY,
    sku TEXT NOT NULL REFERENCES products(sku),
    quantity_delta INTEGER NOT NULL CHECK (quantity_delta <> 0),
    reason TEXT NOT NULL
);

CREATE INDEX products_by_category ON products(category_id);
CREATE INDEX movements_by_sku ON stock_movements(sku);

CREATE VIEW inventory_status AS
WITH balances AS (
    SELECT sku, SUM(quantity_delta) AS stock
    FROM stock_movements
    GROUP BY sku
)
SELECT p.sku, p.name, c.name AS category,
       COALESCE(b.stock, 0) AS stock, p.minimum, p.unit_cents,
       CASE WHEN COALESCE(b.stock, 0) = 0 THEN 'Out of stock'
            WHEN COALESCE(b.stock, 0) < p.minimum THEN 'Low stock'
            ELSE 'Healthy' END AS status
FROM products AS p
JOIN categories AS c ON p.category_id = c.id
LEFT JOIN balances AS b ON p.sku = b.sku;
