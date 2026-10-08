import logging
from decimal import Decimal

import pytest

from ecommerce_scraper.catalogue import PricingError, build_report, expand_product
from ecommerce_scraper.models import HddOption, ProductPage, ResultItem

ALL_HDD = (
    HddOption(128, available=True),
    HddOption(256, available=True),
    HddOption(512, available=True),
    HddOption(1024, available=False),
)


def make_product(
    *,
    name: str = "Dell Latitude 5580",
    price: str = "1178.19",
    hdd: tuple[HddOption, ...] = (),
    colors: tuple[str, ...] = (),
) -> ProductPage:
    return ProductPage(
        url="https://shop.test/product/117",
        name=name,
        description='15.6" FHD',
        base_price=Decimal(price),
        hdd_options=hdd,
        colors=colors,
    )


class TestExpandProduct:
    def test_each_available_hdd_option_becomes_its_own_product_with_site_surcharge(self) -> None:
        # Surcharges mirror the site's client-side JS (app.js EcommerceProduct.updatePrice):
        # the static HTML only ever shows the 128 GB price.
        items = expand_product(make_product(hdd=ALL_HDD))

        assert [(i.name, i.price) for i in items] == [
            ("Dell Latitude 5580 128 GB", Decimal("1178.19")),
            ("Dell Latitude 5580 256 GB", Decimal("1198.19")),
            ("Dell Latitude 5580 512 GB", Decimal("1218.19")),
        ]

    def test_hdd_variants_are_ordered_by_capacity_whatever_the_page_order(self) -> None:
        hdd = (HddOption(512, True), HddOption(128, True), HddOption(256, True))

        items = expand_product(make_product(hdd=hdd))

        assert [i.name for i in items] == [
            "Dell Latitude 5580 128 GB",
            "Dell Latitude 5580 256 GB",
            "Dell Latitude 5580 512 GB",
        ]

    def test_disabled_hdd_option_is_not_offered(self) -> None:
        items = expand_product(make_product(hdd=ALL_HDD))

        assert not any("1024" in i.name for i in items)

    def test_product_without_hdd_options_is_returned_once_unchanged(self) -> None:
        items = expand_product(make_product(name="Nokia 123", price="24.99"))

        assert items == [
            ResultItem(name="Nokia 123", description='15.6" FHD', price=Decimal("24.99"))
        ]

    def test_single_available_hdd_option_still_names_the_configuration(self) -> None:
        hdd = (HddOption(256, available=True), HddOption(1024, available=False))

        items = expand_product(make_product(hdd=hdd))

        assert [(i.name, i.price) for i in items] == [
            ("Dell Latitude 5580 256 GB", Decimal("1198.19"))
        ]

    def test_product_whose_every_hdd_option_is_disabled_is_dropped_with_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        hdd = (HddOption(128, available=False), HddOption(256, available=False))

        with caplog.at_level(logging.WARNING):
            items = expand_product(make_product(hdd=hdd))

        assert items == []
        assert "https://shop.test/product/117" in caplog.text

    def test_unknown_hdd_capacity_fails_loudly_instead_of_guessing_a_price(self) -> None:
        hdd = (HddOption(128, available=True), HddOption(2048, available=True))

        with pytest.raises(PricingError, match=r"2048.*https://shop.test/product/117"):
            expand_product(make_product(hdd=hdd))

    def test_unknown_capacity_that_is_disabled_is_ignored(self) -> None:
        hdd = (HddOption(128, available=True), HddOption(2048, available=False))

        assert len(expand_product(make_product(hdd=hdd))) == 1

    def test_multiple_colors_are_lowercased_and_attached_to_every_hdd_variant(self) -> None:
        items = expand_product(make_product(hdd=ALL_HDD, colors=("Gold", "White", "Black")))

        assert [i.colors for i in items] == [("gold", "white", "black")] * 3

    @pytest.mark.parametrize("colors", [(), ("Gold",), ("Gold", "gold", " GOLD ")])
    def test_colors_are_omitted_unless_there_are_multiple_distinct_options(
        self, colors: tuple[str, ...]
    ) -> None:
        (item,) = expand_product(make_product(colors=colors))

        assert item.colors is None

    def test_duplicate_colors_are_collapsed_keeping_first_seen_order(self) -> None:
        (item,) = expand_product(make_product(colors=("White", "Black", "white")))

        assert item.colors == ("white", "black")


class TestBuildReport:
    def test_total_is_exact_decimal_sum_of_every_result(self) -> None:
        # 0.1 + 0.2 + 0.7 is 0.9999999999999999 in binary floating point.
        products = [
            make_product(price="0.1"),
            make_product(price="0.2"),
            make_product(price="0.7"),
        ]

        report = build_report(products)

        assert report.total == Decimal("1.0")

    def test_total_includes_every_hdd_variant(self) -> None:
        report = build_report([make_product(price="100", hdd=ALL_HDD), make_product(price="5")])

        # 100 + 120 + 140 (laptop variants) + 5
        assert report.total == Decimal("365")
        assert len(report.results) == 4

    def test_results_keep_input_order(self) -> None:
        report = build_report([make_product(name="B"), make_product(name="A")])

        assert [r.name for r in report.results] == ["B", "A"]

    def test_empty_catalogue_gives_zero_total(self) -> None:
        report = build_report([])

        assert report.results == ()
        assert report.total == Decimal("0")
