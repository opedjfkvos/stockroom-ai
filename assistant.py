"""Translate questions into bounded inventory tool calls."""

import json
import os
import re
from http.client import HTTPException
from urllib.request import Request, urlopen

from inventory import TOOLS, Plan, answer_from_rows, catalog_scope, name_matches, run_tool, tokens

DEFAULT_MODEL = "qwen2.5:1.5b"
OUT_OF_SCOPE = (
    "I can check current stock, reorder needs, out-of-stock products, highest stock, "
    "or inventory value. Try asking about one of these."
)
UNSUPPORTED_REQUEST = re.compile(
    r"\b(forecast|predict|weather|translate|sales|revenue|historical|yesterday|tomorrow|"
    r"last (?:week|month|year)|next (?:week|month|year)|shipping|delivery fee|"
    r"most expensive|cheapest|lowest stock|least stock|unit price|repair)\b"
    r"|\b(?:was|were)\b.*\b(?:stock|on hand)\b"
    r"|\bwill\b.*\b(?:stock|run out|sell out|on hand)\b"
    r"|^(?:please\s+)?(?:add|order|buy|purchase|delete|update|remove|set|change|reduce|deduct|insert|reorder)\b"
    r"|\bwhich category\b"
    r"|^how much (?:is|does) (?:a|an)\b"
)
CATEGORY_ALIASES = {"network": "Networking"}
# Remove routing words from the entire question. Any remaining noun phrase must
# match the catalogue as a unit; matching a single word would drop qualifiers.
SCOPE_WORDS = set("""
    a about all an any are available be below by can catalogue catalog check completely
    count could current currently do doe entire everything for from had has have highest
    how i in inventory is item left list low many me minimum more most need of on our
    out please product rank reorder reordering replenish replenishment should show
    sold stock tell than the there total unit value valuation we what whether which
    whole with worth you across warehouse buy much now today running hand tied up
    money into every at to between biggest pile sku
""".split())
REORDER_INTENT = re.compile(
    r"\b(reorder(?:ing)?|replenish(?:ment)?|low stock|below (?:the )?(?:minimum|reorder point)|"
    r"buy more|running low)\b"
)
PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "tool": {"type": "string", "enum": [*TOOLS, "unsupported"]},
        "category": {"type": "string", "maxLength": 100},
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
- out_of_stock: which products have zero or negative units.
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


def question_scope(question, products, categories):
    if re.search(r"\b(?:and|or)\b", question):
        raise UnsupportedQuestion("Ask about one product or category at a time.")

    sku_mentions = re.findall(r"\b[a-z][a-z0-9]{0,19}-[0-9][a-z0-9-]*\b", question)
    if len(set(sku_mentions)) > 1:
        raise UnsupportedQuestion("Ask about one product or category at a time.")

    # These complete phrases refer to the catalogue, not to a product.
    question = re.sub(r"\beverything on (?:the )?shelves\b", "everything", question)
    words = tokens(question)
    category_words = set()
    found_categories = set()
    for name in categories:
        names = [name, *(alias for alias, canonical in CATEGORY_ALIASES.items() if name == canonical)]
        for candidate in names:
            span = tokens(candidate)
            width = len(span)
            for index in range(len(words) - width + 1):
                if words[index:index + width] == span:
                    category_words.update(range(index, index + width))
                    found_categories.add(name)
    if len(found_categories) > 1:
        raise UnsupportedQuestion("Ask about one category at a time.")
    category = next(iter(found_categories), "")

    if sku_mentions:
        # Remove the SKU and any explicitly named category before validating
        # the remaining words. A second noun phrase is ambiguous.
        digits = set(tokens(sku_mentions[0]))
        if any(word not in SCOPE_WORDS and word not in digits
               for index, word in enumerate(words) if index not in category_words):
            raise UnsupportedQuestion("Use a product name or SKU from the catalogue.")
        return category, sku_mentions[0].upper()

    # A full product name takes precedence over interpretation as a category.
    full_subject = tuple(word for word in words if word not in SCOPE_WORDS)
    exact = [sku for sku, name in products if full_subject == tokens(name)]
    if len(exact) == 1:
        return "", exact[0]

    subject = tuple(word for index, word in enumerate(words)
                    if index not in category_words and word not in SCOPE_WORDS)
    if not subject:
        return category, ""
    keyword = " ".join(subject)
    if len(keyword) > 100:
        raise UnsupportedQuestion(OUT_OF_SCOPE)
    matching = [sku for sku, name in products if name_matches(name, keyword)]
    if not matching:
        message = "Use a product name or SKU from the catalogue." if any(
            name_matches(name, word) for _, name in products for word in subject
        ) else OUT_OF_SCOPE
        raise UnsupportedQuestion(message)
    return category, matching[0] if len(matching) == 1 else keyword


