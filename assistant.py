"""Translate questions into bounded inventory tool calls."""

import json
import os
import re
from http.client import HTTPException
from urllib.request import Request, urlopen

from inventory import CATEGORIES, ITEMS, TOOLS, Plan, answer_from_rows, name_matches, run_tool, tokens

DEFAULT_MODEL = "qwen2.5:1.5b"
OUT_OF_SCOPE = (
    "I can check current stock, reorder needs, out-of-stock products, highest stock, "
    "or inventory value. Try asking about one of these."
)
UNSUPPORTED_REQUEST = re.compile(
    r"\b(forecast|predict|weather|translate|sales|revenue|historical|yesterday|tomorrow|"
    r"last (?:week|month|year)|next (?:week|month|year)|delete|update|remove|set|change|"
    r"shipping|delivery fee|most expensive|cheapest)\b"
    r"|\bwill (?:we|it|the) .+\b(?:run out|sell out)\b"
    r"|\b(?:add|order|buy|purchase|reorder)\s+\d+\b"
)
CATEGORY_ALIASES = {"network": "Networking"}
PRODUCT_BOUNDARIES = {
    "a", "an", "any", "are", "about", "do", "for", "from", "have", "in", "is", "left",
    "many", "need", "needs", "of", "on", "out", "please", "sold", "stock", "the", "which", "worth",
    "list", "show", "below", "completely", "reorder", "reordering", "replenish", "replenishment",
}
PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "tool": {"type": "string", "enum": [*TOOLS, "unsupported"]},
        "category": {"type": "string", "enum": ["", *CATEGORIES]},
        "product": {"type": "string", "maxLength": 100},
    },
    "required": ["tool", "category", "product"],
    "additionalProperties": False,
}
EXAMPLES = (
    "What needs reordering?",
    "Do we have any webcams?",
    "Which accessories have the most stock?",
    "What is our current inventory value?",
)
PROMPT = """You route read-only questions about today's warehouse inventory.
Return one JSON object with keys tool, category, product.

Choose the tool that answers exactly what was asked:
- lookup: whether a product is available, or how many units are on hand.
- reorder: which stock levels are below their reorder point.
- out_of_stock: which products have exactly zero units.
- compare: which product has the highest number of units in stock.
- valuation: total value of the stock on hand at unit cost.
- unsupported: anything else, including stock forecasts, past inventory, delivery,
  prices charged to customers, ranking by unit price, and changes to inventory.

A category must be explicitly named. Do not infer a category from a product.
Copy the requested product keyword or SKU. For the whole warehouse, set both
filters to "". For unsupported, also set both filters to "".

"Which lines need replenishment now?" -> {"tool":"reorder","category":"","product":""}
"How much is the entire stock worth?" -> {"tool":"valuation","category":"","product":""}
"Is the laptop stand available?" -> {"tool":"lookup","category":"","product":"laptop stand"}
"Will the laptop stand sell out in two days?" -> {"tool":"unsupported","category":"","product":""}
"Mark the laptop stand unavailable." -> {"tool":"unsupported","category":"","product":""}

Return only the JSON object. Do not calculate amounts or write SQL."""



class ModelUnavailable(Exception):
    """The local model could not respond with a usable plan."""


class UnsupportedQuestion(ValueError):
    """The question falls outside the supported inventory queries."""


def product_filter(question):
    sku_mentions = re.findall(r"\b[a-z]{3}-[0-9]+\b", question)
    if len(sku_mentions) > 1:
        raise UnsupportedQuestion("Ask about one product or category at a time.")
    if sku_mentions:
        return sku_mentions[0].upper()

    words = tokens(question)
    generic_usb_lookup = re.search(r"\bdo we have (?:any )?usb[?.]?$", question)
    if generic_usb_lookup:
        return "usb"
    product_names = [(sku, name) for sku, name, *_ in ITEMS]
    for width in range(min(len(words), max(len(tokens(name)) for _, name in product_names)), 0, -1):
        for start in range(len(words) - width + 1):
            keyword = " ".join(words[start:start + width])
            if keyword in {"usb", "wi", "fi", "c", "gb", "tb"} or keyword.isdecimal():
                continue
            matching = [sku for sku, name in product_names if name_matches(name, keyword)]
            if matching:
                end = start + width
                adjacent = words[max(0, start - 1):start] + words[end:end + 1]
                if any(word not in PRODUCT_BOUNDARIES for word in adjacent):
                    raise UnsupportedQuestion("Use a product name or SKU from the catalogue.")
                return matching[0] if len(matching) == 1 else keyword
    return ""


