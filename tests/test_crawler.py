import logging
from decimal import Decimal

import httpx
import pytest

from ecommerce_scraper.crawler import ScrapeError, discover_product_urls, scrape
from ecommerce_scraper.fetcher import Fetcher, RetryPolicy
from tests.support import FakeSite, Page, Redirect, listing_html, product_html

ROOT = "https://shop.test/store"


def mini_store() -> dict[str, Page]:
    """A small store with the same shape (and the same traps) as the real site.

    * the home and category pages only show a few "featured" products that are
      also listed elsewhere;
    * pagination is truncated, so page 3 is only linked from page 2;
    * there are links to other sites and to other sections of the same host;
    * product pages live outside the category paths, as on the real site
      (``/static/product/N``, not ``/static/phones/product/N``).
    """
    return {
        ROOT: listing_html(
            products=["/store/product/3"],
            nav=["/store/laptops", "/store/phones", "https://other.test/store/x"],
        ),
        f"{ROOT}/laptops": listing_html(
            products=["/store/product/1", "/store/product/2"],
            nav=["/store/laptops?page=2", "/store/phones"],
        ),
        f"{ROOT}/laptops?page=2": listing_html(
            products=["/store/product/10"], nav=["/store/laptops?page=3", "/store/laptops"]
        ),
        f"{ROOT}/laptops?page=3": listing_html(
            products=["/store/product/3"], nav=["/store/laptops"]
        ),
        f"{ROOT}/phones": listing_html(
            products=["/store/product/4", "https://other.test/store/product/99"],
            nav=[
                "/store/laptops",
                "/elsewhere/listing",  # same host, outside the start URL's path
                "/storefront",  # shares a string prefix, but not a path segment
            ],
        ),
        f"{ROOT}/product/1": product_html("Laptop One", "$100.10"),
        f"{ROOT}/product/2": product_html("Laptop Two", "$200.20"),
        f"{ROOT}/product/3": product_html("Laptop Three", "$300.30"),
        f"{ROOT}/product/4": product_html("Phone Four", "$4"),
        f"{ROOT}/product/10": product_html("Laptop Ten", "$10"),
    }


def fetcher_for(site: FakeSite) -> Fetcher:
    client = httpx.AsyncClient(transport=site.transport())
    return Fetcher(client, max_concurrency=4, retry=RetryPolicy(attempts=1))


async def test_discovers_every_in_scope_product_once_in_numeric_id_order() -> None:
    site = FakeSite(mini_store())

    urls = await discover_product_urls(fetcher_for(site), ROOT, max_pages=50)

    assert urls == [
        f"{ROOT}/product/1",
        f"{ROOT}/product/2",
        f"{ROOT}/product/3",
        f"{ROOT}/product/4",
        f"{ROOT}/product/10",  # numeric, not lexicographic, ordering
    ]


async def test_fetches_each_listing_page_exactly_once_and_never_leaves_scope() -> None:
    site = FakeSite(mini_store())

    await discover_product_urls(fetcher_for(site), ROOT, max_pages=50)

    assert dict(site.hits) == {
        ROOT: 1,
        f"{ROOT}/laptops": 1,
        f"{ROOT}/laptops?page=2": 1,
        f"{ROOT}/laptops?page=3": 1,
        f"{ROOT}/phones": 1,
    }


async def test_starting_from_a_subcategory_only_crawls_that_subcategory() -> None:
    site = FakeSite(mini_store())

    urls = await discover_product_urls(fetcher_for(site), f"{ROOT}/laptops", max_pages=50)

    # Product pages sit outside /store/laptops but are still followed; the phones
    # category is not crawled.
    assert urls == [f"{ROOT}/product/{n}" for n in (1, 2, 3, 10)]
    assert f"{ROOT}/phones" not in site.hits


async def test_scrape_produces_report_from_product_pages() -> None:
    site = FakeSite(mini_store())

    report = await scrape(fetcher_for(site), ROOT, max_pages=50)

    assert [(r.name, r.price) for r in report.results] == [
        ("Laptop One", Decimal("100.10")),
        ("Laptop Two", Decimal("200.20")),
        ("Laptop Three", Decimal("300.30")),
        ("Phone Four", Decimal("4")),
        ("Laptop Ten", Decimal("10")),
    ]
    assert report.total == Decimal("614.60")
    assert site.hits[f"{ROOT}/product/3"] == 1  # listed twice, fetched once


async def test_refuses_to_crawl_more_than_max_pages() -> None:
    site = FakeSite(mini_store())

    with pytest.raises(ScrapeError, match="max_pages"):
        await discover_product_urls(fetcher_for(site), ROOT, max_pages=3)

    assert sum(site.hits.values()) <= 3


async def test_endless_pagination_is_stopped_by_max_pages() -> None:
    pages: dict[str, Page] = {
        f"{ROOT}?page={n}": listing_html(nav=[f"/store?page={n + 1}"]) for n in range(1, 1000)
    }
    pages[ROOT] = listing_html(nav=["/store?page=1"])

    with pytest.raises(ScrapeError, match="max_pages"):
        await discover_product_urls(fetcher_for(FakeSite(pages)), ROOT, max_pages=20)


async def test_failing_listing_page_aborts_rather_than_returning_partial_results() -> None:
    pages = mini_store()
    pages[f"{ROOT}/phones"] = 500

    with pytest.raises(ScrapeError, match=r"/store/phones"):
        await scrape(fetcher_for(FakeSite(pages)), ROOT, max_pages=50)


