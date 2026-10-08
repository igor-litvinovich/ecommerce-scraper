"""End-to-end smoke tests against the real site.

Deselected by default (they depend on a third-party website); run with
``uv run pytest -m live``. CI runs them daily as a canary. They assert
invariants rather than exact figures, so they keep passing if the catalogue's
contents change, but fail if the site's structure or pricing logic changes
underneath us.
"""

import json
import re
from decimal import Decimal
from urllib.parse import urljoin

import httpx
import pytest

from ecommerce_scraper.catalogue import HDD_SURCHARGE_USD
from ecommerce_scraper.cli import DEFAULT_START_URL, main

pytestmark = pytest.mark.live


def test_scrapes_the_real_site(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main(["--compact"])

    out, err = capsys.readouterr()
    assert exit_code == 0, err
    report = json.loads(out, parse_float=Decimal)
    results = report["results"]

    # 147 catalogue products at the time of writing, most with three HDD options.
    assert len(results) > 300
    assert report["total"] == sum(item["price"] for item in results)
    assert all(item["name"] and item["price"] > 0 for item in results)
    assert all(len(item.get("colors", ["a", "b"])) >= 2 for item in results)
    assert not any(item["name"].endswith(" 1024 GB") for item in results)  # disabled option


def test_hdd_surcharges_still_match_the_sites_javascript() -> None:
    """The HDD price rule is copied from the site's JS; detect if the original changes."""
    product_url = f"{DEFAULT_START_URL}/product/140"
    with httpx.Client(timeout=30, follow_redirects=True) as client:
        page = client.get(product_url).raise_for_status().text
        script = re.search(r'<script[^>]+src="(?P<src>[^"]*/app\.js[^"]*)"', page)
        assert script, "app.js is no longer referenced from product pages"
        javascript = client.get(urljoin(product_url, script["src"])).raise_for_status().text

    update_price = re.search(
        r"EcommerceProduct=.*?updatePrice:function\(\w+\)\{(?P<body>.*?)\}\s*,\s*updateTitle",
        javascript,
        re.DOTALL,
    )
    assert update_price, "EcommerceProduct.updatePrice not found: re-verify HDD_SURCHARGE_USD"
    site_rule = {
        int(capacity): Decimal(surcharge)
        for capacity, surcharge in re.findall(r'case"(\d+)":\w+=(\d+)', update_price["body"])
    }

    # The JS lists only non-zero surcharges; anything else (128 GB) adds nothing.
    assert site_rule == {gb: usd for gb, usd in HDD_SURCHARGE_USD.items() if usd}
