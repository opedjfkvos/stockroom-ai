DROP VIEW inventory_status;

CREATE VIEW inventory_status AS
WITH balances AS (
    SELECT sku, SUM(quantity_delta) AS stock
    FROM stock_movements
    GROUP BY sku
)
SELECT p.sku, p.name, c.name AS category,
       COALESCE(b.stock, 0) AS stock, p.minimum, p.unit_cents,
       CASE WHEN COALESCE(b.stock, 0) <= 0 THEN 'Out of stock'
            WHEN COALESCE(b.stock, 0) < p.minimum THEN 'Low stock'
            ELSE 'Healthy' END AS status
FROM products AS p
JOIN categories AS c ON p.category_id = c.id
LEFT JOIN balances AS b ON p.sku = b.sku;
