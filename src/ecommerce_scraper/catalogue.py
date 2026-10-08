"""Business rules that turn scraped product pages into the final report."""

import logging
from collections.abc import Iterable, Mapping
from decimal import Decimal

from ecommerce_scraper.models import HddOption, ProductPage, Report, ResultItem

logger = logging.getLogger(__name__)

# The static HTML only ever shows the price of the default (128 GB) configuration.
# Selecting another HDD option re-prices the product client-side, in the site's
# app.js (`EcommerceProduct.updatePrice`), by adding a fixed surcharge to that
# displayed price. We mirror that rule here; see the README for the trade-off
# versus driving a headless browser.
HDD_SURCHARGE_USD: Mapping[int, Decimal] = {
    128: Decimal(0),
    256: Decimal(20),
    512: Decimal(40),
    1024: Decimal(60),
}


class PricingError(ValueError):
    """A product offers a configuration we do not know how to price."""


def expand_product(product: ProductPage) -> list[ResultItem]:
    """Return one result per purchasable configuration of ``product``."""
    colors = _normalise_colors(product.colors)

    if not product.hdd_options:
        return [_result(product, product.name, product.base_price, colors)]

    available = sorted(
        (option for option in product.hdd_options if option.available),
        key=lambda option: option.capacity_gb,
    )
    if not available:
        logger.warning("Skipping %s: every HDD option is unavailable", product.url)
        return []

    return [
        _result(
            product,
            f"{product.name} {option.capacity_gb} GB",
            product.base_price + _surcharge(option, product.url),
            colors,
        )
        for option in available
    ]


def build_report(products: Iterable[ProductPage]) -> Report:
    """Expand every product into results and total their prices exactly."""
    results = tuple(item for product in products for item in expand_product(product))
    return Report(results=results, total=sum((item.price for item in results), Decimal(0)))


def _surcharge(option: HddOption, url: str) -> Decimal:
    try:
        return HDD_SURCHARGE_USD[option.capacity_gb]
    except KeyError:
        # Guessing (the site's JS would just add $0) risks publishing a wrong
        # price; failing makes the rule change visible so it can be updated.
        raise PricingError(
            f"no known surcharge for {option.capacity_gb} GB HDD option on {url}"
        ) from None


def _normalise_colors(colors: Iterable[str]) -> tuple[str, ...] | None:
    unique = tuple(dict.fromkeys(color.strip().lower() for color in colors if color.strip()))
    # The output contract only asks for colours when there is an actual choice.
    return unique if len(unique) > 1 else None


def _result(
    product: ProductPage, name: str, price: Decimal, colors: tuple[str, ...] | None
) -> ResultItem:
    return ResultItem(name=name, description=product.description, price=price, colors=colors)
