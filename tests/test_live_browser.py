"""Checks the copied JavaScript pricing rule against what a real browser displays.

Static scraping cannot see prices the site computes client-side, so the HDD
surcharges were copied from the site's JavaScript. These tests open every product
with HDD options in headless Chromium, click each option as a shopper would, and
compare the price the page then shows with the price this scraper reports.

Deselected by default with the other live tests. They need the optional browser
tooling: ``uv sync --group browser && uv run playwright install chromium``.
"""

import asyncio
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest

from ecommerce_scraper.catalogue import expand_product
from ecommerce_scraper.cli import DEFAULT_START_URL
from ecommerce_scraper.crawler import discover_product_urls
from ecommerce_scraper.fetcher import Fetcher, RetryPolicy
from ecommerce_scraper.parsing import parse_product

pytestmark = pytest.mark.live

CENTS = Decimal("0.01")
SITE_HOST = urlsplit(DEFAULT_START_URL).hostname


@pytest.fixture(scope="module")
def product_urls() -> list[str]:
    async def discover() -> list[str]:
        async with httpx.AsyncClient(timeout=30) as client:
            fetcher = Fetcher(
                client, max_concurrency=5, retry=RetryPolicy(), requests_per_second=10
            )
            return await discover_product_urls(fetcher, DEFAULT_START_URL, max_pages=200)

    # Playwright's sync API keeps an event loop running on this thread, so run the
    # crawler's own loop on a worker thread instead.
    with ThreadPoolExecutor(max_workers=1) as worker:
        return worker.submit(asyncio.run, discover()).result()


@pytest.fixture(scope="module")
def page() -> Iterator[Any]:
    playwright_api = pytest.importorskip("playwright.sync_api")
    with playwright_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page()
            # The pricing script is served by the site itself; trackers, chat widgets,
            # images and fonts are irrelevant here and only slow every page load down.
            page.route("**/*", _skip_irrelevant_requests)
            yield page
        finally:
            browser.close()


def _skip_irrelevant_requests(route: Any) -> None:
    request = route.request
    third_party = urlsplit(request.url).hostname != SITE_HOST
    if third_party or request.resource_type in {"image", "font", "media"}:
        route.abort()
    else:
        route.continue_()


def displayed_prices(page: Any) -> list[str]:
    """Click every enabled HDD option and return the price text shown after each click."""
    shown = []
    for swatch in page.locator("button.swatch").all():
        if swatch.is_enabled():
            swatch.click()
            shown.append(page.locator(".caption .price").inner_text())
    return shown


def test_every_reported_hdd_price_matches_the_browser(page: Any, product_urls: list[str]) -> None:
    mismatches, checked = [], 0
    for url in product_urls:
        page.goto(url)
        # Our price is computed from the very HTML the browser loaded.
        product = parse_product(page.content(), url)
        if not product.hdd_options:
            continue
        reported = [item.price for item in expand_product(product)]
        # The site's JS adds the surcharge in binary floating point, so it sometimes
        # displays artefacts such as $517.1700000000001; compare in cents.
        shown = [Decimal(text.lstrip("$")).quantize(CENTS) for text in displayed_prices(page)]
        if shown != reported:
            mismatches.append((url, shown, reported))
        checked += 1

    assert checked >= 100, "expected well over 100 products with HDD options"
    assert not mismatches


def test_site_displays_float_artefacts_that_we_report_as_exact_cents(page: Any) -> None:
    url = f"{DEFAULT_START_URL}/product/92"
    page.goto(url)
    reported = [item.price for item in expand_product(parse_product(page.content(), url))]

    shown = displayed_prices(page)

    assert shown == ["$497.17", "$517.1700000000001", "$537.1700000000001"]
    assert reported == [Decimal("497.17"), Decimal("517.17"), Decimal("537.17")]


def test_the_1024_gb_option_cannot_be_selected(page: Any) -> None:
    page.goto(f"{DEFAULT_START_URL}/product/140")

    assert not page.locator('button.swatch[value="1024"]').is_enabled()
