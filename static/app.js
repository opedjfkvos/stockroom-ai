const element = (id) => document.getElementById(id);
const currency = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" });
const rows = [];
const mode = element("mode");
const question = element("question");
const result = element("result");
const table = element("table-body");

function tag(name, content = "", className = "") {
  const item = document.createElement(name);
  item.textContent = content;
  item.className = className;
  return item;
}

function money(cents) {
  return currency.format(cents / 100);
}

function renderRows() {
  const filter = element("filter").value.trim().toLowerCase();
  const category = element("category").value;
  const status = element("stock").value;
  const filtered = rows.filter((row) =>
    (!category || row.category === category) &&
    (!status || row.status === status) &&
    (row.name.toLowerCase().includes(filter) || row.sku.toLowerCase().includes(filter))
  );

  table.replaceChildren();
  element("record-count").textContent = `${filtered.length} of ${rows.length} products`;
  if (!filtered.length) {
    const cell = tag("td", "No products match these filters.");
    cell.colSpan = 6;
    const line = tag("tr");
    line.append(cell);
    table.append(line);
  }
  for (const row of filtered) {
    const product = tag("td", row.name, "product");
    product.append(tag("span", row.sku, "sku"));
    const statusCell = tag("td");
    const className = row.status === "Healthy" ? "status" : row.stock > 0 ? "status low" : "status out";
    statusCell.append(tag("span", row.status, className));
    const line = tag("tr");
    line.append(product, tag("td", row.category), tag("td", row.stock),
      tag("td", row.minimum), tag("td", money(row.unit_cents)), statusCell);
    table.append(line);
  }
}

function renderBars(categories) {
  const totals = categories.map((category) => rows
    .filter((row) => row.category === category)
    .reduce((total, row) => total + row.stock, 0));
  const maximum = Math.max(1, ...totals);
  element("bars").replaceChildren();
  categories.forEach((category, index) => {
    const line = tag("div", "", "bar");
    const bar = tag("progress");
    bar.max = maximum;
    bar.value = Math.max(0, totals[index]);
    bar.setAttribute("aria-label", category);
    line.append(tag("span", category), bar, tag("span", totals[index]));
    element("bars").append(line);
  });
}

async function fetchJson(path, options) {
  const response = await fetch(path, options);
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || "Request failed.");
  return payload;
}

async function refreshInventory() {
  try {
    const data = await fetchJson("/api/inventory");
    rows.splice(0, rows.length, ...data.rows);
    element("load-error").className = "hidden";
    element("load-error").textContent = "";
    element("metric-products").textContent = data.metrics.products;
    element("metric-units").textContent = data.metrics.units;
    element("metric-reorder").textContent = data.metrics.reorder;
    element("metric-value").textContent = money(data.metrics.value_cents);
    const categorySelect = element("category");
    const selectedCategory = categorySelect.value;
    categorySelect.replaceChildren(new Option("All categories", ""));
    for (const category of data.categories) categorySelect.add(new Option(category));
    if (data.categories.includes(selectedCategory)) categorySelect.value = selectedCategory;

    element("examples").replaceChildren();
    for (const example of data.examples) {
      const button = tag("button", example);
      button.type = "button";
      button.addEventListener("click", () => {
        question.value = example;
        question.focus();
      });
      element("examples").append(button);
    }
    renderBars(data.categories);
    renderRows();
  } catch (error) {
    element("load-error").className = "error";
    element("load-error").textContent = `Could not refresh the inventory. Displayed data may be out of date. ${error.message}`;
  }
}

function renderAnswer(data) {
  const details = tag("details");
  const scope = [data.plan.category, data.plan.product].filter(Boolean).join(" · ");
  const plan = tag("p", `Tool: ${data.plan.tool} · ${scope || "All categories"}`, "tool");
  const sources = tag("ul");
  for (const row of data.rows) {
    sources.append(tag("li", `${row.name} (${row.sku}): ${row.stock} on hand, reorder point ${row.minimum}, unit cost ${money(row.unit_cents)}`));
  }
  details.append(tag("summary", `Evidence · ${data.rows.length} records`), plan);
  if (data.rows.length) details.append(sources);
  result.replaceChildren(tag("p", data.answer), details);
}

element("ask-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = element("ask");
  const selectedMode = mode.value;
  button.disabled = mode.disabled = true;
  result.replaceChildren(tag("p", selectedMode === "demo" ? "Checking inventory…" : "Waiting for local model…"));
  try {
    const data = await fetchJson("/api/ask", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question: question.value, mode: selectedMode }),
    });
    renderAnswer(data);
    await refreshInventory();
  } catch (error) {
    result.replaceChildren(tag("p", error.message, "error"));
  } finally {
    button.disabled = mode.disabled = false;
  }
});

mode.addEventListener("change", () => {
  element("mode-help").textContent = mode.value === "demo"
    ? "Local demo with preset routing."
    : "Uses Ollama on this computer to select a tool.";
  result.replaceChildren(tag("p", "Ask a question to see the answer and the records behind it."));
});
element("filter").addEventListener("input", renderRows);
element("category").addEventListener("change", renderRows);
element("stock").addEventListener("change", renderRows);
refreshInventory();
