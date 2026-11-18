# Stockroom AI

Stockroom AI is an AI-assisted portfolio prototype for answering questions about a sample inventory. It includes a browser dashboard, a Python API and a SQLite database. The dashboard opens in Demo mode with no model downloads, API keys, or third-party Python packages.


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

Select **Evidence** after an answer to inspect the tool and records used. The catalogue can also be filtered by name or SKU, category and stock status.

## Run with a local language model

Start Ollama and install the example model.

    ollama pull qwen2.5:1.5b

With Ollama running at its default local address, choose **Local LLM** in the web UI and ask a question. Set the `STOCKROOM_MODEL` environment variable before starting the app to use a different installed Ollama model. Smaller models can misinterpret questions, so use the Evidence panel to check tool selection and filters.

## Evaluate question routing

```sh
python model_eval.py --demo
python model_eval.py --model qwen2.5:1.5b
```

The 18 labeled prompts check tool selection, category and product filters, and refusal of unsupported requests. The live-model command sends each prompt directly to Ollama to measure the model's routing, including cases the app normally rejects before contacting the model. The Demo command checks the deterministic planner with the app's scope checks. The dataset and scoring rules are in [model_eval.py](model_eval.py).

| Router | Result |
| --- | --- |
| Demo | 18 / 18 on the labeled suite |
| qwen2.5:1.5b | Awaiting a run against a local Ollama instance |

## How a question is answered

1. A question enters `POST /api/ask`.
2. Demo mode selects a tool using deterministic rules. Local LLM mode asks Ollama for a JSON object that identifies a tool and optional filters.
3. Python validates the tool and filters. Unsupported requests return a help message before any inventory tool runs.
4. SQLite runs the selected query with bound parameters. The connection is read-only after the initial seed.
5. Python calculates or summarizes the result using the returned records. The model does not generate SQL or inventory quantities.

The API exposes `GET /api/inventory` for the catalogue and dashboard metrics. It exposes `POST /api/ask` with JSON fields `question` and `mode` (`demo` or `ollama`) for the assistant. The sample database is recreated in memory, and the server binds to localhost.

## Verify

    python -m unittest discover -s tests -v

The checks cover tool selection, stock totals, reorder quantities, price calculations, invalid input, and the HTTP integration with a stand-in for Ollama. This exercises the request and response contract. It does not measure the quality of a live model.

The GitHub Actions workflow is configured to run the unit tests and the Demo evaluation on Python 3.10 and 3.12.

## Files

- `app.py` serves the client and the JSON endpoints.
- `inventory.py` seeds SQLite and implements the five inventory tools.
- `assistant.py` routes demo requests and validates Ollama plans.
- `static/` contains the dashboard.
- `tests/` contains the regression checks.

This prototype was prepared with AI assistance for portfolio review and practice. The inventory is synthetic.

Distributed under the [MIT License](LICENSE).
