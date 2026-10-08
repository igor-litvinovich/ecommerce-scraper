"""Crawl orchestration: discover product pages, fetch them, and build the report."""

import asyncio
import logging
import math
import re
import sys
from collections.abc import Callable, Coroutine, Iterable, Sequence
from typing import Any
from urllib.parse import urlsplit

from ecommerce_scraper.catalogue import PricingError, build_report
from ecommerce_scraper.fetcher import Fetcher, FetchError
from ecommerce_scraper.models import ListingPage, ProductPage, Report
from ecommerce_scraper.parsing import ParseError, parse_listing, parse_product

logger = logging.getLogger(__name__)

_TRAILING_NUMBER = re.compile(r"(\d+)/?$")
_PROGRESS_REPORTS = 10  # log product-fetch progress roughly every 10%


class OutOfScopeError(FetchError):
    """A page redirected somewhere the crawl must not follow."""


class ScrapeError(Exception):
    """The scrape could not produce a complete, trustworthy report."""

    def __init__(self, message: str, failures: Sequence[Exception] = ()) -> None:
        super().__init__(message)
        self.failures = list(failures)


async def scrape(fetcher: Fetcher, start_url: str, *, max_pages: int) -> Report:
    """Crawl the store rooted at ``start_url`` and return the full report.

    Any page that cannot be fetched or understood aborts the run: a report that
    omits products would carry a wrong ``total``.
    """
    product_urls = await discover_product_urls(fetcher, start_url, max_pages=max_pages)
    if not product_urls:
        raise ScrapeError(f"no products found under {start_url}; has the site's markup changed?")

    progress = _Progress(total=len(product_urls), noun="product pages")
    products = await _run_all(_fetch_product(fetcher, url, progress) for url in product_urls)
    try:
        return build_report(products)
    except PricingError as exc:
        raise ScrapeError(str(exc), [exc]) from exc


async def discover_product_urls(fetcher: Fetcher, start_url: str, *, max_pages: int) -> list[str]:
    """Breadth-first crawl of navigation pages (home, categories, pagination).

    The home and category pages only show a handful of "featured" products, and
    pagination links are truncated, so we follow every in-scope navigation link
    until no new pages appear. Products are de-duplicated by URL and returned in
    numeric id order so output is stable between runs.
    """
    # The very first request may be redirected (http -> https, a www. prefix), so the
    # crawl scope is fixed from where the start page actually lives.
    ((start_url, first_listing),) = await _run_all([_fetch_start(fetcher, start_url)])
    in_scope = _scope_checker(start_url)
    same_origin = _origin_checker(start_url)
    visited = {start_url}
    products: dict[str, None] = {}  # insertion-ordered set
    listings = [first_listing]
    pages_fetched = 1

    while True:
        next_frontier: dict[str, None] = {}
        for listing in listings:
            # Product pages live outside the category paths (/static/product/N), so
            # products are only restricted to the same site; navigation stays in scope.
            products.update(dict.fromkeys(filter(same_origin, listing.product_urls)))
            next_frontier.update(
                dict.fromkeys(
                    link
                    for link in listing.navigation_urls
                    if in_scope(link) and link not in visited
                )
            )
        logger.info(
            "Crawled %d listing page(s) so far; %d products found", pages_fetched, len(products)
        )
        frontier = list(next_frontier)
        if not frontier:
            break
        # Bound the number of requests made, not just distinct URLs, so that even a
        # revisiting bug ends in a clear error rather than an endless crawl.
        pages_fetched += len(frontier)
        if pages_fetched > max_pages:
            raise ScrapeError(
                f"crawl would exceed max_pages={max_pages}; is pagination looping, "
                "or does the limit need raising?"
            )
        visited.update(frontier)
        listings = await _run_all(_fetch_listing(fetcher, url, in_scope) for url in frontier)

    logger.info("Discovered %d products across %d listing pages", len(products), pages_fetched)
    return sorted(products, key=_product_id_order)


async def _fetch_start(fetcher: Fetcher, url: str) -> tuple[str, ListingPage]:
    page = await fetcher.fetch(url)
    return page.url, parse_listing(page.text, page.url)


async def _fetch_listing(
    fetcher: Fetcher, url: str, in_scope: Callable[[str], bool]
) -> ListingPage:
    page = await fetcher.fetch(url)
    if not in_scope(page.url):
        raise OutOfScopeError(url, f"redirected outside the crawl scope (to {page.url})")
    return parse_listing(page.text, page.url)


async def _fetch_product(fetcher: Fetcher, url: str, progress: "_Progress") -> ProductPage:
    page = await fetcher.fetch(url)
    if urlsplit(page.url)[:2] != urlsplit(url)[:2]:  # scheme and host
        raise OutOfScopeError(url, f"redirected to another site (to {page.url})")
    product = parse_product(page.text, url)
    progress.advance()
    return product


class _Progress:
    """Logs a status line roughly every 10% of a batch, keeping the log short."""

    def __init__(self, *, total: int, noun: str) -> None:
        self._total = total
        self._noun = noun
        self._done = 0
        self._every = max(1, math.ceil(total / _PROGRESS_REPORTS))

    def advance(self) -> None:
        self._done += 1
        if self._done % self._every == 0 or self._done == self._total:
            percent = 100 * self._done // self._total
            logger.info("Fetched %d/%d %s (%d%%)", self._done, self._total, self._noun, percent)


async def _run_all[T](coroutines: Iterable[Coroutine[Any, Any, T]]) -> list[T]:
    """Run concurrently, preserving order.

    Fails fast: the first fetch/parse failure cancels the remaining work, so a
    struggling site is not hit with hundreds more doomed (and retried) requests.
    """
    try:
        async with asyncio.TaskGroup() as group:
            tasks = [group.create_task(coroutine) for coroutine in coroutines]
    except ExceptionGroup as errors:
        expected, unexpected = errors.split((FetchError, ParseError))
        if unexpected is not None or expected is None:
            raise  # a bug, not a scraping failure: keep the full traceback
        failures = [error for error in expected.exceptions if isinstance(error, Exception)]
        summary = "; ".join(str(failure) for failure in failures)
        raise ScrapeError(f"{len(failures)} page(s) failed: {summary}", failures) from errors
    return [task.result() for task in tasks]


def _origin_checker(start_url: str) -> Callable[[str], bool]:
    start = urlsplit(start_url)
    return lambda url: urlsplit(url)[:2] == start[:2]  # same scheme and host


def _scope_checker(start_url: str) -> Callable[[str], bool]:
    """Only crawl navigation pages on the same host and under the start URL's path."""
    same_origin = _origin_checker(start_url)
    prefix = urlsplit(start_url).path.rstrip("/")

    def in_scope(url: str) -> bool:
        path = urlsplit(url).path.rstrip("/")
        return same_origin(url) and (path == prefix or path.startswith(f"{prefix}/"))

    return in_scope


def _product_id_order(url: str) -> tuple[int, str]:
    match = _TRAILING_NUMBER.search(urlsplit(url).path)
    return (int(match[1]) if match else sys.maxsize, url)