async def test_failing_product_page_aborts_and_names_the_page() -> None:
    pages = mini_store()
    pages[f"{ROOT}/product/2"] = 503

    with pytest.raises(ScrapeError, match=r"/store/product/2") as exc_info:
        await scrape(fetcher_for(FakeSite(pages)), ROOT, max_pages=50)

    assert len(exc_info.value.failures) == 1


async def test_unparseable_product_page_aborts_and_names_the_page() -> None:
    pages = mini_store()
    pages[f"{ROOT}/product/4"] = "<html><body>Under maintenance</body></html>"

    with pytest.raises(ScrapeError, match=r"/store/product/4"):
        await scrape(fetcher_for(FakeSite(pages)), ROOT, max_pages=50)


async def test_unpriceable_product_aborts() -> None:
    pages = mini_store()
    pages[f"{ROOT}/product/4"] = product_html(
        "Phone Four",
        extra='<div class="swatches"><button class="swatch" value="2048"></button></div>',
    )

    with pytest.raises(ScrapeError, match="2048"):
        await scrape(fetcher_for(FakeSite(pages)), ROOT, max_pages=50)


async def test_listing_page_served_as_a_200_maintenance_page_aborts() -> None:
    pages = mini_store()
    pages[f"{ROOT}/laptops?page=2"] = "<html><body>Temporarily unavailable</body></html>"

    with pytest.raises(ScrapeError, match=r"/store/laptops\?page=2"):
        await scrape(fetcher_for(FakeSite(pages)), ROOT, max_pages=50)


async def test_finding_no_products_is_an_error_not_an_empty_report() -> None:
    # e.g. the product cards' markup changed while the page layout did not.
    site = FakeSite({ROOT: listing_html()})

    with pytest.raises(ScrapeError, match="no products"):
        await scrape(fetcher_for(site), ROOT, max_pages=50)


async def test_unreachable_start_page_is_reported() -> None:
    with pytest.raises(ScrapeError, match="404"):
        await scrape(fetcher_for(FakeSite({})), ROOT, max_pages=50)


async def test_programming_errors_propagate_instead_of_masquerading_as_scrape_failures() -> None:
    def broken_transport(request: httpx.Request) -> httpx.Response:
        raise RuntimeError("bug in our code")

    client = httpx.AsyncClient(transport=httpx.MockTransport(broken_transport))
    fetcher = Fetcher(client, max_concurrency=1, retry=RetryPolicy(attempts=1))

    with pytest.raises(ExceptionGroup) as exc_info:
        await scrape(fetcher, ROOT, max_pages=50)

    assert exc_info.group_contains(RuntimeError, match="bug in our code")


async def test_links_on_a_redirected_page_resolve_against_its_final_url() -> None:
    site = FakeSite(
        {
            ROOT: listing_html(nav=["/store/old-laptops"]),
            f"{ROOT}/old-laptops": Redirect("/store/laptops/"),
            # Relative to the final URL this is /store/product/1; relative to the
            # requested URL it would wrongly be /product/1.
            f"{ROOT}/laptops/": listing_html(products=["../product/1"]),
        }
    )

    urls = await discover_product_urls(fetcher_for(site), ROOT, max_pages=50)

    assert urls == [f"{ROOT}/product/1"]


async def test_listing_page_redirecting_outside_the_crawl_scope_aborts() -> None:
    pages = mini_store()
    pages[f"{ROOT}/phones"] = Redirect("/elsewhere/phones")
    pages["https://shop.test/elsewhere/phones"] = listing_html(products=["/store/product/4"])

    with pytest.raises(ScrapeError) as exc_info:
        await scrape(fetcher_for(FakeSite(pages)), ROOT, max_pages=50)

    message = str(exc_info.value)
    assert "outside the crawl scope" in message
    assert f"{ROOT}/phones" in message
    assert "https://shop.test/elsewhere/phones" in message


async def test_product_page_redirecting_to_another_site_aborts() -> None:
    pages = mini_store()
    pages[f"{ROOT}/product/4"] = Redirect("https://other.test/product/4", status=302)
    pages["https://other.test/product/4"] = product_html("Imposter", "$1")

    with pytest.raises(ScrapeError) as exc_info:
        await scrape(fetcher_for(FakeSite(pages)), ROOT, max_pages=50)

    message = str(exc_info.value)
    assert "another site" in message
    assert f"{ROOT}/product/4" in message
    assert "https://other.test/product/4" in message


async def test_logs_product_fetch_progress_about_every_ten_percent(
    caplog: pytest.LogCaptureFixture,
) -> None:
    pages: dict[str, Page] = {
        ROOT: listing_html(products=[f"/store/product/{n}" for n in range(1, 21)]),
        **{f"{ROOT}/product/{n}": product_html(f"Product {n}") for n in range(1, 21)},
    }

    with caplog.at_level(logging.INFO, logger="ecommerce_scraper"):
        await scrape(fetcher_for(FakeSite(pages)), ROOT, max_pages=50)

    messages = [record.getMessage() for record in caplog.records]
    product_progress = [m for m in messages if m.startswith("Fetched") and "/20 product" in m]
    assert product_progress[-1] == "Fetched 20/20 product pages (100%)"
    assert 5 <= len(product_progress) <= 11  # periodic, but not a line per page
    assert any(m.startswith("Crawled 1 listing page") for m in messages)
