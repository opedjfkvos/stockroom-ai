-- Stock exposure by category, including categories that currently have no stock.
SELECT c.name AS category,
       COUNT(i.sku) AS products,
       COALESCE(SUM(i.stock), 0) AS units_on_hand,
       COALESCE(SUM(i.stock * i.unit_cents), 0) AS inventory_value_cents
FROM categories AS c
LEFT JOIN inventory_status AS i ON i.category = c.name
GROUP BY c.id, c.name
ORDER BY inventory_value_cents DESC, c.name;

-- Replenishment workload and estimated purchasing cost by category.
SELECT category,
       COUNT(*) AS products_below_minimum,
       SUM(minimum - stock) AS units_to_order,
       SUM((minimum - stock) * unit_cents) AS reorder_cost_cents
FROM inventory_status
WHERE stock < minimum
GROUP BY category
ORDER BY units_to_order DESC, category;
