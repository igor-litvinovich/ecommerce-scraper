"""Command-line entry point.

Contract: the JSON report goes to stdout (or ``--output``), diagnostics go to
stderr, and the exit code says whether the report can be trusted:

* ``0`` - success, complete report written
* ``1`` - scrape failed; nothing written (an existing ``--output`` file is kept)
* ``2`` - invalid command-line usage
* ``70`` - internal error (a bug); logged with its traceback, nothing written
* ``130`` - interrupted (Ctrl-C)
* ``143`` - terminated (SIGTERM, e.g. a scheduler stopping the job)
"""

import argparse
import asyncio
import logging
import math
import os
import signal
import sys
import threading
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
EXIT_INTERNAL_ERROR = 70  # EX_SOFTWARE in sysexits.h
EXIT_INTERRUPTED = 130
EXIT_TERMINATED = 143  # 128 + SIGTERM

logger = logging.getLogger("ecommerce_scraper")


def main(
    argv: Sequence[str] | None = None, *, transport: httpx.AsyncBaseTransport | None = None
) -> int:
    """Run the CLI. ``transport`` lets tests or embedders swap out the network layer."""
    args = _build_parser().parse_args(argv)
    _configure_logging(args.log_level)

    # Turn SIGTERM into an orderly shutdown, like Ctrl-C: in-flight work is cancelled
    # and no partial report is written. Signal handlers can only be set on the main thread.
    on_main_thread = threading.current_thread() is threading.main_thread()
    previous_handler = signal.signal(signal.SIGTERM, _terminate) if on_main_thread else None
    try:
        return _run(args, transport)
    except _Terminated:
        logger.error("Terminated, no report written")
        return EXIT_TERMINATED
    finally:
        if on_main_thread:
            signal.signal(signal.SIGTERM, previous_handler)


class _Terminated(BaseException):
    """Raised by the SIGTERM handler; a BaseException so no ``except Exception`` eats it."""


def _terminate(signum: int, frame: object) -> None:
    raise _Terminated


def _run(args: argparse.Namespace, transport: httpx.AsyncBaseTransport | None) -> int:
    try:
        report = asyncio.run(_scrape(args, transport))
        text = render_json(report, indent=None if args.compact else 2)
    except ScrapeError as exc:
        logger.error("Scrape failed, no report written: %s", exc)
        return EXIT_SCRAPE_FAILED
    except KeyboardInterrupt:
        logger.error("Interrupted, no report written")
        return EXIT_INTERRUPTED
    except Exception:
        logger.exception("Internal error (please report it), no report written")
        return EXIT_INTERNAL_ERROR

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
        deadline = asyncio.timeout(args.deadline)  # None means no overall limit
        try:
            async with deadline:
                report = await scrape(fetcher, args.start_url, max_pages=args.max_pages)
        except TimeoutError:
            if not deadline.expired():
                raise
            raise ScrapeError(f"run exceeded the {args.deadline:g}s deadline") from None

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
        "2 usage error, 70 internal error, 130 interrupted, 143 terminated",
    )
    parser.add_argument(
        "--start-url",
        type=_http_url,
        default=DEFAULT_START_URL,
        metavar="URL",
        help="where to start; navigation stays under this URL (default: %(default)s)",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=_writable_destination,
        default="-",
        metavar="PATH",
        help="file to write the JSON report to, atomically (default: stdout)",
    )
    parser.add_argument(
        "--compact", action="store_true", help="emit single-line JSON instead of pretty-printing"
    )
    parser.add_argument(
        "--rate-limit",
        type=_bounded(float, minimum=0),
        default=10.0,
        metavar="N",
        help="maximum requests per second; 0 disables the limit (default: %(default)s)",
    )
    parser.add_argument(
        "--concurrency",
        type=_bounded(int, minimum=1),
        default=5,
        metavar="N",
        help="maximum simultaneous requests (default: %(default)s)",
    )
    parser.add_argument(
        "--timeout",
        type=_bounded(float, minimum=0, inclusive=False),
        default=15.0,
        metavar="SECONDS",
        help="seconds allowed for each network step: connect, send, receive (default: %(default)s)",
    )
    parser.add_argument(
        "--deadline",
        type=_bounded(float, minimum=0, inclusive=False),
        default=None,
        metavar="SECONDS",
        help="give up if the whole run takes longer than this (default: no limit)",
    )
    parser.add_argument(
        "--max-attempts",
        type=_bounded(int, minimum=1),
        default=3,
        metavar="N",
        help="attempts per page on transient errors, with backoff (default: %(default)s)",
    )
    parser.add_argument(
        "--max-pages",
        type=_bounded(int, minimum=1),
        default=200,
        metavar="N",
        help="safety limit on listing-page requests (default: %(default)s)",
    )
    parser.add_argument(
        "--log-level",
        type=str.upper,
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        metavar="LEVEL",
        help="stderr verbosity: debug, info, warning or error (default: %(default)s)",
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
