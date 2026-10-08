from decimal import Decimal
from typing import Any

import pytest

from ecommerce_scraper.models import HddOption
from ecommerce_scraper.parsing import ParseError, parse_listing, parse_price, parse_product
from tests.support import SITE, read_fixture


class TestParsePrice:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("$1178.19", Decimal("1178.19")),
            ("$1149", Decimal("1149")),  # site renders whole-dollar prices without decimals
            ("$1124.2", Decimal("1124.2")),  # ...and drops trailing zeros
            ("  $93.99\n\t", Decimal("93.99")),
            ("$1,178.19", Decimal("1178.19")),
            ("$ 24.99", Decimal("24.99")),
        ],
    )
    def test_parses_dollar_amounts_exactly(self, text: str, expected: Decimal) -> None:
        assert parse_price(text) == expected

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "$",
            "1178.19",
            "€10.00",
            "$abc",
            "$-5.00",
            "$1.2.3",
            "$1,17,8.19",
            "$12 99",
            "$1.005",  # sub-cent amounts are not real prices
            "$\u00b2",  # a superscript two is a "digit" to str.isdigit(), not to us
        ],
    )
    def test_rejects_anything_that_is_not_a_plain_usd_amount(self, text: str) -> None:
        with pytest.raises(ParseError):
            parse_price(text)


class TestParseProduct:
    def test_laptop_with_hdd_options_marks_disabled_option_unavailable(self) -> None:
        url = f"{SITE}/product/140"

        product = parse_product(read_fixture("product_laptop_hdd.html"), url)

        assert product.url == url
        assert product.name == "Dell Latitude 5480"
        assert product.description == (
            'Dell Latitude 5480, 14" FHD, Core i7-7600U, 8GB, 256GB SSD, Linux + Windows 10 Home'
        )
        assert product.base_price == Decimal("1338.37")
        assert product.hdd_options == (
            HddOption(capacity_gb=128, available=True),
            HddOption(capacity_gb=256, available=True),
            HddOption(capacity_gb=512, available=True),
            HddOption(capacity_gb=1024, available=False),
        )
        assert product.colors == ()

    def test_phone_colors_exclude_the_select_placeholder(self) -> None:
        product = parse_product(read_fixture("product_phone_colors.html"), f"{SITE}/product/1")

        assert product.name == "Nokia 123"
        assert product.description == "7 day battery"
        assert product.base_price == Decimal("24.99")
        assert product.hdd_options == ()
        assert product.colors == ("Gold", "White", "Black")

    def test_tablet_has_both_hdd_options_and_colors(self) -> None:
        product = parse_product(
            read_fixture("product_tablet_hdd_and_colors.html"), f"{SITE}/product/26"
        )

        assert product.name == "IdeaTab S5000"
        assert product.base_price == Decimal("172.99")
        assert [o.capacity_gb for o in product.hdd_options if o.available] == [128, 256, 512]
        assert product.colors == ("Gold", "White", "Black")

    def test_normalises_whitespace_including_non_breaking_spaces(self) -> None:
        html = _product_html(
            name="\n   Lenovo\u00a0  ThinkPad \n", description="Core\u00a0i5-4210U,\n  4GB"
        )

        product = parse_product(html, "https://example.test/product/1")

        assert product.name == "Lenovo ThinkPad"
        assert product.description == "Core i5-4210U, 4GB"

    def test_ignores_selects_that_are_not_the_colour_picker(self) -> None:
        html = _product_html(
            extra='<div class="dropdown"><select aria-label="transmission">'
            '<option value="manual">Manual</option><option value="auto">Auto</option>'
            "</select></div>"
        )

        assert parse_product(html, "https://example.test/product/1").colors == ()

    @pytest.mark.parametrize("missing", ["name", "description", "price"])
    def test_missing_required_field_raises_with_context(self, missing: str) -> None:
        omit: dict[str, Any] = {missing: None}
        html = _product_html(**omit)

        with pytest.raises(ParseError, match=rf"{missing}.*https://example.test/product/9"):
            parse_product(html, "https://example.test/product/9")

    def test_unparseable_price_names_the_page(self) -> None:
        with pytest.raises(ParseError, match=r"'Call us'.*https://example.test/product/9"):
            parse_product(_product_html(price="Call us"), "https://example.test/product/9")

    def test_empty_name_is_rejected(self) -> None:
        with pytest.raises(ParseError, match="name"):
            parse_product(_product_html(name="   "), "https://example.test/product/9")

    def test_non_numeric_hdd_option_raises(self) -> None:
        html = _product_html(
            extra='<div class="swatches"><button class="btn swatch" value="1TB">1TB</button></div>'
        )

        with pytest.raises(ParseError, match="HDD"):
            parse_product(html, "https://example.test/product/9")

    def test_non_ascii_digit_hdd_option_raises_parse_error(self) -> None:
        # "\u00b2" (superscript two) passes str.isdigit() but int() rejects it.
        html = _product_html(
            extra='<div class="swatches"><button class="btn swatch" value="\u00b2">x</button></div>'
        )

        with pytest.raises(ParseError, match="HDD"):
            parse_product(html, "https://example.test/product/9")

    def test_page_without_product_card_raises(self) -> None:
        with pytest.raises(ParseError):
            parse_product("<html><body><h1>502 Bad Gateway</h1></body></html>", "https://x.test/p")


