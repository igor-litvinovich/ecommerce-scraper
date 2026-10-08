"""Command-line entry point.

Contract: the JSON report goes to stdout (or ``--output``), diagnostics go to
stderr, and the exit code says whether the report can be trusted:

* ``0`` - success, complete report written
* ``1`` - scrape failed; nothing written (an existing ``--output`` file is kept)
* ``2`` - invalid command-line usage
* ``130`` - interrupted
"""

import argparse
import asyncio
import logging
import math
import os
import sys
import time
from collections.abc import Callable, Sequence
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from ecommerce_scraper.crawler import ScrapeError, scrape
from ecommerce_scraper.fetcher import Fetcher, RetryPolicy
from ecommerce_scraper.models import Report
from ecommerce_scraper.output import render_json, write_report

DEFAULT_START_URL = "https://webscraper.io/test-sites/e-commerce/static"
PROJECT_URL = "https://github.com/igor-litvinovich/ecommerce-scraper"

EXIT_OK = 0
EXIT_SCRAPE_FAILED = 1
EXIT_INTERRUPTED = 130

logger = logging.getLogger("ecommerce_scraper")


def main(
    argv: Sequence[str] | None = None, *, transport: httpx.AsyncBaseTransport | None = None
) -> int:
    """Run the CLI. ``transport`` lets tests or embedders swap out the network layer."""
    args = _build_parser().parse_args(argv)
    _configure_logging(args.log_level)

    try:
        report = asyncio.run(_scrape(args, transport))
    except ScrapeError as exc:
        logger.error("Scrape failed, no report written: %s", exc)
        return EXIT_SCRAPE_FAILED
    except KeyboardInterrupt:
        logger.error("Interrupted, no report written")
        return EXIT_INTERRUPTED

    text = render_json(report, indent=None if args.compact else 2)
    try:
        write_report(text, args.output)
    except OSError as exc:
        logger.error("Could not write report to %s: %s", args.output, exc)
        return EXIT_SCRAPE_FAILED
    return EXIT_OK


async def _scrape(args: argparse.Namespace, transport: httpx.AsyncBaseTransport | None) -> Report:
    started = time.monotonic()
    async with httpx.AsyncClient(
        transport=transport,
        timeout=httpx.Timeout(args.timeout),
        headers={"User-Agent": f"ecommerce-scraper/{_version()} (+{PROJECT_URL})"},
        limits=httpx.Limits(max_connections=args.concurrency),
    ) as client:
        fetcher = Fetcher(
            client,
            max_concurrency=args.concurrency,
            requests_per_second=args.rate_limit or None,
            retry=RetryPolicy(attempts=args.max_attempts),
        )
        report = await scrape(fetcher, args.start_url, max_pages=args.max_pages)

    logger.info(
        "Scraped %d results (total %s) with %d HTTP requests in %.1fs",
        len(report.results),
        report.total,
        fetcher.request_count,
        time.monotonic() - started,
    )
    return report


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ecommerce-scraper",
        description="Scrape the webscraper.io static e-commerce test site into a JSON report.",
        epilog="exit status: 0 success, 1 scrape or write failed (nothing written), "
        "2 usage error, 130 interrupted",
    )
    parser.add_argument(
        "--start-url",
        type=_http_url,
        default=DEFAULT_START_URL,
        help="where to start; navigation stays under this URL (default: the test site)",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=_writable_destination,
        default="-",
        help="file to write the JSON report to, atomically (default: stdout)",
    )
    parser.add_argument(
        "--compact", action="store_true", help="emit single-line JSON instead of pretty-printing"
    )
    parser.add_argument(
        "--rate-limit",
        type=_bounded(float, minimum=0),
        default=10.0,
        help="maximum requests per second; 0 disables the limit (default: %(default)s)",
    )
    parser.add_argument(
        "--concurrency",
        type=_bounded(int, minimum=1),
        default=5,
        help="maximum simultaneous requests (default: %(default)s)",
    )
    parser.add_argument(
        "--timeout",
        type=_bounded(float, minimum=0, inclusive=False),
        default=15.0,
        help="seconds allowed for each network step: connect, send, receive (default: %(default)s)",
    )
    parser.add_argument(
        "--max-attempts",
        type=_bounded(int, minimum=1),
        default=3,
        help="attempts per page on transient errors, with backoff (default: %(default)s)",
    )
    parser.add_argument(
        "--max-pages",
        type=_bounded(int, minimum=1),
        default=200,
        help="safety limit on listing-page requests (default: %(default)s)",
    )
    parser.add_argument(
        "--log-level",
        type=str.upper,
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="stderr verbosity, case-insensitive (default: %(default)s)",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {_version()}")
    return parser


def _bounded[N: (int, float)](
    kind: Callable[[str], N], *, minimum: N, inclusive: bool = True
) -> Callable[[str], N]:
    def convert(text: str) -> N:
        try:
            value = kind(text)
        except ValueError:
            raise argparse.ArgumentTypeError(f"{text!r} is not a valid number") from None
        if not math.isfinite(value):
            raise argparse.ArgumentTypeError(f"{text!r} is not a finite number")
        if value < minimum or (not inclusive and value == minimum):
            relation = ">=" if inclusive else ">"
            raise argparse.ArgumentTypeError(f"must be {relation} {minimum}")
        return value

    return convert


def _http_url(text: str) -> str:
    parts = urlsplit(text)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise argparse.ArgumentTypeError(f"{text!r} is not an http(s) URL")
    return text


def _writable_destination(text: str) -> str:
    # Checked up front so a mistake fails in milliseconds, not after a full crawl.
    if text == "-":
        return text
    if not text.strip():
        raise argparse.ArgumentTypeError("output path is empty (use '-' for stdout)")
    path = Path(text).resolve()
    if path.is_dir():
        raise argparse.ArgumentTypeError(f"{text!r} is a directory, not a file")
    if path.exists() and not path.is_file():
        # A device such as /dev/null is written through, so only it must be writable.
        if not os.access(path, os.W_OK):
            raise argparse.ArgumentTypeError(f"{text!r} is not writable")
        return text
    if not path.parent.is_dir():
        raise argparse.ArgumentTypeError(f"directory for {text!r} does not exist")
    # The atomic write creates a temporary file next to the report.
    if not os.access(path.parent, os.W_OK):
        raise argparse.ArgumentTypeError(f"directory for {text!r} is not writable")
    return text


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=level,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        force=True,
    )
    if level != "DEBUG":
        # httpx logs every request at INFO; our own logs already summarise progress.
        logging.getLogger("httpx").setLevel(logging.WARNING)


def _version() -> str:
    try:
        return version("ecommerce-scraper")
    except PackageNotFoundError:  # pragma: no cover - running from a source checkout
        return "0+unknown"
