import json
from decimal import Decimal

from ecommerce_scraper.models import Report, ResultItem
from ecommerce_scraper.output import render_json

REPORT = Report(
    results=(
        ResultItem(
            name="Dell Latitude 5580 128 GB", description='15.6" FHD', price=Decimal("1178.19")
        ),
        ResultItem(
            name="Samsung Galaxy",
            description="5 mpx. Android 5.0",
            price=Decimal("93.99"),
            colors=("gold", "white", "black"),
        ),
    ),
    total=Decimal("1272.18"),
)


def test_matches_the_specified_output_shape() -> None:
    assert json.loads(render_json(REPORT)) == {
        "results": [
            {
                "name": "Dell Latitude 5580 128 GB",
                "description": '15.6" FHD',
                "price": 1178.19,
            },
            {
                "name": "Samsung Galaxy",
                "description": "5 mpx. Android 5.0",
                "price": 93.99,
                "colors": ["gold", "white", "black"],
            },
        ],
        "total": 1272.18,
    }


def test_prices_are_json_numbers_written_exactly() -> None:
    text = render_json(REPORT, indent=None)

    assert '"price": 1178.19' in text
    assert '"total": 1272.18' in text


def test_total_does_not_leak_binary_float_error() -> None:
    report = Report(results=(), total=Decimal("345701.52"))

    assert '"total": 345701.52' in render_json(report)


def test_compact_rendering_is_a_single_line() -> None:
    assert render_json(REPORT, indent=None).count("\n") == 1  # just the trailing newline


def test_pretty_rendering_is_indented() -> None:
    assert '\n  "results": [' in render_json(REPORT, indent=2)


def test_every_amount_is_a_json_float_even_whole_dollars() -> None:
    # One consistent type: a consumer checking isinstance(price, float) must not
    # trip over the 75 products whose price happens to be a whole dollar amount.
    report = Report(
        results=(ResultItem(name="MSI", description="d", price=Decimal("1149")),),
        total=Decimal("1149.00"),
    )

    document = json.loads(render_json(report))

    assert document["results"][0]["price"] == 1149.0
    assert isinstance(document["results"][0]["price"], float)
    assert isinstance(document["total"], float)
