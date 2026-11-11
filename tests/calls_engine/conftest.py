"""Fixtures for the call engine tests (the test files here are generated from the development
project by scripts/sync_plugin.py).

They need the call dependencies. A plain checkout without them skips these tests; CI sets
XMPP_REQUIRE_CALL_DEPS=1, so a missing dependency there is an error, not a silent skip.
"""

import asyncio
import gc
import importlib.util
import os
import warnings

import pytest_asyncio

_NEEDED = ("aiortc", "av", "numpy", "webrtcvad")
_MISSING = [m for m in _NEEDED if importlib.util.find_spec(m) is None]
if _MISSING and os.environ.get("XMPP_REQUIRE_CALL_DEPS"):
    raise RuntimeError(f"XMPP_REQUIRE_CALL_DEPS is set but {', '.join(_MISSING)} cannot be imported")
collect_ignore_glob = ["test_*.py"] if _MISSING else []


@pytest_asyncio.fixture(autouse=True)
async def _let_transports_close():
    """asyncio closes a transport's socket one loop turn after `close()`; a test that ends right
    after closing a peer would otherwise leave that to the garbage collector (ResourceWarning)."""
    yield
    for _ in range(5):
        await asyncio.sleep(0.02)
    # aioice and aiortc leave a few sockets/tasks for the collector; collect them here, inside
    # this test, so a warning about them never lands in a later, unrelated test.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        gc.collect()
