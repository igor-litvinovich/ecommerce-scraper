"""Polite, resilient HTTP GETs: bounded concurrency plus retry with backoff."""

import asyncio
import logging
import random
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

# Statuses that usually mean "try again shortly". Anything else in the 4xx/5xx
# range is treated as permanent so we fail fast instead of hammering the site.
_RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    attempts: int = 3
    """Total attempts per URL, including the first one."""
    backoff_seconds: float = 0.5
    """Delay before the first retry; doubles on each subsequent retry."""
    max_backoff_seconds: float = 10.0
    """Upper bound for any single delay, including server-requested ones."""
    jitter: float = 0.2
    """Fraction of each backoff that is randomised, so requests that failed together
    don't all retry in lockstep. A server's ``Retry-After`` is never shortened."""

    def __post_init__(self) -> None:
        if self.attempts < 1:
            raise ValueError("attempts must be at least 1")
        if self.backoff_seconds < 0 or self.max_backoff_seconds < 0:
            raise ValueError("backoff durations must not be negative")
        if not 0 <= self.jitter <= 1:
            raise ValueError("jitter must be between 0 and 1")

    def delay_before_retry(
        self, retry_number: int, retry_after: float | None = None, *, draw: float = 0.0
    ) -> float:
        """Seconds to wait before retry ``retry_number``; ``draw`` is uniform in [0, 1)."""
        backoff = min(self.backoff_seconds * 2.0 ** (retry_number - 1), self.max_backoff_seconds)
        requested = min(retry_after or 0, self.max_backoff_seconds)
        return max(backoff * (1 - self.jitter * draw), requested)


@dataclass(frozen=True, slots=True)
class FetchedPage:
    url: str
    """Final URL after any redirects; relative links on the page resolve against it."""
    text: str


class FetchError(Exception):
    """A URL could not be fetched, even after retrying."""

    def __init__(self, url: str, reason: str) -> None:
        super().__init__(f"{reason} fetching {url}")
        self.url = url


class Fetcher:
    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        max_concurrency: int,
        retry: RetryPolicy,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        random_fraction: Callable[[], float] = random.random,
    ) -> None:
        self._client = client
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._retry = retry
        self._sleep = sleep
        self._random_fraction = random_fraction
        self.request_count = 0

    async def fetch(self, url: str) -> FetchedPage:
        """GET ``url`` (following redirects), or raise :class:`FetchError`."""
        attempt = 1
        while True:
            try:
                # Only hold a concurrency slot while a request is actually in
                # flight, not while backing off.
                async with self._semaphore:
                    return await self._attempt(url)
            except _TransientError as failure:
                if attempt == self._retry.attempts:
                    raise FetchError(url, f"{failure} after {attempt} attempt(s)") from failure
                delay = self._retry.delay_before_retry(
                    attempt, failure.retry_after, draw=self._random_fraction()
                )
                logger.warning("%s fetching %s; retrying in %.1fs", failure, url, delay)
                await self._sleep(delay)
                attempt += 1

    async def _attempt(self, url: str) -> FetchedPage:
        self.request_count += 1
        logger.debug("GET %s", url)
        try:
            response = await self._client.get(url, follow_redirects=True)
        except httpx.UnsupportedProtocol as exc:  # a transport error, but never transient
            raise FetchError(url, f"{type(exc).__name__}: {exc}") from exc
        except httpx.TransportError as exc:  # timeouts, DNS, refused/reset connections...
            raise _TransientError(f"{type(exc).__name__}: {exc}") from exc
        except httpx.RequestError as exc:  # redirect loops, undecodable bodies: permanent
            raise FetchError(url, f"{type(exc).__name__}: {exc}") from exc

        if response.is_success:
            return FetchedPage(url=str(response.url), text=response.text)
        reason = f"HTTP {response.status_code}"
        if response.status_code in _RETRYABLE_STATUSES:
            raise _TransientError(reason, _parse_retry_after(response.headers.get("Retry-After")))
        raise FetchError(url, reason)


class _TransientError(Exception):
    def __init__(self, reason: str, retry_after: float | None = None) -> None:
        super().__init__(reason)
        self.retry_after = retry_after


def _parse_retry_after(value: str | None) -> float | None:
    # Only the delta-seconds form is supported; an HTTP-date falls back to backoff.
    if value is None or not re.fullmatch(r"[0-9]+", value.strip()):
        return None
    return float(value)