def demo_plan(normalized, category, product, categories):
    """Deterministic routing for the catalogue scope resolved from the question."""
    if re.search(r"\b(out of stock|sold out)\b", normalized):
        tool = "out_of_stock"
    elif REORDER_INTENT.search(normalized):
        tool = "reorder"
    elif re.search(r"\b(inventory value|stock value|inventory valuation|stock worth|value of (?:the )?(?:inventory|stock)|money is tied up)\b", normalized):
        tool = "valuation"
    elif (product or category) and re.search(r"\b(value of|worth)\b", normalized):
        tool = "valuation"
    elif re.search(r"\b(most stock|highest stock|compare stock|rank by stock|biggest pile)\b", normalized):
        tool = "compare"
    else:
        tool = "lookup"

    if tool == "lookup":
        lookup_intent = re.search(
            r"^(?:show|list|what about|do we have|how many|stock (?:for|of|in))\b",
            normalized,
        ) or re.search(r"\b(?:units|stock|available|inventory|on hand)\b|\b(?:are there|there are) any .+ left\b", normalized)
        catalogue_request = re.fullmatch(
            r"(?:show|list) (?:me )?(?:the |all )?(?:current )?(?:inventory|products|items|stock)[?.]?",
            normalized,
        )
        if not (catalogue_request or lookup_intent and (product or category)):
            raise UnsupportedQuestion(OUT_OF_SCOPE)
    return Plan(tool, category, product, allowed_categories=categories)


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
        return Plan(**plan, allowed_categories=None)
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
    normalized = question.casefold()
    proposed = None
    if mode == "ollama":
        proposed = ollama_plan(question, model)
        if proposed is None:
            raise UnsupportedQuestion(OUT_OF_SCOPE)
    products, categories = catalog_scope()
    category, product = question_scope(normalized, products, categories)
    if mode == "demo":
        return demo_plan(normalized, category, product, categories)

    if not (category or product) and (proposed.category or proposed.product):
        if not re.search(r"\b(?:everything|entire (?:inventory|stock|warehouse)|whole warehouse|total (?:inventory|stock) value)\b",
                         normalized):
            raise UnsupportedQuestion("Specify a product, category, or the entire inventory.")
    tool = "reorder" if REORDER_INTENT.search(normalized) else proposed.tool
    return Plan(tool, category, product, allowed_categories=categories)


def query_inventory(question, mode="demo"):
    model = os.environ.get("STOCKROOM_MODEL", DEFAULT_MODEL)
    plan = plan_question(question, mode, model)
    rows = run_tool(plan)
    scope_exists = (
        not rows and plan.tool in {"reorder", "out_of_stock"}
        and bool(run_tool(Plan("lookup", category=plan.category, product=plan.product, allowed_categories=None)))
    )
    return {
        "answer": answer_from_rows(plan, rows, scope_exists=scope_exists),
        "plan": plan.to_dict(),
        "rows": rows,
        "mode": mode,
        "model": model if mode == "ollama" else None,
    }
