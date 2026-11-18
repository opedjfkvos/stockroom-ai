# Stockroom AI

[![Tests](https://github.com/opedjfkvos/stockroom-ai/actions/workflows/tests.yml/badge.svg?branch=main)](https://github.com/opedjfkvos/stockroom-ai/actions/workflows/tests.yml)

Stockroom AI is an AI-assisted portfolio prototype for answering questions about a sample inventory. It includes a browser dashboard, a Python API and a SQLite database. The dashboard opens in Demo mode with no model downloads, API keys, or third-party Python packages.


![Stockroom AI dashboard with the reorder result and supporting inventory records](docs/dashboard.png)

## Run locally

Use Python 3.10 or newer.

    python app.py

Open http://127.0.0.1:8080. If the port is busy, set a different one with `python app.py --port 8082`.

Try these examples:

| Question | Tool | Expected result |
| --- | --- | --- |
| What needs reordering? | Reorder | 5 products, 20 units needed |
| Do we have any webcams? | Lookup | 0 units for the 1080p Webcam |
| Which accessories have the most stock? | Compare | Braided USB-C Cable, 48 units |
| What is our current inventory value? | Valuation | $7,372.90 |
| Which products are out of stock? | Out of stock | 2 products |

Select **Evidence** after an answer to inspect the tool, scope, source records and unit costs. The dashboard reloads inventory after each successful question. The catalogue can also be filtered by name or SKU, category and stock status.

## Run with a local language model

Start Ollama and install the example model.

    ollama pull qwen2.5:1.5b

With Ollama running at its default local address, choose **Local LLM** in the web UI and ask a question. Set the `STOCKROOM_MODEL` environment variable before starting the app to use a different installed Ollama model. Smaller models can misinterpret questions, so use the Evidence panel to check tool selection and filters.

## Evaluate question routing

```sh
python model_eval.py --demo
python model_eval.py --model qwen2.5:1.5b
```

The small 18-question smoke suite checks the documented Demo behavior in CI. The 12-question diagnostic contains paraphrases and out-of-scope requests. The Demo rules for “sold out” and questions of the form “there are any … left” were added after reviewing this set. The scope and routing changes also used this set for verification. Its score is a regression result. Add `--strict` to make diagnostic mismatches fail a scripted run. CI gates on the smoke suite. The reported bugs have their own regression tests.

```sh
python model_eval.py --demo --holdout
python model_eval.py --model qwen2.5:1.5b --holdout
python model_eval.py --model qwen2.5:1.5b --holdout --app
```

| Run | Exact plans | Correct tools | Correct filters | Correct refusals | Incorrect refusals on supported requests |
| --- | ---: | ---: | ---: | ---: | ---: |
| Demo, smoke | 18/18 | 14/14 | 14/14 | 4/4 | 0/14 |
| Demo, partly tuned diagnostic (current) | 12/12 | 7/7 | 7/7 | 5/5 | 0/7 |
| Qwen 1.5B, original prompt, raw | 2/12 | 5/7 | 2/7 | 0/5 | 0/7 |
| Qwen 1.5B, revised prompt, raw (562f1a8) | 6/12 | 6/7 | 6/7 | 1/5 | 0/7 |
| Qwen 1.5B, revised prompt with app checks (562f1a8) | 11/12 | 6/7 | 7/7 | 5/5 | 0/7 |
| Qwen 1.5B, live catalogue revision, raw | 4/12 | 5/7 | 4/7 | 1/5 | 0/7 |
| Qwen 1.5B, live catalogue revision with app checks | 11/12 | 6/7 | 7/7 | 5/5 | 0/7 |

Tool and filter accuracy use all supported questions as their denominator. A refusal scores zero on both. Filters are scored independently of the tool, and product keywords are equivalent if they match the same sample SKUs. The raw run sends prompts directly to Ollama. The `--app` run uses the same planner and scope checks as the HTTP API. This diagnostic was reviewed before tuning the prompt and the app checks. Its results are useful for regression checks; new store questions are needed to estimate generalization. See [model_eval.py](model_eval.py) for the questions and scoring rules.

In the [Ollama run on 18 November 2026](https://github.com/opedjfkvos/stockroom-ai/actions/runs/44352150255), Qwen (model ID `65ec06548149`) produced valid plans but often guessed the scope or selected a tool for an unsupported request. Examples:

| Question | Model output | Expected behavior |
| --- | --- | --- |
| What should we buy more of? | Compared keyboards in Peripherals | Reorder report for the whole catalogue |
| How much money is tied up in everything on the shelves? | Valued only Audio | Value the whole catalogue |
| Tell me what to charge for shipping a webcam. | Looked up webcam stock | Decline. The database has no shipping rates |

The [revised run](https://github.com/opedjfkvos/stockroom-ai/actions/runs/44357363346) used the same Qwen model and diagnostic questions. The app removed a category the model had guessed for the webcam and declined all five unsupported requests. One request still failed: “What should we buy more of?” was routed to `compare` instead of `reorder`.

The [live-catalogue run](https://github.com/opedjfkvos/stockroom-ai/actions/runs/44363807156) routed that purchase question correctly. Its remaining app error was “Between the accessories, what do we have the biggest pile of?” Ollama chose `lookup` instead of `compare`. The raw score dropped after the output schema and prompt changed. These are results from the same 12-question diagnostic, including its known paraphrases.

## How a question is answered

1. A question enters `POST /api/ask`.
2. Demo mode selects a tool using deterministic rules. Local LLM mode asks Ollama for a JSON object that identifies a tool and optional filters.
3. Python validates the plan and resolves the actual category and product from the question. For an ambiguous scope, the assistant can request clarification. Unsupported requests return a help message before an inventory tool runs.
4. SQLite runs the selected query with bound parameters. The connection is read-only after the initial seed.
5. Python calculates or summarizes the result using the returned records. The model does not generate SQL or inventory quantities.

The API exposes `GET /api/inventory` for the catalogue and dashboard metrics. It exposes `POST /api/ask` with JSON fields `question` and `mode` (`demo` or `ollama`) for the assistant. The server binds to localhost. Both modes read product names and categories from the current SQLite database and ask for clarification when the subject cannot be matched. Supported scope wording is limited to English.

## SQLite schema

On first use the app creates the [base schema](sql/schema.sql), seeds a local SQLite file at `data/stockroom.sqlite3`, and applies [migration 2](sql/002_stock_status.sql). Existing v1 databases keep their original products and movements. A missing database file during operation produces an error so the dashboard cannot silently switch to sample data. Set `STOCKROOM_DB` to select a different file. The schema separates categories, products and signed stock movements. The `inventory_status` view joins the catalogue with aggregated movements, so a product with no movements has zero on-hand stock. Foreign keys are enforced during initialization, and indexes support category and movement lookups. App queries open the database read-only.

[Example SQL reports](sql/reports.sql) summarize stock value and replenishment costs by category. The app calculates dashboard totals in SQLite.

## Verify

    python -m unittest discover -s tests -v

Tests cover Demo routing, stock and price calculations, the real `POST /api/ask` endpoint, and persistence and read-only access in SQLite. The SQL reports are reconciled with the dashboard and reorder query. The movement test records a delivery and dispatch and checks the resulting balances and statuses. Separate tests exercise the Ollama client against a stub server. Live-model quality is measured by the standalone evaluation when Ollama is available.

The GitHub Actions workflow is configured to run the unit tests and the Demo evaluation on Python 3.10 and 3.12.

## Files

- `app.py` serves the client and the JSON endpoints.
- `inventory.py` initializes SQLite and implements the five inventory tools.
- `sql/` contains the schema and reporting queries.
- `assistant.py` routes demo requests and validates Ollama plans.
- `static/` contains the dashboard.
- `tests/` contains the regression checks.

This prototype was prepared with AI assistance for portfolio review and practice. The inventory is synthetic.

Distributed under the [MIT License](LICENSE).
