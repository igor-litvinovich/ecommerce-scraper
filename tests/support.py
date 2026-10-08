"""Shared test helpers."""

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import httpx

FIXTURES = Path(__file__).parent / "fixtures"
SITE = "https://webscraper.io/test-sites/e-commerce/static"


def read_fixture(name: str) -> str:
    """Return a saved copy of a real page from the target site."""
    return (FIXTURES / name).read_text(encoding="utf-8")


@dataclass(frozen=True)
class Redirect:
    location: str
    status: int = 301


type Page = str | int | Redirect


class FakeSite:
    """In-memory website for :class:`httpx.MockTransport`.

    ``pages`` maps full URLs (including any query string) to HTML, to an int
    status code to simulate a failing page, or to a :class:`Redirect`. Unknown
    URLs return 404.
    """

    def __init__(self, pages: dict[str, Page]) -> None:
        self.pages = pages
        self.hits: Counter[str] = Counter()
        self.user_agents: set[str] = set()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.hits[url] += 1
        self.user_agents.add(request.headers.get("User-Agent", ""))
        page = self.pages.get(url, 404)
        if isinstance(page, Redirect):
            return httpx.Response(page.status, headers={"Location": page.location})
        if isinstance(page, int):
            return httpx.Response(page)
        return httpx.Response(200, text=page, headers={"Content-Type": "text/html"})

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)


def listing_html(*, products: Sequence[str] = (), nav: Sequence[str] = ()) -> str:
    """Listing/category page using the same classes as the real site."""
    cards = "".join(
        f'<div class="thumbnail"><a href="{href}" class="title">x</a></div>' for href in products
    )
    links = "".join(f'<li><a class="category-link" href="{href}">c</a></li>' for href in nav)
    return f'<html><body><ul id="side-menu">{links}</ul>{cards}</body></html>'


def product_html(name: str, price: str = "$10.00", extra: str = "") -> str:
    return (
        '<div class="card thumbnail"><div class="caption">'
        f'<h4 class="price">{price}</h4><h4 class="title">{name}</h4>'
        f'<p class="description">About {name}</p></div>{extra}</div>'
    )
