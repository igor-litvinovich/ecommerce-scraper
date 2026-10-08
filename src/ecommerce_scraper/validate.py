"""Independent checker for a report's output contract.

It shares no code with the scraper, so it can vet any report (a fresh one, an old
one, a teammate's) before it is published. The contract is the brief's format plus
the interpretations documented in the README: colours are lowercase and listed only
when there are at least two, prices are positive amounts in cents, and no entry
appears twice (distinct products always differ in some field, so an identical pair
means the same product was emitted twice and the total double-counts it)::

    ecommerce-scraper-validate report.json        # exit 0 valid, 1 invalid, 2 unreadable
    ecommerce-scraper | ecommerce-scraper-validate -
"""

import argparse
import json
import sys
from collections.abc import Sequence
from decimal import Decimal
from typing import Any

_TOP_LEVEL_FIELDS = ("results", "total")
_REQUIRED_ITEM_FIELDS = ("name", "description", "price")
_ITEM_FIELDS = frozenset({*_REQUIRED_ITEM_FIELDS, "colors"})
_CENTS = Decimal("0.01")
_MIN_COLORS = 2


def validate_report(document: Any) -> list[str]:
    """Return every contract violation in a parsed report (empty when it is valid)."""
    if not isinstance(document, dict):
        return ["report must be a JSON object"]

    problems = [f"missing '{field}'" for field in _TOP_LEVEL_FIELDS if field not in document]
    problems += [
        f"unexpected field '{field}'" for field in document if field not in _TOP_LEVEL_FIELDS
    ]

    results = document.get("results")
    if "results" in document and not isinstance(results, list):
        problems.append("'results' must be an array")
        results = None
    elif results == []:
        problems.append("'results' is empty: a scrape that finds no products is a failure")
    total = _number(document.get("total"))
    if "total" in document and total is None:
        problems.append("'total' must be a number")

    prices: list[Decimal] = []
    first_seen: dict[str, int] = {}
    for index, item in enumerate(results or []):
        where = f"results[{index}]"
        if not isinstance(item, dict):
            problems.append(f"{where} must be an object")
            continue
        item_problems = _item_problems(item, where)
        problems += item_problems
        if (price := _number(item.get("price"))) is not None and not item_problems:
            prices.append(price)
        fingerprint = json.dumps(item, sort_keys=True, default=str)
        if fingerprint in first_seen:
            problems.append(f"{where} duplicates results[{first_seen[fingerprint]}]")
        else:
            first_seen[fingerprint] = index

    # The total is only checkable once every price is itself valid.
    if total is not None and results is not None and len(prices) == len(results):
        expected = sum(prices, Decimal(0))
        if total != expected:
            problems.append(f"'total' is {total} but the prices sum to {expected}")
    return problems


def _item_problems(item: dict[str, Any], where: str) -> list[str]:
    problems = [f"{where} is missing '{f}'" for f in _REQUIRED_ITEM_FIELDS if f not in item]
    problems += [f"{where} has unexpected field '{f}'" for f in item if f not in _ITEM_FIELDS]
    if "name" in item and not (isinstance(item["name"], str) and item["name"].strip()):
        problems.append(f"{where}.name must be a non-empty string")
    if "description" in item and not isinstance(item["description"], str):
        problems.append(f"{where}.description must be a string")
    if "price" in item:
        price = _number(item["price"])
        if price is None:
            problems.append(f"{where}.price must be a number")
        elif price <= 0:
            problems.append(f"{where}.price must be positive")
        elif price != price.quantize(_CENTS):
            problems.append(f"{where}.price has more than 2 decimals")
    if "colors" in item:
        problems += _color_problems(item["colors"], f"{where}.colors")
    return problems


def _color_problems(colors: Any, where: str) -> list[str]:
    if not isinstance(colors, list) or not all(isinstance(color, str) for color in colors):
        return [f"{where} must be an array of strings"]
    problems = []
    if len(colors) < _MIN_COLORS:
        problems.append(f"{where} needs at least {_MIN_COLORS} options (omit it otherwise)")
    if any(not color.strip() or color != color.strip().lower() for color in colors):
        problems.append(f"{where} must be lowercase, non-empty strings")
    if len(set(colors)) != len(colors):
        problems.append(f"{where} has duplicates")
    return problems


def _number(value: Any) -> Decimal | None:
    """The value as an exact Decimal, or None if it is not a finite JSON number."""
    if isinstance(value, bool) or not isinstance(value, int | float | Decimal):
        return None
    number = Decimal(repr(value)) if isinstance(value, float) else Decimal(value)
    return number if number.is_finite() else None


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ecommerce-scraper-validate",
        description="Check a scraper report against the output contract.",
        epilog="exit status: 0 valid, 1 invalid, 2 unreadable",
    )
    parser.add_argument("report", help="path to the JSON report ('-' reads stdin)")
    args = parser.parse_args(argv)

    try:
        if args.report == "-":
            text = sys.stdin.read()
        else:
            with open(args.report, encoding="utf-8") as stream:
                text = stream.read()
    except (OSError, UnicodeDecodeError) as exc:  # missing, unreadable, or not UTF-8
        print(f"cannot read {args.report}: {exc}", file=sys.stderr)
        return 2

    try:
        document = json.loads(text, parse_float=Decimal)  # exact, for the total check
    except json.JSONDecodeError as exc:
        print(f"{args.report}: not valid JSON ({exc})", file=sys.stderr)
        return 1

    problems = validate_report(document)
    for problem in problems:
        print(f"{args.report}: {problem}", file=sys.stderr)
    if problems:
        return 1
    print(f"OK: {len(document['results'])} results, total {document['total']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
