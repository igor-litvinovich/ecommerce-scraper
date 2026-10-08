import logging
from collections.abc import AsyncIterator, Iterator

import pytest

from tests.support import close_mock_clients


@pytest.fixture(autouse=True)
def _restore_logging() -> Iterator[None]:
    """main() configures the root logger, as a CLI should; undo that between tests."""
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)


@pytest.fixture
async def mock_clients_closed() -> AsyncIterator[None]:
    """Close every client made with ``support.mock_client`` once the test ends.

    Opt-in (``pytestmark``) rather than autouse: the browser tests run Playwright's own
    event loop on the test thread, which cannot host an async fixture.
    """
    yield
    await close_mock_clients()
