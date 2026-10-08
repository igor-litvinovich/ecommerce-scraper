from decimal import Decimal

from ecommerce_scraper.models import Report, ResultItem


def test_report_total_is_always_the_sum_of_its_results() -> None:
    report = Report(
        results=(
            ResultItem(name="A", description="a", price=Decimal("1.10")),
            ResultItem(name="B", description="b", price=Decimal("2.20")),
        )
    )

    assert report.total == Decimal("3.30")


def test_empty_report_totals_zero() -> None:
    assert Report(results=()).total == Decimal(0)
