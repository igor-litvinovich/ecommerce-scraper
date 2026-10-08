"""Pure HTML -> domain-type parsing.

Nothing here performs I/O, so every rule about the site's markup can be tested
against saved pages. All CSS selectors live at the top of the module: when the
site's markup changes, this is the only place that should need editing.
"""

import re
from decimal import Decimal
from urllib.parse import urldefrag, urljoin

from bs4 import BeautifulSoup, Tag

from ecommerce_scraper.models import HddOption, ListingPage, ProductPage

_PRODUCT_CARD = ".thumbnail"
_NAME = ".caption .title"
_DESCRIPTION = ".caption .description"
_PRICE = ".caption .price"
_HDD_SWATCH = ".swatches button.swatch"
_COLOR_OPTION = 'select[aria-label="color"] option'

# Every page of the store carries the category sidebar. Its absence means we were
# served something else (maintenance page, captcha, error page) despite HTTP 200.
_STORE_LAYOUT = "#side-menu"
_PRODUCT_LINK = "a.title[href]"
_NAVIGATION_LINK = ", ".join(
    [
        "a.category-link[href]",
        "a.subcategory-link[href]",
        # Pagination is rendered truncated ("1-10 ... 19 20"), so we take every
        # page link we can see and let the crawler discover the rest transitively.
        "ul.pagination a.page-link[href]",
    ]
)

# "$1178.19", "$1149", "$1124.2", "$1,178.19". Anything else (other currencies,
# negatives, garbage) is rejected: an unnoticed mis-parsed price corrupts the total.
_PRICE_PATTERN = re.compile(
    r"\$\s*(?P<whole>\d{1,3}(?:,\d{3})+|\d+)(?P<fraction>\.\d{1,2})?", re.ASCII
)
_HDD_CAPACITY = re.compile(r"[0-9]+")  # ASCII only: str.isdigit() also accepts "\u00b2"


class ParseError(ValueError):
    """The page did not have the structure we rely on."""


def parse_price(text: str) -> Decimal:
    """Convert a displayed USD price such as ``"$1,178.19"`` into an exact Decimal."""
    match = _PRICE_PATTERN.fullmatch(text.strip())
    if match is None:
        raise ParseError(f"unrecognised price {text!r}")
    return Decimal(match["whole"].replace(",", "") + (match["fraction"] or ""))


def parse_product(html: str, url: str) -> ProductPage:
    """Extract the facts we need from a product detail page."""
    card = BeautifulSoup(html, "lxml").select_one(_PRODUCT_CARD)
    if card is None:
        raise ParseError(f"no product card found on {url}")

    name = _required_text(card, _NAME, "name", url)
    if not name:
        raise ParseError(f"empty product name on {url}")

    price_text = _required_text(card, _PRICE, "price", url)
    try:
        base_price = parse_price(price_text)
    except ParseError as exc:
        raise ParseError(f"{exc} on {url}") from exc

    return ProductPage(
        url=url,
        name=name,
        description=_required_text(card, _DESCRIPTION, "description", url),
        base_price=base_price,
        hdd_options=_parse_hdd_options(card, url),
        colors=_parse_colors(card),
    )


def parse_listing(html: str, page_url: str) -> ListingPage:
    """Extract product links and onward navigation links from any listing-style page."""
    soup = BeautifulSoup(html, "lxml")
    if soup.select_one(_STORE_LAYOUT) is None:
        raise ParseError(f"{page_url} does not look like a store page (no category sidebar)")
    return ListingPage(
        product_urls=_absolute_links(soup, _PRODUCT_LINK, page_url),
        navigation_urls=_absolute_links(soup, _NAVIGATION_LINK, page_url),
    )


def _parse_hdd_options(card: Tag, url: str) -> tuple[HddOption, ...]:
    options = []
    for button in card.select(_HDD_SWATCH):
        value = _attribute(button, "value") or _clean_text(button)
        if not _HDD_CAPACITY.fullmatch(value):
            raise ParseError(f"unrecognised HDD option {value!r} on {url}")
        # The site renders out-of-stock configurations as disabled buttons.
        options.append(HddOption(capacity_gb=int(value), available=not button.has_attr("disabled")))
    return tuple(options)


def _parse_colors(card: Tag) -> tuple[str, ...]:
    colors = []
    for option in card.select(_COLOR_OPTION):
        value = _attribute(option, "value")
        # Per HTML semantics an <option> without a value attribute submits its text.
        label = " ".join(value.split()) if value is not None else _clean_text(option)
        if label:  # skips the "Select color" placeholder, whose value is ""
            colors.append(label)
    return tuple(colors)


def _required_text(card: Tag, selector: str, field: str, url: str) -> str:
    element = card.select_one(selector)
    if element is None:
        raise ParseError(f"missing product {field} on {url}")
    return _clean_text(element)


def _clean_text(element: Tag) -> str:
    # str.split() with no argument also splits on non-breaking spaces, which the
    # site uses inside some descriptions ("Core\xa0i5").
    return " ".join(element.get_text().split())


def _attribute(element: Tag, name: str) -> str | None:
    value = element.get(name)
    if value is None:
        return None
    return value if isinstance(value, str) else " ".join(value)


def _absolute_links(soup: BeautifulSoup, selector: str, base_url: str) -> tuple[str, ...]:
    links = (
        urldefrag(urljoin(base_url, href)).url
        for anchor in soup.select(selector)
        if (href := _attribute(anchor, "href"))
    )
    return tuple(dict.fromkeys(links))  # de-duplicate, preserving document order
