"""Offline end-to-end test against a recorded copy of the whole site.

``fixtures/site_snapshot.json.xz`` holds every page the scraper requested on
2026-10-07 (179 pages, ~30 KB compressed; re-record deliberately with
``scripts/record_site_snapshot.py``). The expected figures below were established
before this code base existed, by a throwaway profiling crawl of the live site, and agree
with the examples given in the brief.
"""

import json
import logging
import lzma
from decimal import Decimal
from typing import Any

import pytest

from ecommerce_scraper.cli import main
from ecommerce_scraper.validate import validate_report
from tests.support import FIXTURES, FakeSite, Page


@pytest.fixture(scope="module")
def site() -> FakeSite:
    snapshot = lzma.decompress((FIXTURES / "site_snapshot.json.xz").read_bytes())
    pages: dict[str, Page] = json.loads(snapshot)
    return FakeSite(pages)


@pytest.fixture(scope="module")
def report(site: FakeSite, tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    destination = tmp_path_factory.mktemp("snapshot") / "report.json"

    exit_code = main(
        ["--output", str(destination), "--rate-limit", "0"], transport=site.transport()
    )

    root.handlers[:], root.level = handlers, level  # main() configures logging, as a CLI should
    assert exit_code == 0
    report: dict[str, Any] = json.loads(destination.read_text(), parse_float=Decimal)
    return report


def test_produces_the_independently_verified_result(report: dict[str, Any]) -> None:
    assert len(report["results"]) == 423  # 138 products x 3 HDD options + 9 phones
    assert report["total"] == Decimal("345701.52")
    assert sum(1 for item in report["results"] if "colors" in item) == 72  # 21 tablets x 3 + 9


def test_report_satisfies_the_independent_contract_validator(report: dict[str, Any]) -> None:
    assert validate_report(report) == []


def test_matches_the_examples_given_in_the_brief(report: dict[str, Any]) -> None:
    results = report["results"]
    assert {
        "name": "Dell Latitude 5580 128 GB",
        "description": 'Dell Latitude 5580, 15.6" FHD, Core i5-7300U, 16GB, 256GB SSD, '
        "Linux + Windows 10 Home",
        "price": Decimal("1178.19"),
    } in results
    assert {
        "name": "Samsung Galaxy",
        "description": "5 mpx. Android 5.0",
        "price": Decimal("93.99"),
        "colors": ["gold", "white", "black"],
    } in results


def test_fetches_every_page_exactly_once(report: dict[str, Any], site: FakeSite) -> None:
    assert set(site.hits) == set(site.pages)
    assert set(site.hits.values()) == {1}
