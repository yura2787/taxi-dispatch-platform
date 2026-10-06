"""
Service-wide test fixtures.

Only universal fixtures live here: an HTTP client for the app and a real Redis.
Domain fixtures (drivers, offers, ...) belong in the test module that needs them,
or in a ``conftest.py`` of a test subpackage once there is one.

A local fixture with the same name shadows the one defined here, so a test that
needs a differently configured object just overrides it locally.
"""

import httpx
import pytest
from redis.asyncio import Redis

from app import config
from app.main import app


@pytest.fixture
async def client():
    """HTTP client that calls the app in-process, without a server."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def ensure_not_dev_db(client: Redis) -> None:
    """Fail the test instead of flushing DB 0, where the dev data lives."""
    # No DB in the URL means DB 0.
    db = client.connection_pool.connection_kwargs.get("db") or 0
    if db == 0:
        pytest.fail(
            f"Tests must not use Redis DB 0 (REDIS_URL={config.REDIS_URL}); "
            "run them with `make test`, which points them at DB 15."
        )


@pytest.fixture
async def redis():
    """Real Redis on REDIS_URL (DB 15), empty at the start and at the end of a test."""
    client = Redis.from_url(config.REDIS_URL)
    try:
        ensure_not_dev_db(client)
        await client.flushdb()
        yield client
        await client.flushdb()
    finally:
        await client.aclose()
