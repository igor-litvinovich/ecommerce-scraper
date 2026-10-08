import asyncio
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from ecommerce_scraper.fetcher import Fetcher, FetchError, RateLimiter, RetryPolicy
from tests.support import mock_client

URL = "https://shop.test/page"

pytestmark = pytest.mark.usefixtures("mock_clients_closed")


class FakeClock:
    """Deterministic monotonic clock; its sleep records the delay, lets other tasks run,
    then jumps time forward instead of waiting."""

    def __init__(self) -> None:
        self.now = 0.0
        self.delays: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.delays.append(seconds)
        wake_at = self.now + seconds
        await asyncio.sleep(0)  # like a real sleep, let other tasks run meanwhile
        self.now = max(self.now, wake_at)


def make_fetcher(
    handler: Callable[[httpx.Request], Any],
    *,
    retry: RetryPolicy | None = None,
    max_concurrency: int = 4,
    random_fraction: float = 0.0,
) -> tuple[Fetcher, FakeClock]:
    """Fetcher on a fake clock (whose ``delays`` record every wait) and a fixed jitter
    draw (0.0 = no jitter)."""
    clock = FakeClock()
    client = mock_client(handler)
    fetcher = Fetcher(
        client,
        max_concurrency=max_concurrency,
        retry=retry or RetryPolicy(attempts=3, backoff_seconds=0.5, max_backoff_seconds=10),
        clock=clock,
        sleep=clock.sleep,
        random_fraction=lambda: random_fraction,
    )
    return fetcher, clock


class Scripted:
    """Handler that plays back the given responses (or raises the given errors) in order,
    remembering every request it received."""

    def __init__(self, *responses: httpx.Response | Exception) -> None:
        self._queue = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        outcome = self._queue.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


async def test_follows_redirects_and_reports_the_final_url() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/old":
            return httpx.Response(301, headers={"Location": "/new"})
        return httpx.Response(200, text="moved here")

    # The client is not configured to follow redirects; the fetcher must do so itself.
    fetcher, _ = make_fetcher(handler)

    page = await fetcher.fetch("https://shop.test/old")

    assert page.url == "https://shop.test/new"
    assert page.text == "moved here"


async def test_returns_body_of_successful_response() -> None:
    fetcher, sleep = make_fetcher(Scripted(httpx.Response(200, text="<html>ok</html>")))

    assert (await fetcher.fetch(URL)).text == "<html>ok</html>"
    assert sleep.delays == []
    assert fetcher.request_count == 1


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
async def test_retries_transient_status_codes(status: int) -> None:
    handler = Scripted(httpx.Response(status), httpx.Response(200, text="recovered"))
    fetcher, sleep = make_fetcher(handler)

    assert (await fetcher.fetch(URL)).text == "recovered"
    assert sleep.delays == [0.5]


@pytest.mark.parametrize(
    "error",
    [
        httpx.ConnectError("connection refused"),
        httpx.ReadTimeout("timed out"),
        httpx.RemoteProtocolError("peer closed connection"),
    ],
)
async def test_retries_network_errors(error: Exception) -> None:
    fetcher, _ = make_fetcher(Scripted(error, httpx.Response(200, text="recovered")))

    assert (await fetcher.fetch(URL)).text == "recovered"


async def test_backs_off_exponentially_and_gives_up_after_configured_attempts() -> None:
    handler = Scripted(httpx.Response(503), httpx.Response(503), httpx.Response(503))
    fetcher, sleep = make_fetcher(handler)

    with pytest.raises(FetchError, match=r"503.*https://shop.test/page"):
        await fetcher.fetch(URL)

    assert len(handler.requests) == 3
    assert sleep.delays == [0.5, 1.0]  # no pointless sleep after the final attempt


async def test_backoff_is_capped() -> None:
    handler = Scripted(*[httpx.Response(503)] * 4, httpx.Response(200))
    fetcher, sleep = make_fetcher(
        handler, retry=RetryPolicy(attempts=5, backoff_seconds=1, max_backoff_seconds=3)
    )

    await fetcher.fetch(URL)

    assert sleep.delays == [1, 2, 3, 3]


async def test_honours_retry_after_header_within_the_cap() -> None:
    handler = Scripted(httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(200))
    fetcher, sleep = make_fetcher(handler)

    await fetcher.fetch(URL)

    assert sleep.delays == [7]


async def test_gives_up_rather_than_retrying_before_a_long_retry_after() -> None:
    # Retrying after our 10 s cap when the server asked for 120 s would be impolite.
    handler = Scripted(httpx.Response(429, headers={"Retry-After": "120"}), httpx.Response(200))
    fetcher, sleep = make_fetcher(handler)

    with pytest.raises(FetchError, match="asked to wait 120"):
        await fetcher.fetch(URL)

    assert len(handler.requests) == 1
    assert sleep.delays == []


@pytest.mark.parametrize("value", ["Wed, 21 Oct 2015 07:28:00 GMT", "\u00b2", "-3", ""])
async def test_ignores_unparseable_retry_after(value: str) -> None:
    raw_header = [(b"Retry-After", value.encode())]  # as bytes, the way a server sends it
    handler = Scripted(httpx.Response(503, headers=raw_header), httpx.Response(200))
    fetcher, sleep = make_fetcher(handler)

    await fetcher.fetch(URL)

    assert sleep.delays == [0.5]


@pytest.mark.parametrize("status", [400, 403, 404, 410])
async def test_does_not_retry_permanent_client_errors(status: int) -> None:
    handler = Scripted(httpx.Response(status))
    fetcher, sleep = make_fetcher(handler)

    with pytest.raises(FetchError, match=str(status)):
        await fetcher.fetch(URL)

    assert len(handler.requests) == 1
    assert sleep.delays == []


