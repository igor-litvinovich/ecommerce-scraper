"""Domain types shared across the scraper.

Prices are always :class:`~decimal.Decimal` — never ``float`` — so that adding
HDD surcharges and summing hundreds of prices cannot introduce rounding drift.
"""

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class HddOption:
    """One storage configuration offered on a product page."""

    capacity_gb: int
    available: bool


@dataclass(frozen=True, slots=True)
class ProductPage:
    """Raw facts scraped from a single product detail page, before any business rules."""

    url: str
    name: str
    description: str
    base_price: Decimal
    hdd_options: tuple[HddOption, ...]
    colors: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ListingPage:
    """Links discovered on a navigational page (home, category, or paginated listing)."""

    product_urls: tuple[str, ...]
    navigation_urls: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ResultItem:
    """One purchasable product (or product configuration) in the final report."""

    name: str
    description: str
    price: Decimal
    colors: tuple[str, ...] | None = None


@dataclass(frozen=True, slots=True)
class Report:
    results: tuple[ResultItem, ...]
    total: Decimal
