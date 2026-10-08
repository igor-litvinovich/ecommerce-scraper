"""Shared test helpers."""

from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"
SITE = "https://webscraper.io/test-sites/e-commerce/static"


def read_fixture(name: str) -> str:
    """Return a saved copy of a real page from the target site."""
    return (FIXTURES / name).read_text(encoding="utf-8")
