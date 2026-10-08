"""Polite, resilient HTTP GETs: rate limiting, bounded concurrency, retry with backoff."""

import asyncio
import logging
import random
import re
import time
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


class RateLimiter:
    """Spaces the *start* of successive requests at least ``1 / per_second`` apart,
    and can hold every request back while the server asks us to slow down."""

    def __init__(
        self,
        per_second: float | None,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if per_second is not None and per_second <= 0:
            raise ValueError("per_second must be positive, or None to disable rate limiting")
        self._interval = 1 / per_second if per_second else 0.0
        self._clock = clock
        self._sleep = sleep
        self._next_start = float("-inf")
        self._lock = asyncio.Lock()

    async def wait_for_turn(self) -> None:
        async with self._lock:  # waiters queue up and leave one interval apart
            now = self._clock()
            if self._next_start > now:
                await self._sleep(self._next_start - now)
            self._next_start = max(now, self._next_start) + self._interval

    def pause_until(self, moment: float) -> None:
        """Hold back every request, from every worker, until ``moment``."""
        self._next_start = max(self._next_start, moment)


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
        requests_per_second: float | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        random_fraction: Callable[[], float] = random.random,
    ) -> None:
        self._client = client
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._rate_limiter = RateLimiter(requests_per_second, clock=clock, sleep=sleep)
        self._clock = clock
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
                limit = self._retry.max_backoff_seconds
                if failure.retry_after is not None and failure.retry_after > limit:
                    # Retrying sooner than the server asked would be impolite.
                    reason = f"{failure}; server asked to wait {failure.retry_after:g}s"
                    raise FetchError(url, f"{reason}, more than the {limit:g}s limit") from failure
                delay = self._retry.delay_before_retry(
                    attempt, failure.retry_after, draw=self._random_fraction()
                )
                if failure.throttled:  # the whole client is too fast, not just this request
                    self._rate_limiter.pause_until(self._clock() + delay)
                logger.warning("%s fetching %s; retrying in %.1fs", failure, url, delay)
                await self._sleep(delay)
                attempt += 1

    async def _attempt(self, url: str) -> FetchedPage:
        await self._rate_limiter.wait_for_turn()  # every attempt counts, retries included
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
            retry_after = _parse_retry_after(response.headers.get("Retry-After"))
            throttled = response.status_code == httpx.codes.TOO_MANY_REQUESTS or bool(retry_after)
            raise _TransientError(reason, retry_after, throttled=throttled)
        raise FetchError(url, reason)


class _TransientError(Exception):
    def __init__(
        self, reason: str, retry_after: float | None = None, *, throttled: bool = False
    ) -> None:
        super().__init__(reason)
        self.retry_after = retry_after
        self.throttled = throttled


def _parse_retry_after(value: str | None) -> float | None:
    # Only the delta-seconds form is supported; an HTTP-date falls back to backoff.
    if value is None or not re.fullmatch(r"[0-9]+", value.strip()):
        return None
    return float(value)
