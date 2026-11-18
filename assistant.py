"""Translate questions into bounded inventory tool calls."""

import json
import os
import re
from urllib.error import URLError
from urllib.request import Request, urlopen

from inventory import CATEGORIES, ITEMS, TOOLS, Plan, answer_from_rows, run_tool

DEFAULT_MODEL = "qwen2.5:1.5b"
OUT_OF_SCOPE = (
    "I can check current stock, reorder needs, out-of-stock products, highest stock, "
    "or inventory value. Try asking about one of these."
)
UNSUPPORTED_REQUEST = re.compile(
    r"\b(forecast|predict|weather|translate|sales|revenue|historical|yesterday|tomorrow|"
    r"last (?:week|month|year)|next (?:week|month|year)|delete|update|remove|set|change)\b"
    r"|\b(?:add|order|buy|purchase|reorder)\s+\d+\b"
)
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
PROMPT = (
    "Plan exactly one read-only inventory tool call using the supplied JSON schema. "
    "Tools: lookup for item or category stock, reorder for stock below minimum, "
    "out_of_stock for zero units, compare for highest stock, valuation for total value. Use an empty string for "
    "unspecified filters. Product is a product name keyword or SKU, only if asked. "
    "Use unsupported with empty filters for unrelated questions, write requests, historical "
    "or future data, or any calculation the listed tools cannot answer. "
    "Respond with the plan only, never with prose, SQL, or invented quantities."
)


class ModelUnavailable(Exception):
    """The local model could not respond with a usable plan."""


class UnsupportedQuestion(ValueError):
    """The question falls outside the supported inventory queries."""


def demo_plan(question):
    """Deterministic keyword routing so reviewers can try the app without a model."""
    normalized = question.casefold()
    if "out of stock" in normalized:
        tool = "out_of_stock"
    elif re.search(r"\b(reorder(?:ing)?|replenish(?:ment)?|low stock|below (?:the )?(?:minimum|reorder point))\b", normalized):
        tool = "reorder"
    elif re.search(r"\b(inventory value|stock value|inventory valuation|stock worth|value of (?:the )?(?:inventory|stock))\b", normalized):
        tool = "valuation"
    elif re.search(r"\b(most stock|highest stock|compare stock|rank by stock)\b", normalized):
        tool = "compare"
    else:
        tool = "lookup"

    category = next((name for name in CATEGORIES if name.casefold() in normalized), "")
    words = re.sub(r"[^a-z0-9-]", " ", normalized).split()
    matches = [row for row in ITEMS if row[0].casefold() in words or row[1].casefold() in normalized]
    if not matches:
        aliases = ("webcam", "keyboard", "mouse", "headset", "router", "ssd", "hub", "microphone", "monitor")
        alias = next((token for token in aliases if token in words or token + "s" in words), "")
        matches = [row for row in ITEMS if alias and alias in row[1].casefold()]
        if len(matches) > 1:
            return Plan(tool, category, alias)
    product = matches[0][0] if len(matches) == 1 else ""
    if tool == "lookup":
        lookup_intent = re.search(
            r"^(?:show|list|what about|do we have|how many|stock (?:for|of|in))\b",
            normalized,
        ) or re.search(r"\b(?:units|in stock|inventory)\b", normalized)
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
        if plan == {"tool": "unsupported", "category": "", "product": ""}:
            return None
        return Plan(**plan)
    except (URLError, TimeoutError, ValueError, TypeError, KeyError) as exc:
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
    plan = demo_plan(question) if mode == "demo" else ollama_plan(question, model)
    if plan is None:
        raise UnsupportedQuestion(OUT_OF_SCOPE)
    return plan


def query_inventory(question, mode="demo"):
    model = os.environ.get("STOCKROOM_MODEL", DEFAULT_MODEL)
    plan = plan_question(question, mode, model)
    rows = run_tool(plan)
    return {
        "answer": answer_from_rows(plan, rows),
        "plan": plan.to_dict(),
        "rows": rows,
        "mode": mode,
        "model": model if mode == "ollama" else None,
    }