class TestParseListing:
    def test_listing_page_yields_absolute_product_and_pagination_links(self) -> None:
        page_url = f"{SITE}/computers/laptops"

        listing = parse_listing(read_fixture("listing_laptops_page1.html"), page_url)

        assert len(listing.product_urls) == 6
        assert all(u.startswith(f"{SITE}/product/") for u in listing.product_urls)
        # Pagination is truncated ("1-10 ... 19 20"), so we must at least see the
        # next page; later pages are discovered as the crawl progresses.
        assert f"{SITE}/computers/laptops?page=2" in listing.navigation_urls
        assert f"{SITE}/computers/laptops?page=20" in listing.navigation_urls
        assert f"{SITE}/computers/tablets" in listing.navigation_urls
        assert f"{SITE}/phones" in listing.navigation_urls

    def test_last_listing_page_still_parses_products(self) -> None:
        listing = parse_listing(
            read_fixture("listing_laptops_last_page.html"), f"{SITE}/computers/laptops?page=20"
        )

        assert len(listing.product_urls) >= 1
        assert f"{SITE}/computers/laptops?page=19" in listing.navigation_urls

    def test_home_page_exposes_categories_and_featured_products(self) -> None:
        listing = parse_listing(read_fixture("home.html"), SITE)

        assert set(listing.navigation_urls) >= {f"{SITE}/computers", f"{SITE}/phones"}
        assert listing.product_urls == (
            f"{SITE}/product/26",
            f"{SITE}/product/140",
            f"{SITE}/product/15",
        )

    def test_category_page_exposes_subcategories(self) -> None:
        listing = parse_listing(read_fixture("category_computers.html"), f"{SITE}/computers")

        assert {f"{SITE}/computers/laptops", f"{SITE}/computers/tablets"} <= set(
            listing.navigation_urls
        )

    def test_strips_fragments_and_deduplicates_links(self) -> None:
        html = """
            <ul id="side-menu"></ul>
            <a class="title" href="/product/1#reviews">A</a>
            <a class="title" href="/product/1">A again</a>
            <ul class="pagination"><li><a class="page-link" href="?page=2#top">2</a></li>
            <li><a class="page-link next" href="?page=2" rel="next">&rsaquo;</a></li></ul>
        """

        listing = parse_listing(html, "https://shop.test/cat")

        assert listing.product_urls == ("https://shop.test/product/1",)
        assert listing.navigation_urls == ("https://shop.test/cat?page=2",)

    def test_store_page_without_products_is_empty_not_an_error(self) -> None:
        listing = parse_listing('<ul id="side-menu"></ul>', "https://shop.test/")

        assert listing.product_urls == ()
        assert listing.navigation_urls == ()

    @pytest.mark.parametrize(
        "html",
        [
            "<html><body><h1>Temporarily unavailable</h1></body></html>",
            "<html><body><form id='captcha'>Are you human?</form></body></html>",
            "",
        ],
    )
    def test_page_without_the_store_layout_is_rejected(self, html: str) -> None:
        # A 200 response is not proof we got a listing page. Treating a maintenance or
        # captcha page as "no products here" would shrink the report with no error.
        with pytest.raises(ParseError, match=r"https://shop\.test/laptops"):
            parse_listing(html, "https://shop.test/laptops")


def _product_html(
    *,
    name: str | None = "Widget",
    description: str | None = "A widget",
    price: str | None = "$10.00",
    extra: str = "",
) -> str:
    """Minimal product page mirroring the real site's markup."""
    parts = ['<div class="card thumbnail"><div class="caption">']
    if price is not None:
        parts.append(f'<h4 class="price"><span itemprop="price">{price}</span></h4>')
    if name is not None:
        parts.append(f'<h4 class="title card-title">{name}</h4>')
    if description is not None:
        parts.append(f'<p class="description card-text">{description}</p>')
    parts.append(f"</div>{extra}</div>")
    return "".join(parts)
