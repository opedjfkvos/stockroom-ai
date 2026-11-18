"""Score Stockroom routing against a fixed set of questions and filters."""

import argparse
import sys

from assistant import DEFAULT_MODEL, ModelUnavailable, UnsupportedQuestion, ollama_plan, plan_question
from inventory import ITEMS, Plan, name_matches

SMOKE_CASES = (
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


HOLDOUT_CASES = (
    ("What should we buy more of?", Plan("reorder")),
    ("Which items are running low in Audio?", Plan("reorder", "Audio")),
    ("Are any monitors completely sold out?", Plan("out_of_stock", product="monitor")),
    ("Between the accessories, what do we have the biggest pile of?", Plan("compare", "Accessories")),
    ("How much money is tied up in everything on the shelves?", Plan("valuation")),
    ("Could you check whether there are any webcams left?", Plan("lookup", product="PER-503")),
    ("How many units of the Ethernet Adapter are on hand?", Plan("lookup", product="NET-402")),
    ("Tell me what to charge for shipping a webcam.", None),
    ("What's the most expensive product we sell?", None),
    ("Will we run out of mice by Friday?", None),
    ("How many keyboards did we have last month?", None),
    ("Set the available USB Headsets to zero.", None),
)


def product_scope(product):
    keyword = product.casefold()
    if not keyword:
        return None
    return {sku for sku, name, *_ in ITEMS if keyword == sku.casefold() or name_matches(name, keyword)}


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
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--demo", action="store_true", help="Evaluate the deterministic router.")
    modes.add_argument("--app", action="store_true", help="Evaluate Ollama with the app's scope checks.")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--holdout", action="store_true", help="Use the separate paraphrase and scope suite.")
    parser.add_argument("--strict", action="store_true", help="Fail for any mismatch, including on the diagnostic set.")
    args = parser.parse_args()
    cases = HOLDOUT_CASES if args.holdout else SMOKE_CASES
    correct = 0
    supported = tool_hits = filter_hits = refused_supported = unsupported = refusal_hits = 0
    for question, expected in cases:
        try:
            if args.demo or args.app:
                actual = plan_question(question, "demo" if args.demo else "ollama", args.model)
            else:
                actual = ollama_plan(question, args.model)
        except UnsupportedQuestion:
            actual = None
        except ModelUnavailable as exc:
            print(f"Evaluation stopped at {question!r}\n{exc}", file=sys.stderr)
            return 2
        if expected is None:
            unsupported += 1
            refusal_hits += actual is None
        else:
            supported += 1
            refused_supported += actual is None
            tool_hits += actual is not None and actual.tool == expected.tool
            filter_hits += (
                actual is not None and actual.category == expected.category
                and product_scope(actual.product) == product_scope(expected.product)
            )
        ok = matches(actual, expected)
        correct += ok
        print(f"{'PASS' if ok else 'MISS'}  {question}")
        if not ok:
            print(f"      expected {expected!r}\n      received {actual!r}")
    label = "Demo" if args.demo else f"{args.model} with app scope checks" if args.app else args.model
    print(f"\n{label}: {correct}/{len(cases)}")
    print(f"Tool accuracy on supported questions: {tool_hits}/{supported}")
    print(f"Filter accuracy on supported questions: {filter_hits}/{supported}")
    print(f"Correct refusals: {refusal_hits}/{unsupported}")
    print(f"Incorrect refusals on supported questions: {refused_supported}/{supported}")
    return int((args.strict or not args.holdout) and correct != len(cases))


if __name__ == "__main__":
    sys.exit(main())
