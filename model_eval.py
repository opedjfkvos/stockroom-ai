"""Score Stockroom routing against a fixed set of questions and filters."""

import argparse
import sys

from assistant import DEFAULT_MODEL, ModelUnavailable, UnsupportedQuestion, ollama_plan, plan_question
from inventory import ITEMS, Plan

CASES = (
    ("What needs reordering?", Plan("reorder")),
    ("Which Audio products are below minimum?", Plan("reorder", "Audio")),
    ("Any low stock in Networking?", Plan("reorder", "Networking")),
    ("Which products are out of stock?", Plan("out_of_stock")),
    ("Which Displays are out of stock?", Plan("out_of_stock", "Displays")),
    ("Which accessories have the most stock?", Plan("compare", "Accessories")),
    ("Which Storage product has the highest stock?", Plan("compare", "Storage")),
    ("What is our current inventory value?", Plan("valuation")),
    ("What is the inventory value for Audio?", Plan("valuation", "Audio")),
    ("What is the stock value for STO-601?", Plan("valuation", product="STO-601")),
    ("Do we have any webcams?", Plan("lookup", product="PER-503")),
    ("What about NET-402?", Plan("lookup", product="NET-402")),
    ("How many units of the USB Headset are in stock?", Plan("lookup", product="AUD-201")),
    ("List the products in Peripherals.", Plan("lookup", "Peripherals")),
    ("Tell me a joke.", None),
    ("What is the value of pi?", None),
    ("Delete PER-503 from inventory.", None),
    ("How much stock did we have yesterday?", None),
)


def product_scope(product):
    keyword = product.casefold()
    if not keyword:
        return None
    return {sku for sku, name, *_ in ITEMS if keyword == sku.casefold() or keyword in name.casefold()}


def matches(actual, expected):
    if actual is None or expected is None:
        return actual is expected
    return (
        actual.tool == expected.tool
        and actual.category == expected.category
        and product_scope(actual.product) == product_scope(expected.product)
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--demo", action="store_true", help="Evaluate the deterministic router.")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    args = parser.parse_args()
    correct = 0
    for question, expected in CASES:
        try:
            actual = plan_question(question, "demo", args.model) if args.demo else ollama_plan(question, args.model)
        except UnsupportedQuestion:
            actual = None
        except ModelUnavailable as exc:
            print(f"Evaluation stopped at {question!r}\n{exc}", file=sys.stderr)
            return 2
        ok = matches(actual, expected)
        correct += ok
        print(f"{'PASS' if ok else 'MISS'}  {question}")
        if not ok:
            print(f"      expected {expected!r}\n      received {actual!r}")
    print(f"\n{'Demo' if args.demo else args.model}: {correct}/{len(CASES)}")
    return int(correct != len(CASES))


if __name__ == "__main__":
    sys.exit(main())