async def test_network_error_after_last_attempt_is_wrapped_with_url() -> None:
    fetcher, _ = make_fetcher(
        Scripted(httpx.ConnectError("boom"), httpx.ConnectError("boom"), httpx.ConnectError("boom"))
    )

    with pytest.raises(FetchError, match=r"ConnectError.*https://shop.test/page") as exc_info:
        await fetcher.fetch(URL)

    assert exc_info.value.url == URL


async def test_never_exceeds_max_concurrency() -> None:
    in_flight = 0
    peak = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1
        return httpx.Response(200, text=str(request.url))

    client = mock_client(handler)
    fetcher = Fetcher(client, max_concurrency=3, retry=RetryPolicy(attempts=1))

    pages = await asyncio.gather(*(fetcher.fetch(f"{URL}/{i}") for i in range(10)))

    assert peak == 3
    assert [page.text for page in pages] == [f"{URL}/{i}" for i in range(10)]


async def test_jitter_shortens_each_backoff_by_up_to_the_configured_fraction() -> None:
    handler = Scripted(*[httpx.Response(503)] * 4, httpx.Response(200))
    policy = RetryPolicy(attempts=5, backoff_seconds=1, max_backoff_seconds=3, jitter=0.5)
    fetcher, sleep = make_fetcher(handler, retry=policy, random_fraction=1.0)

    await fetcher.fetch(URL)

    # Applied after the cap, so capped retries do not all fire in lockstep.
    assert sleep.delays == [0.5, 1, 1.5, 1.5]


async def test_jitter_never_shortens_a_server_requested_retry_after() -> None:
    handler = Scripted(httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(200))
    policy = RetryPolicy(attempts=2, jitter=0.5)
    fetcher, sleep = make_fetcher(handler, retry=policy, random_fraction=1.0)

    await fetcher.fetch(URL)

    assert sleep.delays == [7]


def test_retry_policy_rejects_nonsensical_values() -> None:
    with pytest.raises(ValueError, match="attempts"):
        RetryPolicy(attempts=0)
    with pytest.raises(ValueError, match="backoff"):
        RetryPolicy(backoff_seconds=-1)
    with pytest.raises(ValueError, match="jitter"):
        RetryPolicy(jitter=1.5)


@pytest.mark.parametrize(
    "error",
    [
        httpx.TooManyRedirects("redirect loop"),
        httpx.DecodingError("malformed gzip body"),
        httpx.UnsupportedProtocol("Request URL has an unsupported protocol 'ftp://'"),
    ],
)
async def test_other_request_errors_fail_immediately_with_url(error: Exception) -> None:
    handler = Scripted(error)
    fetcher, sleep = make_fetcher(handler)

    with pytest.raises(FetchError, match=r"https://shop.test/page"):
        await fetcher.fetch(URL)

    assert len(handler.requests) == 1
    assert sleep.delays == []


async def test_rate_limit_spaces_request_starts_evenly() -> None:
    clock = FakeClock()
    started: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        started.append(clock())
        return httpx.Response(200)

    client = mock_client(handler)
    fetcher = Fetcher(
        client,
        max_concurrency=4,
        retry=RetryPolicy(attempts=1),
        requests_per_second=2,
        clock=clock,
        sleep=clock.sleep,
    )

    await asyncio.gather(*(fetcher.fetch(f"{URL}/{i}") for i in range(4)))

    assert started == [0.0, 0.5, 1.0, 1.5]


async def test_rate_limit_counts_retries_too() -> None:
    clock = FakeClock()
    started: list[float] = []
    responses = [httpx.Response(503), httpx.Response(200)]

    def handler(request: httpx.Request) -> httpx.Response:
        started.append(clock())
        return responses.pop(0)

    client = mock_client(handler)
    fetcher = Fetcher(
        client,
        max_concurrency=1,
        retry=RetryPolicy(attempts=2, backoff_seconds=0.1, jitter=0),
        requests_per_second=1,
        clock=clock,
        sleep=clock.sleep,
    )

    await fetcher.fetch(URL)

    assert started == [0.0, 1.0]  # backoff (0.1 s) is shorter than the rate-limit spacing


async def test_without_rate_limit_requests_are_not_delayed() -> None:
    clock = FakeClock()
    client = mock_client(lambda _: httpx.Response(200))
    fetcher = Fetcher(
        client, max_concurrency=4, retry=RetryPolicy(attempts=1), clock=clock, sleep=clock.sleep
    )

    await asyncio.gather(*(fetcher.fetch(f"{URL}/{i}") for i in range(4)))

    assert clock.now == 0.0


def test_rate_limiter_rejects_non_positive_rates() -> None:
    with pytest.raises(ValueError, match="positive"):
        RateLimiter(0)


async def test_throttling_response_pauses_every_worker_not_just_the_one_throttled() -> None:
    clock = FakeClock()
    started: dict[str, list[float]] = {"/a": [], "/b": []}
    throttled_once = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal throttled_once
        started[request.url.path].append(clock())
        if request.url.path == "/a" and not throttled_once:
            throttled_once = True
            return httpx.Response(429, headers={"Retry-After": "5"})
        return httpx.Response(200)

    client = mock_client(handler)
    fetcher = Fetcher(
        client, max_concurrency=1, retry=RetryPolicy(attempts=2), clock=clock, sleep=clock.sleep
    )

    await asyncio.gather(fetcher.fetch("https://shop.test/a"), fetcher.fetch("https://shop.test/b"))

    assert started["/a"] == [0.0, 5.0]
    assert started["/b"] == [5.0]  # held back too, although it was never throttled itself
