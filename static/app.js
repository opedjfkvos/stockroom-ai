const element = (id) => document.getElementById(id);
const languagePicker = element("language");
const launch = new URLSearchParams(location.search);
const savedLanguage = launch.get("lang") || localStorage.getItem("stockroom-language");
languagePicker.value = ["en", "ru", "uk"].includes(savedLanguage) ? savedLanguage : "en";
const locale = () => ({ en: "en-US", ru: "ru-RU", uk: "uk-UA" })[languagePicker.value];
let currency = new Intl.NumberFormat(locale(), { style: "currency", currency: "USD" });

function t(key, variables = {}) {
  const localized = messages[languagePicker.value];
  const template = localized && Object.hasOwn(localized, key) ? localized[key] : key;
  return template.replace(/\{(\w+)\}/g, (match, name) => variables[name] ?? match);
}

const localizedElements = [...document.querySelectorAll("[data-i18n], [placeholder], [aria-label]")]
  .flatMap((node) => [
    ...(node.hasAttribute("data-i18n") ? [[node, "", node.textContent]] : []),
    ...["placeholder", "aria-label"].filter((attr) => node.hasAttribute(attr))
      .map((attr) => [node, attr, node.getAttribute(attr)]),
  ]);

function applyLanguage() {
  document.documentElement.lang = languagePicker.value;
  localStorage.setItem("stockroom-language", languagePicker.value);
  for (const [node, attr, key] of localizedElements) {
    if (attr) node.setAttribute(attr, t(key));
    else node.textContent = t(key);
  }
}
const sourcePicker = element("inventory-source");
sourcePicker.value = launch.get("source") === "imported" ||
  localStorage.getItem("stockroom-inventory-source") === "imported" ? "imported" : "sample";
let refreshRequest = 0;
const rows = [];
const mode = element("mode");
if (launch.get("mode") === "ollama" || localStorage.getItem("stockroom-mode") === "ollama") mode.value = "ollama";
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
  element("record-count").textContent = t("{shown} of {total} products", { shown: filtered.length, total: rows.length });
  if (!filtered.length) {
    const cell = tag("td", t("No products match these filters."));
    cell.colSpan = 6;
    const line = tag("tr");
    line.append(cell);
    table.append(line);
  }
  for (const row of filtered) {
    const product = tag("td", row.name, "product");
    const sku = tag("button", row.sku, "sku sku-link");
    sku.type = "button";
    sku.setAttribute("aria-label", t("Ask about {name} ({sku})", row));
    sku.addEventListener("click", () => {
      question.value = t("What about SKU {sku}?", row);
      question.focus();
    });
    product.append(sku);
    const statusCell = tag("td");
    const className = row.status === "Healthy" ? "status" : row.stock > 0 ? "status low" : "status out";
    statusCell.append(tag("span", t(row.status), className));
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
  if (!response.ok) throw new Error(t(payload.error || "Request failed."));
  return payload;
}

async function refreshInventory() {
  const request = ++refreshRequest;
  const source = sourcePicker.value;
  try {
    const data = await fetchJson(`/api/inventory?source=${source}`);
    if (request !== refreshRequest) return;
    currency = new Intl.NumberFormat(locale(), { style: "currency", currency: data.currency || "USD" });
    const label = t(source === "imported" ? "Imported inventory" : "Sample inventory");
    element("inventory-label").textContent = data.currency ? `${label} · ${data.currency}` : label;
    if (data.imported_at) {
      element("inventory-label").textContent += " · " + t("Imported {date}", { date: new Date(data.imported_at).toLocaleString(locale()) });
    }
    rows.splice(0, rows.length, ...data.rows);
    const negatives = rows.filter((row) => row.stock < 0).length;
    const warning = element("balance-warning");
    warning.classList.toggle("hidden", !negatives);
    warning.textContent = negatives
      ? t("Negative stock on {count} items. Check the source data. Negative balances are valued at zero.", { count: negatives })
      : "";
    element("load-error").className = "hidden";
    element("load-error").textContent = "";
    element("metric-products").textContent = data.metrics.products;
    element("metric-units").textContent = data.metrics.units;
    element("metric-reorder").textContent = data.metrics.reorder;
    element("metric-value").textContent = money(data.metrics.value_cents);
    const categorySelect = element("category");
    const selectedCategory = categorySelect.value;
    categorySelect.replaceChildren(new Option(t("All categories"), ""));
    for (const category of data.categories) categorySelect.add(new Option(category));
    if (data.categories.includes(selectedCategory)) categorySelect.value = selectedCategory;

    element("examples").replaceChildren();
    for (const example of data.examples) {
      const button = tag("button", t(example));
      button.type = "button";
      button.addEventListener("click", () => {
        question.value = t(example);
        question.focus();
      });
      element("examples").append(button);
    }
    renderBars(data.categories);
    renderRows();
    return true;
  } catch (error) {
    if (request !== refreshRequest) return;
    clearInventory();
    element("inventory-label").textContent = t("Inventory unavailable");
    element("load-error").className = "error notice";
    element("load-error").textContent = error.message;
    return false;
  }
}