def question_scope(normalized):
    if re.search(r"\b(?:and|or)\b", normalized):
        raise UnsupportedQuestion("Ask about one product or category at a time.")
    words = set(tokens(normalized))
    categories = {name for name in CATEGORIES if set(tokens(name)) <= words}
    categories.update(name for alias, name in CATEGORY_ALIASES.items() if alias in words)
    if len(categories) > 1:
        raise UnsupportedQuestion("Ask about one category at a time.")
    category = next(iter(categories), "")
    return category, product_filter(normalized)


def demo_plan(question):
    """Deterministic keyword routing so reviewers can try the app without a model."""
    normalized = question.casefold()
    category, product = question_scope(normalized)
    if re.search(r"\b(out of stock|sold out)\b", normalized):
        tool = "out_of_stock"
    elif re.search(r"\b(reorder(?:ing)?|replenish(?:ment)?|low stock|below (?:the )?(?:minimum|reorder point))\b", normalized):
        tool = "reorder"
    elif re.search(r"\b(inventory value|stock value|inventory valuation|stock worth|value of (?:the )?(?:inventory|stock))\b", normalized):
        tool = "valuation"
    elif (product or category) and re.search(r"\b(value of|worth)\b", normalized):
        tool = "valuation"
    elif re.search(r"\b(most stock|highest stock|compare stock|rank by stock)\b", normalized):
        tool = "compare"
    else:
        tool = "lookup"

    if tool == "lookup":
        lookup_intent = re.search(
            r"^(?:show|list|what about|do we have|how many|stock (?:for|of|in))\b",
            normalized,
        ) or re.search(r"\b(?:units|in stock|inventory|on hand)\b|\b(?:are there|there are) any .+ left\b", normalized)
        catalogue_request = re.fullmatch(
            r"(?:show|list) (?:me )?(?:the |all )?(?:current )?(?:inventory|products|items|stock)[?.]?",
            normalized,
        )
        if not (catalogue_request or lookup_intent and (product or category)):
            raise UnsupportedQuestion(OUT_OF_SCOPE)
    return Plan(tool, category, product)


def ollama_plan(question, model, endpoint="http://127.0.0.1:11434"):
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": PROMPT},
            {"role": "user", "content": question},
        ],
        "stream": False,
        "format": PLAN_SCHEMA,
        "options": {"temperature": 0},
    }
    request = Request(
        endpoint.rstrip("/") + "/api/chat",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urlopen(request, timeout=35) as response:
            reply = response.read(256_001)
        if len(reply) > 256_000:
            raise ValueError("Model response too long.")
        plan = json.loads(json.loads(reply)["message"]["content"])
        if not isinstance(plan, dict) or set(plan) != set(PLAN_SCHEMA["required"]):
            raise ValueError("Invalid plan shape.")
        if plan["tool"] == "unsupported":
            return None
        return Plan(**plan)
    except (OSError, HTTPException, ValueError, TypeError, KeyError) as exc:
        raise ModelUnavailable(
            "The local model did not return a valid plan. Check Ollama and the installed model, or switch to Demo."
        ) from exc


def plan_question(question, mode, model):
    if not isinstance(question, str) or not question.strip() or len(question) > 280:
        raise ValueError("Enter a question of 1 to 280 characters.")
    if mode not in ("demo", "ollama"):
        raise ValueError("Unknown assistant mode.")
    question = question.strip()
    if UNSUPPORTED_REQUEST.search(question.casefold()):
        raise UnsupportedQuestion(OUT_OF_SCOPE)
    if mode == "demo":
        return demo_plan(question)
    normalized = question.casefold()
    category, product = question_scope(normalized)
    plan = ollama_plan(question, model)
    if plan is None:
        raise UnsupportedQuestion(OUT_OF_SCOPE)
    if not (category or product) and (plan.category or plan.product):
        whole_catalogue = re.search(r"\b(?:all|everything|entire|whole|total)\b", normalized)
        if not whole_catalogue:
            raise UnsupportedQuestion("Specify a product, category, or the entire inventory.")
    return Plan(plan.tool, category, product)


def query_inventory(question, mode="demo"):
    model = os.environ.get("STOCKROOM_MODEL", DEFAULT_MODEL)
    plan = plan_question(question, mode, model)
    rows = run_tool(plan)
    scope_exists = (
        not rows and plan.product and plan.tool in {"reorder", "out_of_stock"}
        and bool(run_tool(Plan("lookup", category=plan.category, product=plan.product)))
    )
    return {
        "answer": answer_from_rows(plan, rows, scope_exists=scope_exists),
        "plan": plan.to_dict(),
        "rows": rows,
        "mode": mode,
        "model": model if mode == "ollama" else None,
    }
