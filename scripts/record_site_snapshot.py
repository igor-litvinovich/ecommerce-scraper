"""Record every page the scraper requests into ``tests/fixtures/site_snapshot.json.xz``.

The snapshot backs the offline end-to-end test (``tests/test_snapshot.py``), so the
exact result for a known catalogue is checked on every run without touching the
network. Refresh it only deliberately (e.g. after the site changes), and update
the expected figures in that test from an independent source when you do.

Usage: ``uv run python scripts/record_site_snapshot.py``
"""

import json
import lzma
import sys
import tempfile
from pathlib import Path

import httpx

from ecommerce_scraper.cli import main

SNAPSHOT = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "site_snapshot.json.xz"


class RecordingTransport(httpx.AsyncBaseTransport):
    """Real network transport that keeps a copy of every successful HTML response."""

    def __init__(self) -> None:
        self._network = httpx.AsyncHTTPTransport()
        self.pages: dict[str, str] = {}

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self._network.handle_async_request(request)
        await response.aread()
        if response.status_code == httpx.codes.OK:
            self.pages[str(request.url)] = response.text
        return response

    async def aclose(self) -> None:
        await self._network.aclose()


def record() -> int:
    recorder = RecordingTransport()
    with tempfile.TemporaryDirectory() as scratch:
        exit_code = main(["--output", str(Path(scratch) / "report.json")], transport=recorder)
    if exit_code != 0:
        print("Scrape failed; snapshot not written.", file=sys.stderr)
        return exit_code

    payload = json.dumps(dict(sorted(recorder.pages.items())), indent=0).encode("utf-8")
    SNAPSHOT.write_bytes(lzma.compress(payload, preset=9 | lzma.PRESET_EXTREME))
    print(f"Recorded {len(recorder.pages)} pages to {SNAPSHOT} ({SNAPSHOT.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(record())