function renderAnswer(data) {
  const details = tag("details");
  const scope = [data.plan.category, data.plan.product].filter(Boolean).join(" · ");
  const plan = tag("p", t("Operation") + " · " + t(data.plan.tool) + " · " + (scope || t("All categories")), "tool");
  const sources = tag("ul");
  for (const row of data.rows) {
    sources.append(tag("li", t("{name} ({sku}): {stock} on hand, reorder point {minimum}, unit cost {cost}", {
      ...row, cost: money(row.unit_cents),
    })));
  }
  details.append(tag("summary", t("Evidence · {count} records", { count: data.rows.length })), plan);
  if (data.rows.length) details.append(sources);
  result.replaceChildren(tag("p", data.answer), details);
}

element("ask-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = element("ask");
  const selectedMode = mode.value;
  const selectedSource = sourcePicker.value;
  button.disabled = mode.disabled = sourcePicker.disabled = languagePicker.disabled = true;
  result.replaceChildren(tag("p", t(selectedMode === "demo" ? "Checking inventory…" : "Waiting for local model…")));
  try {
    const data = await fetchJson("/api/ask", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question: question.value, mode: selectedMode, source: selectedSource, language: languagePicker.value }),
    });
    if (selectedSource === sourcePicker.value) {
      const current = await refreshInventory();
      if (current && selectedSource === sourcePicker.value) renderAnswer(data);
      else result.replaceChildren(tag("p", t("Inventory unavailable. Please check the source."), "error"));
    }
  } catch (error) {
    result.replaceChildren(tag("p", t(error.message), "error"));
  } finally {
    button.disabled = mode.disabled = sourcePicker.disabled = languagePicker.disabled = false;
  }
});

function updateModeHelp() {
  element("mode-help").textContent = t(mode.value === "demo"
    ? "Stock lookup works without a model."
    : "Uses local Ollama with verified catalogue filters.");
}

mode.addEventListener("change", () => {
  localStorage.setItem("stockroom-mode", mode.value);
  updateModeHelp();
  result.replaceChildren(tag("p", t("Ask a question to see the answer and the records behind it.")));
});
element("filter").addEventListener("input", renderRows);
element("category").addEventListener("change", renderRows);
element("stock").addEventListener("change", renderRows);
function switchInventory() {
  localStorage.setItem("stockroom-inventory-source", sourcePicker.value);
  element("inventory-label").textContent = t(sourcePicker.value === "sample" ? "Sample inventory" : "Imported inventory");
  clearInventory();
  result.replaceChildren(tag("p", t("Ask a question about the selected inventory.")));
  return refreshInventory();
}

function clearInventory() {
  rows.length = 0;
  element("balance-warning").classList.add("hidden");
  element("examples").replaceChildren();
  element("category").replaceChildren(new Option(t("All categories"), ""));
  for (const metric of ["products", "units", "reorder", "value"]) element(`metric-${metric}`).textContent = "—";
  element("bars").replaceChildren();
  renderRows();
}

sourcePicker.addEventListener("change", switchInventory);

const importFile = element("import-file");
const importCurrency = element("import-currency");
const importPreview = element("import-preview");
const importConfirm = element("import-confirm");
const importResult = element("import-result");
const importTable = element("import-table");
let selectedImport = null;

function clearPreview() {
  selectedImport = null;
  importConfirm.hidden = importTable.hidden = true;
  importResult.replaceChildren();
}

importFile.addEventListener("change", clearPreview);
importCurrency.addEventListener("change", clearPreview);

function setImportBusy(busy) {
  importFile.disabled = importCurrency.disabled = importPreview.disabled = importConfirm.disabled = busy;
}

function sendSpreadsheet(action, file, code, confirmation) {
  const params = new URLSearchParams({ filename: file.name, currency: code });
  const headers = { "Content-Type": "application/octet-stream", "X-Stockroom-Import": "1" };
  if (confirmation) headers["X-Stockroom-Confirmation"] = confirmation;
  return fetchJson(`/api/import/${action}?${params}`, { method: "POST", headers, body: file });
}

element("import-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  clearPreview();
  setImportBusy(true);
  const file = importFile.files[0];
  const code = importCurrency.value;
  try {
    const data = await sendSpreadsheet("preview", file, code);
    importResult.append(tag("p", t("{count} valid rows · {code} · showing up to 10 rows", { count: data.count, code })));
    if (data.errors.length) {
      const issues = tag("ul", "", "error");
      for (const problem of data.errors) issues.append(tag("li", t(problem)));
      importResult.append(issues);
    }
    if (data.preview.length) {
      const preview = tag("table");
      const head = tag("tr");
      for (const label of ["SKU", "Product", "Category", "On hand", "Minimum", "Unit cost"]) {
        const cell = tag("th", t(label));
        cell.scope = "col";
        head.append(cell);
      }
      const formatter = new Intl.NumberFormat(locale(), { style: "currency", currency: code });
      const body = tag("tbody");
      for (const item of data.preview) {
        const line = tag("tr");
        for (const value of [item.sku, item.name, item.category, item.stock, item.minimum,
          formatter.format(item.unit_cents / 100)]) line.append(tag("td", value));
        body.append(line);
      }
      const headings = tag("thead");
      headings.append(head);
      preview.append(headings, body);
      importTable.replaceChildren(preview);
      importTable.hidden = false;
    }
    if (data.confirmation) {
      selectedImport = { file, code, confirmation: data.confirmation };
      importResult.append(tag("p", t("Confirm to update the listed SKUs. Other SKUs stay in the inventory.")));
      importConfirm.hidden = false;
    }
  } catch (error) {
    importResult.replaceChildren(tag("p", t(error.message), "error"));
  } finally {
    setImportBusy(false);
  }
});

importConfirm.addEventListener("click", async () => {
  if (!selectedImport) return;
  const { file, code, confirmation } = selectedImport;
  setImportBusy(true);
  try {
    const data = await sendSpreadsheet("confirm", file, code, confirmation);
    clearPreview();
    importResult.append(tag("p", t("Imported {count} products.", { count: data.count })));
    sourcePicker.value = "imported";
    await switchInventory();
  } catch (error) {
    clearPreview();
    importResult.replaceChildren(tag("p", t(error.message), "error"));
  } finally {
    setImportBusy(false);
  }
});

languagePicker.addEventListener("change", () => {
  applyLanguage();
  updateModeHelp();
  clearPreview();
  result.replaceChildren(tag("p", t("Ask a question about the selected inventory.")));
  refreshInventory();
  fetch("/api/preferences", {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Stockroom-Preference": "1" },
    body: JSON.stringify({ language: languagePicker.value }),
  }).catch(() => {});
});

applyLanguage();
localStorage.setItem("stockroom-mode", mode.value);
localStorage.setItem("stockroom-inventory-source", sourcePicker.value);
updateModeHelp();
if (launch.has("lang") || launch.has("mode") || launch.has("source")) {
  history.replaceState(null, "", location.pathname);
}
refreshInventory();
